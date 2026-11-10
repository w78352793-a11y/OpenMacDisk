# -*- coding: utf-8 -*-
"""未加密 APFS 容器/卷的只读解析（净室实现）。

依据 Apple《Apple File System Reference》及公开的 APFS 格式说明/头文件
（如 dissect.apfs 的结构定义、SANS FOR518 APFS Reference Sheet、
jtsylve.blog 的 APFS Internals 系列）独立编写，只使用 Python 标准库；
未复制任何商业软件或 GPL 项目的代码。所有结构偏移、Fletcher64 算法、
B-tree 节点布局均来自上述公开资料，并用本仓库 tests/test_apfs.py 中
自行构造的合成镜像逐字节验证。

支持范围（单容器、未加密、无快照、无克隆的最新 checkpoint）:
  * 4 KiB 或由 nx_block_size 指定的块大小；base 为容器/分区的字节偏移。
  * NXSB 容器超块 + checkpoint descriptor area（Fletcher64 校验、选最新 xid）。
  * 容器 / 卷对象映射（omap）B-tree：虚拟 OID -> 物理块地址。
  * APSB 卷超块、卷 omap、FS root B-tree（root/internal/leaf 节点、
    固定或可变长度 KV、root 节点末尾的 btree_info_t）。
  * j_file_info / j_inode（文件信息，含逻辑大小）、j_drec（目录项）、
    j_file_extent（普通 extent）、j_symlink（符号链接目标）记录。
  * 目录层级、UTF-8 文件名、空文件、跨多个 extent 的普通文件读取。
  * APFSVolume 提供与 HFSVolume 完全相同的对外接口；APFSContainer
    枚举一个容器内的多个卷。

明确不支持（一律抛 APFSError 并给出中文提示，绝不静默返回错误数据）:
  快照 / 快照卷、克隆与共享数据流（clone / dstream_id / sibling）、
  加密容器或加密卷（nx_keylocker、wrapped_crypto_state、OBJ_ENCRYPTED）、
  压缩（com.apple.decmpfs 等 xattr）、稀疏文件（extent 物理块为 0）、
  通用扩展属性 xattr、硬链接、非连续(树状) checkpoint 描述符区、
  签名系统卷(SSV)的 FILE_INFO 数据哈希记录、Fusion / 分区外物理地址映射。

关于净室合成记录 profile：
  FS 树的 key 一律以 j_key_t（小端 u64，高 4 位为记录类型、低 60 位为
  对象 ID）开头，记录类型号沿用 Apple 的 j_obj_types 枚举。本模块把承载
  文件元数据的记录称为 ``j_file_info``：既支持 Apple 的 ``j_inode_val_t``
  (type 3，大小取自 INO_EXT_TYPE_DSTREAM 扩展字段) 这一真实镜像路径，
  也支持合成镜像使用的紧凑 ``j_file_info_val`` (type 13，带 ``FINF``
  magic、显式 logical_size)；后者与 Apple 新版规范中同号的文件数据哈希
  记录用 magic 区分，未识别的 type 13 记录视为不支持的签名卷记录并报错。
  符号链接按 Apple 规范表现为名为 ``com.apple.fs.symlink`` 的 XATTR 记录。

物理地址换算：APFS 的 paddr 是相对容器起点的块号（与 nx_xp_desc_base、
nx_keylocker.pr_start_paddr 同一坐标系），绝对字节偏移为
``base + paddr * nx_block_size``；nx_keylocker 的 prange 仅用于加密检测。
"""
from __future__ import annotations

import struct

from .hfs import CatalogNode, ForkData, HFSError

# ---------------------------------------------------------------------------
# 常量：均来自 Apple APFS Reference 的公开定义
# ---------------------------------------------------------------------------

NXSB_MAGIC = b'NXSB'
APSB_MAGIC = b'APSB'

# obj_phys_t（32 字节）：o_cksum(8) o_oid(8) o_xid(8) o_type(4) o_subtype(4)
O_CKSUM = 0x00
O_OID = 0x08
O_XID = 0x10
O_TYPE = 0x18
O_SUBTYPE = 0x1C

OBJECT_TYPE_NX_SUPERBLOCK = 0x01
OBJECT_TYPE_BTREE = 0x02
OBJECT_TYPE_BTREE_NODE = 0x03
OBJECT_TYPE_OMAP = 0x0B
OBJECT_TYPE_CHECKPOINT_MAP = 0x0C
OBJECT_TYPE_FS = 0x0D
OBJECT_TYPE_FSTREE = 0x0E

# o_type 高位标志
OBJ_STORAGETYPE_MASK = 0xC0000000
OBJ_PHYSICAL = 0x40000000
OBJ_EPHEMERAL = 0x80000000
OBJ_ENCRYPTED = 0x10000000

# nx_superblock_t 关键字段偏移
NX_MAGIC = 0x20
NX_BLOCK_SIZE = 0x24
NX_BLOCK_COUNT = 0x28
NX_FEATURES = 0x30
NX_XP_DESC_BLOCKS = 0x68
NX_XP_DATA_BLOCKS = 0x6C
NX_XP_DESC_BASE = 0x70
NX_XP_DATA_BASE = 0x78
NX_XP_DESC_INDEX = 0x88
NX_XP_DESC_LEN = 0x8C
NX_OMAP_OID = 0xA0
NX_MAX_FILE_SYSTEMS = 0xB4
NX_FS_OID = 0xB8            # nx_fs_oid[100]，每项 8 字节
NX_MAX_FILE_SYSTEMS_LIMIT = 100
NX_FLAGS = 0x4F0            # nx_flags（NX_CRYPTO_SW 等位）
NX_KEYLOCKER = 0x510        # prange_t: pr_start_paddr(8) + pr_block_count(8)
NX_CRYPTO_SW = 0x00000004

