# -*- coding: utf-8 -*-
"""拆分 / 合并镜像（Split / Join）的回归测试。

对应 TransMac 卷节点右键菜单里的 Split：把大 DMG 切成固定大小的
分片文件，并可按清单无损合并回原镜像。
"""
import hashlib
import json
import os
import tempfile
import unittest

from openmacdisk import writer
from openmacdisk.writer import WriteError


class TestSplitJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_split_')
        self.src = os.path.join(self.tmp, '大镜像.dmg')
        self.data = (b'OpenMacDisk split test\n' * 120000)[:2_500_000]
        with open(self.src, 'wb') as f:
            f.write(self.data)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_split_join_roundtrip(self):
        m = writer.split_image(self.src, chunk_mb=1)
        self.assertEqual(m['size'], len(self.data))
        self.assertEqual(m['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(len(m['parts']), 3)
        self.assertTrue(all(os.path.isfile(os.path.join(m['directory'], p['name']))
                            for p in m['parts']))
        self.assertTrue(os.path.isfile(m['manifest']))

        out = os.path.join(self.tmp, 'merged.dmg')
        n = writer.join_parts(m['manifest'], out)
        self.assertEqual(n, len(self.data))
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.data)

    def test_join_accepts_first_part(self):
        m = writer.split_image(self.src, chunk_mb=1)
        first = os.path.join(m['directory'], m['parts'][0]['name'])
        self.assertEqual(writer.manifest_path(first), m['manifest'])
        out = os.path.join(self.tmp, 'from_part.dmg')
        self.assertEqual(writer.join_parts(first, out), len(self.data))
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.data)

    def test_corrupted_part_rejected(self):
        m = writer.split_image(self.src, chunk_mb=1)
        part = os.path.join(m['directory'], m['parts'][1]['name'])
        with open(part, 'r+b') as f:
            f.seek(0)
            f.write(b'X')
        out = os.path.join(self.tmp, 'bad.dmg')
        with self.assertRaises(WriteError):
            writer.join_parts(m['manifest'], out)
        self.assertFalse(os.path.exists(out))

    def test_split_does_not_overwrite_existing(self):
        first = writer.split_image(self.src, chunk_mb=1)
        second = writer.split_image(self.src, chunk_mb=1)
        self.assertNotEqual(first['directory'], second['directory'])
        self.assertNotEqual(first['manifest'], second['manifest'])
        with open(first['manifest'], encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['parts']), 3)

    def test_manifest_missing(self):
        with self.assertRaises(WriteError):
            writer.manifest_path(os.path.join(self.tmp, 'nope.001'))


if __name__ == '__main__':
    unittest.main()
