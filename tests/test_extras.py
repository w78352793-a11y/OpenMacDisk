# -*- coding: utf-8 -*-
"""合成样本单元测试：Extents Overflow B-tree（>8 碎片）与 LZFSE 解码。

只依赖标准库；用 tempfile 落盘合成 B-tree 并自行清理。
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from openmacdisk.hfs import ForkData                 # noqa: E402
from openmacdisk import hfs_extents, lzfse          # noqa: E402

NODE = 512
CNID = 42
KIND_INDEX = 0
KIND_HEADER = 1
KIND_LEAF = 0xFF


class FakeVolume:
    """只暴露 hfs_extents 需要的接口，喂入合成 B-tree 字节。"""

    def __init__(self, tree: bytes):
        self._tree = tree
        self.extents_file = ForkData(size=len(tree), clump=NODE,
                                     blocks=1, extents=[(0, 1)])
        # 故意与 extents 树的 512 不同，验证实现从树自己的头节点取 nodeSize
        self.node_size = 4096

    def read_fork(self, fork, max_bytes=None):
        if max_bytes is None:
            return self._tree
        return self._tree[:max_bytes]


def _node(kind, height, records):
    """按 TN1150 构造一个 512 字节 B-tree 节点，含节点尾记录偏移表。"""
    buf = bytearray(NODE)
    nrec = len(records)
    struct.pack_into('>IIBBHH', buf, 0, 0, 0, kind, height, nrec, 0)
    off = 14
    offsets = []
    for rec in records:
        offsets.append(off)
        buf[off:off + len(rec)] = rec
        off += len(rec)
    offsets.append(off)                       # free space 起点
    start = NODE - 2 * len(offsets)          # 节点尾 nrec+1 个 uint16
    struct.pack_into('>%dH' % len(offsets), buf, start, *offsets)
    return bytes(buf)


def _header_node():
    """头节点（节点 0）：BTHeaderRec.nodeSize 位于绝对偏移 32。"""
    hdr = bytearray(106)
    struct.pack_into('>H', hdr, 0, 1)        # treeDepth
    struct.pack_into('>I', hdr, 2, 1)        # rootNode
    struct.pack_into('>I', hdr, 6, 2)        # leafRecords
    struct.pack_into('>I', hdr, 10, 2)       # firstLeafNode
    struct.pack_into('>I', hdr, 14, 2)       # lastLeafNode
    struct.pack_into('>H', hdr, 18, NODE)    # nodeSize <- header record +18 / 绝对 +32
    struct.pack_into('>H', hdr, 20, 10)      # maxKeyLength
    struct.pack_into('>I', hdr, 22, 4)       # totalNodes
    struct.pack_into('>I', hdr, 26, 0)       # freeNodes
    return _node(KIND_HEADER, 0, [bytes(hdr), bytes(128), bytes(16)])


def _index_node():
    """索引节点：塞一条与 CNID 匹配的诱饵，验证实现确实跳过索引节点。"""
    key = struct.pack('>HBBII', 10, 0, 0, CNID, 999)
    fake_extents = b''.join(struct.pack('>II', 900 + j, 1) for j in range(8))
    # 索引记录本应是 key + 4 字节子节点指针；附带伪 extent 数据作诱饵
    rec = key + struct.pack('>I', 2) + fake_extents
    return _node(KIND_INDEX, 1, [rec])


def _extent_record(start_block, extents, fork_type=0):
    """一条 overflow 记录：12 字节 key + 8 个 (startBlock, blockCount)。"""
    key = struct.pack('>HBBII', 10, fork_type, 0, CNID, start_block)
    data = b''.join(struct.pack('>II', s, c) for s, c in extents)
    return key + data


FIRST8 = [(100 + i, 1) for i in range(8)]
OVF_A = [(200 + i, 1) for i in range(8)]
OVF_B = [(300 + i, 1) for i in range(8)]


def _full_tree():
    leaf = _node(KIND_LEAF, 1, [_extent_record(8, OVF_A),
                               _extent_record(16, OVF_B)])
    return _header_node() + _index_node() + leaf


class TestExtentsOverflow(unittest.TestCase):
    def _fork(self, blocks=24, extents=None):
        return ForkData(size=blocks * 512, clump=512, blocks=blocks,
                        extents=list(FIRST8 if extents is None else extents))

    def test_all_extents_merges_more_than_8(self):
        """24 段碎片：前 8 个 + 两条 8-extent overflow 记录必须按序合并完整。"""
        vol = FakeVolume(_full_tree())
        fork = self._fork(blocks=24)
        got = hfs_extents.all_extents(vol, fork, CNID)
        self.assertEqual(len(got), 24)
        self.assertEqual(got, FIRST8 + OVF_A + OVF_B)
        self.assertEqual(sum(c for _s, c in got), fork.blocks)
        # extra_extents 单独：按 key.startBlock 升序，且未误收索引节点诱饵
        self.assertEqual(hfs_extents.extra_extents(vol, CNID, 0, 8),
                         OVF_A + OVF_B)
        # 不匹配的资源 fork 不应命中
        self.assertEqual(hfs_extents.extra_extents(vol, CNID, 0xFF, 8), [])

    def test_all_extents_falls_back_when_no_records(self):
        """空树/无记录/读失败时退回原始 8 个 extent，且不抛异常。"""
        fork = self._fork(blocks=24)          # 故意 > 8，强制走 overflow 读取
        cases = [
            b'',                                        # 空树
            _header_node(),                             # 只有头节点
            _header_node() + _node(KIND_LEAF, 1, []),   # 空叶节点
            b'\x00' * NODE,                            # 垃圾数据（头节点签名不对）
        ]
        for tree in cases:
            with self.subTest(tree_len=len(tree)):
                self.assertEqual(hfs_extents.all_extents(FakeVolume(tree), fork, CNID),
                                 FIRST8)

        class Broken:
            node_size = 512
            extents_file = ForkData(1, 1, 1, [(0, 1)])

            def read_fork(self, f, max_bytes=None):
                raise OSError('IO error')

        self.assertEqual(hfs_extents.all_extents(Broken(), fork, CNID), FIRST8)
        # 前 8 个已覆盖 blocks 时根本不应触碰树
        self.assertEqual(
            hfs_extents.all_extents(Broken(), self._fork(blocks=8), CNID),
            FIRST8)

    def test_extents_tree_via_tempfile(self):
        """合成 B-tree 落盘再读回（tempfile，自行清理）。"""
        tree = _full_tree()
        fd, path = tempfile.mkstemp(prefix='extents_', suffix='.bin')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(tree)
            with open(path, 'rb') as f:
                vol = FakeVolume(f.read())
            self.assertEqual(
                hfs_extents.all_extents(vol, self._fork(blocks=24), CNID),
                FIRST8 + OVF_A + OVF_B)
        finally:
            os.unlink(path)


class TestLZFSE(unittest.TestCase):
    def test_raw_block(self):
        """内置解码器正确处理 bvx- 未压缩块（含多块拼接与 bvx$）。"""
        payload = b'Hello, UDIF LZFSE raw block!'
        data = b'bvx-' + struct.pack('<I', len(payload)) + payload + b'bvx$'
        self.assertEqual(lzfse.decompress(data, len(payload)), payload)

        a, b = b'AAAA', b'BBBBBB'
        two = (b'bvx-' + struct.pack('<I', len(a)) + a +
               b'bvx-' + struct.pack('<I', len(b)) + b + b'bvx$')
        self.assertEqual(lzfse.decompress(two, len(a) + len(b)), a + b)
        self.assertEqual(lzfse.decompress(two, 3), (a + b)[:3])

    def test_lzvn_block(self):
        """bvxn / LZVN 块：纯字面量指令 + sml_d/sml_m 距离复用。"""
        p1 = (bytes([0xE0 | 5]) + b'Hello' +
              bytes([0xE0 | 8]) + b', LZVN! ' + b'\x06')
        raw1 = b'Hello, LZVN! '
        d1 = b'bvxn' + struct.pack('<II', len(raw1), len(p1)) + p1
        self.assertEqual(lzfse.decompress(d1, len(raw1)), raw1)

        # 0xC0: sml_d L=3 M=3 D=3，字面量 'abc' → 'abcabc'，d_prev=3
        # 0xF3: sml_m M=3，复用 D=3 → 'abcabcabc'
        p2 = bytes([0xC0, 3]) + b'abc' + bytes([0xF3]) + b'\x06'
        raw2 = b'abcabcabc'
        d2 = b'bvxn' + struct.pack('<II', len(raw2), len(p2)) + p2
        self.assertEqual(lzfse.decompress(d2, len(raw2)), raw2)

    def test_unsupported_block_raises(self):
        """bvx1/bvx2 无外部解码器时明确抛 LZFSEError，绝不返回错误数据。"""
        if lzfse.full_support():
            self.skipTest('本机存在外部 LZFSE 解码器，bvx2 可被完整解码')
        for magic in (b'bvx2', b'bvx1'):
            with self.subTest(magic=magic):
                with self.assertRaises(lzfse.LZFSEError) as cm:
                    lzfse.decompress(magic + struct.pack('<I', 4) + bytes(20), 4)
                self.assertIn('该 LZFSE 块类型需要外部解码器',
                              str(cm.exception))

    def test_capabilities(self):
        """available()/capabilities() 如实标注内置子集范围。"""
        self.assertIsInstance(lzfse.available(), bool)
        caps = lzfse.capabilities()
        self.assertIn('bvx-', caps['pure_python_blocks'])
        self.assertIn('bvxn', caps['pure_python_blocks'])
        self.assertIn('bvx2', caps['unsupported_without_external'])
        self.assertEqual(caps['full_lzfse'], lzfse.full_support())


if __name__ == '__main__':
    unittest.main()