# apfs_superblock_t 关键字段偏移
APFS_MAGIC = 0x20
APFS_META_CRYPTO_CPFLAGS = 0x64   # wrapped_crypto_state_t.cpflags
APFS_OMAP_OID = 0x80              # 卷 omap（物理 OID）
APFS_ROOT_TREE_OID = 0x88         # FS root B-tree（虚拟 OID）
APFS_VOLNAME_OFFSETS = (0x2C0, 0x230, 0x108)  # 不同资料给出的候选偏移
APFS_VOLNAME_LEN = 256

# omap_phys_t
OM_SNAP_COUNT = 0x24
OM_TREE_OID = 0x30

# btree_node_phys_t
BTN_FLAGS = 0x20
BTN_LEVEL = 0x22
BTN_NKEYS = 0x24
BTN_TABLE_SPACE = 0x28       # nloc_t: off(u16) + len(u16)
BTN_FREE_SPACE = 0x2C
BTN_DATA = 0x38              # 56
BTREE_INFO_SIZE = 40         # root 节点末尾的 btree_info_t

BTNODE_ROOT = 0x0001
BTNODE_LEAF = 0x0002
BTNODE_FIXED_KV_SIZE = 0x0004
BTNODE_NOHEADER = 0x0010

BTREE_PHYSICAL = 0x00000010
BTREE_KV_NONALIGNED = 0x00000040

# omap B-tree 的键/值（固定 16 字节）
OMAP_KEY_SIZE = 16           # ok_oid(8) + ok_xid(8)
OMAP_VAL_SIZE = 16           # ov_flags(4) + ov_size(4) + ov_paddr(8)
OMAP_VAL_DELETED = 0x00000001
CHILD_PTR_SIZE = 8           # 内部节点的值 = 子节点 OID / paddr

# j_obj_types（Apple 公开枚举）
APFS_TYPE_SNAP_METADATA = 1
APFS_TYPE_EXTENT = 2
APFS_TYPE_INODE = 3
APFS_TYPE_XATTR = 4
APFS_TYPE_SIBLING_LINK = 5
APFS_TYPE_DSTREAM_ID = 6
APFS_TYPE_CRYPTO_STATE = 7
APFS_TYPE_FILE_EXTENT = 8
APFS_TYPE_DIR_REC = 9
APFS_TYPE_DIR_STATS = 10
APFS_TYPE_SNAP_NAME = 11
APFS_TYPE_SIBLING_MAP = 12
APFS_TYPE_FILE_INFO = 13
APFS_TYPE_EXPANDED = 14

OBJ_ID_MASK = 0x0FFFFFFFFFFFFFFF
OBJ_TYPE_SHIFT = 60
J_FILE_EXTENT_LEN_MASK = 0x00FFFFFFFFFFFFFF
DREC_TYPE_MASK = 0x000F
J_DREC_LEN_MASK = 0x3FF      # name_len_and_hash 低 10 位为名字长度（含 NUL）
DT_DIR = 4
DT_REG = 8
DT_LNK = 10
ROOT_DIR_INO_NUM = 2
ROOT_DIR_PARENT = 1
INO_EXT_TYPE_DSTREAM = 8
SYMLINK_XATTR_NAME = b'com.apple.fs.symlink'

# 合成镜像紧凑 j_file_info_val 的 magic（'FINF'）
FILE_INFO_MAGIC = 0x46494E46
FILE_INFO_VERSION = 1
FILE_INFO_VAL_SIZE = 40

FLETCHER_MOD = 0xFFFFFFFF
MAX_BTREE_DEPTH = 64
MAX_CHECKPOINT_BLOCKS = 4096


