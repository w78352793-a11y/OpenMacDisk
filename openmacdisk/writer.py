# -*- coding: utf-8 -*-
"""HFS+ 卷 / UDIF(DMG) 写入与整卷解压。

依据 Apple TN1150（HFS+）与公开的 UDIF 容器结构独立实现，纯标准库：
  - write_raw_disk():  由一组文件生成裸 HFS+ 磁盘镜像（含合法 Catalog B-tree）；
  - write_udif():      把裸镜像流式压缩成标准 UDIF/DMG（zlib 块表 + plist + koly）；
  - create_dmg():      从 Windows 文件夹/文件「压缩」成 DMG；
  - expand_dmg():      DMG 解压/展开为裸 IMG（刻录 / 重封装等底层用途）；
  - expand_to_editable_dmg(): eXpand，产出未压缩、可继续编辑的 UDIF DMG；
  - compress_to_dmg(): Compress，eXpand 的逆操作，流式重建压缩 UDIF DMG；
  - extract_all():     整卷解压到 Windows 目录，保留目录结构；
  - verify_image():    逻辑磁盘与文件的哈希校验。

所有写操作都由调用方显式给出目标路径；本模块不触碰物理磁盘，也不修改源镜像。
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import struct
import time
import zlib
from dataclasses import dataclass, field

from .udif import (SECTOR, BLOCK_RAW, BLOCK_ZLIB, BLOCK_TERM,
                   BLOCK_COMMENT, BLOCK_ZERO, BLOCK_FREE)
from .image import Disk

BLOCK = 4096
NODE = 4096
HFS_EPOCH = 2082844800          # 1904-01-01 → 1970-01-01
MAX_NAME = 255
CHUNK = 1 << 20                 # 压缩块 1 MiB（512 的整数倍）

NODE_DESC = struct.Struct('>IIBBHH')
BTH = struct.Struct('>HIIIIHHIIHIBBI16I')   # 106 字节 BTHeaderRec
FORK_HDR = struct.Struct('>QII')
MISH = struct.Struct('>4sIQQQII')
RUN = struct.Struct('>IIQQQQ')
KOLY = struct.Struct('>4sIIIQQQQQII16s')

KIND_HEADER, KIND_INDEX, KIND_LEAF, KIND_FREE = 1, 0, 0xFF, 0xFE
REC_FOLDER, REC_FILE, REC_FOLDER_THREAD, REC_FILE_THREAD = 1, 2, 3, 4
ROOT_CNID = 2
ROOT_PARENT = 1


class WriteError(Exception):
    pass


# ---------------------------------------------------------------- 数据源

@dataclass
class Entry:
    """待写入卷的一个条目；path 为卷内 POSIX 风格相对路径。"""
    path: str
    is_dir: bool = False
    size: int = 0
    src: str | None = None          # Windows 本地文件路径
    data: bytes | None = None       # 内存数据
    vol: object | None = None       # 既有 HFSVolume（重建镜像时用）
    node: object | None = None      # 既有 CatalogNode
    start_block: int = 0
    block_count: int = 0
    cnid: int = 0
    parent: int = ROOT_CNID

    def read(self, off: int, length: int) -> bytes:
        if length <= 0 or off >= self.size:
            return b''
        length = min(length, self.size - off)
        if self.data is not None:
            return self.data[off:off + length]
        if self.src:
            with open(self.src, 'rb') as f:
                f.seek(off)
                return f.read(length)
        if self.vol is not None and self.node is not None:
            return read_file_range(self.vol, self.node, off, length)
        return b'\x00' * length


def read_file_range(vol, node, off: int, length: int) -> bytes:
    """按 fork extents 从既有卷中读取文件的一段，避免整文件载入内存。"""
    if node.kind != 'file' or node.data_fork is None:
        return b''
    length = min(length, max(0, node.size - off))
    if length <= 0:
        return b''
    out = bytearray()
    cursor = 0
    end = off + length
    bs = vol.block_size
    exts = node.data_fork.extents
    try:
        from .hfs_extents import all_extents
        exts = all_extents(vol, node.data_fork, node.cnid)
    except Exception:
        pass
    for start, count in exts:
        ext_len = count * bs
        if cursor + ext_len <= off:
            cursor += ext_len
            continue
        lo, hi = max(off, cursor), min(end, cursor + ext_len)
        if hi > lo:
            disk_off = start * bs + (lo - cursor)
            out.extend(vol.reader.read(vol.base + disk_off, hi - lo))
        cursor += ext_len
        if cursor >= end:
            break
    return bytes(out[:length])


def _safe_name(name: str) -> str:
    """HFS+ 名称：去掉 NUL / 冒号（经典 Mac 分隔符），限制 255 个 UTF-16 单元。"""
    name = (name or '').replace('\x00', '').replace(':', '-')
    return name[:MAX_NAME] or 'untitled'


def collect_paths(paths, cb=None) -> list[Entry]:
    """把 Windows 路径（文件/文件夹）收集成卷内条目列表。"""
    out: list[Entry] = []
    seen: set[str] = set()

    def add(rel, is_dir, size=0, src=None):
        rel = '/'.join(_safe_name(p) for p in rel.split('/') if p)
        if not rel or rel in seen:
            return
        seen.add(rel)
        out.append(Entry(path=rel, is_dir=is_dir, size=size, src=src))

    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            base = os.path.basename(p.rstrip('\\/')) or 'folder'
            add(base, True)
            for root, dirs, files in os.walk(p):
                rel_root = os.path.relpath(root, p)
                prefix = '' if rel_root == '.' else rel_root.replace('\\', '/')
                rel_dir = base + (('/' + prefix) if prefix else '')
                add(rel_dir, True)
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        continue
                    add(rel_dir + '/' + fn, False, size, fp)
                    if cb:
                        cb(min(99, len(out) % 100), '扫描 %s' % fn[:40])
        elif os.path.isfile(p):
            add(os.path.basename(p), False, os.path.getsize(p), p)
    return out


def entries_from_volume(vol, cb=None) -> list[Entry]:
    """把已打开卷的全部条目转成可重建的数据源（不落盘）。"""
    out: list[Entry] = []
    for n in vol.walk():
        if n is vol.root or not n.path:
            continue
        parts = n.path.split('/')
        rel = '/'.join(_safe_name(x) for x in parts)
        out.append(Entry(path=rel, is_dir=n.kind == 'folder',
                         size=n.size if n.kind == 'file' else 0,
                         vol=vol, node=n))
        if cb and len(out) % 500 == 0:
            cb(min(99, len(out) % 100), '收集 %d 项' % len(out))
    return out


# ---------------------------------------------------------------- Catalog B-tree

def cat_key(parent: int, name: str) -> bytes:
    # 根目录记录的 name 必须为空，不能套用 _safe_name 的缺省名
    name = (name or '').replace('\x00', '').replace(':', '-')[:MAX_NAME]
    raw = name.encode('utf-16-be')
    klen = 6 + len(raw)
    return struct.pack('>HIH', klen, parent, len(raw) // 2) + raw


def _folder_record(cnid: int, valence: int, stamp: int) -> bytes:
    b = bytearray(88)
    struct.pack_into('>hHI', b, 0, REC_FOLDER, 0, valence)
    struct.pack_into('>I', b, 8, cnid)
    struct.pack_into('>I', b, 12, stamp)
    return bytes(b)


def _file_record(cnid: int, size: int, start: int, count: int,
                 stamp: int) -> bytes:
    b = bytearray(248)
    struct.pack_into('>hHI', b, 0, REC_FILE, 0, 0)
    struct.pack_into('>I', b, 8, cnid)
    struct.pack_into('>I', b, 12, stamp)
    FORK_HDR.pack_into(b, 88, size, BLOCK, count)
    if count:
        struct.pack_into('>II', b, 104, start, count)
    FORK_HDR.pack_into(b, 168, 0, BLOCK, 0)
    return bytes(b)


def _thread_record(kind: int, parent: int, name: str) -> bytes:
    b = bytearray(520)
    struct.pack_into('>hHI', b, 0, kind, 0, parent)
    raw = _safe_name(name).encode('utf-16-be')
    struct.pack_into('>H', b, 6, len(raw) // 2)
    b[8:8 + len(raw)] = raw
    return bytes(b)


def _key_cmp(key: bytes):
    """HFS+ 目录键排序：parentID → 名称（近似大小写折叠）。"""
    parent = struct.unpack_from('>I', key, 2)[0]
    nlen = struct.unpack_from('>H', key, 6)[0]
    name = key[8:8 + nlen * 2].decode('utf-16-be', 'replace')
    return (parent, name.upper(), name)


def _build_records(entries: list[Entry], volume_name: str,
                   data_start: int) -> tuple[list[tuple[bytes, bytes]], int]:
    """生成全部 (key, record)，顺带分配 CNID 与数据块。"""
    folders: dict[str, Entry] = {}
    for e in entries:
        if e.is_dir:
            folders[e.path.strip('/')] = e
        parts = e.path.strip('/').split('/')
        for i in range(1, len(parts)):
            p = '/'.join(parts[:i])
            if p not in folders:
                folders[p] = Entry(path=p, is_dir=True)

    ordered = sorted(folders.values(), key=lambda x: (x.path.count('/'), x.path))
    cnid = ROOT_CNID + 1
    ids: dict[str, int] = {'': ROOT_CNID}
    for f in ordered:
        ids[f.path.strip('/')] = cnid
        f.cnid = cnid
        parent_path = '/'.join(f.path.strip('/').split('/')[:-1])
        f.parent = ids.get(parent_path, ROOT_CNID)
        cnid += 1

    stamp = int(time.time()) + HFS_EPOCH
    # 先给文件分配 parent / CNID / 数据块，再统计各目录的 valence
    block = data_start
    files = [e for e in entries if not e.is_dir]
    for e in files:
        parts = e.path.strip('/').split('/')
        e.parent = ids.get('/'.join(parts[:-1]), ROOT_CNID)
        e.cnid = cnid
        cnid += 1
        e.start_block = block
        e.block_count = (e.size + BLOCK - 1) // BLOCK
        block += e.block_count

    valence: dict[int, int] = {}
    for e in list(ordered) + files:
        p = e.parent or ROOT_CNID
        valence[p] = valence.get(p, 0) + 1

    records: list[tuple[bytes, bytes]] = [
        (cat_key(ROOT_PARENT, ''),
         _folder_record(ROOT_CNID, valence.get(ROOT_CNID, 0), stamp)),
        (cat_key(ROOT_CNID, ''), _thread_record(REC_FOLDER_THREAD,
                                                ROOT_PARENT, volume_name)),
    ]
    for f in ordered:
        name = f.path.strip('/').split('/')[-1]
        records.append((cat_key(f.parent, name),
                        _folder_record(f.cnid, valence.get(f.cnid, 0), stamp)))
        records.append((cat_key(f.cnid, ''),
                        _thread_record(REC_FOLDER_THREAD, f.parent, name)))

    for e in files:
        parts = e.path.strip('/').split('/')
        records.append((cat_key(e.parent, parts[-1]),
                        _file_record(e.cnid, e.size, e.start_block,
                                     e.block_count, stamp)))
        records.append((cat_key(e.cnid, ''),
                        _thread_record(REC_FILE_THREAD, e.parent, parts[-1])))

    records.sort(key=lambda kv: _key_cmp(kv[0]))
    return records, cnid


def _pack_node(kind: int, height: int, records: list[bytes],
               flink: int = 0, blink: int = 0,
               node_size: int = NODE) -> bytes:
    buf = bytearray(node_size)
    NODE_DESC.pack_into(buf, 0, flink, blink, kind, height, len(records), 0)
    pos = 14
    offs = []
    for rec in records:
        offs.append(pos)
        buf[pos:pos + len(rec)] = rec
        pos += len(rec)
        if pos & 1:
            pos += 1
    offs.append(pos)
    tail = node_size - 2 * (len(records) + 1)
    if pos > tail:
        raise WriteError('B-tree 节点溢出（记录 %d 条 / %d 字节）'
                         % (len(records), pos))
    # TN1150：记录偏移表在节点末尾倒序存放
    struct.pack_into('>%dH' % len(offs), buf, tail, *reversed(offs))
    return bytes(buf)


def _split_records(records: list[bytes],
                   node_size: int = NODE) -> list[list[bytes]]:
    """按节点容量切分记录，返回每节点的记录列表。"""
    pages, cur, used = [], [], 14
    for rec in records:
        cost = len(rec) + (len(rec) & 1) + 2   # 数据 + 对齐 + 偏移表项
        if cur and used + cost > node_size - 2:
            pages.append(cur)
            cur, used = [], 14
        cur.append(rec)
        used += cost
    if cur:
        pages.append(cur)
    return pages or [[]]


def build_catalog(records: list[tuple[bytes, bytes]]) -> tuple[bytes, int]:
    """构造完整 Catalog B-tree（头节点 + 叶节点 + 索引节点 + 空闲节点）。"""
    leaf_pages = _split_records([k + v for k, v in records])
    leaves = []
    for i, page in enumerate(leaf_pages):
        flink = i + 2 if i + 1 < len(leaf_pages) else 0
        blink = i if i > 0 else 0
        leaves.append(_pack_node(KIND_LEAF, 1, page, flink, blink))

    nodes: list[bytes] = [b''] + leaves       # 0 号留作头节点
    child_ids = list(range(1, len(leaves) + 1))
    child_keys = [_key_of_last(p) for p in leaf_pages]
    height = 1
    level = 1

    while len(child_ids) > 1:
        level += 1
        idx_records = [k + struct.pack('>I', cid)
                       for k, cid in zip(child_keys, child_ids)]
        pages = _split_records(idx_records)
        new_ids, new_keys = [], []
        for page in pages:
            nid = len(nodes)
            nodes.append(_pack_node(KIND_INDEX, level, page))
            new_ids.append(nid)
            new_keys.append(_key_of_last(page, index=True))
        child_ids, child_keys = new_ids, new_keys
        height = level

    root = child_ids[0]
    used = len(nodes)
    free_count = max(2, used // 8)
    total_nodes = used + free_count
    for j in range(free_count):
        nid = len(nodes)
        flink = nid + 1 if j + 1 < free_count else 0
        nodes.append(_pack_node(KIND_FREE, 0, [], flink, 0))

    # 头节点：BTHeaderRec + 128 字节用户数据 + 节点占用位图
    map_size = min((total_nodes + 7) // 8, NODE - 248 - 8)
    bitmap = bytearray(map_size)
    for i in range(used):
        bitmap[i // 8] |= 0x80 >> (i % 8)
    hdr = BTH.pack(height, root, len(records),
                   1 if leaves else 0, len(leaves), NODE, 516,
                   total_nodes, free_count, 0, NODE, 0, 0, 0,
                   *([0] * 16))
    nodes[0] = _pack_node(KIND_HEADER, 0, [hdr, bytes(128), bytes(bitmap)])
    return b''.join(nodes), total_nodes


def _key_of_last(page: list[bytes], index: bool = False) -> bytes:
    """取出节点中最大的 key（索引项只保留 key，去掉 4 字节子节点号）。"""
    last = page[-1] if page else cat_key(0, '')
    if index:
        return last[:-4]
    klen = struct.unpack_from('>H', last, 0)[0]
    return last[:2 + klen]


def empty_btree() -> bytes:
    """extents / attributes 文件用的空 B-tree（头节点 + 空叶节点）。"""
    hdr = BTH.pack(1, 1, 0, 1, 1, NODE, 516, 2, 0, 0, NODE, 0, 0, 0,
                   *([0] * 16))
    bitmap = bytes([0xC0]) + bytes(7)
    return (_pack_node(KIND_HEADER, 0, [hdr, bytes(128), bitmap]) +
            _pack_node(KIND_LEAF, 1, []))


# ---------------------------------------------------------------- 裸 HFS+ 卷

def _fork(size: int, start: int, count: int, clump: int = BLOCK) -> bytes:
    b = bytearray(80)
    FORK_HDR.pack_into(b, 0, size, clump, count)
    if count:
        struct.pack_into('>II', b, 16, start, count)
    return bytes(b)


def write_raw_disk(path: str, entries: list[Entry], volume_name: str,
                   cb=None, extra_blocks: int = 0) -> int:
    """生成裸 HFS+ 磁盘镜像，返回总字节数；extra_blocks 追加空闲容量。"""
    cb = cb or (lambda *_: None)
    volume_name = _safe_name(volume_name)[:64] or 'Untitled'
    extra_blocks = max(0, int(extra_blocks))
    data_blocks = sum((e.size + BLOCK - 1) // BLOCK
                      for e in entries if not e.is_dir)
    # 先估算 catalog 节点数（记录数决定），再迭代确定卷大小
    probe_records, _ = _build_records(entries, volume_name, 0)
    probe_cat, probe_nodes = build_catalog(probe_records)
    cat_blocks = probe_nodes * NODE // BLOCK
    ext_blocks = 2
    attr_blocks = 2

    total_blocks = 1 + 1 + ext_blocks + cat_blocks + attr_blocks + \
        data_blocks + 1 + max(16, data_blocks // 20) + extra_blocks
    for _ in range(4):
        alloc_blocks = max(1, ((total_blocks + 7) // 8 + BLOCK - 1) // BLOCK)
        new_total = (1 + alloc_blocks + ext_blocks + cat_blocks +
                     attr_blocks + data_blocks + 1 +
                     max(16, data_blocks // 20) + extra_blocks)
        if new_total == total_blocks:
            break
        total_blocks = new_total
    alloc_blocks = max(1, ((total_blocks + 7) // 8 + BLOCK - 1) // BLOCK)

    alloc_start = 1
    ext_start = alloc_start + alloc_blocks
    cat_start = ext_start + ext_blocks
    attr_start = cat_start + cat_blocks
    data_start = attr_start + attr_blocks
    last_block = total_blocks - 1
    volume_size = total_blocks * BLOCK

    records, next_cnid = _build_records(entries, volume_name, data_start)
    cat_data, cat_nodes = build_catalog(records)
    if cat_nodes * NODE // BLOCK != cat_blocks:
        raise WriteError('Catalog 节点数估算不一致')

    # 分配位图
    bitmap = bytearray(alloc_blocks * BLOCK)
    used = [(0, 1), (alloc_start, alloc_blocks), (ext_start, ext_blocks),
            (cat_start, cat_blocks), (attr_start, attr_blocks),
            (data_start, data_blocks), (last_block, 1)]
    for start, count in used:
        for b in range(start, start + count):
            bitmap[b // 8] |= 0x80 >> (b % 8)

    stamp = int(time.time()) + HFS_EPOCH
    vh = bytearray(512)
    vh[0:2] = b'H+'
    struct.pack_into('>H', vh, 2, 4)
    struct.pack_into('>I', vh, 4, 0x00000100)      # 干净卸载标志
    vh[8:12] = b'8.10'
    for off in (16, 20, 24, 28):
        struct.pack_into('>I', vh, off, stamp)
    struct.pack_into('>I', vh, 32,
                     sum(1 for e in entries if not e.is_dir))
    struct.pack_into('>I', vh, 36,
                     sum(1 for e in entries if e.is_dir) + 1)
    struct.pack_into('>I', vh, 40, BLOCK)
    struct.pack_into('>I', vh, 44, total_blocks)
    struct.pack_into('>I', vh, 48, total_blocks - sum(c for _, c in used))
    struct.pack_into('>I', vh, 52, data_start + data_blocks)
    struct.pack_into('>I', vh, 56, BLOCK)
    struct.pack_into('>I', vh, 60, BLOCK)
    struct.pack_into('>I', vh, 64, next_cnid)
    struct.pack_into('>I', vh, 68, 1)
    struct.pack_into('>Q', vh, 72, 1)              # encodingsBitmap
    vh[112:192] = _fork((total_blocks + 7) // 8, alloc_start, alloc_blocks)
    vh[192:272] = _fork(2 * NODE, ext_start, ext_blocks)
    vh[272:352] = _fork(cat_nodes * NODE, cat_start, cat_blocks)
    vh[352:432] = _fork(2 * NODE, attr_start, attr_blocks)

    cb(5, '写入卷结构')
    with open(path, 'wb') as f:
        f.truncate(volume_size)
        f.seek(1024)
        f.write(vh)
        f.seek(alloc_start * BLOCK)
        f.write(bitmap)
        f.seek(ext_start * BLOCK)
        f.write(empty_btree())
        f.seek(cat_start * BLOCK)
        f.write(cat_data)
        f.seek(attr_start * BLOCK)
        f.write(empty_btree())
        files = [e for e in entries if not e.is_dir]
        for i, e in enumerate(files):
            if not e.size:
                continue
            f.seek(e.start_block * BLOCK)
            done = 0
            while done < e.size:
                take = min(CHUNK, e.size - done)
                f.write(e.read(done, take))
                done += take
            if i % 20 == 0 or i + 1 == len(files):
                cb(5 + 90 * (i + 1) / max(1, len(files)),
                   '写入 %d/%d：%s' % (i + 1, len(files), e.path[-46:]))
        f.seek(volume_size - 1024)
        f.write(vh)                                # 备用卷头
        f.write(bytes(512))
    cb(100, '裸镜像完成')
    return volume_size


# ---------------------------------------------------------------- UDIF / DMG

# Apple hdiutil / TransMac 生成的 UDIF 都是 512 扇区（256 KiB）一块；
# 咱们以前用 1 MiB，块表规模、mish 缓冲字段都和原版对不上。
UDIF_CHUNK = 512 * SECTOR
STANDARD_BLKX_NAME = 'whole disk (Apple_HFS : 0)'
# Apple hdiutil 写 ZLIB 段用的是 zlib level 7（段头 0x78da）。level 6 会
# 让每个 256 KiB 块多出几字节，2.4 GB 的镜像累计差 11 KB，于是
# 「原版 → eXpand → Compress」回不去原来的文件。实测 9 个抽样段逐字节命中。
UDIF_ZLIB_LEVEL = 7


def read_udif_template(path: str) -> dict | None:
    """只读源 DMG 尾部的 koly + XML plist，取出可复用的资源叉模板。

    TransMac 的 eXpand / Compress 并不重新发明容器元数据，而是沿用原镜像
    的 resource-fork（blkx 名称 / ID / 属性 / CRC32 校验和 / plst 分区资源）
    与 koly 固定字段，只替换块表。第三方解析器（7-Zip、Bandizip 等）正是
    靠这些字段定位 Apple_HFS 分区；缺了 plst 就会把整盘当一个坏的 0.hfs。
    """
    try:
        size = os.path.getsize(path)
        if size < 512:
            return None
        with open(path, 'rb') as f:
            f.seek(size - 512)
            koly = f.read(512)
            if koly[:4] != b'koly':
                return None
            xo, xl = struct.unpack_from('>QQ', koly, 0xD8)
            if not (0 <= xo < size and 0 < xl <= size - xo):
                return None
            f.seek(xo)
            plist = plistlib.loads(f.read(xl))
        rf = plist.get('resource-fork')
        if not isinstance(rf, dict) or not rf.get('blkx'):
            return None
        return {'koly': koly, 'resource_fork': rf}
    except Exception:
        return None


def _standard_resource_fork() -> dict:
    """无源模板时的标准资源叉：blkx 用原版名称，plst 分区资源占位。"""
    plst = bytearray(1032)
    plst[517] = plst[519] = 1          # 与真实 DMG 的 plst 标志位一致
    return {
        'blkx': [{'Attributes': '0x0050', 'Data': b'', 'ID': '0',
                  'Name': STANDARD_BLKX_NAME}],
        'plst': [{'Attributes': '0x0050', 'Data': bytes(plst),
                  'ID': '0', 'Name': ''}],
    }


def _mish_head(rf: dict, crc32_value: int | None = None) -> bytearray:
    """复用源 mish 的 204 字节头，但 CRC32 一律按本次逻辑盘重算。

    以前只在旧自产镜像（type=0）上写新 CRC，套用真实模板时沿用源文件的值，
    卷被编辑过后校验和就变成错的了 —— macOS 会按它校验整盘。
    """
    try:
        d = rf['blkx'][0]['Data']
        if len(d) >= 204 and d[:4] == b'mish':
            head = bytearray(d[:204])
            struct.pack_into('>II', head, 0x40, 2, 32)
            if crc32_value is not None:
                struct.pack_into('>I', head, 0x48,
                                 crc32_value & 0xFFFFFFFF)
            return head
    except Exception:
        pass
    head = bytearray(204)
    MISH.pack_into(head, 0, b'mish', 1, 0, 0, 0, 520, 0)
    struct.pack_into('>II', head, 64, 2, 32)
    if crc32_value is not None:
        struct.pack_into('>I', head, 0x48, crc32_value & 0xFFFFFFFF)
    return head


def _template_comments(template: dict | None) -> tuple[int, int] | None:
    """取出源 DMG 首尾两条 COMMENT run 的注释 ID（Apple/hdiutil 惯例）。

    真实 macOS DMG 的 blkx 块表是：COMMENT(卷首) + 数据块… +
    COMMENT(卷尾，记录总扇区数与数据叉长度) + TERM。7-Zip 靠最后两条
    run 判定块表是否闭合；缺了它们会把整个分区标成 CORRUPTED、目录树为空。
    """
    if not template:
        return None
    try:
        d = template['resource_fork']['blkx'][0]['Data']
        n = struct.unpack_from('>I', d, 0xC8)[0]
        if n < 2:
            return None
        first = RUN.unpack_from(d, 204)
        last = RUN.unpack_from(d, 204 + 40 * (n - 2))
        if first[0] == BLOCK_COMMENT and last[0] == BLOCK_COMMENT:
            return first[1], last[1]
    except Exception:
        pass
    return None


def _apple_plist_xml(pl: dict) -> bytes:
    """按 Apple hdiutil 的序列化习惯输出 plist。

    CFPropertyList 用 CR（经典 Mac 行尾）和旧 DTD 名 "Apple Computer"；
    plistlib 用 LF 和新 DTD 名。这一差异正好让 XML 少 9 字节，也是
    往返产物无法与原版逐字节相同的最后一处文本差异。
    """
    raw = plistlib.dumps(pl, fmt=plistlib.FMT_XML, sort_keys=True)
    return raw.replace(b'-//Apple//DTD PLIST 1.0//EN',
                       b'-//Apple Computer//DTD PLIST 1.0//EN').replace(
                           b'\n', b'\r')


def _build_resource_fork(rf: dict) -> bytes:
    """生成 UDIF 尾部的独立二进制资源叉（经典 Mac resource fork 格式）。

    布局：16 B 文件头 + 240 B 系统保留区 + 数据区（每个资源 4 B 长度 +
    内容）+ 资源映射（类型表 / 引用表 / 名字表）。XML plist 与这份二进制
    资源叉是同一份元数据的两种编码，Apple 与 7-Zip 都会读它；缺了它，
    「解开再合起来」的文件就比原版少 404 KB，koly 里的 RsrcFork 只能填 0。
    这里按公开的 resource fork 结构独立实现，不复制任何第三方字节。
    """
    entries: list[tuple[bytes, bytes, bytes, int]] = []
    for kind in (b'blkx', b'plst'):
        key = kind.decode('ascii')
        for i, res in enumerate(rf.get(key, [])):
            data = res.get('Data', b'')
            name = (res.get('Name') or '').encode('utf-8')[:255]
            raw_id = str(res.get('ID', str(i)))
            try:
                rid = int(raw_id, 0) if raw_id.lower().startswith('0x') \
                    else int(raw_id)
            except ValueError:
                rid = i
            entries.append((kind, data, name, rid))
    if not entries:
        return b''
    area = bytearray()
    offs: list[int] = []
    for _kind, data, _name, _rid in entries:
        offs.append(len(area))
        area += struct.pack('>I', len(data)) + data
    data_area = bytes(area)
    map_off = 256 + len(data_area)

    kinds: list[bytes] = []
    for kind, _data, _name, _rid in entries:
        if kind not in kinds:
            kinds.append(kind)

    # 类型表固定在映射偏移 28；引用表紧随其后；名字表再紧随引用表。
    type_list_off = 28
    type_list_len = 2 + 8 * len(kinds)
    name_list_off = type_list_off + type_list_len + 12 * len(entries)
    name_blob = bytearray()
    name_offsets: list[int] = []
    for _kind, _data, name, _rid in entries:
        if name:
            name_offsets.append(len(name_blob))
            name_blob += bytes([len(name)]) + name
        else:
            name_offsets.append(0xFFFF)

    m = bytearray(16)           # 映射开头是文件头的副本，全 0
    m += struct.pack('>IHHHH', 0, 0, 0, type_list_off, name_list_off)
    m += struct.pack('>H', len(kinds) - 1)
    ref_cursor = type_list_len
    for kind in kinds:
        count = sum(1 for k, *_ in entries if k == kind)
        m += struct.pack('>4sHH', kind, count - 1, ref_cursor)
        ref_cursor += 12 * count
    for (kind, _data, _name, rid), off, noff in zip(
            entries, offs, name_offsets):
        m += struct.pack('>hH', rid, noff)
        m += bytes([0x50])                  # 资源属性，与 Apple 一致
        m += off.to_bytes(3, 'big')
        m += bytes(4)                       # reserved handle
    m += name_blob
    m = bytes(m)

    head = struct.pack('>IIII', 256, map_off, len(data_area), len(m))
    return head + bytes(256 - len(head)) + data_area + m


def _master_checksum(template: dict | None,
                     crc32_value: int | None) -> bytes | None:
    """返回可直接沿用的源主校验和块（0x160:0x1E8），否则返回 None。

    koly 里的 master checksum 覆盖的是容器元数据。逻辑盘 CRC32 未变说明
    这一路只是重排压缩段（eXpand → Compress 往返），元数据实际内容相同，
    源值依然成立，沿用它才能让往返产物与原版逐字节相同；一旦用户编辑过
    卷（CRC32 变化），就不能给一个过期的值，改为清零 —— type=0 表示
    「不提供校验和」，macOS 会跳过而不是拿错误值拒绝挂载。
    """
    if not template or crc32_value is None:
        return None
    try:
        src = template['resource_fork']['blkx'][0]['Data']
        src_crc = struct.unpack_from('>I', src, 0x48)[0]
        if src_crc == (crc32_value & 0xFFFFFFFF):
            return bytes(template['koly'][0x160:0x1E8])
    except Exception:
        pass
    return None


def _build_xml(runs: list[tuple], sector_count: int,
               template: dict | None = None,
               crc32_value: int | None = None,
               data_len: int = 0) -> bytes:
    """生成完整 plist：保留 blkx / plst 全部元数据，只重建块表。"""
    rf = None
    if template:
        src = template['resource_fork']
        if len(src.get('blkx', [])) == 1:
            rf = {k: v for k, v in src.items() if k != 'blkx'}
            rf['blkx'] = [dict(src['blkx'][0])]
    if rf is None:
        rf = _standard_resource_fork()
    head = _mish_head(rf, crc32_value)
    struct.pack_into('>Q', head, 0x10, sector_count)
    if not data_len and runs:
        data_len = runs[-1][3] + runs[-1][4]
    comments = _template_comments(template)
    pieces: list[bytes] = []
    if comments:
        pieces.append(RUN.pack(BLOCK_COMMENT, comments[0], 0, 0, 0, 0))
    pieces.extend(RUN.pack(t, 0, s, c, o, l) for t, s, c, o, l in runs)
    if comments:
        pieces.append(RUN.pack(BLOCK_COMMENT, comments[1], sector_count,
                               0, data_len, 0))
    # TERM run 必须收在总扇区数 / 数据叉末尾，写全 0 会被第三方解析器
    # 判定为块表未闭合（Method=CORRUPTED、只有 0.hfs、校验和显示 0）。
    pieces.append(RUN.pack(BLOCK_TERM, 0, sector_count, 0, data_len, 0))
    struct.pack_into('>I', head, 200, len(pieces))
    rf['blkx'][0]['Data'] = bytes(head) + b''.join(pieces)
    # 原版 blkx 只有 Attributes/Data/ID/Name 四个键；多出的 CFName 会让
    # 一些按固定键序读取的第三方解析器错位，进而把分区标成 CORRUPTED。
    rf['blkx'][0].pop('CFName', None)
    return _apple_plist_xml({'resource-fork': rf})


def _koly(data_len: int, xml_off: int, xml_len: int,
          sector_count: int, template: dict | None = None,
          rsrc_off: int = 0, rsrc_len: int = 0,
          master_checksum: bytes | None = None,
          data_fork_crc: int | None = None) -> bytes:
    if template:
        t = bytearray(template['koly'][:512])
        # DataForkChecksum 覆盖的是数据叉本身：只要重排/压缩过，源值就过期。
        # - 源本来就不提供（type=0）：原样保留，保证 create→eXpand→Compress
        #   的逐字节一致性；
        # - 源提供 type=2：按实际写出的数据叉重算 CRC32（无编辑往返时算出来
        #   与源值相同）。拿不到 CRC 时清零为 type=0，macOS 会跳过校验，
        #   而不是拿一个不匹配的值拒绝挂载。
        src_type, src_size = struct.unpack_from('>II', t, 0x50)
        if src_type == 2 and src_size == 32:
            src_crc = struct.unpack_from('>I', t, 0x58)[0]
            if data_fork_crc is not None:
                new_crc = data_fork_crc & 0xFFFFFFFF
                if new_crc != src_crc:
                    # koly 前 0x160 字节变了，源主校验和随之失效
                    master_checksum = None
                t[0x58:0xD8] = bytes(0xD8 - 0x58)
                struct.pack_into('>I', t, 0x58, new_crc)
            else:
                t[0x50:0xD8] = bytes(0xD8 - 0x50)
                master_checksum = None
        # 主校验和只在逻辑盘内容未变时可沿用，过期的值必须清零。
        t[0x160:0x1E8] = master_checksum or bytes(0x1E8 - 0x160)
    else:
        t = bytearray(512)
        KOLY.pack_into(t, 0, b'koly', 4, 512, 1,
                       0, 0, 0, 0, 0, 0, 1, bytes(16))
        struct.pack_into('>II', t, 0x50, 0, 0)   # 不提供 data fork 校验和
        struct.pack_into('>I', t, 0x1E8, 1)
        t[0x160:0x1E8] = master_checksum or bytes(0x1E8 - 0x160)
    struct.pack_into('>Q', t, 0x20, data_len)
    struct.pack_into('>QQ', t, 0x28, rsrc_off, rsrc_len)
    struct.pack_into('>QQ', t, 0xD8, xml_off, xml_len)
    struct.pack_into('>Q', t, 0x1EC, sector_count)
    return bytes(t)


def _write_trailer(fo, runs: list[tuple], data_len: int,
                   sector_count: int, template: dict | None,
                   crc32_value: int | None,
                   data_fork_crc: int | None = None) -> None:
    # Apple 的标准尾部顺序：数据叉 | XML plist | 二进制资源叉 | koly。
    # 以前少写了资源叉，koly 只能把 RsrcFork 填 0，第三方解析器既读不到
    # 分区资源，又会因为沿用源文件的越界偏移直接拒绝打开。
    xml = _build_xml(runs, sector_count, template, crc32_value, data_len)
    rf = plistlib.loads(xml)['resource-fork']
    rsrc = _build_resource_fork(rf)
    fo.write(xml)
    fo.write(rsrc)
    fo.write(_koly(data_len, data_len, len(xml), sector_count, template,
                   data_len + len(xml), len(rsrc),
                   _master_checksum(template, crc32_value),
                   data_fork_crc))


def write_udif(out_path: str, raw_path: str, volume_name: str = 'Untitled',
               compressed: bool = True, cb=None) -> int:
    """把裸磁盘镜像封装为 UDIF/DMG；返回生成的文件大小。"""
    cb = cb or (lambda *_: None)
    raw_size = os.path.getsize(raw_path)
    if raw_size % SECTOR:
        raise WriteError('裸镜像大小不是 512 的整数倍')
    sector_count = raw_size // SECTOR
    runs: list[tuple] = []
    data_len = 0
    crc = 0
    cb(2, '压缩镜像数据')
    with open(raw_path, 'rb') as fi, open(out_path, 'wb') as fo:
        sector = 0
        while True:
            buf = fi.read(UDIF_CHUNK)
            if not buf:
                break
            if len(buf) % SECTOR:
                buf += bytes(SECTOR - len(buf) % SECTOR)
            crc = zlib.crc32(buf, crc)
            count = len(buf) // SECTOR
            if compressed:
                packed = zlib.compress(buf, UDIF_ZLIB_LEVEL)
                if len(packed) < len(buf):
                    typ, payload = BLOCK_ZLIB, packed
                else:
                    typ, payload = BLOCK_RAW, buf
            else:
                typ, payload = BLOCK_RAW, buf
            runs.append((typ, sector, count, data_len, len(payload)))
            fo.write(payload)
            data_len += len(payload)
            sector += count
            cb(2 + 88 * min(1, sector / max(1, sector_count)),
               '压缩 %d/%d 扇区' % (sector, sector_count))

        _write_trailer(fo, runs, data_len, sector_count, None,
                       crc & 0xFFFFFFFF)
    cb(100, 'DMG 生成完成')
    return os.path.getsize(out_path)


def create_dmg(out_path: str, entries: list[Entry], volume_name: str,
               compressed: bool = True, cb=None, extra_blocks: int = 0) -> int:
    """从条目列表生成 DMG（先裸卷，再 UDIF 封装）。

    extra_blocks 在卷末尾追加指定数量的空闲块，用于「原地写不下时重建镜像」
    的场景，保证重建出的 DMG 仍留有可继续编辑的余量。
    """
    import tempfile
    d = os.path.dirname(os.path.abspath(out_path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.omd_', suffix='.img', dir=d)
    os.close(fd)
    try:
        write_raw_disk(tmp, entries, volume_name,
                       lambda p, m: cb(p * 0.55, m) if cb else None,
                       extra_blocks=extra_blocks)
        size = write_udif(out_path, tmp, volume_name, compressed,
                          lambda p, m: cb(55 + p * 0.45, m) if cb else None)
        return size
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def create_from_paths(out_path: str, paths, volume_name: str | None = None,
                      compressed: bool = True, cb=None) -> int:
    name = volume_name or os.path.basename(
        os.path.abspath(paths[0]).rstrip('\\/')) or 'Untitled'
    return create_dmg(out_path, collect_paths(paths, cb), name,
                      compressed, cb)


def create_blank_dmg(out_path: str, volume_name: str = 'Untitled',
                     size_mb: int = 16, cb=None) -> int:
    """创建已格式化为 HFS+ 的空白 DMG（空闲区全零，压缩后很小）。"""
    import tempfile
    d = os.path.dirname(os.path.abspath(out_path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.omd_blank_', suffix='.img', dir=d)
    os.close(fd)
    try:
        write_raw_disk(tmp, [], volume_name,
                       lambda p, m: cb(p * 0.55, m) if cb else None,
                       extra_blocks=max(1, int(size_mb)) * 256)
        return write_udif(out_path, tmp, volume_name, True,
                          lambda p, m: cb(55 + p * 0.45, m) if cb else None)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def expand_dmg(image_path: str, out_path: str, cb=None) -> int:
    """把任意 UDIF/DMG（或裸镜像）展开为不含压缩的裸 IMG。

    这是底层函数，供重封装、刻录（Windows 刻录器只认裸镜像）使用；
    资源管理器右键的「eXpand」请用 expand_to_editable_dmg()，它会产出
    仍可编辑的 .dmg，而不是裸 IMG 或散落的文件。
    """
    cb = cb or (lambda *_: None)
    d = Disk(image_path)
    try:
        reader = d.reader
        total = d.logical_size
        pos, done = 0, 0
        with open(out_path, 'wb') as f:
            while pos < total:
                buf = reader.read(pos, min(8 << 20, total - pos))
                if not buf:
                    break
                f.write(buf)
                pos += len(buf)
                done += len(buf)
                cb(100 * done / max(1, total),
                   '展开 %s / %s' % (_hum(done), _hum(total)))
        cb(100, '展开完成')
        return done
    finally:
        d.close()


def expand_to_editable_dmg(image_path: str, out_path: str, cb=None) -> int:
    """eXpand：把压缩 DMG 展开为「未压缩、可继续编辑」的 UDIF DMG。

    解压的是 UDIF 容器里的压缩段，磁盘内容（分区表 + HFS+ 卷）原样保留，
    再用全 RAW 块重新封装成标准 DMG（blkx + plist + koly）。产物仍是
    单个 .dmg 文件，可以直接打开、添加/删除/重命名/格式化，不会出现
    一堆散落的文件或无法再编辑的裸 IMG。

    与 repack_dmg() 的区别：这里直接从镜像 reader 流式写出，不产生
    额外的临时裸镜像，大镜像也能一次展开。
    """
    cb = cb or (lambda *_: None)
    src = os.path.abspath(image_path)
    dst = os.path.abspath(out_path)
    if src == dst:
        raise WriteError('输出路径不能与源镜像相同')
    root, ext = os.path.splitext(dst)
    if ext.lower() != '.dmg':
        dst = root + '.dmg'
    volume_name = os.path.splitext(os.path.basename(dst))[0] or 'Untitled'
    # 沿用源镜像的 resource-fork / koly 元数据，只重建块表
    template = read_udif_template(image_path)

    with Disk(image_path) as disk:
        reader = disk.reader
        total = disk.logical_size
        sector_count = (total + SECTOR - 1) // SECTOR
        runs: list[tuple] = []
        data_len = 0
        crc = 0
        data_crc = 0
        pos = 0
        with open(dst, 'wb') as fo:
            while pos < total:
                buf = reader.read(pos, min(UDIF_CHUNK, total - pos))
                if not buf:
                    break
                raw = buf if len(buf) % SECTOR == 0 else (
                    buf + bytes(SECTOR - len(buf) % SECTOR))
                crc = zlib.crc32(raw, crc)
                count = len(raw) // SECTOR
                runs.append((BLOCK_RAW, pos // SECTOR, count,
                             data_len, len(raw)))
                fo.write(raw)
                data_crc = zlib.crc32(raw, data_crc)
                data_len += len(raw)
                pos += len(buf)
                cb(90 * min(1.0, pos / max(1, total)),
                   '展开 %s / %s' % (_hum(pos), _hum(total)))
            _write_trailer(fo, runs, data_len, sector_count, template,
                           crc & 0xFFFFFFFF, data_crc & 0xFFFFFFFF)
    cb(100, 'eXpand 完成')
    return os.path.getsize(dst)


def compress_to_dmg(image_path: str, out_path: str, cb=None) -> int:
    """Compress：把（编辑完的）DMG 压成体积更小的标准 UDIF DMG。

    对应 TransMac 卷节点右键的「Compress」，也是 eXpand 的逆操作：
    从镜像 reader 流式读出逻辑磁盘，按 256 KiB（512 扇区）分块做 zlib 压缩，压得更小的
    写 BLOCK_ZLIB 段、压不动的保留 BLOCK_RAW 段，最后重建 blkx / plist /
    koly。分区表与 HFS+ 卷内容逐字节不变，产物仍是单个 .dmg，可直接打开、
    再次 eXpand 后继续编辑（压缩卷本身只读，与 TransMac 行为一致）。

    与 repack_dmg() 的区别：这里不落一份等大的临时裸 IMG，2 GB+ 的大卷
    也不需要额外的空闲磁盘空间。
    """
    cb = cb or (lambda *_: None)
    src = os.path.abspath(image_path)
    dst = os.path.abspath(out_path)
    if src == dst:
        raise WriteError('输出路径不能与源镜像相同')
    root, ext = os.path.splitext(dst)
    if ext.lower() != '.dmg':
        dst = root + '.dmg'
    volume_name = os.path.splitext(os.path.basename(dst))[0] or 'Untitled'
    template = read_udif_template(image_path)

    with Disk(image_path) as disk:
        reader = disk.reader
        total = disk.logical_size
        sector_count = (total + SECTOR - 1) // SECTOR
        runs: list[tuple] = []
        data_len = 0
        crc = 0
        data_crc = 0
        pos = 0
        with open(dst, 'wb') as fo:
            while pos < total:
                buf = reader.read(pos, min(UDIF_CHUNK, total - pos))
                if not buf:
                    break
                raw = buf if len(buf) % SECTOR == 0 else (
                    buf + bytes(SECTOR - len(buf) % SECTOR))
                crc = zlib.crc32(raw, crc)
                packed = zlib.compress(raw, UDIF_ZLIB_LEVEL)
                if len(packed) < len(raw):
                    typ, payload = BLOCK_ZLIB, packed
                else:
                    typ, payload = BLOCK_RAW, raw
                count = len(raw) // SECTOR
                runs.append((typ, pos // SECTOR, count,
                             data_len, len(payload)))
                fo.write(payload)
                data_crc = zlib.crc32(payload, data_crc)
                data_len += len(payload)
                pos += len(buf)
                cb(90 * min(1.0, pos / max(1, total)),
                   '压缩 %s / %s' % (_hum(pos), _hum(total)))
            _write_trailer(fo, runs, data_len, sector_count, template,
                           crc & 0xFFFFFFFF, data_crc & 0xFFFFFFFF)
    cb(100, 'Compress 完成')
    return os.path.getsize(dst)


def extract_all(image_path: str, dest: str, cb=None) -> tuple[int, int]:
    """整卷解压到 Windows 目录，保留目录结构，返回 (成功数, 失败数)。"""
    cb = cb or (lambda *_: None)
    d = Disk(image_path)
    try:
        vol = d.first_volume
        files = [n for n in vol.walk() if n.kind == 'file']
        ok = fail = 0
        for i, n in enumerate(files):
            rel = '/'.join(_safe_name(x) for x in n.path.split('/'))
            target = os.path.join(dest, *rel.split('/'))
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.exists(target):
                    stem, ext = os.path.splitext(target)
                    k = 1
                    while os.path.exists('%s (%d)%s' % (stem, k, ext)):
                        k += 1
                    target = '%s (%d)%s' % (stem, k, ext)
                with open(target, 'wb') as f:
                    off = 0
                    while off < n.size:
                        chunk = read_file_range(vol, n, off, min(CHUNK, n.size - off))
                        if not chunk:
                            break
                        f.write(chunk)
                        off += len(chunk)
                ok += 1
            except Exception:
                fail += 1
            if i % 20 == 0 or i + 1 == len(files):
                cb(100 * (i + 1) / max(1, len(files)),
                   '解压 %d/%d：%s' % (i + 1, len(files), rel[-46:]))
        return ok, fail
    finally:
        d.close()


def extract_selected(vol, nodes, dest: str, cb=None) -> tuple[int, int]:
    """解压选中的文件/目录到 dest，保留相对目录结构。"""
    cb = cb or (lambda *_: None)
    files, seen = [], set()
    for n in nodes:
        if n.kind == 'folder':
            base = n.path
            for f in vol.walk(n):
                if f.kind == 'file' and f.cnid not in seen:
                    seen.add(f.cnid)
                    rel = f.path[len(base):].lstrip('/') if base else f.path
                    files.append((f, rel))
        elif n.kind == 'file' and n.cnid not in seen:
            seen.add(n.cnid)
            files.append((n, '/'.join(_safe_name(x)
                                      for x in n.path.split('/')[-1:])))

    ok = fail = 0
    for i, (n, rel) in enumerate(files):
        rel = '/'.join(_safe_name(x) for x in rel.split('/'))
        target = os.path.join(dest, *rel.split('/'))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target):
                stem, ext = os.path.splitext(target)
                k = 1
                while os.path.exists('%s (%d)%s' % (stem, k, ext)):
                    k += 1
                target = '%s (%d)%s' % (stem, k, ext)
            with open(target, 'wb') as f:
                off = 0
                while off < n.size:
                    chunk = read_file_range(vol, n, off,
                                            min(CHUNK, n.size - off))
                    if not chunk:
                        break
                    f.write(chunk)
                    off += len(chunk)
            ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0 or i + 1 == len(files):
            cb(100 * (i + 1) / max(1, len(files)),
               '解压 %d/%d' % (i + 1, len(files)))
    return ok, fail


def repack_dmg(src: str, out_path: str, compressed: bool = True,
               cb=None) -> int:
    """把已有镜像重新封装（压缩/未压缩互转），保持内容不变。"""
    import tempfile
    cb = cb or (lambda *_: None)
    d = os.path.dirname(os.path.abspath(out_path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.omd_repack_', suffix='.img', dir=d)
    os.close(fd)
    try:
        expand_dmg(src, tmp, lambda p, m: cb(p * 0.5, m))
        return write_udif(out_path, tmp,
                          os.path.splitext(os.path.basename(out_path))[0],
                          compressed, lambda p, m: cb(50 + p * 0.5, m))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def verify_image(image_path: str, cb=None) -> dict:
    """计算镜像文件与逻辑磁盘的 SHA-256 / 大小，用于完整性校验。"""
    cb = cb or (lambda *_: None)
    out = {'path': image_path, 'file_size': os.path.getsize(image_path)}
    h = hashlib.sha256()
    with open(image_path, 'rb') as f:
        while True:
            b = f.read(4 << 20)
            if not b:
                break
            h.update(b)
    out['file_sha256'] = h.hexdigest()

    d = Disk(image_path)
    try:
        reader = d.reader
        total = d.logical_size
        pos, done = 0, 0
        lh = hashlib.sha256()
        while pos < total:
            b = reader.read(pos, min(8 << 20, total - pos))
            if not b:
                break
            lh.update(b)
            pos += len(b)
            done += len(b)
            cb(100 * done / max(1, total), '校验 %s' % _hum(done))
        out['logical_size'] = total
        out['logical_sha256'] = lh.hexdigest()
        out['volumes'] = [v['name'] for v in d.volumes]
        out['partitions'] = len(d.partitions)
        out['errors'] = list(d.errors)
        return out
    finally:
        d.close()


def _hum(n) -> str:
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.1f %s' % (n, u)
        n /= 1024.0


# ---------------------------------------------------------------- 镜像编辑

def _norm_path(p: str) -> str:
    return '/'.join(_safe_name(x) for x in (p or '').strip('/').split('/') if x)


def _local_entry(src: str, path: str) -> Entry:
    src = os.path.abspath(src)
    rel = _norm_path(path) or _safe_name(os.path.basename(src))
    return Entry(path=rel, is_dir=False, size=os.path.getsize(src), src=src)


def edit_image(src_image: str, out_image: str | None, operations: list,
               volume_name: str | None = None, cb=None,
               compressed: bool = True, extra_blocks: int = 0) -> int:
    """在已有镜像上执行一组编辑操作，产出新的合法 HFS+ DMG。

    operations 中的每一项为 dict：
      {'op':'delete',  'path':'dir/file.txt'}        删除文件或整个目录
      {'op':'rename',  'path':'dir/file.txt', 'name':'new.txt'}
      {'op':'mkdir',   'path':'dir/newfolder'}
      {'op':'add',     'src':'D:\\\\a.txt', 'path':'dir/a.txt'}
      {'op':'replace', 'path':'dir/a.txt', 'src':'D:\\\\a.txt'}

    out_image 为空或与 src_image 相同时覆盖原镜像（先写临时文件，关闭源
    句柄后原子替换）。返回输出文件大小。

    compressed / extra_blocks 用于「原地写不下」的兜底重建：传
    compressed=False 得到仍可编辑的未压缩 DMG，extra_blocks 预留空闲空间。
    """
    import shutil
    import tempfile

    src = os.path.abspath(src_image)
    if not os.path.isfile(src):
        raise WriteError('找不到镜像：%s' % src)
    dest = os.path.abspath(out_image) if out_image else src
    overwrite = dest == src
    tmpdir = os.path.dirname(dest) or '.'
    fd, tmp_out = tempfile.mkstemp(prefix='.omd_edit_', suffix='.dmg',
                                   dir=tmpdir)
    os.close(fd)
    try:
        with Disk(src) as disk:
            vol = disk.first_volume
            entries = entries_from_volume(vol, cb)
            name = volume_name or os.path.splitext(os.path.basename(dest))[0]
            by_path = {e.path: e for e in entries}
            for op in operations:
                kind = (op.get('op') or '').lower()
                path = _norm_path(op.get('path', ''))
                if kind == 'delete':
                    entries = [e for e in entries
                               if e.path != path and
                               not e.path.startswith(path + '/')]
                elif kind == 'rename':
                    new_name = _safe_name(op.get('name', ''))
                    if not new_name:
                        raise WriteError('重命名需要新名称')
                    parent = path.rsplit('/', 1)[0] if '/' in path else ''
                    new_path = (parent + '/' if parent else '') + new_name
                    for e in entries:
                        if e.path == path:
                            e.path = new_path
                        elif e.path.startswith(path + '/'):
                            e.path = new_path + e.path[len(path):]
                elif kind == 'mkdir':
                    if path and path not in {e.path for e in entries}:
                        entries.append(Entry(path=path, is_dir=True))
                elif kind in ('add', 'replace'):
                    local = op.get('src')
                    if not local or not os.path.isfile(local):
                        raise WriteError('找不到要加入的文件：%s' % local)
                    rel = _norm_path(op.get('path', '')) or \
                        _safe_name(os.path.basename(local))
                    e = _local_entry(local, rel)
                    old = by_path.get(rel)
                    if old is not None:
                        entries.remove(old)
                    entries.append(e)
                    by_path[rel] = e
                else:
                    raise WriteError('未知编辑操作：%s' % kind)
            # 去掉重复路径（后者优先），并保证父目录存在
            seen, uniq, parents = set(), [], set()
            for e in reversed(entries):
                if e.path in seen:
                    continue
                seen.add(e.path)
                uniq.append(e)
                parts = e.path.split('/')
                for i in range(1, len(parts)):
                    parents.add('/'.join(parts[:i]))
            entries = list(reversed(uniq))
            have = {e.path for e in entries}
            for p in sorted(parents, key=len):
                if p not in have:
                    entries.append(Entry(path=p, is_dir=True))
                    have.add(p)
            if cb:
                cb(60, '共 %d 个条目，正在生成卷…' % len(entries))
            create_dmg(tmp_out, entries, name, compressed, cb,
                       extra_blocks=extra_blocks)
        os.replace(tmp_out, dest)
        return os.path.getsize(dest)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass


def format_image(image_path: str, volume_name: str = 'Untitled',
                 size_mb: int = 16, cb=None) -> int:
    """把已有镜像（或新建文件）格式化为空白 HFS+ 卷。"""
    import tempfile
    src = os.path.abspath(image_path)
    d = os.path.dirname(src) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.omd_fmt_', suffix='.dmg', dir=d)
    os.close(fd)
    try:
        create_blank_dmg(tmp, volume_name, size_mb, cb)
        os.replace(tmp, src)
        return os.path.getsize(src)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def format_drive(index: int, volume_name: str = 'Macintosh HD',
                 size_mb: int = 16, cb=None) -> int:
    """把物理磁盘格式化为 HFS+ 卷（危险，沿用 shell 的安全约束）。"""
    import tempfile
    from . import shell
    if index == 0:
        raise WriteError('出于安全考虑，拒绝格式化系统盘 PhysicalDrive0')
    fd, tmp = tempfile.mkstemp(prefix='.omd_fdrive_', suffix='.dmg')
    os.close(fd)
    try:
        create_blank_dmg(tmp, volume_name, size_mb, cb)
        return shell.restore_to_drive(tmp, index, cb, True)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------ Split / Join
def _split_dir_name(image_path: str, out_dir: str | None) -> str:
    """绝不覆盖已有分片：目标目录有清单时自动换到带序号的新目录。"""
    base = os.path.basename(image_path)
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(image_path)),
                               base + '_split')
    chosen, k = out_dir, 1
    while os.path.exists(os.path.join(chosen, base + '.split.json')):
        k += 1
        chosen = '%s (%d)' % (out_dir, k)
    return chosen


def split_image(image_path: str, out_dir: str | None = None,
                chunk_mb: float = 650, cb=None) -> dict:
    """把镜像拆成固定大小的分片（对应 TransMac 的 Split）。

    生成 ``<原名>.001 / .002 / ...`` 和 ``<原名>.split.json`` 清单，
    清单记录每个分片与整个文件的 SHA-256，用 join_parts() 可无损还原。
    流式读写，不把整个镜像载入内存；从不覆盖已有分片。
    """
    import json
    cb = cb or (lambda *_: None)
    if not os.path.isfile(image_path):
        raise WriteError('找不到镜像文件：%s' % image_path)
    try:
        chunk = int(float(chunk_mb) * 1024 * 1024)
    except (TypeError, ValueError):
        raise WriteError('分片大小无效')
    if chunk <= 0:
        raise WriteError('分片大小必须大于 0 MB')
    base = os.path.basename(image_path)
    total = os.path.getsize(image_path)
    directory = _split_dir_name(image_path, out_dir)
    os.makedirs(directory, exist_ok=True)
    whole, parts = hashlib.sha256(), []
    idx, written, fh, part_hash, part_size, name = 1, 0, None, None, 0, ''
    try:
        with open(image_path, 'rb') as src:
            while True:
                want = min(CHUNK, chunk - part_size) if fh is not None else CHUNK
                buf = src.read(want)
                if not buf:
                    break
                if fh is None:
                    name = '%s.%03d' % (base, idx)
                    fh = open(os.path.join(directory, name), 'wb')
                    part_hash, part_size = hashlib.sha256(), 0
                fh.write(buf)
                whole.update(buf)
                part_hash.update(buf)
                part_size += len(buf)
                written += len(buf)
                cb(100 * written / max(1, total),
                   '拆分 %s / %s  第 %d 片' % (_hum(written), _hum(total), idx))
                if part_size >= chunk:
                    fh.close()
                    parts.append({'name': name, 'size': part_size,
                                  'sha256': part_hash.hexdigest()})
                    fh, part_size = None, 0
                    idx += 1
        if fh is not None:
            fh.close()
            parts.append({'name': name, 'size': part_size,
                          'sha256': part_hash.hexdigest()})
        if written != total:
            raise WriteError('读取镜像不完整：%d / %d 字节' % (written, total))
    except BaseException:
        # 用户取消 / 写入出错：删掉本次产生的分片，只删本函数建的空目录
        if fh is not None:
            fh.close()
        names = {p['name'] for p in parts}
        if name:
            names.add(name)
        for nm in names:
            try:
                os.remove(os.path.join(directory, nm))
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise
    finally:
        if fh is not None:
            fh.close()
    manifest = {'tool': 'OpenMacDisk', 'version': 1, 'source': base,
                'size': total, 'sha256': whole.hexdigest(),
                'chunk_bytes': chunk, 'parts': parts}
    mp = os.path.join(directory, base + '.split.json')
    with open(mp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest['directory'], manifest['manifest'] = directory, mp
    cb(100, '拆分完成：%d 个分片' % len(parts))
    return manifest


def manifest_path(path: str) -> str:
    """由清单路径或任一 .NNN 分片路径找到 .split.json。"""
    import re
    p = os.path.abspath(path)
    if p.lower().endswith('.split.json'):
        cand = p
    elif re.search(r'\.\d{3}$', p):
        cand = p.rsplit('.', 1)[0] + '.split.json'
    else:
        cand = p + '.split.json'
    if not os.path.isfile(cand):
        raise WriteError('找不到分片清单（.split.json）：%s' % cand)
    return cand


def read_manifest(path: str) -> dict:
    import json
    mp = manifest_path(path)
    try:
        with open(mp, encoding='utf-8') as f:
            m = json.load(f)
        if not m.get('parts') or 'sha256' not in m or 'size' not in m:
            raise ValueError('清单内容不完整')
    except Exception as e:
        raise WriteError('分片清单无法读取：%s' % e)
    m['directory'], m['manifest'] = os.path.dirname(mp), mp
    return m


def join_parts(manifest_or_part: str, out_path: str, cb=None) -> int:
    """按清单把分片合并回原镜像；逐片 + 整体 SHA-256 校验。"""
    cb = cb or (lambda *_: None)
    m = read_manifest(manifest_or_part)
    directory, parts = m['directory'], m['parts']
    out = os.path.abspath(out_path)
    for p in parts:
        if os.path.abspath(os.path.join(directory, p['name'])) == out:
            raise WriteError('输出路径不能覆盖分片文件：%s' % p['name'])
    checked = []
    for i, p in enumerate(parts):
        fp = os.path.join(directory, p['name'])
        if not os.path.isfile(fp):
            raise WriteError('缺少第 %d 个分片：%s' % (i + 1, p['name']))
        if os.path.getsize(fp) != p['size']:
            raise WriteError('分片大小不符：%s' % p['name'])
        h = hashlib.sha256()
        with open(fp, 'rb') as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                h.update(b)
        if h.hexdigest() != p['sha256']:
            raise WriteError('分片校验失败（可能被改动）：%s' % p['name'])
        checked.append(fp)
        cb(50 * (i + 1) / len(parts),
           '校验分片 %d/%d' % (i + 1, len(parts)))
    whole, total = hashlib.sha256(), 0
    with open(out, 'wb') as dst:
        for i, fp in enumerate(checked):
            with open(fp, 'rb') as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    dst.write(b)
                    whole.update(b)
                    total += len(b)
            cb(50 + 50 * (i + 1) / len(checked),
               '合并 %d/%d' % (i + 1, len(checked)))
    if total != m['size'] or whole.hexdigest() != m['sha256']:
        raise WriteError('合并结果校验失败：%d / %d 字节' % (total, m['size']))
    cb(100, '合并完成：%s' % _hum(total))
    return total
