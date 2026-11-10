# -*- coding: utf-8 -*-
"""HFS+ 卷原地编辑（TransMac 写入流程）的回归测试。

覆盖：压缩 DMG 只读判定、eXpand 后可原地写、删除/重命名/新建目录/
加入文件/替换、原地格式化、B-tree 记录偏移表的 TN1150 倒序规范。
"""
import os
import shutil
import struct
import tempfile
import unittest

from openmacdisk import inplace, writer
from openmacdisk.image import Disk


class TestInplace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_inplace_')
        self.folder = os.path.join(self.tmp, '数据')
        os.makedirs(os.path.join(self.folder, '子目录'))
        self.a = b'inplace HFS+ test\n' * 3000
        self.b = b'second file\n' * 100
        with open(os.path.join(self.folder, '子目录', 'a.txt'), 'wb') as f:
            f.write(self.a)
        with open(os.path.join(self.folder, 'b.txt'), 'wb') as f:
            f.write(self.b)
        self.compressed = os.path.join(self.tmp, 'src.dmg')
        writer.create_from_paths(self.compressed, [self.folder], '测试卷')
        self.dmg = os.path.join(self.tmp, 'editable.dmg')
        writer.expand_to_editable_dmg(self.compressed, self.dmg)
        self.original_size = os.path.getsize(self.dmg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paths(self, path):
        with Disk(path) as d:
            return [n.path for n in d.first_volume.walk()]

    def test_compressed_is_read_only_expanded_is_writable(self):
        ok, why = inplace.is_writable(self.compressed)
        self.assertFalse(ok)
        self.assertIn('eXpand', why)
        ok, why = inplace.is_writable(self.dmg)
        self.assertTrue(ok, why)

    def test_inplace_edit_all_operations(self):
        r = inplace.edit_inplace(self.dmg, [
            {'op': 'delete', 'path': '数据/子目录/a.txt'},
            {'op': 'rename', 'path': '数据', 'name': '资料'},
            {'op': 'mkdir', 'path': '资料/新建文件夹'},
        ])
        self.assertEqual(r['mode'], 'inplace')
        self.assertEqual((r['deleted'], r['renamed'], r['created']), (1, 1, 1))
        self.assertEqual(os.path.getsize(self.dmg), self.original_size,
                         '原地编辑不能改变镜像文件大小')
        with Disk(self.dmg) as d:
            v = d.first_volume
            self.assertIsNone(v.find('数据/子目录/a.txt'))
            self.assertIsNotNone(v.find('资料/新建文件夹'))
            self.assertEqual(v.read_file(v.find('资料/b.txt')), self.b)

        extra = os.path.join(self.tmp, 'add.bin')
        payload = b'ADD-FILE-DATA' * 100
        with open(extra, 'wb') as f:
            f.write(payload)
        r = inplace.edit_inplace(self.dmg, [
            {'op': 'add', 'src': extra, 'path': '资料/新文件.bin'},
            {'op': 'delete', 'path': '资料/b.txt'},
            {'op': 'replace', 'src': extra, 'path': '资料/新文件.bin'},
            {'op': 'add', 'src': extra, 'path': '资料/新建文件夹/子文件.bin'},
        ])
        self.assertEqual((r['added'], r['deleted']), (3, 1))
        with Disk(self.dmg) as d:
            v = d.first_volume
            self.assertEqual(v.read_file(v.find('资料/新文件.bin')), payload)
            self.assertEqual(
                v.read_file(v.find('资料/新建文件夹/子文件.bin')), payload)
            self.assertIsNone(v.find('资料/b.txt'))

    def test_inplace_format_keeps_capacity(self):
        r = inplace.format_inplace(self.dmg, '空白卷')
        self.assertTrue(r['formatted'])
        self.assertEqual(os.path.getsize(self.dmg), self.original_size)
        self.assertEqual(self._paths(self.dmg), [''])

    def test_btree_offset_table_is_tn1150_reverse_order(self):
        """生成的 B-tree 节点必须按规范把记录偏移表倒序写在节点末尾。"""
        node = writer._pack_node(writer.KIND_LEAF, 1,
                                 [b'abcd', b'efgh', b'ijkl'])
        ns = len(node)
        desc = writer.NODE_DESC.unpack_from(node, 0)
        nrec = desc[4]
        offs = struct.unpack_from('>%dH' % (nrec + 1), node,
                                  ns - 2 * (nrec + 1))
        self.assertGreater(offs[0], offs[-1],
                           '偏移表第一个应是 free-space 偏移，最后一个是记录 0')
        self.assertEqual(offs[-1], 14)
        # 解析器必须能自动识别这种规范布局
        restored = tuple(reversed(offs))
        self.assertLess(restored[0], restored[-1])
        self.assertEqual(restored[:-1], (14, 18, 22))


if __name__ == '__main__':
    unittest.main()