class APFSError(HFSError):
    """APFS 解析错误（继承 HFSError，便于主程序统一捕获）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _u8(buf, off):
    return struct.unpack_from('<B', buf, off)[0]


def _u16(buf, off):
    return struct.unpack_from('<H', buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def _u64(buf, off):
    return struct.unpack_from('<Q', buf, off)[0]


def _fletcher64(block: bytes) -> int:
    """APFS Fletcher64：对整个块除前 8 字节校验和外的小端 u32 字求和。

    算法与 Apple Reference 一致：
        sum1 = Σw, sum2 = Σ(n-i+1)w (mod 0xFFFFFFFF)
        low  = MOD - (sum1 + sum2) mod MOD
        high = MOD - (sum1 + low)  mod MOD
    使得把 low/high 追加到数据后两个累加和都归零。
    """
    n = (len(block) - 8) // 4
    words = struct.unpack_from('<%dI' % n, block, 8)
    sum1 = sum2 = 0
    for w in words:
        sum1 = (sum1 + w) % FLETCHER_MOD
        sum2 = (sum2 + sum1) % FLETCHER_MOD
    low = FLETCHER_MOD - ((sum1 + sum2) % FLETCHER_MOD)
    high = FLETCHER_MOD - ((sum1 + low) % FLETCHER_MOD)
    return (high << 32) | low


def _cksum_ok(block: bytes) -> bool:
    return len(block) >= 16 and _u64(block, O_CKSUM) == _fletcher64(block)


def _align8(n):
    return (n + 7) & ~7


def _as_paddr(oid: int) -> int:
    """物理 / 临时 OID 即物理块地址；去掉高 4 位存储类型标记。"""
    return oid & 0x0FFFFFFFFFFFFFFF


def _decode_utf8(raw: bytes, what: str) -> str:
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise APFSError('%s 不是合法 UTF-8：%r（%s）' % (what, raw[:64], exc))


class _IO:
    """把 (paddr, 长度) 换算成 reader.read(base + paddr*bs, length)。"""

    def __init__(self, reader, base: int, block_size: int):
        self.reader = reader
        self.base = base
        self.block_size = block_size
        # 上限要用**逻辑盘大小**：UDIF 的 .size 是压缩后的文件长度，
        # 比逻辑盘小得多，会把合法的容器块误判为越界（如 GPT+APFS 的 DMG）。
        size = getattr(reader, 'logical_size', getattr(reader, 'size', 0))
        self.max_blocks = max(0, (size - base) // block_size) if size else 1 << 40

    def offset(self, paddr: int) -> int:
        return self.base + paddr * self.block_size

    def block(self, paddr: int, label: str = '对象', verify: bool = True) -> bytes:
        if paddr < 0 or paddr >= self.max_blocks:
            raise APFSError('%s 的物理块地址 %d 超出镜像范围（共 %d 块）'
                            % (label, paddr, self.max_blocks))
        data = self.reader.read(self.offset(paddr), self.block_size)
        if len(data) != self.block_size:
            raise APFSError('读取%s（块 %d）失败：镜像被截断' % (label, paddr))
        if verify and not _cksum_ok(data):
            raise APFSError('%s（块 %d）Fletcher64 校验失败'
                            % (label, paddr))
        return data


# ---------------------------------------------------------------------------
# B-tree 与对象映射
# ---------------------------------------------------------------------------

class _BTree:
    """遍历一棵 APFS B-tree 的全部叶记录。

    resolve_child(oid) 把内部节点里的 8 字节子节点标识转成物理块地址：
      * 物理树（omap）：标识本身就是 paddr；
      * 虚拟树（FS tree）：标识是虚拟 OID，需经卷 omap 查表。
    """

    def __init__(self, io: _IO, root_paddr: int, resolve_child,
                 key_size: int | None = None, val_size: int | None = None,
                 label: str = 'B-tree'):
        self.io = io
        self.root_paddr = root_paddr
        self.resolve_child = resolve_child
        self.key_size = key_size
        self.val_size = val_size
        self.label = label

    def _root_sizes(self):
        root = self.io.block(self.root_paddr, '%s 根节点' % self.label)
        if not (_u16(root, BTN_FLAGS) & BTNODE_ROOT):
            # 非 root 也能遍历，但没有 btree_info_t，尺寸由调用方给定
            return self.key_size, self.val_size
        info = self.io.block_size - BTREE_INFO_SIZE
        return _u32(root, info + 8), _u32(root, info + 12)

    def iter_records(self):
        key_size, val_size = self._root_sizes()
        if self.key_size is not None:
            key_size = self.key_size
        if self.val_size is not None:
            val_size = self.val_size

        stack = [(self.root_paddr, 0)]
        seen = set()
        while stack:
            paddr, depth = stack.pop()
            if depth > MAX_BTREE_DEPTH:
                raise APFSError('%s 层级过深（疑似循环引用）' % self.label)
            if paddr in seen:
                raise APFSError('%s 检测到节点循环引用（块 %d）'
                                % (self.label, paddr))
            seen.add(paddr)
            node = self.io.block(paddr, '%s 节点' % self.label)
            yield from self._node(node, paddr, depth, key_size, val_size, stack)

    def _node(self, node, paddr, depth, key_size, val_size, stack):
        flags = _u16(node, BTN_FLAGS)
        level = _u16(node, BTN_LEVEL)
        nkeys = _u32(node, BTN_NKEYS)
        tbl_off = _u16(node, BTN_TABLE_SPACE)
        tbl_len = _u16(node, BTN_TABLE_SPACE + 2)
        bs = self.io.block_size
        fixed = bool(flags & BTNODE_FIXED_KV_SIZE)
        entry_size = 4 if fixed else 8

        if _u32(node, O_TYPE) & OBJ_ENCRYPTED:
            raise APFSError('不支持加密的 %s 节点（块 %d）'
                            % (self.label, paddr))

        toc_start = BTN_DATA + tbl_off
        key_start = toc_start + tbl_len
        val_end = bs - (BTREE_INFO_SIZE if flags & BTNODE_ROOT else 0)
        if toc_start < BTN_DATA or key_start > bs or nkeys < 0:
            raise APFSError('%s 节点（块 %d）表头非法' % (self.label, paddr))
        if toc_start + nkeys * entry_size > min(key_start, bs):
            raise APFSError('%s 节点（块 %d）TOC 越界' % (self.label, paddr))

        for i in range(nkeys):
            pos = toc_start + i * entry_size
            if fixed:
                k_off = _u16(node, pos)
                v_off = _u16(node, pos + 2)
                k_len = key_size
                # 内部节点的值固定为 8 字节子节点指针
                v_len = CHILD_PTR_SIZE if level > 0 else val_size
            else:
                k_off = _u16(node, pos)
                k_len = _u16(node, pos + 2)
                v_off = _u16(node, pos + 4)
                v_len = _u16(node, pos + 6)

            if not k_len or not v_len:
                continue
            if v_off in (0xFFFF, 0xFFFE):
                continue  # ghost / empty-value
            k_start = key_start + k_off
            v_start = val_end - v_off
            if k_start < 0 or k_start + k_len > bs or v_start < 0 or v_start + v_len > bs:
                raise APFSError('%s 节点（块 %d）第 %d 条记录越界'
                                % (self.label, paddr, i))
            key = bytes(node[k_start:k_start + k_len])
            value = bytes(node[v_start:v_start + v_len])

            if level == 0:
                yield key, value
            else:
                if len(value) < CHILD_PTR_SIZE:
                    raise APFSError('%s 内部节点（块 %d）子节点指针过短'
                                    % (self.label, paddr))
                child_oid = _u64(value, 0)
                stack.append((self.resolve_child(child_oid), depth + 1))


class _Omap:
    """对象映射：虚拟 OID -> 物理块地址（取 xid <= 当前 xid 的最新项）。"""

    def __init__(self, io: _IO, tree_paddr: int, xid: int, label: str):
        self.io = io
        self.xid = xid
        self.label = label
        self.tree = _BTree(io, tree_paddr, _as_paddr,
                           key_size=OMAP_KEY_SIZE, val_size=OMAP_VAL_SIZE,
                           label='%s omap B-tree' % label)
        self._cache: dict[int, int] = {}

    def lookup(self, oid: int) -> int:
        if oid in self._cache:
            return self._cache[oid]
        best_xid = -1
        best_paddr = None
        for key, value in self.tree.iter_records():
            if len(key) < OMAP_KEY_SIZE or len(value) < OMAP_VAL_SIZE:
                continue
            ok_oid, ok_xid = struct.unpack_from('<QQ', key, 0)
            if ok_oid != oid or ok_xid > self.xid or ok_xid <= best_xid:
                continue
            flags, _size, paddr = struct.unpack_from('<IIQ', value, 0)
            if flags & OMAP_VAL_DELETED:
                continue
            if paddr <= 0 or paddr >= self.io.max_blocks:
                continue
            best_xid, best_paddr = ok_xid, paddr
        if best_paddr is None:
            raise APFSError('%s 中找不到 OID 0x%x 的物理地址（xid=%d）'
                            % (self.label, oid, self.xid))
        self._cache[oid] = best_paddr
        return best_paddr


# ---------------------------------------------------------------------------
# 容器解析（NXSB / checkpoint / 容器 omap / 卷列表）
# ---------------------------------------------------------------------------

class _Container:
    """一个 APFS 容器；APFSVolume / APFSContainer 共用，避免重复解析。"""

    def __init__(self, reader, base: int = 0):
        head = reader.read(base, 4096)
        if len(head) < 64 or head[NX_MAGIC:NX_MAGIC + 4] != NXSB_MAGIC:
            raise APFSError('未找到 APFS 容器超块 NXSB（签名 %r）'
                            % (head[NX_MAGIC:NX_MAGIC + 4] if len(head) >= NX_MAGIC + 4 else b''))
        block_size = _u32(head, NX_BLOCK_SIZE)
        if block_size < 512 or block_size > (1 << 20) or block_size % 8:
            raise APFSError('非法 nx_block_size=%d' % block_size)
        self.io = _IO(reader, base, block_size)
        self.block_size = block_size
        self.signature = 'NXSB'

        block0 = self.io.block(0, '容器超块(块 0)')
        if block0[NX_MAGIC:NX_MAGIC + 4] != NXSB_MAGIC:
            raise APFSError('容器超块 NXSB 签名不匹配')

        nxsb = self._latest_checkpoint(block0)
        self.block_count = _u64(nxsb, NX_BLOCK_COUNT)
        self.xid = _u64(nxsb, O_XID)
        self.features = _u64(nxsb, NX_FEATURES)
        self.nx_flags = _u64(nxsb, NX_FLAGS)
        kl_start, kl_count = struct.unpack_from('<QQ', nxsb, NX_KEYLOCKER)
        self.nx_keylocker_prange = (kl_start, kl_count)

        # ---- 明确拒绝：加密 ----
        if kl_count or (self.nx_flags & NX_CRYPTO_SW):
            raise APFSError('不支持加密 APFS 容器（nx_keylocker 非空或 NX_CRYPTO_SW，'
                            '需要 FileVault/软件加密解锁）')
        if _u32(nxsb, O_TYPE) & OBJ_ENCRYPTED:
            raise APFSError('不支持加密 APFS 容器（容器超块带 OBJ_ENCRYPTED 标志）')

        # ---- 容器 omap ----
        omap_paddr = _as_paddr(_u64(nxsb, NX_OMAP_OID))
        omap_obj = self.io.block(omap_paddr, '容器 omap 对象')
        self._check_encrypted(omap_obj, '容器 omap 对象')
        if _u32(omap_obj, OM_SNAP_COUNT):
            raise APFSError('不支持快照（容器 omap om_snap_count=%d）'
                            % _u32(omap_obj, OM_SNAP_COUNT))
        tree_paddr = _as_paddr(_u64(omap_obj, OM_TREE_OID))
        self.omap = _Omap(self.io, tree_paddr, self.xid, '容器')

        # ---- 卷列表（nx_fs_oid[] 为虚拟 OID）----
        max_fs = min(_u32(nxsb, NX_MAX_FILE_SYSTEMS) or NX_MAX_FILE_SYSTEMS_LIMIT,
                     NX_MAX_FILE_SYSTEMS_LIMIT)
        self.volumes = []       # [(virtual_oid, physical_paddr), ...]
        for i in range(max_fs):
            oid = _u64(nxsb, NX_FS_OID + 8 * i)
            if not oid:
                continue
            paddr = self.omap.lookup(oid)
            self.volumes.append((oid, paddr))
        if not self.volumes:
            raise APFSError('容器内没有可用卷（nx_fs_oid[] 全为空）')

    @staticmethod
    def _check_encrypted(block, label):
        if _u32(block, O_TYPE) & OBJ_ENCRYPTED:
            raise APFSError('不支持加密对象：%s' % label)

    def _latest_checkpoint(self, block0):
        """扫描 checkpoint descriptor area，取 Fletcher64 有效且 xid 最大的 NXSB。"""
        desc_raw = _u32(block0, NX_XP_DESC_BLOCKS)
        if desc_raw & 0x80000000:
            raise APFSError('不支持非连续(树状) checkpoint 描述符区')
        desc_blocks = desc_raw & 0x7FFFFFFF
        desc_base = _u64(block0, NX_XP_DESC_BASE)
        desc_index = _u32(block0, NX_XP_DESC_INDEX)
        desc_len = _u32(block0, NX_XP_DESC_LEN)
        if desc_index >= max(desc_blocks, 1) or desc_len < 1:
            raise APFSError('checkpoint 描述符区的 index/len 非法')

        candidates = []
        for i in range(min(desc_blocks, MAX_CHECKPOINT_BLOCKS)):
            paddr = desc_base + i
            if paddr >= self.io.max_blocks:
                break
            raw = self.io.block(paddr, 'checkpoint 描述符', verify=False)
            if raw[NX_MAGIC:NX_MAGIC + 4] != NXSB_MAGIC:
                continue
            if not _cksum_ok(raw):
                continue
            if (_u32(raw, O_TYPE) & 0x0000FFFF) != OBJECT_TYPE_NX_SUPERBLOCK:
                continue
            candidates.append((_u64(raw, O_XID), raw))
        # 块 0 本身也是一份 NXSB 拷贝，作为兜底候选（正常镜像描述符区应有更新的）
        if _cksum_ok(block0):
            candidates.append((_u64(block0, O_XID), block0))
        if not candidates:
            raise APFSError('checkpoint 描述符区没有 Fletcher64 校验有效的 NXSB')
        candidates.sort(key=lambda c: c[0])
        return candidates[-1][1]


# ---------------------------------------------------------------------------
# 卷解析
# ---------------------------------------------------------------------------

class APFSVolume:
    """一个未加密 APFS 卷。reader 只需 read(offset, length) 与 size。

    对外接口与 HFSVolume 完全一致：
      __init__(reader, base=0) / .root / .find(path) / .walk(node=None)
      / .read_file(node, max_bytes=None) / .summary() / .signature
      / .file_count / .folder_count
    """

    def __init__(self, reader, base: int = 0, *, _container: _Container | None = None,
                 _index: int = 0):
        self.reader = reader
        self.base = base
        self.container = _container if _container is not None else _Container(reader, base)
        self.block_size = self.container.block_size
        self.signature = 'APSB'

        if _index < 0 or _index >= len(self.container.volumes):
            raise APFSError('卷序号 %d 越界（容器共 %d 个卷）'
                            % (_index, len(self.container.volumes)))
        self.volume_oid, self.volume_paddr = self.container.volumes[_index]
        self.volume_index = _index

        apsb = self.container.io.block(self.volume_paddr, '卷超块 APSB')
        if apsb[APFS_MAGIC:APFS_MAGIC + 4] != APSB_MAGIC:
            raise APFSError('卷超块 APSB 签名不匹配（得到 %r）'
                            % apsb[APFS_MAGIC:APFS_MAGIC + 4])
        self.container._check_encrypted(apsb, '卷超块 APSB')
        self.apsb = apsb
        self.cpflags = _u32(apsb, APFS_META_CRYPTO_CPFLAGS)
        if self.cpflags:
            raise APFSError('不支持加密卷（wrapped_crypto_state.cpflags=0x%x，'
                            '需要卷加密密钥）' % self.cpflags)
        # 快照由 omap.om_snap_count 与 FS 树 type 1/11 记录可靠判定；
        # apfs_num_snapshots 的偏移在公开资料中不一致，不用它做拒绝条件，
        # 避免在正常卷上误报。
        self.volume_name = self._read_volname(apsb)
        self.role = self._read_role(apsb)
        self.file_count = 0
        self.folder_count = 0

        # 卷 omap + FS root B-tree
        vol_omap_paddr, root_tree_oid = self._volume_pointers(apsb)
        vol_omap_obj = self.container.io.block(vol_omap_paddr, '卷 omap 对象')
        self.container._check_encrypted(vol_omap_obj, '卷 omap 对象')
        if _u32(vol_omap_obj, OM_SNAP_COUNT):
            raise APFSError('不支持快照（卷 omap om_snap_count=%d）'
                            % _u32(vol_omap_obj, OM_SNAP_COUNT))
        vol_tree_paddr = _as_paddr(_u64(vol_omap_obj, OM_TREE_OID))
        self.omap = _Omap(self.container.io, vol_tree_paddr,
                          self.container.xid, '卷')
        self.fs_root_paddr = self.omap.lookup(root_tree_oid)

        self.fs_tree = _BTree(self.container.io, self.fs_root_paddr,
                              self.omap.lookup, label='FS root')
        self.nodes: dict[int, CatalogNode] = {}
        self._by_parent: dict[int, list] = {}
        self._extents: dict[int, list] = {}       # stream_oid -> [(logical, length, paddr)]
        self._sizes: dict[int, int] = {}
        self._private_ids: dict[int, int] = {}
        self._symlink_targets: dict[int, bytes] = {}
        self._skipped_xattrs: set = set()
        self.root = None
        self._load_fs_records()

    # ---------- APSB 辅助 ----------

    def _volume_pointers(self, apsb):
        """apfs_omap_oid / apfs_root_tree_oid 的偏移在公开资料里有 0x80 与
        0x84 两种说法；以 omap 对象的校验和为准择优，避免读错结构。"""
        errors = []
        for off in (APFS_OMAP_OID, 0x84):
            omap_paddr = _as_paddr(_u64(apsb, off))
            root_oid = _u64(apsb, off + 8)
            if not omap_paddr or not root_oid:
                continue
            if omap_paddr >= self.container.io.max_blocks:
                continue
            obj = self.container.io.block(omap_paddr, '卷 omap 对象', verify=False)
            if _cksum_ok(obj) and (_u32(obj, O_TYPE) & 0x0000FFFF) == OBJECT_TYPE_OMAP:
                return omap_paddr, root_oid
            errors.append('0x%x' % off)
        raise APFSError('无法定位卷 omap / FS root B-tree（候选偏移 %s）'
                        % ('/'.join(errors) or '无'))

    def _read_volname(self, apsb):
        for off in APFS_VOLNAME_OFFSETS:
            if off + APFS_VOLNAME_LEN > len(apsb):
                continue
            raw = apsb[off:off + APFS_VOLNAME_LEN].split(b'\x00', 1)[0]
            if not raw:
                continue
            try:
                name = raw.decode('utf-8')
            except UnicodeDecodeError:
                continue
            if name and all(ch.isprintable() for ch in name):
                return name
        return 'APFS'

    def _read_role(self, apsb):
        for off in (0x334, 0x3C4, 0x330):
            if off + 2 <= len(apsb):
                role = _u16(apsb, off)
                if role in (0x0000, 0x0040, 0x0100, 0x0200, 0x0400, 0x0800):
                    return role
        return 0

    # ---------- FS 记录 ----------

    def _load_fs_records(self):
        for key, value in self.fs_tree.iter_records():
            if len(key) < 8:
                raise APFSError('FS 记录 key 短于 8 字节（无法解析 j_key_t）')
            jkey = _u64(key, 0)
            oid = jkey & OBJ_ID_MASK
            rtype = jkey >> OBJ_TYPE_SHIFT
            self._parse_record(oid, rtype, key, value)

        # 根目录：cnid=2，没有父目录项，需要显式构造
        root = self.nodes.get(ROOT_DIR_INO_NUM)
        if root is None:
            root = CatalogNode(ROOT_DIR_INO_NUM, ROOT_DIR_PARENT, '', 'folder')
            self.nodes[ROOT_DIR_INO_NUM] = root
        root.kind = 'folder'
        self.root = root

        for node in self.nodes.values():
            self._by_parent.setdefault(node.parent, []).append(node)
        self.root.path = ''
        self._assign_paths(self.root)
        self.file_count = sum(1 for n in self.nodes.values() if n.kind == 'file')
        self.folder_count = sum(1 for n in self.nodes.values()
                                if n.kind == 'folder' and n is not self.root)

    def _parse_record(self, oid, rtype, key, value):
        if rtype == APFS_TYPE_DIR_REC:
            self._parse_drec(oid, key, value)
        elif rtype == APFS_TYPE_INODE:
            self._parse_inode(oid, value)
        elif rtype == APFS_TYPE_FILE_INFO:
            self._parse_file_info(oid, value)
        elif rtype == APFS_TYPE_FILE_EXTENT:
            self._parse_file_extent(oid, key, value)
        elif rtype == APFS_TYPE_XATTR:
            self._parse_xattr(oid, key, value)
        elif rtype == APFS_TYPE_DIR_STATS or rtype == APFS_TYPE_EXTENT:
            return  # 目录统计 / 物理 extent 记录不影响目录树与文件数据
        elif rtype in (APFS_TYPE_SNAP_METADATA, APFS_TYPE_SNAP_NAME):
            raise APFSError('不支持快照（FS 树含 type=%d 快照记录）' % rtype)
        elif rtype == APFS_TYPE_DSTREAM_ID:
            # refcnt==1 是普通文件的数据流引用计数；>1 才表示共享/克隆数据流
            refcnt = _u32(value, 0) if len(value) >= 4 else 0
            if refcnt > 1:
                raise APFSError('不支持克隆 / 共享数据流'
                                '（DSTREAM_ID refcnt=%d）' % refcnt)
            return
        elif rtype in (APFS_TYPE_SIBLING_LINK, APFS_TYPE_SIBLING_MAP):
            raise APFSError('不支持克隆 / 硬链接（FS 树含 type=%d 记录）' % rtype)
        elif rtype == APFS_TYPE_CRYPTO_STATE:
            raise APFSError('不支持加密文件（FS 树含 CRYPTO_STATE 记录）')
        elif rtype == APFS_TYPE_EXPANDED:
            raise APFSError('不支持扩展记录（APFS_TYPE_EXPANDED / 克隆映射等）')
        else:
            raise APFSError('不支持的 FS 记录类型 %d（对象 0x%x）'
                            % (rtype, oid))

    def _parse_drec(self, parent_oid, key, value):
        # j_drec_hashed_key_t：j_key_t(8B) + name_len_and_hash(u32,4B) + name。
        # 名字从 offset 12 开始；name_len_and_hash 低 10 位是名字长度（含结尾
        # NUL），高 22 位是 hash。合成测试镜像曾误用「u16 长度 + offset 10」，
        # 真实 APFS 卷上会把前 2 个 hash 字节当成名字开头，导致 UTF-8 解码失败。
        if len(key) < 12 or len(value) < 18:
            raise APFSError('j_drec 记录截断')
        name_len = _u32(key, 8) & J_DREC_LEN_MASK
        raw = bytes(key[12:12 + name_len]).split(b'\x00', 1)[0]
        name = _decode_utf8(raw, '目录项名称')
        if not name or name in ('.', '..'):
            return
        file_id = _u64(value, 0)
        flags = _u16(value, 16)
        dtype = flags & DREC_TYPE_MASK
        kind = 'folder' if dtype == DT_DIR else 'file'
        node = self.nodes.get(file_id)
        if node is None:
            node = CatalogNode(file_id, parent_oid, name, kind)
            self.nodes[file_id] = node
        else:
            node.parent = parent_oid
            node.name = name
            node.kind = kind

    def _parse_inode(self, oid, value):
        """Apple j_inode_val_t：大小来自 INO_EXT_TYPE_DSTREAM 扩展字段。"""
        if len(value) < 92:
            raise APFSError('j_inode_val 记录截断')
        private_id = _u64(value, 8)
        self._private_ids[oid] = private_id
        size = self._dstream_size(value[92:])
        self._sizes[oid] = size

    def _dstream_size(self, blob):
        """解析 xf_blob_t / x_field_t，取 INO_EXT_TYPE_DSTREAM 字段里的逻辑大小。

        真实 APFS 卷（GPT+APFS DMG 实测）布局：
          * xfield 数据区紧跟字段数组（ae = 4 + 4*num_exts），
            **每个字段占用的长度按 8 字节对齐**（= align8(x_size)）；
          * DSTREAM 字段数据 40 字节 = {size, alloced_size, default_crypto_id,
            total_bytes_written, total_bytes_read}，**逻辑大小在 offset 0**，
            alloced_size（块大小整数倍）在 offset 8。
        之前误按「字段紧密排列 + size 在 offset 8」解析，真实镜像上全部错位。
        """
        if len(blob) < 4:
            return 0
        num_exts = _u16(blob, 0)
        array_end = 4 + 4 * num_exts
        if array_end > len(blob):
            return 0
        pos = array_end
        for i in range(num_exts):
            x_type = _u8(blob, 4 + 4 * i)
            x_size = _u16(blob, 4 + 4 * i + 2)
            if x_size <= 0 or pos + x_size > len(blob):
                break
            data = blob[pos:pos + x_size]
            if x_type == INO_EXT_TYPE_DSTREAM and len(data) >= 8:
                size = _u64(data, 0)     # DSTREAM 数据第 0 个 u64 即逻辑大小
                if size <= (1 << 50):
                    return size
            pos += _align8(x_size)       # 下一字段按 8 字节对齐
            if pos > len(blob):
                break
        return 0

    def _parse_file_info(self, oid, value):
        """合成镜像的紧凑 j_file_info_val；带 FINF magic 与显式 logical_size。

        Apple 新版规范把 type 13 用于签名系统卷的文件数据哈希，本模块用
        magic 严格区分：不带 magic 的 type 13 一律视为不支持的记录。"""
        if (len(value) < FILE_INFO_VAL_SIZE or
                _u32(value, 0) != FILE_INFO_MAGIC or
                _u32(value, 4) != FILE_INFO_VERSION):
            raise APFSError('不支持的 j_file_info(type 13) 记录：'
                            '疑似签名系统卷(SSV)文件数据哈希或未知变体')
        size = _u64(value, 8)
        stream_id = _u64(value, 24)
        self._sizes[oid] = size
        self._private_ids.setdefault(oid, stream_id)

    def _parse_file_extent(self, oid, key, value):
        if len(key) < 16 or len(value) < 24:
            raise APFSError('j_file_extent 记录截断')
        logical = _u64(key, 8)
        length = _u64(value, 0) & J_FILE_EXTENT_LEN_MASK
        phys = _u64(value, 8)
        if phys == 0:
            raise APFSError('不支持稀疏文件（对象 0x%x 的 extent 物理块地址为 0）' % oid)
        if length == 0 or length % self.block_size:
            raise APFSError('对象 0x%x 的 extent 长度 %d 不是块大小的整数倍'
                            % (oid, length))
        if phys >= self.container.io.max_blocks:
            raise APFSError('对象 0x%x 的 extent 物理块 %d 超出镜像范围'
                            % (oid, phys))
        self._extents.setdefault(oid, []).append((logical, length, phys))

    def _parse_xattr(self, oid, key, value):
        if len(key) < 10:
            raise APFSError('j_xattr 记录截断')
        name_len = _u16(key, 8)
        xname = bytes(key[10:10 + name_len]).split(b'\x00', 1)[0]
        if xname == SYMLINK_XATTR_NAME:
            if len(value) < 4:
                raise APFSError('符号链接 xattr 值截断')
            xdata_len = _u16(value, 2)
            self._symlink_targets[oid] = bytes(value[4:4 + xdata_len])
            return
        # 其它 xattr（com.apple.quarantine / decmpfs / 资源叉等）只是元数据，
        # 不影响目录树与文件数据 extent；列出文件时跳过即可（与 TransMac 一致）。
        # 真正读取被压缩文件的内容时，再由 read_file 单独报清晰错误，绝不在
        # 这里静默给出错误数据。
        self._skipped_xattrs.add((oid, bytes(xname)))

    def _assign_paths(self, node: CatalogNode):
        kids = sorted(self._by_parent.get(node.cnid, []), key=lambda x: x.name)
        node.children = kids
        for k in kids:
            k.path = (node.path + '/' + k.name).lstrip('/')
            # 节点属性在目录项/文件信息都解析完之后再补齐
            k.size = self._sizes.get(k.cnid, len(self._symlink_targets.get(k.cnid, b'')))
            k.data_fork = self._make_fork(k)
            if k.kind == 'folder':
                self._assign_paths(k)

    def _make_fork(self, node: CatalogNode):
        """复用 hfs.ForkData：APFS extent 的 (物理块, 块数) 与 HFS 语义一致。"""
        if node.kind != 'file':
            return None
        stream = self._private_ids.get(node.cnid, node.cnid)
        exts = sorted(self._extents.get(stream, []), key=lambda e: e[0])
        if not exts:
            return ForkData(node.size, self.block_size, 0, [])
        pairs = [(paddr, length // self.block_size) for _l, length, paddr in exts]
        blocks = sum(c for _s, c in pairs)
        return ForkData(node.size, self.block_size, blocks, pairs)

    # ---------- 对外 API（与 HFSVolume 一致）----------

    def find(self, path: str) -> CatalogNode | None:
        path = (path or '').strip('/')
        if not path:
            return self.root
        node = self.root
        for part in path.split('/'):
            node = next((c for c in node.children if c.name == part), None)
            if node is None:
                return None
        return node

    def walk(self, node=None):
        node = node or self.root
        yield node
        for c in node.children:
            yield from self.walk(c)

    def read_file(self, node: CatalogNode, max_bytes: int | None = None) -> bytes:
        if node is None or node.kind != 'file':
            raise APFSError('不是文件节点')
        if node.cnid in self._symlink_targets:
            data = self._symlink_targets[node.cnid]
            return data if max_bytes is None else data[:max_bytes]
        want = node.size if max_bytes is None else min(max_bytes, node.size)
        if want <= 0:
            return b''
        stream = self._private_ids.get(node.cnid, node.cnid)
        exts = sorted(self._extents.get(stream, []), key=lambda e: e[0])
        if not exts:
            raise APFSError('文件 /%s 没有数据 extent（可能为空文件但未声明大小）'
                            % node.path)
        total = sum(length for _logical, length, _phys in exts)
        if want > total:
            raise APFSError('文件 /%s 声明大小 %d 超过 extent 总容量 %d'
                            % (node.path, want, total))
        if want > (1 << 30):
            raise APFSError('文件 /%s 过大（%d 字节），请用 max_bytes 限制读取量'
                            % (node.path, want))
        out = bytearray(want)
        covered = 0
        for logical, length, phys in exts:
            if logical > covered:
                raise APFSError('不支持稀疏文件 / 文件存在空洞：/%s' % node.path)
            lo = max(logical, 0)
            hi = min(want, logical + length)
            if hi <= lo:
                covered = max(covered, logical + length)
                continue
            n = hi - lo
            src = self.container.io.offset(phys) + (lo - logical)
            data = self.reader.read(src, n)
            if len(data) != n:
                raise APFSError('读取文件 /%s 数据失败：镜像被截断' % node.path)
            out[lo:hi] = data
            covered = max(covered, logical + length)
        if covered < want:
            raise APFSError('文件 /%s 的 extent 覆盖不足（声明 %d 字节，实际 %d）'
                            % (node.path, want, covered))
        return bytes(out)

    def summary(self) -> dict:
        return {
            'signature': self.signature,
            'version': 1,
            'block_size': self.block_size,
            'total_blocks': self.container.block_count,
            'free_blocks': 0,
            'volume_bytes': self.container.block_count * self.block_size,
            'files': self.file_count,
            'folders': self.folder_count,
            'catalog_bytes': 0,
            'node_size': self.block_size,
            'indexed_nodes': len(self.nodes),
            'root_children': len(self.root.children) if self.root else 0,
            # APFS 专有字段
            'fs': 'APFS',
            'volume_name': self.volume_name,
            'volume_index': self.volume_index,
            'volume_oid': self.volume_oid,
            'role': self.role,
            'checkpoint_xid': self.container.xid,
            'fs_root_block': self.fs_root_paddr,
            'nx_keylocker_prange': self.container.nx_keylocker_prange,
        }


class APFSContainer:
    """一个 APFS 容器；.volumes 为 APFSVolume 列表。"""

    def __init__(self, reader, base: int = 0):
        self.reader = reader
        self.base = base
        self.container = _Container(reader, base)
        self.signature = 'NXSB'
        self.block_size = self.container.block_size
        self.block_count = self.container.block_count
        self.xid = self.container.xid
        self.volumes = [
            APFSVolume(reader, base, _container=self.container, _index=i)
            for i in range(len(self.container.volumes))
        ]

    def summary(self) -> dict:
        return {
            'signature': self.signature,
            'fs': 'APFS',
            'block_size': self.block_size,
            'block_count': self.block_count,
            'container_bytes': self.block_count * self.block_size,
            'checkpoint_xid': self.xid,
            'volume_count': len(self.volumes),
            'nx_keylocker_prange': self.container.nx_keylocker_prange,
            'volumes': [v.summary() for v in self.volumes],
        }
