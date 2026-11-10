# -*- coding: utf-8 -*-
"""合成 APFS 镜像 + 只读解析器单元测试（不依赖真实 DMG）。

测试自建一份最小但结构完整的未加密 APFS 容器镜像：4 KiB 块、NXSB、
checkpoint 描述符、容器/卷 omap_phys 与 omap B-tree、APSB、含
header(btree_info_t)/internal/leaf 节点的 FS B-tree、j_drec / j_file_info /
j_inode / j_file_extent / j_symlink 记录、以及文件数据块；然后写入
tempfile 临时文件，用 openmacdisk.image.RawImage 作为 reader 交给解析器。

镜像构造与解析代码相互独立：Fletcher64、块布局、记录编码都在本文件里
用自己的实现写一遍，避免「用被测代码构造样本」的自证循环。
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from openmacdisk.apfs import APFSContainer, APFSError, APFSVolume  # noqa: E402
from openmacdisk.image import RawImage  # noqa: E402

BS = 4096

# ---- 块布局（容器相对物理块号）----
B_NXSB = 0
B_DESC = 1
B_CKPT_MAP = 2
B_NX_OMAP = 3
B_NX_OMAP_TREE = 4
B_APSB = 5
B_VOL_OMAP = 6
B_VOL_OMAP_TREE = 7
B_FS_ROOT = 8
B_FS_LEAF_A = 9
B_FS_LEAF_B = 10
B_HELLO_0 = 11
B_HELLO_1 = 12
B_README = 13
BLOCK_COUNT = 15

NXSB_MAGIC = b'NXSB'
APSB_MAGIC = b'APSB'

OBJ_PHYSICAL = 0x40000000
OBJ_ENCRYPTED = 0x10000000
OBJECT_TYPE_NX_SUPERBLOCK = 0x01
OBJECT_TYPE_BTREE = 0x02
OBJECT_TYPE_BTREE_NODE = 0x03
OBJECT_TYPE_OMAP = 0x0B
OBJECT_TYPE_FS = 0x0D
OBJECT_TYPE_FSTREE = 0x0E

BTNODE_ROOT = 0x0001
BTNODE_LEAF = 0x0002
BTNODE_FIXED_KV_SIZE = 0x0004
BTREE_KV_NONALIGNED = 0x00000040
BTREE_UINT64_KEYS = 0x00000001
BTREE_PHYSICAL = 0x00000010
BTREE_INFO_SIZE = 40

APFS_TYPE_EXTENT = 2
APFS_TYPE_INODE = 3
APFS_TYPE_XATTR = 4
APFS_TYPE_DSTREAM_ID = 6
APFS_TYPE_FILE_EXTENT = 8
APFS_TYPE_DIR_REC = 9
APFS_TYPE_SNAP_NAME = 11
APFS_TYPE_FILE_INFO = 13

DT_DIR = 4
DT_REG = 8
DT_LNK = 10
ROOT_INO = 2
INO_EXT_TYPE_DSTREAM = 8
FILE_INFO_MAGIC = 0x46494E46  # 'FINF'
SYMLINK_XATTR = b'com.apple.fs.symlink'

VIRT_FS_OID = 0x100
VIRT_FS_ROOT = 0x400
VIRT_LEAF_A = 0x401
VIRT_LEAF_B = 0x402
XID = 2

# ---- 测试文件内容 ----
HELLO = bytes((i * 37 + 11) & 0xFF for i in range(6000))
README = b'# README\nAPFS synthetic image.\n'
SYMLINK_TARGET = b'docs/readme.md'


def align8(n):
    return (n + 7) & ~7


def fletcher64(block):
    """独立实现的 APFS Fletcher64（对 block[8:] 的小端 u32 字）。"""
    mod = 0xFFFFFFFF
    n = (len(block) - 8) // 4
    words = struct.unpack_from('<%dI' % n, block, 8)
    s1 = s2 = 0
    for w in words:
        s1 = (s1 + w) % mod
        s2 = (s2 + s1) % mod
    low = mod - ((s1 + s2) % mod)
    high = mod - ((s1 + low) % mod)
    return (high << 32) | low


def fix_cksum(block):
    block[0:8] = b'\x00' * 8
    struct.pack_into('<Q', block, 0, fletcher64(bytes(block)))
    return block


def obj_header(oid, xid, otype, subtype=0):
    b = bytearray(32)
    struct.pack_into('<QQII', b, 8, oid, xid, otype, subtype)
    return b


def blank_block():
    return bytearray(BS)


def make_node(records, *, flags, level, oid, xid, otype, subtype,
              fixed_kv=False, key_size=0, val_size=0, bt_flags=0):
    """构造一个 btree_node_phys_t：TOC -> keys -> free -> values -> btree_info_t。

    值与键按 8 字节语义顺序排布；kvloc_t.v.off 表示从 value area 末端
    到该值起点的距离（Apple Reference 的负偏移约定）。
    """
    node = blank_block()
    node[0:32] = obj_header(oid, xid, otype, subtype)
    entry_size = 4 if fixed_kv else 8
    toc_len = len(records) * entry_size
    struct.pack_into('<HHI', node, 0x20, flags, level, len(records))
    struct.pack_into('<HH', node, 0x28, 0, toc_len)  # btn_table_space

    toc_start = 0x38
    key_start = toc_start + toc_len
    val_end = BS - (BTREE_INFO_SIZE if flags & BTNODE_ROOT else 0)

    key_pos = key_start
    k_entries = []
    for k, v in records:
        if fixed_kv:
            assert len(k) == key_size and len(v) == val_size
        node[key_pos:key_pos + len(k)] = k
        k_entries.append((key_pos - key_start, len(k)))
        key_pos += len(k)

    val_pos = val_end
    v_entries = []
    for k, v in records:
        val_pos -= len(v)
        assert val_pos >= key_pos, '节点数据区溢出'
        node[val_pos:val_pos + len(v)] = v
        v_entries.append((val_end - val_pos, len(v)))

    for i, ((k_off, k_len), (v_off, v_len)) in enumerate(zip(k_entries, v_entries)):
        pos = toc_start + i * entry_size
        if fixed_kv:
            struct.pack_into('<HH', node, pos, k_off, v_off)
        else:
            struct.pack_into('<HHHH', node, pos, k_off, k_len, v_off, v_len)

    # btn_free_space：off 相对 key area 起点
    struct.pack_into('<HH', node, 0x2C, key_pos - key_start, val_pos - key_pos)

    if flags & BTNODE_ROOT:
        info = BS - BTREE_INFO_SIZE
        struct.pack_into('<IIII', node, info, bt_flags, BS, key_size, val_size)
        struct.pack_into('<IIQQ', node, info + 16,
                         max((len(k) for k, _v in records), default=0),
                         max((len(v) for _k, v in records), default=0),
                         len(records), 1)
    return fix_cksum(node)


# ---- FS 记录编码 ----

def jkey(oid, rtype):
    return struct.pack('<Q', (rtype << 60) | oid)


def drec_key(parent, name):
    # 真实 APFS j_drec_hashed_key_t：j_key_t(8B) + name_len_and_hash(u32，低
    # 10 位为名字长度含结尾 NUL，高 22 位为 hash，合成镜像置 0) + name。
    raw = name.encode('utf-8') + b'\x00'
    return jkey(parent, APFS_TYPE_DIR_REC) + struct.pack('<I', len(raw)) + raw


def drec_val(file_id, dtype):
    return struct.pack('<QQH', file_id, 0, dtype) + b'\x00\x00'


def inode_val(cnid, size, mode):
    v = bytearray(92)
    struct.pack_into('<QQ', v, 0, 1, cnid)      # parent_id, private_id
    struct.pack_into('<H', v, 80, mode)         # mode
    xf = bytearray(40)
    struct.pack_into('<HH', xf, 0, 1, 40)      # xf_num_exts, xf_used_data
    xf[4], xf[5] = INO_EXT_TYPE_DSTREAM, 0x20
    struct.pack_into('<H', xf, 6, 32)          # x_size
    struct.pack_into('<QQQQ', xf, 8, size, align8(size), 0, 0)
    return bytes(v) + bytes(xf)


def file_info_val(size, stream_id, mode):
    return struct.pack('<IIQQQII', FILE_INFO_MAGIC, 1, size,
                       align8(size), stream_id, mode, 0)


def extent_key(stream, logical):
    return jkey(stream, APFS_TYPE_FILE_EXTENT) + struct.pack('<Q', logical)


def extent_val(length, phys):
    return struct.pack('<QQQ', length, phys, 0)


def xattr_key(oid, name):
    raw = name + b'\x00'
    return jkey(oid, APFS_TYPE_XATTR) + struct.pack('<H', len(raw)) + raw


def xattr_val(data):
    return struct.pack('<HH', 0, len(data)) + data


def omap_key(oid, xid):
    return struct.pack('<QQ', oid, xid)


def omap_val(paddr):
    return struct.pack('<IIQ', 0, BS, paddr)


def omap_object(tree_paddr, snap_count=0):
    b = blank_block()
    b[0:32] = obj_header(tree_paddr, XID, OBJ_PHYSICAL | OBJECT_TYPE_OMAP, 0)
    struct.pack_into('<IIII', b, 0x20, 0, snap_count, 0, 0)
    struct.pack_into('<Q', b, 0x30, tree_paddr)   # om_tree_oid
    return fix_cksum(b)


def make_nxsb(xid, encrypted=False):
    b = blank_block()
    b[0:32] = obj_header(1, xid, OBJ_PHYSICAL | OBJECT_TYPE_NX_SUPERBLOCK, 0)
    struct.pack_into('<4sI', b, 0x20, NXSB_MAGIC, BS)
    struct.pack_into('<Q', b, 0x28, BLOCK_COUNT)
    struct.pack_into('<Q', b, 0x58, 0x1000)       # nx_next_oid
    struct.pack_into('<Q', b, 0x60, xid + 1)      # nx_next_xid
    struct.pack_into('<I', b, 0x68, 1)            # nx_xp_desc_blocks
    struct.pack_into('<I', b, 0x6C, 1)            # nx_xp_data_blocks
    struct.pack_into('<Q', b, 0x70, B_DESC)       # nx_xp_desc_base
    struct.pack_into('<Q', b, 0x78, B_CKPT_MAP)   # nx_xp_data_base
    struct.pack_into('<I', b, 0x80, 1)
    struct.pack_into('<I', b, 0x84, 1)
    struct.pack_into('<I', b, 0x88, 0)            # nx_xp_desc_index
    struct.pack_into('<I', b, 0x8C, 1)            # nx_xp_desc_len
    struct.pack_into('<I', b, 0x90, 0)
    struct.pack_into('<I', b, 0x94, 1)
    struct.pack_into('<Q', b, 0xA0, B_NX_OMAP)    # nx_omap_oid（物理）
    struct.pack_into('<I', b, 0xB4, 100)          # nx_max_file_systems
    struct.pack_into('<Q', b, 0xB8, VIRT_FS_OID)  # nx_fs_oid[0]（虚拟）
    if encrypted:
        struct.pack_into('<Q', b, 0x518, 1)       # nx_keylocker.pr_block_count
    return fix_cksum(b)


def make_apsb():
    b = blank_block()
    b[0:32] = obj_header(VIRT_FS_OID, XID, OBJECT_TYPE_FS, 0)
    struct.pack_into('<4sI', b, 0x20, APSB_MAGIC, 0)
    struct.pack_into('<Q', b, 0x80, B_VOL_OMAP)       # apfs_omap_oid
    struct.pack_into('<Q', b, 0x88, VIRT_FS_ROOT)     # apfs_root_tree_oid
    struct.pack_into('<Q', b, 0xB8, 4)                # apfs_num_files
    struct.pack_into('<Q', b, 0xC0, 1)                # apfs_num_directories
    name = b'SyntheticAPFS\x00'
    b[0x2C0:0x2C0 + len(name)] = name
    return fix_cksum(b)


def make_records(feature=None):
    """返回 (leaf_a_records, leaf_b_records)。"""
    leaf_a = []
    leaf_b = []

    # --- j_drec 目录项 ---
    for parent, name, cnid, dtype in [
            (ROOT_INO, 'hello.txt', 16, DT_REG),
            (ROOT_INO, 'empty.txt', 17, DT_REG),
            (ROOT_INO, 'docs', 18, DT_DIR),
            (ROOT_INO, 'link', 20, DT_LNK),
            (18, 'readme.md', 19, DT_REG)]:
        leaf_a.append((drec_key(parent, name), drec_val(cnid, dtype)))

    # --- j_inode_val（type 3，真实 APFS 路径：DSTREAM 扩展字段给大小）---
    sizes = {ROOT_INO: 0, 16: len(HELLO), 17: 0, 18: 0,
             19: len(README), 20: len(SYMLINK_TARGET)}
    for cnid, size in sizes.items():
        mode = 0o40755 if cnid in (ROOT_INO, 18) else 0o100644
        leaf_a.append((jkey(cnid, APFS_TYPE_INODE), inode_val(cnid, size, mode)))

    # --- j_file_info（type 13，合成 profile 的紧凑文件信息记录）---
    for cnid, size in sizes.items():
        mode = 0o40755 if cnid in (ROOT_INO, 18) else 0o100644
        leaf_b.append((jkey(cnid, APFS_TYPE_FILE_INFO),
                       file_info_val(size, cnid, mode)))

    # --- j_file_extent（type 8）---
    leaf_b.append((extent_key(16, 0), extent_val(BS, B_HELLO_0)))
    leaf_b.append((extent_key(16, BS), extent_val(BS, B_HELLO_1)))
    leaf_b.append((extent_key(19, 0), extent_val(BS, B_README)))

    # --- j_symlink（type 4，com.apple.fs.symlink xattr）---
    leaf_b.append((xattr_key(20, SYMLINK_XATTR), xattr_val(SYMLINK_TARGET)))

    # --- 可选注入：明确不支持的特性 ---
    if feature == 'xattr':
        leaf_b.append((xattr_key(16, b'com.apple.decmpfs'), xattr_val(b'cmpf')))
    elif feature == 'sparse':
        leaf_b[3] = (extent_key(16, 0), extent_val(BS, 0))
    elif feature == 'ssv_file_info':
        leaf_b.append((jkey(16, APFS_TYPE_FILE_INFO),
                       struct.pack('<HB', 32, 32) + b'\xaa' * 32))
    elif feature == 'clone':
        leaf_b.append((jkey(16, APFS_TYPE_DSTREAM_ID), struct.pack('<I', 2)))
    elif feature == 'snapshot_record':
        leaf_b.append((jkey(21, APFS_TYPE_SNAP_NAME), b'snap\x00'))
    return leaf_a, leaf_b


def build_image(feature=None):
    """构造完整镜像并返回一个 bytes。"""
    leaf_a, leaf_b = make_records(feature)
    blocks = {}

    blocks[B_NXSB] = make_nxsb(1, encrypted=(feature == 'encryption'))
    blocks[B_DESC] = make_nxsb(XID, encrypted=(feature == 'encryption'))
    blocks[B_CKPT_MAP] = blank_block()  # 描述符区之外的块，解析器应跳过

    # 容器 omap：虚拟卷 OID -> APSB 物理块
    blocks[B_NX_OMAP] = omap_object(B_NX_OMAP_TREE,
                                   snap_count=1 if feature == 'snapshot' else 0)
    blocks[B_NX_OMAP_TREE] = make_node(
        [(omap_key(VIRT_FS_OID, XID), omap_val(B_APSB))],
        flags=BTNODE_ROOT | BTNODE_LEAF | BTNODE_FIXED_KV_SIZE,
        level=0, oid=B_NX_OMAP_TREE, xid=XID,
        otype=OBJ_PHYSICAL | OBJECT_TYPE_BTREE, subtype=OBJECT_TYPE_OMAP,
        fixed_kv=True, key_size=16, val_size=16,
        bt_flags=BTREE_UINT64_KEYS | BTREE_PHYSICAL)

    blocks[B_APSB] = make_apsb()

    # 卷 omap：FS root + 两个叶节点的虚拟 OID -> 物理块
    blocks[B_VOL_OMAP] = omap_object(B_VOL_OMAP_TREE)
    blocks[B_VOL_OMAP_TREE] = make_node(
        [(omap_key(VIRT_FS_ROOT, XID), omap_val(B_FS_ROOT)),
         (omap_key(VIRT_LEAF_A, XID), omap_val(B_FS_LEAF_A)),
         (omap_key(VIRT_LEAF_B, XID), omap_val(B_FS_LEAF_B))],
        flags=BTNODE_ROOT | BTNODE_LEAF | BTNODE_FIXED_KV_SIZE,
        level=0, oid=B_VOL_OMAP_TREE, xid=XID,
        otype=OBJ_PHYSICAL | OBJECT_TYPE_BTREE, subtype=OBJECT_TYPE_OMAP,
        fixed_kv=True, key_size=16, val_size=16,
        bt_flags=BTREE_UINT64_KEYS | BTREE_PHYSICAL)

    # FS B-tree 根（internal，带 btree_info_t），两个子节点为虚拟 OID
    blocks[B_FS_ROOT] = make_node(
        [(jkey(2, 0), struct.pack('<Q', VIRT_LEAF_A)),
         (jkey(20, 0), struct.pack('<Q', VIRT_LEAF_B))],
        flags=BTNODE_ROOT, level=1, oid=VIRT_FS_ROOT, xid=XID,
        otype=OBJECT_TYPE_BTREE, subtype=OBJECT_TYPE_FSTREE,
        bt_flags=BTREE_KV_NONALIGNED)

    blocks[B_FS_LEAF_A] = make_node(
        leaf_a, flags=BTNODE_LEAF, level=0, oid=VIRT_LEAF_A, xid=XID,
        otype=OBJECT_TYPE_BTREE_NODE, subtype=OBJECT_TYPE_FSTREE)
    blocks[B_FS_LEAF_B] = make_node(
        leaf_b, flags=BTNODE_LEAF, level=0, oid=VIRT_LEAF_B, xid=XID,
        otype=OBJECT_TYPE_BTREE_NODE, subtype=OBJECT_TYPE_FSTREE)

    # 文件数据块
    blocks[B_HELLO_0] = bytearray(HELLO[:BS].ljust(BS, b'\x00'))
    blocks[B_HELLO_1] = bytearray(HELLO[BS:].ljust(BS, b'\x00'))
    blocks[B_README] = bytearray(README.ljust(BS, b'\x00'))

    out = bytearray(BLOCK_COUNT * BS)
    for paddr, blk in blocks.items():
        out[paddr * BS:(paddr + 1) * BS] = blk
    return bytes(out)


class TestAPFS(unittest.TestCase):
    def _reader(self, data):
        fd, path = tempfile.mkstemp(prefix='apfs_', suffix='.img')
        os.close(fd)
        with open(path, 'wb') as f:
            f.write(data)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        reader = RawImage(path)
        self.addCleanup(reader.close)   # LIFO：先关句柄，再删文件
        return reader

    def test_directory_tree_walk(self):
        vol = APFSVolume(self._reader(build_image()), 0)
        seen = {n.path: (n.kind, n.size) for n in vol.walk()}
        self.assertIn('', seen)
        self.assertEqual(seen['hello.txt'], ('file', len(HELLO)))
        self.assertEqual(seen['empty.txt'], ('file', 0))
        self.assertEqual(seen['docs'], ('folder', 0))
        self.assertEqual(seen['docs/readme.md'], ('file', len(README)))
        self.assertEqual(seen['link'], ('file', len(SYMLINK_TARGET)))
        self.assertEqual(seen[''], ('folder', 0))
        self.assertEqual(vol.find('docs').kind, 'folder')
        self.assertEqual(vol.signature, 'APSB')
        self.assertEqual(vol.file_count, 4)
        self.assertEqual(vol.folder_count, 1)
        s = vol.summary()
        self.assertEqual(s['files'], 4)
        self.assertEqual(s['folders'], 1)
        self.assertEqual(s['root_children'], 4)
        self.assertEqual(s['block_size'], BS)
        self.assertTrue(s['volume_name'])

    def test_find_by_path(self):
        vol = APFSVolume(self._reader(build_image()), 0)
        self.assertIs(vol.find(''), vol.root)
        self.assertIs(vol.find('/'), vol.root)
        n = vol.find('/docs/readme.md')
        self.assertIsNotNone(n)
        self.assertEqual(n.name, 'readme.md')
        self.assertEqual(n.size, len(README))
        self.assertEqual(n.parent, 18)
        self.assertEqual(vol.find('docs').cnid, 18)
        self.assertIsNone(vol.find('docs/nope'))
        self.assertIsNone(vol.find('nope'))

    def test_file_content_bytes(self):
        vol = APFSVolume(self._reader(build_image()), 0)
        self.assertEqual(vol.read_file(vol.find('hello.txt')), HELLO)
        self.assertEqual(vol.read_file(vol.find('empty.txt')), b'')
        self.assertEqual(vol.read_file(vol.find('/docs/readme.md')), README)
        self.assertEqual(vol.read_file(vol.find('link')), SYMLINK_TARGET)
        # max_bytes 截断
        self.assertEqual(vol.read_file(vol.find('hello.txt'), 100), HELLO[:100])
        self.assertEqual(vol.read_file(vol.find('hello.txt'), 99999), HELLO)
        # data_fork 复用了 hfs.ForkData，extent 与 HFS 语义一致
        fork = vol.find('hello.txt').data_fork
        self.assertIsNotNone(fork)
        self.assertEqual(fork.size, len(HELLO))
        self.assertEqual(fork.extents, [(B_HELLO_0, 1), (B_HELLO_1, 1)])
        # 目录不是文件
        with self.assertRaises(APFSError):
            vol.read_file(vol.find('docs'))

    def test_not_apfs_raises(self):
        reader = self._reader(b'\x00' * (4 * BS))
        with self.assertRaises(APFSError):
            APFSVolume(reader, 0)
        with self.assertRaises(APFSError):
            APFSContainer(reader, 0)
        # 有 NXSB 字样但校验和错误，同样拒绝
        bad = bytearray(build_image())
        bad[0x20:0x24] = NXSB_MAGIC
        bad[0x24:0x28] = struct.pack('<I', BS)
        bad[0x100] ^= 0xFF
        with self.assertRaises(APFSError):
            APFSVolume(self._reader(bytes(bad)), 0)

    def test_container_enumerates_volumes(self):
        container = APFSContainer(self._reader(build_image()), 0)
        self.assertEqual(len(container.volumes), 1)
        self.assertIsInstance(container.volumes[0], APFSVolume)
        self.assertEqual(container.volumes[0].signature, 'APSB')
        s = container.summary()
        self.assertEqual(s['volume_count'], 1)
        self.assertEqual(s['signature'], 'NXSB')
        self.assertEqual(s['block_size'], BS)
        self.assertEqual(s['checkpoint_xid'], XID)
        self.assertEqual(s['volumes'][0]['fs'], 'APFS')

    def test_partition_base_offset(self):
        """APFS 分区起点(base)为字节偏移，物理块地址须叠加 base。"""
        base = 64 * 512
        data = b'\xCC' * base + build_image()
        reader = self._reader(data)
        vol = APFSVolume(reader, base)
        self.assertEqual(vol.read_file(vol.find('hello.txt')), HELLO)
        self.assertEqual(vol.find('docs/readme.md').size, len(README))
        container = APFSContainer(reader, base)
        self.assertEqual(len(container.volumes), 1)
        self.assertEqual(container.volumes[0].read_file(
            container.volumes[0].find('link')), SYMLINK_TARGET)

    def test_unsupported_features_raise(self):
        features = ('encryption', 'snapshot', 'snapshot_record',
                    'sparse', 'ssv_file_info', 'clone')
        for feature in features:
            with self.subTest(feature=feature):
                with self.assertRaises(APFSError):
                    APFSVolume(self._reader(build_image(feature)), 0)

    def test_metadata_xattr_is_skipped_not_fatal(self):
        """com.apple.quarantine / decmpfs 等 xattr 只是元数据，列出目录时跳过
        （与 TransMac 一致），不应导致整个卷打不开。真实 GPT+APFS DMG 普遍
        带 quarantine 属性。"""
        vol = APFSVolume(self._reader(build_image('xattr')), 0)
        self.assertIsNotNone(vol.find('hello.txt'))
        self.assertEqual(vol.read_file(vol.find('hello.txt')), HELLO)


if __name__ == '__main__':
    unittest.main()
