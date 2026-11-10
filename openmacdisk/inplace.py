# -*- coding: utf-8 -*-
"""HFS+ 卷原地编辑：在已有卷上直接修改，不重建整卷。

标准 macOS 磁盘工具（如 hdiutil / Disk Utility）对压缩 DMG 的处理流程：

    打开镜像 → 分区表 → 挂载 HFS+/HFSX 卷 → 发现条目
      → 复制到 Mac 卷 / 删除文件 / 重命名 / 新建文件夹
      → 写 Catalog B-tree + allocation bitmap + 卷头 → 刷新视图
    压缩 DMG 是只读的："This is a compressed dmg file. It must be expanded
    before it is written." → 先 eXpand 成未压缩 DMG 才能写。

本模块据此实现：
  * 直接把 UDIF 里的 RAW 段映射成可写逻辑磁盘（不解压、不另存）；
  * 读出 Catalog B-tree 的全部叶记录，**保留其它文件原始的 fork extents**，
    因此文件数据块位置完全不动；
  * 只重写 Catalog 节点、allocation bitmap、卷头与备用卷头；
  * 元数据写入带回滚，失败时恢复原卷，不留半成品；
  * 不产生与镜像等大的临时文件，也不替换镜像文件本身
    （Windows 上替换正在被打开的文件会报 WinError 5）。

支持：HFS+/HFSX、未压缩 UDIF（全 RAW 段）或裸 IMG、单卷、8 段内 extent。
不支持：压缩/加密 DMG（提示先 eXpand）、APFS、多卷、碎片文件 >8 extents、
超过原 Catalog 节点容量的大量新增。这些情况会抛 InplaceError，由界面层
回退到「重建卷」路径，不会静默损坏镜像。

纯 Python 标准库，依据 Apple TN1150 与公开 UDIF 结构独立实现（净室）。
"""
from __future__ import annotations

import os
import struct
import time
from types import SimpleNamespace

from .udif import (SECTOR, UDIFImage, BLOCK_RAW, BLOCK_ZERO, BLOCK_FREE,
                   BLOCK_COMMENT, BLOCK_TERM)
from .image import Disk
from .hfs import HFSError, _parse_key
from .writer import (
    BLOCK, NODE_DESC, BTH, KIND_HEADER, KIND_INDEX, KIND_LEAF, KIND_FREE,
    REC_FOLDER, REC_FILE, REC_FOLDER_THREAD, REC_FILE_THREAD,
    ROOT_CNID, ROOT_PARENT, HFS_EPOCH,
    cat_key, _folder_record, _file_record, _thread_record,
    _key_cmp, _key_of_last, _pack_node, _split_records, _safe_name,
    WriteError,
)


class InplaceError(Exception):
    """当前镜像不支持原地写入，调用方应提示 eXpand 或回退到重建卷。"""


# ------------------------------------------------------------------ 可写逻辑磁盘

