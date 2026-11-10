# -*- coding: utf-8 -*-
"""HFS+ / UDIF 写入侧的端到端测试：生成 → 回读 → 解压 → 转换 → 校验。"""
import os
import struct
import tempfile
import unittest

from openmacdisk import writer
from openmacdisk.image import Disk
from openmacdisk.hfs import NODE_DESC


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_writer_')
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

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_extract(self):
        out = os.path.join(self.tmp, 'a.dmg')
        size = writer.create_from_paths(out, [self.src], '测试卷')
        self.assertGreater(size, 0)

        with Disk(out) as d:
            s = d.summary()
            self.assertEqual(s['format'], 'UDIF')
            self.assertGreater(s['logical_size'], 0)
            self.assertEqual(len(s['volumes']), 1)
            self.assertEqual(s['volumes'][0]['files'], 3)
            vol = d.first_volume
            for rel, data in self.payloads.items():
                n = vol.find('源目录/' + rel)
                self.assertIsNotNone(n, rel)
                self.assertEqual(n.size, len(data))
                self.assertEqual(vol.read_file(n), data)

        dest = os.path.join(self.tmp, 'out')
        os.makedirs(dest)
        ok, fail = writer.extract_all(out, dest)
        self.assertEqual((ok, fail), (3, 0))
        for rel, data in self.payloads.items():
            p = os.path.join(dest, '源目录', *rel.split('/'))
            self.assertTrue(os.path.isfile(p), p)
            with open(p, 'rb') as f:
                self.assertEqual(f.read(), data)

    def test_many_files_build_btree(self):
        folder = os.path.join(self.tmp, 'many')
        os.makedirs(folder)
        for i in range(120):
            with open(os.path.join(folder, 'f%03d.txt' % i), 'wb') as f:
                f.write(('x%d' % i).encode() * (i + 1))
        out = os.path.join(self.tmp, 'many.dmg')
        writer.create_from_paths(out, [folder], '多文件卷')
        with Disk(out) as d:
            vol = d.first_volume
            self.assertEqual(len(list(vol.walk())), 122)
            n = vol.find('many/f119.txt')
            self.assertEqual(len(vol.read_file(n)), len('x119') * 120)
            # B-tree 头节点合法，且存在索引/叶/空闲节点
            cat = vol.read_fork(vol.catalog_file)
            ns = vol.node_size
            desc = NODE_DESC.unpack_from(cat, 0)
            self.assertEqual(desc[2], 1)          # header node
            self.assertGreaterEqual(desc[4], 3)
            self.assertEqual(struct.unpack_from('>H', cat, 32)[0], ns)
            kinds = {NODE_DESC.unpack_from(cat, i * ns)[2]
                     for i in range(len(cat) // ns)}
            self.assertIn(0xFF, kinds)            # leaf
            self.assertIn(0xFE, kinds)            # free
            self.assertIn(0, kinds)               # index

    def test_blank_expand_repack_verify(self):
        blank = os.path.join(self.tmp, 'blank.dmg')
        writer.create_blank_dmg(blank, '空白', 8)
        with Disk(blank) as d:
            self.assertGreaterEqual(d.summary()['logical_size'], 8 << 20)
            self.assertEqual(len(d.volumes), 1)

        src = os.path.join(self.tmp, 'src.dmg')
        writer.create_from_paths(src, [self.src], '源')
        raw = os.path.join(self.tmp, 'src.img')
        self.assertEqual(writer.expand_dmg(src, raw),
                         os.path.getsize(raw))
        rep = os.path.join(self.tmp, 'rep.dmg')
        writer.repack_dmg(src, rep, compressed=True)
        with Disk(rep) as d:
            self.assertEqual(d.first_volume.find(
                '源目录/hello.txt').size,
                len(self.payloads['hello.txt']))

        r = writer.verify_image(src)
        self.assertEqual(len(r['file_sha256']), 64)
        self.assertEqual(len(r['logical_sha256']), 64)
        self.assertGreater(r['logical_size'], 0)

    def test_catalog_key_and_name_rules(self):
        k = writer.cat_key(2, '')
        self.assertEqual(struct.unpack_from('>H', k, 0)[0], 6)
        self.assertEqual(len(k), 8)
        k2 = writer.cat_key(2, '中')
        self.assertEqual(struct.unpack_from('>H', k2, 0)[0], 8)
        self.assertEqual(writer._safe_name('a:b\x00c'), 'a-bc')

    def test_shell_keys_only(self):
        """只读检查右键菜单定义，不写注册表。"""
        from openmacdisk import shell
        keys = shell._keys()
        self.assertEqual(len(keys), 51)
        self.assertTrue(all(k[0].startswith('Software\\Classes')
                            for k in keys))
        self.assertTrue(any('--extract-here' in c for _b, _l, c in keys))
        self.assertTrue(any('--create' in c for _b, _l, c in keys))


if __name__ == '__main__':
    unittest.main()
