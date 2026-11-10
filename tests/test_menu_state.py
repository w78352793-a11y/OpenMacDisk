# -*- coding: utf-8 -*-
"""卷节点右键状态机：eXpand 与 Compress 永远只出现一个。

TransMac 的卷节点右键是按镜像状态切换的：压缩 DMG（只读）显示 eXpand，
展开后的全 RAW DMG（可写）显示 Compress。这里固化底层判定与整条闭环。
"""
import os
import shutil
import tempfile
import unittest

from openmacdisk import inplace, writer


class TestConvertAction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_menu_')
        self.src = os.path.join(self.tmp, '源目录')
        os.makedirs(self.src)
        with open(os.path.join(self.src, 'hello.txt'), 'wb') as f:
            f.write(b'menu state test\n' * 200)
        self.compressed = os.path.join(self.tmp, 'compressed.dmg')
        writer.create_from_paths(self.compressed, [self.src], '测试卷')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compressed_dmg_shows_expand_only(self):
        self.assertEqual(inplace.convert_action(self.compressed), 'expand')

    def test_expanded_dmg_shows_compress_only(self):
        expanded = os.path.join(self.tmp, 'expanded.dmg')
        writer.expand_to_editable_dmg(self.compressed, expanded)
        self.assertEqual(inplace.convert_action(expanded), 'compress')

    def test_full_round_trip_alternates(self):
        expanded = os.path.join(self.tmp, 'expanded.dmg')
        again = os.path.join(self.tmp, 'again.dmg')
        writer.expand_to_editable_dmg(self.compressed, expanded)
        writer.compress_to_dmg(expanded, again)
        self.assertEqual(inplace.convert_action(self.compressed), 'expand')
        self.assertEqual(inplace.convert_action(expanded), 'compress')
        self.assertEqual(inplace.convert_action(again), 'expand')

    def test_raw_img_is_treated_as_uncompressed(self):
        raw = os.path.join(self.tmp, 'raw.img')
        writer.expand_dmg(self.compressed, raw)
        self.assertEqual(inplace.convert_action(raw), 'compress')

    def test_device_or_encrypted_or_missing_shows_neither(self):
        self.assertIsNone(inplace.convert_action('', False))
        self.assertIsNone(inplace.convert_action(
            os.path.join(self.tmp, 'missing.dmg'), False))
        self.assertIsNone(inplace.convert_action(self.tmp, False))  # 目录
        self.assertIsNone(inplace.convert_action(
            r'\\.\PhysicalDrive1', False))
        self.assertIsNone(inplace.convert_action(
            self.compressed, True))          # 加密镜像不提供这两个命令


if __name__ == '__main__':
    unittest.main()
