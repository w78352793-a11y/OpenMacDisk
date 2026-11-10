# -*- coding: utf-8 -*-
"""eXpand 回归：展开后仍是单个、未压缩、可继续编辑的 UDIF DMG。"""
import os
import shutil
import tempfile
import unittest

from openmacdisk import writer
from openmacdisk.image import Disk
from openmacdisk.udif import BLOCK_RAW, UDIFImage


class TestExpandEditable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_expand_')
        self.src = os.path.join(self.tmp, '源目录')
        os.makedirs(os.path.join(self.src, '子目录'))
        self.payloads = {
            'hello.txt': b'Hello OpenMacDisk!\n' * 40,
            '子目录/中文.txt': '中文内容测试'.encode('utf-8') * 30,
            'empty.bin': b'',
        }
        for rel, data in self.payloads.items():
            with open(os.path.join(self.src, *rel.split('/')), 'wb') as f:
                f.write(data)
        self.dmg = os.path.join(self.tmp, 'original.dmg')
        writer.create_from_paths(self.dmg, [self.src], '测试卷')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_expand_produces_editable_dmg(self):
        out = os.path.join(self.tmp, 'expanded.dmg')
        size = writer.expand_to_editable_dmg(self.dmg, out)
        self.assertGreater(size, 0)
        self.assertTrue(os.path.isfile(out))

        # 产物必须是标准 UDIF（尾部 koly），且所有块都是未压缩 RAW
        with open(out, 'rb') as f:
            f.seek(-512, os.SEEK_END)
            self.assertEqual(f.read(4), b'koly')
        img = UDIFImage(out)
        try:
            self.assertTrue(img.runs)
            self.assertTrue(all(r.type == BLOCK_RAW for r in img.runs))
        finally:
            img.close()

        # 逻辑磁盘逐字节不变（分区表 / HFS+ 卷原样保留）
        with Disk(self.dmg) as a, Disk(out) as b:
            self.assertEqual(a.logical_size, b.logical_size)
            self.assertEqual(a.reader.read(0, a.logical_size),
                             b.reader.read(0, b.logical_size))
            vol = b.first_volume
            for rel, data in self.payloads.items():
                node = vol.find('源目录/' + rel)
                self.assertIsNotNone(node, rel)
                self.assertEqual(vol.read_file(node), data)

        # 关键：展开出来的 DMG 能继续编辑（添加 / 删除后仍可回读）
        extra = os.path.join(self.tmp, '新增.txt')
        with open(extra, 'wb') as f:
            f.write(b'added after eXpand\n')
        edited = os.path.join(self.tmp, 'edited.dmg')
        writer.edit_image(out, edited, [
            {'op': 'add', 'src': extra, 'path': '源目录/新增.txt'},
            {'op': 'delete', 'path': '源目录/hello.txt'},
        ])
        with Disk(edited) as d:
            vol = d.first_volume
            self.assertIsNone(vol.find('源目录/hello.txt'))
            node = vol.find('源目录/新增.txt')
            self.assertIsNotNone(node)
            self.assertEqual(vol.read_file(node), b'added after eXpand\n')
            self.assertEqual(vol.read_file(vol.find('源目录/子目录/中文.txt')),
                             self.payloads['子目录/中文.txt'])

    def test_expand_forces_dmg_suffix(self):
        requested = os.path.join(self.tmp, 'whatever.img')
        size = writer.expand_to_editable_dmg(self.dmg, requested)
        real = os.path.join(self.tmp, 'whatever.dmg')
        self.assertGreater(size, 0)
        self.assertTrue(os.path.isfile(real))
        self.assertFalse(os.path.exists(requested))
        with Disk(real) as d:
            self.assertEqual(len(d.volumes), 1)

    def test_expand_rejects_same_path(self):
        with self.assertRaises(writer.WriteError):
            writer.expand_to_editable_dmg(self.dmg, self.dmg)


if __name__ == '__main__':
    unittest.main()
