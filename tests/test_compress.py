# -*- coding: utf-8 -*-
"""Compress 回归：TransMac 卷右键「Compress」= eXpand 的逆操作。

压缩后仍是单个标准 UDIF DMG，逻辑磁盘逐字节不变，且压缩卷只读；
再次 eXpand 后应能继续编辑。
"""
import os
import shutil
import tempfile
import unittest

from openmacdisk import inplace, writer
from openmacdisk.image import Disk
from openmacdisk.udif import BLOCK_RAW, BLOCK_ZLIB, UDIFImage


class TestCompress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_compress_')
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
        # TransMac 的实际场景：先 eXpand 成全 RAW 可编辑卷，编辑完再 Compress
        self.editable = os.path.join(self.tmp, 'editable.dmg')
        writer.expand_to_editable_dmg(self.dmg, self.editable)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_udif(self, path):
        with open(path, 'rb') as f:
            f.seek(-512, os.SEEK_END)
            self.assertEqual(f.read(4), b'koly')

    def test_compress_shrinks_and_keeps_content(self):
        out = os.path.join(self.tmp, 'compressed.dmg')
        size = writer.compress_to_dmg(self.editable, out)
        self.assertGreater(size, 0)
        self.assertTrue(os.path.isfile(out))
        self._assert_udif(out)

        # 空闲区被 zlib 压掉：文件明显小于全 RAW 的可编辑卷
        self.assertLess(size, os.path.getsize(self.editable))
        img = UDIFImage(out)
        try:
            self.assertTrue(img.runs)
            self.assertTrue(any(r.type == BLOCK_ZLIB for r in img.runs))
        finally:
            img.close()

        # 逻辑磁盘（分区表 + HFS+ 卷）与文件内容逐字节不变
        with Disk(self.editable) as a, Disk(out) as b:
            self.assertEqual(a.logical_size, b.logical_size)
            self.assertEqual(a.reader.read(0, a.logical_size),
                             b.reader.read(0, b.logical_size))
            vol = b.first_volume
            for rel, data in self.payloads.items():
                node = vol.find('源目录/' + rel)
                self.assertIsNotNone(node, rel)
                self.assertEqual(vol.read_file(node), data)

        # 与 TransMac 一致：压缩卷只读，要编辑必须先 eXpand
        self.assertFalse(inplace.container_writable(out))

    def test_compress_then_expand_is_editable_again(self):
        out = os.path.join(self.tmp, 'compressed.dmg')
        writer.compress_to_dmg(self.editable, out)
        back = os.path.join(self.tmp, 'back.dmg')
        writer.expand_to_editable_dmg(out, back)
        img = UDIFImage(back)
        try:
            self.assertTrue(all(r.type == BLOCK_RAW for r in img.runs))
        finally:
            img.close()
        self.assertTrue(inplace.container_writable(back))

        extra = os.path.join(self.tmp, '新增.txt')
        with open(extra, 'wb') as f:
            f.write(b'added after Compress + eXpand\n')
        edited = os.path.join(self.tmp, 'edited.dmg')
        writer.edit_image(back, edited, [
            {'op': 'add', 'src': extra, 'path': '源目录/新增.txt'},
        ])
        with Disk(edited) as d:
            self.assertEqual(
                d.first_volume.read_file(d.first_volume.find('源目录/新增.txt')),
                b'added after Compress + eXpand\n')

    def test_compress_forces_dmg_suffix(self):
        requested = os.path.join(self.tmp, 'whatever.img')
        writer.compress_to_dmg(self.editable, requested)
        real = os.path.join(self.tmp, 'whatever.dmg')
        self.assertTrue(os.path.isfile(real))
        self.assertFalse(os.path.exists(requested))
        self._assert_udif(real)
        with Disk(real) as d:
            self.assertEqual(len(d.volumes), 1)

    def test_compress_rejects_same_path_and_reports_progress(self):
        with self.assertRaises(writer.WriteError):
            writer.compress_to_dmg(self.editable, self.editable)

        seen = []

        def cb(pct, msg):
            seen.append(pct)
            if len(seen) >= 2:
                raise RuntimeError('用户取消')      # 模拟进度框点「取消」

        out = os.path.join(self.tmp, 'cancelled.dmg')
        with self.assertRaises(RuntimeError):
            writer.compress_to_dmg(self.editable, out, cb)
        self.assertTrue(seen)                       # 后台回调链路确实生效


if __name__ == '__main__':
    unittest.main()