class WritableImage:
    """把 UDIF 的 RAW 段（或裸 IMG）映射成可读写的逻辑磁盘。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.fh = open(self.path, 'r+b')
        try:
            file_size = os.path.getsize(self.path)
            self.fh.seek(max(0, file_size - 512))
            trailer = self.fh.read(512)
            self.udif = trailer[:4] == b'koly'
            if self.udif:
                probe = UDIFImage(self.path)
                self.runs = probe.runs
                self.fork_off = probe.fork_off
                self.logical_size = probe.logical_size
                probe.close()
                compressed = [r for r in self.runs
                              if r.type not in (BLOCK_RAW, BLOCK_ZERO,
                                               BLOCK_FREE, BLOCK_COMMENT,
                                               BLOCK_TERM)]
                if compressed:
                    raise InplaceError(
                        '该 DMG 含压缩段，卷是只读的。请先执行 eXpand，'
                        '生成未压缩的可编辑 DMG 后再操作。')
            else:
                self.runs = []
                self.fork_off = 0
                self.logical_size = file_size
            self.size = self.logical_size
        except Exception:
            self.fh.close()
            raise

    def _locate(self, off: int, length: int):
        """把一段逻辑区间拆成 (文件偏移, 是否 RAW, 长度) 列表。"""
        if not self.udif:
            return [(off, True, length)]
        parts, pos, left = [], off, length
        for r in self.runs:
            if left <= 0:
                break
            lo = r.sector * SECTOR
            hi = lo + r.count * SECTOR
            if hi <= pos:
                continue
            if pos < lo:
                gap = min(left, lo - pos)
                parts.append((0, False, gap))
                pos += gap
                left -= gap
                if left <= 0:
                    break
            take = min(left, hi - pos)
            if r.type == BLOCK_RAW:
                parts.append((self.fork_off + r.offset + (pos - lo), True,
                              take))
            else:
                parts.append((0, False, take))
            pos += take
            left -= take
        if left > 0:
            parts.append((0, False, left))
        return parts

    def read(self, off: int, length: int) -> bytes:
        if length <= 0:
            return b''
        out = bytearray()
        for file_off, raw, n in self._locate(off, length):
            if raw:
                self.fh.seek(file_off)
                out.extend(self.fh.read(n))
            else:
                out.extend(b'\x00' * n)
        return bytes(out[:length])

    def write(self, off: int, data: bytes) -> int:
        if not data:
            return 0
        written = 0
        for file_off, raw, n in self._locate(off, len(data)):
            chunk = data[written:written + n]
            if raw:
                self.fh.seek(file_off)
                self.fh.write(chunk)
            elif any(chunk):
                raise InplaceError('无法写入稀疏/零填充段，请先 eXpand')
            written += n
        return written

    def flush(self):
        self.fh.flush()

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 原地编辑器

class HFSInplaceEditor:
    """在原 DMG 上直接修改 HFS+ 卷结构。"""

    def __init__(self, path: str, cb=None, volume_index: int = 0):
        self.cb = cb or (lambda *_: None)
        self.path = os.path.abspath(path)
        self.img = WritableImage(self.path)
        self.disk = Disk(self.path, reader=self.img)
        try:
            self._open_volume(volume_index)
        except Exception:
            self.img.close()
            raise

    def _open_volume(self, volume_index: int):
        vols = [v for v in self.disk.volumes
                if v.get('fs') in ('HFS+', 'HFSX')]
        if not vols:
            raise InplaceError('没有可写的 HFS+/HFSX 卷'
                               '（APFS 卷只读，压缩/加密镜像需先 eXpand）')
        info = vols[min(max(0, volume_index), len(vols) - 1)]
        self.base = info['base']
        self.vol = info['volume']
        self.node_size = self.vol.node_size
        self.bs = self.vol.block_size
        self.total_blocks = self.vol.total_blocks
        self.cat_file = self.vol.catalog_file
        self.alloc_file = self.vol.allocation_file
        self.fork_off = getattr(self.vol, 'fork_offset', 0x70)
        self.total_nodes = self.vol.total_nodes
        if self.total_nodes < 3:
            raise InplaceError('Catalog B-tree 节点数异常')

        self.vh = bytearray(self.img.read(self.base + 1024, 1024))
        if self.vh[:2] not in (b'H+', b'HX'):
            raise InplaceError('卷头签名无效')
        self.free_blocks = struct.unpack_from('>I', self.vh, 0x30)[0]
        self.next_alloc = struct.unpack_from('>I', self.vh, 0x34)[0]
        self.next_cnid = struct.unpack_from('>I', self.vh, 0x40)[0] or 16
        self.file_count = struct.unpack_from('>I', self.vh, 0x20)[0]
        self.folder_count = struct.unpack_from('>I', self.vh, 0x24)[0]
        self.bitmap = bytearray(self.vol.read_fork(self.alloc_file))
        self.stamp = int(time.time()) + HFS_EPOCH
        self.undo: list[tuple[int, bytes]] = []
        self.cb(8, 'Discovering Items...')
        self.records = self._dump_records()
        self.nodes = {n.path: n for n in self.vol.walk()}
        self.by_key = {k: d for k, d in self.records}
        # 真实 Mac 卷的 key 可能带 Unicode 归一化/大小写差异，
        # 删除与改名一律使用卷内原始 key，不重新拼装。
        self.path_by_cnid = {n.cnid: n.path for n in self.nodes.values()}
        self.key_by_path = {}
        self.thread_keys = {}
        for k, d in self.records:
            try:
                parent, name, _ = _parse_key(k, 0)
            except Exception:
                continue
            rtype = struct.unpack_from('>h', d, 0)[0] if len(d) >= 2 else 0
            if not name:
                if rtype in (REC_FOLDER_THREAD, REC_FILE_THREAD):
                    self.thread_keys[parent] = k
                continue
            pp = self.path_by_cnid.get(parent)
            if pp is None:
                continue
            self.key_by_path[(pp + '/' if pp else '') + name] = k
        self.cb(14, 'Checking Mac volume...')

    # ---------------- 基础读写 ----------------

    def _meta_write(self, off: int, data: bytes):
        self.undo.append((off, self.img.read(off, len(data))))
        self.img.write(off, data)

    def _write_fork(self, fork, data: bytes):
        """按 fork 的 extents 把字节流写回原有块，绝不越过分配范围。"""
        pos = 0
        for start, count in fork.extents:
            if pos >= len(data):
                break
            n = min(count * self.bs, len(data) - pos)
            if n <= 0:
                continue
            self._meta_write(self.base + start * self.bs, data[pos:pos + n])
            pos += n
        if pos < len(data):
            raise InplaceError('系统文件空间不足，请先用 eXpand 或重建镜像')

    def _dump_records(self):
        cat = self.vol._cat
        ns = self.node_size
        out = []
        for idx in range(len(cat) // ns):
            node = cat[idx * ns:(idx + 1) * ns]
            if len(node) < 14:
                break
            desc = NODE_DESC.unpack_from(node, 0)
            if desc[2] != KIND_LEAF:
                continue
            nrec = desc[4]
            if nrec > (ns - 14) // 6:
                continue
            start = ns - 2 * (nrec + 1)
            if start < 14:
                continue
            offsets = struct.unpack_from('>%dH' % (nrec + 1), node, start)
            if len(offsets) > 1 and offsets[0] > offsets[-1]:
                offsets = tuple(reversed(offsets))
            for i in range(nrec):
                rec = node[offsets[i]:offsets[i + 1]]
                if len(rec) < 8:
                    continue
                klen = struct.unpack_from('>H', rec, 0)[0]
                head = 2 + klen + (klen & 1)
                if head > len(rec):
                    continue
                out.append((bytes(rec[:2 + klen]), bytearray(rec[head:])))
        if not out:
            raise InplaceError('Catalog B-tree 里没有可读记录')
        return out

    # ---------------- 块分配 ----------------

    def _bit(self, block: int) -> bool:
        if block // 8 >= len(self.bitmap):
            return True
        return bool(self.bitmap[block // 8] & (0x80 >> (block % 8)))

    def _set(self, block: int, used: bool):
        if block // 8 >= len(self.bitmap):
            return
        mask = 0x80 >> (block % 8)
        if used:
            self.bitmap[block // 8] |= mask
        else:
            self.bitmap[block // 8] &= ~mask & 0xFF

    def _free_extents(self, extents):
        for start, count in extents:
            for b in range(start, start + count):
                if b < self.total_blocks and self._bit(b):
                    self._set(b, False)
                    self.free_blocks += 1

    def _alloc(self, count: int) -> int:
        if count <= 0:
            return 0
        if count > self.free_blocks:
            raise InplaceError('Not enough space on Mac volume（卷剩余空间不足）')
        probe = max(1, min(self.next_alloc, self.total_blocks - count))
        for _ in range(2):
            run = 0
            for b in range(probe, self.total_blocks):
                if self._bit(b):
                    run = 0
                    continue
                run += 1
                if run >= count:
                    start = b - count + 1
                    for x in range(start, start + count):
                        self._set(x, True)
                    self.free_blocks -= count
                    self.next_alloc = start + count
                    return start
            probe = 1
        raise InplaceError('没有足够的连续空闲块，请先用 eXpand 或重建镜像')

    def _write_file_data(self, local: str, start: int, size: int):
        if size <= 0:
            return
        with open(local, 'rb') as f:
            off = 0
            while off < size:
                chunk = f.read(min(1 << 20, size - off))
                if not chunk:
                    break
                self.img.write(self.base + start * self.bs + off, chunk)
                off += len(chunk)
                self.cb(min(60, 20 + 40 * off / max(1, size)),
                        'Copying File... %d%%' % (100 * off // max(1, size)))
        if off != size:
            raise InplaceError('读取本地文件不完整')

    # ---------------- 记录操作 ----------------

    def _bump_valence(self, parent_cnid: int, delta: int):
        for d in self.by_key.values():
            if (len(d) >= 12 and
                    struct.unpack_from('>h', d, 0)[0] == REC_FOLDER and
                    struct.unpack_from('>I', d, 8)[0] == parent_cnid):
                v = struct.unpack_from('>I', d, 4)[0]
                struct.pack_into('>I', d, 4, max(0, v + delta))
                return

    def _parent_of(self, path: str):
        parent_path = path.rsplit('/', 1)[0] if '/' in path else ''
        node = self.nodes.get(parent_path)
        if node is None:
            raise InplaceError('找不到目标文件夹：%s' % (parent_path or '/'))
        if node.kind != 'folder':
            raise InplaceError('目标不是文件夹：%s' % parent_path)
        return node.cnid

    def _delete_path(self, path: str):
        target = self.nodes.get(path)
        if target is None or not path:
            raise InplaceError('找不到卷内条目：%s' % path)
        doomed = [p for p in self.nodes
                  if p == path or p.startswith(path + '/')]
        removed = 0
        for p in doomed:
            n = self.nodes.pop(p)
            self.by_key.pop(self.key_by_path.get(p) or
                            cat_key(n.parent, n.name), None)
            self.by_key.pop(self.thread_keys.get(n.cnid) or
                            cat_key(n.cnid, ''), None)
            self.key_by_path.pop(p, None)
            self.thread_keys.pop(n.cnid, None)
            if n.kind == 'file':
                self.file_count -= 1
                if n.data_fork:
                    self._free_extents(n.data_fork.extents)
            else:
                self.folder_count -= 1
            removed += 1
        self._bump_valence(target.parent, -1)
        return removed

    def _rename(self, path: str, new_name: str):
        n = self.nodes.get(path)
        if n is None or not path:
            raise InplaceError('找不到卷内条目：%s' % path)
        name = _safe_name(new_name)
        old = self.key_by_path.get(path) or cat_key(n.parent, n.name)
        data = self.by_key.pop(old, None)
        if data is None:
            raise InplaceError('Catalog 记录缺失：%s' % path)
        new = cat_key(n.parent, name)
        if new in self.by_key:
            raise InplaceError('A file or folder with that name already exists')
        self.by_key[new] = data
        self.key_by_path.pop(path, None)
        thread = self.by_key.get(self.thread_keys.get(n.cnid) or
                                 cat_key(n.cnid, ''))
        if thread is not None and len(thread) >= 8:
            raw = name.encode('utf-16-be')
            if 8 + len(raw) <= len(thread):
                struct.pack_into('>H', thread, 6, len(raw) // 2)
                thread[8:8 + len(raw)] = raw
                for i in range(8 + len(raw), min(len(thread), 8 + 510)):
                    thread[i] = 0
        prefix = path.rsplit('/', 1)[0] if '/' in path else ''
        new_path = (prefix + '/' if prefix else '') + name
        for p, child in list(self.nodes.items()):
            if p == path or p.startswith(path + '/'):
                del self.nodes[p]
                child.name = name if p == path else child.name
                child.path = new_path + p[len(path):]
                self.nodes[child.path] = child
                if p != path:
                    k = self.key_by_path.pop(p, None)
                    if k is not None:
                        self.key_by_path[child.path] = k
        self.key_by_path[new_path] = new

    def _mkdir(self, path: str):
        if path in self.nodes or self.by_key.get(
                cat_key(self._parent_of(path), path.rsplit('/', 1)[-1])):
            raise InplaceError('A file or folder with that name already exists')
        name = _safe_name(path.rsplit('/', 1)[-1])
        parent = self._parent_of(path)
        cnid = self.next_cnid
        self.next_cnid += 1
        self.by_key[cat_key(parent, name)] = bytearray(
            _folder_record(cnid, 0, self.stamp))
        self.by_key[cat_key(cnid, '')] = bytearray(
            _thread_record(REC_FOLDER_THREAD, parent, name))
        self._bump_valence(parent, 1)
        self.folder_count += 1
        self.nodes[path] = SimpleNamespace(
            cnid=cnid, parent=parent, name=name, kind='folder',
            data_fork=None, path=path)

    def _add(self, local: str, path: str, replace: bool):
        if not local or not os.path.isfile(local):
            raise InplaceError('找不到要加入的本地文件：%s' % local)
        name = _safe_name(path.rsplit('/', 1)[-1])
        if path in self.nodes:
            if not replace:
                raise InplaceError('A file or folder with that name already exists')
            self._delete_path(path)      # TransMac: Copy and replace existing file
        parent = self._parent_of(path)
        size = os.path.getsize(local)
        need = (size + self.bs - 1) // self.bs
        start = self._alloc(need)
        self._write_file_data(local, start, size)
        cnid = self.next_cnid
        self.next_cnid += 1
        self.by_key[cat_key(parent, name)] = bytearray(
            _file_record(cnid, size, start, need, self.stamp))
        self.by_key[cat_key(cnid, '')] = bytearray(
            _thread_record(REC_FILE_THREAD, parent, name))
        self._bump_valence(parent, 1)
        self.file_count += 1
        self.nodes[path] = SimpleNamespace(
            cnid=cnid, parent=parent, name=name, kind='file',
            data_fork=None, path=path)

    # ---------------- B-tree 重建 / 写回 ----------------

    def _make_header(self, height, root, leaf_records, first_leaf,
                     last_leaf, used, free_head):
        node = bytearray(self.vol._cat[:self.node_size])
        desc = NODE_DESC.unpack_from(node, 0)
        nrec = desc[4]
        offsets = struct.unpack_from('>%dH' % (nrec + 1), node,
                                     self.node_size - 2 * (nrec + 1))
        if len(offsets) > 1 and offsets[0] > offsets[-1]:
            offsets = tuple(reversed(offsets))
        bth = list(BTH.unpack_from(node, offsets[0]))
        bth[0] = height
        bth[1] = root
        bth[2] = leaf_records
        bth[3] = first_leaf
        bth[4] = last_leaf
        bth[7] = self.total_nodes
        bth[8] = self.total_nodes - used
        BTH.pack_into(node, offsets[0], *bth)
        # header 节点的 flink 指向第一个空闲节点
        NODE_DESC.pack_into(node, 0, free_head, desc[1], KIND_HEADER, 0,
                            nrec, 0)
        if nrec >= 3:
            p, q = offsets[2], offsets[3]
            bm = bytearray(q - p)
            for i in range(min(used, len(bm) * 8)):
                bm[i // 8] |= 0x80 >> (i % 8)
            node[p:q] = bm
        return bytes(node)

    def _build_tree(self, records) -> bytes:
        ns = self.node_size
        pages = _split_records([k + bytes(d) for k, d in records],
                               node_size=ns)
        nodes: list[bytes | None] = [None]
        for i, page in enumerate(pages):
            flink = i + 2 if i + 1 < len(pages) else 0
            blink = i if i > 0 else 0
            nodes.append(_pack_node(KIND_LEAF, 1, page, flink, blink,
                                    node_size=ns))
        child_ids = list(range(1, len(nodes)))
        child_keys = [_key_of_last(p) for p in pages]
        level = 1
        while len(child_ids) > 1:
            level += 1
            idx_records = [k + struct.pack('>I', cid)
                           for k, cid in zip(child_keys, child_ids)]
            idx_pages = _split_records(idx_records, node_size=ns)
            new_ids, new_keys = [], []
            for page in idx_pages:
                nid = len(nodes)
                nodes.append(_pack_node(KIND_INDEX, level, page,
                                        node_size=ns))
                new_ids.append(nid)
                new_keys.append(_key_of_last(page, index=True))
            child_ids, child_keys = new_ids, new_keys
        used = len(nodes)
        if used > self.total_nodes:
            raise InplaceError('Catalog 节点不足（需要 %d，可用 %d），'
                               '请先 eXpand 或重建镜像'
                               % (used, self.total_nodes))
        for nid in range(used, self.total_nodes):
            flink = nid + 1 if nid + 1 < self.total_nodes else 0
            nodes.append(_pack_node(KIND_FREE, 0, [], flink, 0,
                                    node_size=ns))
        free_head = used if used < self.total_nodes else 0
        nodes[0] = self._make_header(level, child_ids[0], len(records),
                                     1, len(pages), used, free_head)
        return b''.join(nodes)            # type: ignore[arg-type]

    def _commit_metadata(self):
        records = sorted(self.by_key.items(), key=lambda kv: _key_cmp(kv[0]))
        self.cb(70, 'Writing Catalog B-tree...')
        cat_data = self._build_tree(records)
        if len(cat_data) > sum(c for _s, c in self.cat_file.extents) * self.bs:
            raise InplaceError('Catalog 区域容量不足，请先 eXpand 或重建镜像')
        self._write_fork(self.cat_file, cat_data)
        struct.pack_into('>Q', self.vh, self.fork_off + 160, len(cat_data))
        self.cb(86, 'Updating allocation bitmap / volume header...')
        self._write_fork(self.alloc_file, bytes(self.bitmap))
        struct.pack_into('>I', self.vh, 0x20, max(0, self.file_count))
        struct.pack_into('>I', self.vh, 0x24, max(1, self.folder_count))
        struct.pack_into('>I', self.vh, 0x30, max(0, self.free_blocks))
        struct.pack_into('>I', self.vh, 0x34, self.next_alloc)
        struct.pack_into('>I', self.vh, 0x40, self.next_cnid)
        # 日志式 HFS+ 卷：关闭 journal，避免 macOS 挂载时回放旧日志撤销修改
        attr = struct.unpack_from('>I', self.vh, 4)[0] & ~(1 << 13)
        struct.pack_into('>I', self.vh, 4, attr)
        struct.pack_into('>I', self.vh, 0x0C, 0)       # journalInfoBlock
        wc = struct.unpack_from('>I', self.vh, 0x44)[0]
        struct.pack_into('>I', self.vh, 0x44, (wc + 1) & 0xFFFFFFFF)
        self._meta_write(self.base + 1024, bytes(self.vh[:512]))
        alt = self.base + self.total_blocks * self.bs - 1024
        if alt > self.base + 1024:
            self._meta_write(alt, bytes(self.vh[:512]))
        self.img.flush()

    def rollback(self):
        for off, old in reversed(self.undo):
            try:
                self.img.write(off, old)
            except Exception:
                pass
        self.undo.clear()

    def close(self):
        self.img.close()


# ------------------------------------------------------------------ 对外 API

def is_capacity_error(msg: str) -> bool:
    """判断是否因「空间 / 目录区不足」导致原地写失败（应兜底重建）。

    这类报错说明 UDIF 容器本身可写、HFS+ 卷也认得，只是当前卷里已经没
    有足够的空闲块 / 连续块 / Catalog 节点来容纳新文件。此时 TransMac 的
    做法是重建整卷（重排后必有空间）。区分于「没有 HFS+ 卷 / 镜像损坏」
    等结构性错误——后者重建也无济于事，应原样报错。
    """
    if not msg:
        return False
    keys = ('空间', 'Catalog 节点不足', 'Catalog 区域容量',
            '连续空闲块', '系统文件空间')
    return any(k in msg for k in keys)


def container_writable(path: str) -> bool:
    """轻量判断：UDIF 是否全 RAW 段（只读打开，不解析文件系统）。"""
    try:
        with open(path, 'rb') as f:
            size = os.path.getsize(path)
            f.seek(max(0, size - 512))
            if f.read(4) != b'koly':
                return True          # 裸 IMG / ISO
        u = UDIFImage(path)
        try:
            return all(r.type in (BLOCK_RAW, BLOCK_ZERO, BLOCK_FREE,
                                  BLOCK_COMMENT, BLOCK_TERM)
                       for r in u.runs)
        finally:
            u.close()
    except Exception:
        return False


def convert_action(path: str, encrypted: bool = False) -> str | None:
    """卷节点右键该显示哪个命令（TransMac：两个命令永远互斥）。

    - 压缩 DMG（只读）→ 'expand'：先 eXpand 才能编辑；
    - 已展开的全 RAW DMG / 裸 IMG（可写）→ 'compress'：压回小体积只读 DMG；
    - 物理磁盘、加密镜像、路径无效 → None（两项都不显示）。

    抽成纯函数便于回归测试，界面层只负责把结果翻译成菜单项。
    """
    if not path or not os.path.isfile(path) or encrypted:
        return None
    return 'compress' if container_writable(path) else 'expand'


def is_writable(path: str) -> tuple[bool, str]:
    """判断镜像能不能原地写（压缩 DMG 必须先 eXpand）。"""
    try:
        img = WritableImage(path)
    except InplaceError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    try:
        d = Disk(path, reader=img)
        if not any(v.get('fs') in ('HFS+', 'HFSX') for v in d.volumes):
            return False, '没有可写的 HFS+/HFSX 卷'
        return True, '未压缩镜像，可原地读写'
    except Exception as e:
        return False, str(e)
    finally:
        img.close()


def edit_inplace(path: str, operations: list, cb=None,
                 volume_index: int = 0) -> dict:
    """在**原 DMG 上**执行删除/重命名/新建文件夹/加入/替换。

    operations 与 writer.edit_image 完全同构，便于界面层无缝切换。
    """
    cb = cb or (lambda *_: None)
    ed = HFSInplaceEditor(path, cb, volume_index)
    stat = {'mode': 'inplace', 'deleted': 0, 'renamed': 0,
            'created': 0, 'added': 0}
    try:
        kinds = [(o.get('op') or '').lower() for o in operations]
        if 'delete' in kinds:
            cb(18, 'Deleting Files...')
        elif any(k in ('add', 'replace') for k in kinds):
            cb(18, 'Copying %d File(s) to Mac Volume...'
               % sum(k in ('add', 'replace') for k in kinds))
        elif 'mkdir' in kinds or 'rename' in kinds:
            cb(18, 'Creating items on Mac Volume...')
        # 严格按调用方给的顺序执行（例如先新建文件夹、再往里加文件）
        for o, kind in zip(operations, kinds):
            p = (o.get('path') or '').strip('/')
            if kind == 'delete':
                stat['deleted'] += ed._delete_path(p)
            elif kind in ('add', 'replace'):
                ed._add(o.get('src'), p, kind == 'replace')
                stat['added'] += 1
            elif kind == 'rename':
                ed._rename(p, o.get('name', ''))
                stat['renamed'] += 1
            elif kind == 'mkdir':
                ed._mkdir(p)
                stat['created'] += 1
            else:
                raise InplaceError('未知编辑操作：%s' % kind)
        ed._commit_metadata()
        ed.undo.clear()
        cb(100, '写入完成')
        stat['size'] = os.path.getsize(path)
        return stat
    except Exception:
        ed.rollback()
        raise
    finally:
        ed.close()


def format_inplace(path: str, volume_name: str = 'Untitled', cb=None,
                   volume_index: int = 0) -> dict:
    """原地格式化：清空 Catalog 记录、重置位图与卷头，保留卷容量与分区表。"""
    cb = cb or (lambda *_: None)
    ed = HFSInplaceEditor(path, cb, volume_index)
    try:
        cb(20, 'Formatting...')
        name = _safe_name(volume_name)[:64] or 'Untitled'
        root = bytearray(_folder_record(ROOT_CNID, 0, ed.stamp))
        thread = bytearray(_thread_record(REC_FOLDER_THREAD, ROOT_PARENT,
                                          name))
        ed.by_key = {cat_key(ROOT_PARENT, ''): root,
                     cat_key(ROOT_CNID, ''): thread}
        # 释放所有非系统块：系统块由卷头 5 个 fork 的 extents 决定
        system = set()
        for i in range(5):
            off = ed.fork_off + i * 80
            for j in range(8):
                start, count = struct.unpack_from('>II', ed.vh, off + 16 + j * 8)
                if count:
                    system.update(range(start, start + count))
        system.update((0, 1, ed.total_blocks - 1))
        for b in range(ed.total_blocks):
            if b not in system:
                ed._set(b, False)
        for b in system:
            if b < ed.total_blocks:
                ed._set(b, True)
        ed.free_blocks = ed.total_blocks - len(system)
        ed.file_count, ed.folder_count = 0, 1
        ed.next_cnid = ROOT_CNID + 1
        ed._commit_metadata()
        ed.undo.clear()
        cb(100, 'Format Complete')
        return {'mode': 'inplace', 'formatted': True,
                'size': os.path.getsize(path)}
    except Exception:
        ed.rollback()
        raise
    finally:
        ed.close()


def _human(n: int) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f TB' % n
