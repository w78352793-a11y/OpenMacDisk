# -*- coding: utf-8 -*-
"""HFS / HFS+ 只读解析：卷头、Catalog B-tree、文件 fork 读取。

依据 Apple TN1150 公开格式说明独立实现，未使用任何第三方代码。
支持 HFS+ / HFSX（大小写敏感）与标准 HFS+ 日志卷的只读访问；
APFS 与加密卷不在本模块范围。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

SECTOR = 512
FORK = struct.Struct('>QII')  # logicalSize, clumpSize, totalBlocks
EXTENT = struct.Struct('>II')
NODE_DESC = struct.Struct('>IIBBHH')
CAT_KEY_MIN = struct.Struct('>IH')  # parentID + name length

KIND_HEADER = 1
KIND_INDEX = 0
KIND_LEAF = 0xFF
KIND_FREE = 0xFE

REC_FOLDER = 1
REC_FILE = 2
REC_FOLDER_THREAD = 3
REC_FILE_THREAD = 4

ROOT_CNID = 2


class HFSError(Exception):
    pass


@dataclass
class ForkData:
    size: int
    clump: int
    blocks: int
    extents: list

    @classmethod
    def parse(cls, buf, off=0):
        size, clump, blocks = FORK.unpack_from(buf, off)
        exts = []
        for i in range(8):
            s, c = EXTENT.unpack_from(buf, off + 16 + i * 8)
            if c:
                exts.append((s, c))
        return cls(size, clump, blocks, exts)


@dataclass
class CatalogNode:
    cnid: int
    parent: int
    name: str
    kind: str                       # 'folder' | 'file'
    size: int = 0
    resource_size: int = 0
    data_fork: ForkData | None = None
    resource_fork: ForkData | None = None
    dates: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    path: str = ''

    def to_dict(self):
        return {
            'cnid': self.cnid, 'name': self.name, 'kind': self.kind,
            'size': self.size, 'path': self.path,
            'children': len(self.children),
        }


def _decode_name(raw: bytes) -> str:
    return raw.decode('utf-16-be', 'replace')


def _parse_key(buf: bytes, pos: int):
    """返回 (parentID, name, next_pos)；HFS+ catalog key。"""
    if pos + 6 > len(buf):
        raise HFSError('catalog key 截断')
    klen = struct.unpack_from('>H', buf, pos)[0]
    parent = struct.unpack_from('>I', buf, pos + 2)[0]
    nlen = struct.unpack_from('>H', buf, pos + 6)[0]
    name = _decode_name(buf[pos + 8:pos + 8 + nlen * 2])
    # 记录数据按 2 字节对齐
    end = pos + 2 + klen
    if end % 2:
        end += 1
    return parent, name, end


class HFSVolume:
    """一个 HFS+ 卷。reader 为逻辑磁盘对象，base 为卷起始字节偏移。"""

    # 卷头内 5 个 fork 的偏移；不同实现存在 0x6C/0x70 两种对齐，运行时择优
    FORK_OFFSETS = (0x6C, 0x70)

    def __init__(self, reader, base: int = 0):
        self.reader = reader
        self.base = base
        hdr = reader.read(base + 1024, 1024)
        if len(hdr) < 512 or hdr[:2] not in (b'H+', b'HX'):
            raise HFSError('未找到 HFS+/HFSX 卷签名（%r）' % hdr[:2])
        self.signature = hdr[:2].decode('latin1')
        self.version = struct.unpack_from('>H', hdr, 2)[0]
        self.attributes = struct.unpack_from('>I', hdr, 4)[0]
        self.file_count = struct.unpack_from('>I', hdr, 0x20)[0]
        self.folder_count = struct.unpack_from('>I', hdr, 0x24)[0]
        self.block_size = struct.unpack_from('>I', hdr, 0x28)[0]
        self.total_blocks = struct.unpack_from('>I', hdr, 0x2C)[0]
        self.free_blocks = struct.unpack_from('>I', hdr, 0x30)[0]
        if self.block_size < 512:
            raise HFSError('非法 blockSize %d' % self.block_size)
        forks = None
        for off in self.FORK_OFFSETS:
            cand = [ForkData.parse(hdr, off + i * 80) for i in range(5)]
            cat = cand[2]
            if 0 < cat.size < (self.total_blocks * self.block_size) and cat.extents:
                forks = cand
                self.fork_offset = off
                break
        if forks is None:
            # 兜底：扫描 0x60..0x1B0 找合法的 catalog fork
            for off in range(0x60, 0x1B0, 4):
                c = ForkData.parse(hdr, off + 160) if off + 240 <= len(hdr) else None
                if c and 0 < c.size < self.total_blocks * self.block_size and c.extents:
                    forks = [ForkData.parse(hdr, off + i * 80) for i in range(5)]
                    self.fork_offset = off
                    break
        if forks is None:
            raise HFSError('无法定位 Catalog File fork')
        (self.allocation_file, self.extents_file, self.catalog_file,
         self.attributes_file, self.startup_file) = forks
        self.nodes: dict[int, CatalogNode] = {}
        self._by_parent: dict[int, list] = {}
        self.root = None
        self._load_catalog()

    # ---------- fork 读取 ----------

    def read_fork(self, fork: ForkData, max_bytes: int | None = None,
                  cnid: int | None = None, fork_kind: int = 0) -> bytes:
        want = fork.size if max_bytes is None else min(max_bytes, fork.size)
        if want <= 0:
            return b''
        exts = fork.extents
        # 内嵌 8 段不够覆盖全部块时，查 Extents Overflow B-tree 补齐碎片文件
        if cnid is not None and fork.blocks > sum(c for _s, c in exts):
            try:
                from .hfs_extents import all_extents
                exts = all_extents(self, fork, cnid, fork_kind)
            except Exception:
                pass
        out = bytearray()
        for start, count in exts:
            if len(out) >= want:
                break
            chunk = self.reader.read(self.base + start * self.block_size,
                                     count * self.block_size)
            out.extend(chunk)
        return bytes(out[:want])

    # ---------- B-tree ----------

    def _node(self, index: int) -> bytes:
        return self.read_catalog_node(index)

    def read_catalog_node(self, index: int) -> bytes:
        data = getattr(self, '_cat', None)
        if data is None:
            data = self.read_fork(self.catalog_file)
        return data[index * self.node_size:(index + 1) * self.node_size]

    @property
    def node_size(self):
        return getattr(self, '_node_size', 4096)

    @node_size.setter
    def node_size(self, v):
        self._node_size = v

    def _load_catalog(self):
        cat = self.read_fork(self.catalog_file)
        # 常见节点大小 4096/8192/16384；由 header node 的 BTHeaderRec 决定
        node_size = 0
        for guess in (4096, 8192, 16384, 2048):
            if len(cat) < guess:
                continue
            desc = NODE_DESC.unpack_from(cat, 0)
            if desc[2] == KIND_HEADER and desc[4] >= 3:
                n = struct.unpack_from('>H', cat, 32)[0]
                if 512 <= n <= 32768 and len(cat) >= n:
                    node_size = n
                    break
        if not node_size:
            raise HFSError('无法判定 catalog B-tree 节点大小')
        self.node_size = node_size
        self._cat = cat
        self.total_nodes = len(cat) // node_size
        # 遍历所有叶节点
        for idx in range(self.total_nodes):
            node = cat[idx * node_size:(idx + 1) * node_size]
            if len(node) < 14:
                break
            desc = NODE_DESC.unpack_from(node, 0)
            if desc[2] != KIND_LEAF:
                continue
            nrec = desc[4]
            if nrec > (node_size - 14) // 6:
                continue
            off_start = node_size - 2 * (nrec + 1)
            if off_start < 14:
                continue
            offsets = struct.unpack_from('>%dH' % (nrec + 1), node, off_start)
            if len(offsets) > 1 and offsets[0] > offsets[-1]:
                offsets = tuple(reversed(offsets))
            for i in range(nrec):
                pos = offsets[i]
                if not (14 <= pos < node_size):
                    continue
                try:
                    self._parse_record(node, pos)
                except (struct.error, HFSError, UnicodeError):
                    continue
        self.root = self.nodes.get(ROOT_CNID)
        if self.root is None:
            # 某些镜像没有根目录记录，构造一个虚拟根
            self.root = CatalogNode(ROOT_CNID, 1, '', 'folder')
            self.nodes[ROOT_CNID] = self.root
        for n in self.nodes.values():
            self._by_parent.setdefault(n.parent, []).append(n)
        self.root.path = ''
        self._assign_paths(self.root)

    def _assign_paths(self, node: CatalogNode):
        kids = sorted(self._by_parent.get(node.cnid, []), key=lambda x: x.name)
        node.children = kids
        for k in kids:
            k.path = (node.path + '/' + k.name).lstrip('/')
            if k.kind == 'folder':
                self._assign_paths(k)

    def _parse_record(self, node: bytes, pos: int):
        parent, name, dpos = _parse_key(node, pos)
        if dpos + 2 > len(node):
            return
        rtype = struct.unpack_from('>h', node, dpos)[0]
        if rtype in (REC_FOLDER_THREAD, REC_FILE_THREAD):
            return  # thread 记录仅用于名称规范化，目录树由 key 构建
        if rtype == REC_FOLDER:
            cnid = struct.unpack_from('>I', node, dpos + 8)[0]
            self.nodes[cnid] = CatalogNode(cnid, parent, name, 'folder')
        elif rtype == REC_FILE:
            cnid = struct.unpack_from('>I', node, dpos + 8)[0]
            # HFSPlusCatalogFile 前缀 88 字节：
            # recordType..backupDate(32) + BSDInfo(16) + FndrExtendedFileInfo(32)
            # + textEncoding(4) + reserved2(4)。两个 fork 各 80 字节。
            data_fork = ForkData.parse(node, dpos + 88) if dpos + 168 <= len(node) else None
            rsrc_fork = ForkData.parse(node, dpos + 168) if dpos + 248 <= len(node) else None
            if data_fork is None:
                return
            self.nodes[cnid] = CatalogNode(
                cnid, parent, name, 'file',
                size=data_fork.size,
                resource_size=rsrc_fork.size if rsrc_fork else 0,
                data_fork=data_fork, resource_fork=rsrc_fork)

    # ---------- 对外 API ----------

    def find(self, path: str) -> CatalogNode | None:
        path = path.strip('/')
        if not path:
            return self.root
        node = self.root
        for part in path.split('/'):
            node = next((c for c in node.children if c.name == part), None)
            if node is None:
                return None
        return node

    def read_file(self, node: CatalogNode, max_bytes: int | None = None) -> bytes:
        if node.kind != 'file' or node.data_fork is None:
            raise HFSError('不是文件节点')
        return self.read_fork(node.data_fork, max_bytes, cnid=node.cnid)

    def read_resource_fork(self, node: CatalogNode,
                           max_bytes: int | None = None) -> bytes:
        if node.resource_fork is None:
            raise HFSError('该文件没有资源叉')
        return self.read_fork(node.resource_fork, max_bytes,
                              cnid=node.cnid, fork_kind=0xFF)

    def walk(self, node=None):
        node = node or self.root
        yield node
        for c in node.children:
            yield from self.walk(c)

    def summary(self) -> dict:
        return {
            'signature': self.signature, 'version': self.version,
            'block_size': self.block_size,
            'total_blocks': self.total_blocks, 'free_blocks': self.free_blocks,
            'volume_bytes': self.total_blocks * self.block_size,
            'files': self.file_count, 'folders': self.folder_count,
            'catalog_bytes': self.catalog_file.size,
            'node_size': self.node_size, 'indexed_nodes': len(self.nodes),
            'root_children': len(self.root.children) if self.root else 0,
        }
