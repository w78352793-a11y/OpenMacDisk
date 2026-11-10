# -*- coding: utf-8 -*-
"""UDIF 资源叉 / koly 元数据与真实 DMG（TransMac / hdiutil）对齐校验。

回归背景：以前 eXpand / Compress 自己另造一个只有 blkx 的 plist，缺 plst
分区资源、多出 CFName、校验和字段 type=0 全 0、块切成 1 MiB；第三方解析器
会把整个盘当成一个 CORRUPTED 的 0.hfs，目录树为空、校验和显示 00000000。
"""
from __future__ import annotations

import os
import plistlib
import struct
import tempfile
import unittest
import zlib

from openmacdisk import writer
from openmacdisk.image import Disk

STD_NAME = 'whole disk (Apple_HFS : 0)'


def tail_meta(path):
    """读取尾部 koly + plist，返回 (koly, plist, blkx, mish)。"""
    with open(path, 'rb') as f:
        size = os.path.getsize(path)
        f.seek(size - 512)
        koly = f.read(512)
        xo, xl = struct.unpack_from('>QQ', koly, 0xD8)
        f.seek(xo)
        pl = plistlib.loads(f.read(xl))
    rf = pl['resource-fork']
    blkx = rf['blkx'][0]
    return koly, pl, blkx, blkx['Data']


def read_tail(path):
    """读取 koly、原始 XML 字节与独立二进制资源叉。"""
    with open(path, 'rb') as f:
        size = os.path.getsize(path)
        f.seek(size - 512)
        koly = f.read(512)
        xo, xl = struct.unpack_from('>QQ', koly, 0xD8)
        f.seek(xo)
        xml = f.read(xl)
        ro, rl = struct.unpack_from('>QQ', koly, 0x28)
        f.seek(ro)
        rsrc = f.read(rl) if rl else b''
    return koly, xml, rsrc


class TestUDIFMetadata(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix='omd_meta_')
        src = os.path.join(cls.d, 'src.txt')
        with open(src, 'wb') as f:
            f.write(b'METADATA-CHECK-' * 4000)
        cls.src = src
        cls.orig = os.path.join(cls.d, 'orig.dmg')
        writer.create_dmg(
            cls.orig,
            [writer.Entry(path='src.txt', size=os.path.getsize(src), src=src)],
            'MetaVol', compressed=True)
        cls.expanded = os.path.join(cls.d, 'expanded.dmg')
        writer.expand_to_editable_dmg(cls.orig, cls.expanded)
        cls.compressed = os.path.join(cls.d, 'compressed.dmg')
        writer.compress_to_dmg(cls.expanded, cls.compressed)

    def _check(self, path):
        koly, pl, blkx, mish = tail_meta(path)
        rf = pl['resource-fork']
        self.assertIn('plst', rf, '必须保留 plst 分区资源')
        self.assertEqual(len(rf['plst']), 1)
        self.assertNotIn('CFName', blkx, 'CFName 会让第三方 plist 解析器错位')
        self.assertEqual(blkx['Name'], STD_NAME)
        self.assertEqual(blkx['ID'], '0')
        self.assertEqual(blkx['Attributes'], '0x0050')
        self.assertEqual(struct.unpack_from('>II', koly, 0x50), (0, 0),
                         'koly 不应给出全 0 的 data fork 校验和')
        self.assertEqual(struct.unpack_from('>II', mish, 0x40), (2, 32),
                         'mish 校验和必须是 Apple 的 CRC32 类型')
        self.assertNotEqual(struct.unpack_from('>I', mish, 0x48)[0], 0,
                            'CRC32 不能是 0')
        self.assertEqual(struct.unpack_from('>I', mish, 0x20)[0], 520,
                         '解压缓冲请求须与 256 KiB 块匹配')
        n = struct.unpack_from('>I', mish, 0xC8)[0]
        self.assertGreater(n, 1)
        sector_count = struct.unpack_from('>Q', mish, 0x10)[0]
        data_len = struct.unpack_from('>Q', koly, 0x20)[0]
        all_runs = [struct.unpack_from('>IIQQQQ', mish, 204 + 40 * i)
                    for i in range(n)]
        data_runs = [r for r in all_runs
                     if r[0] in (writer.BLOCK_RAW, writer.BLOCK_ZLIB)]
        self.assertTrue(data_runs)
        for _typ, _comment, _sec, cnt, _off, _ln in data_runs[:4]:
            # 合成卷只有一块且不足 256 KiB；真实卷的每块都是 512 扇区
            self.assertTrue(0 < cnt <= 512)
        term = all_runs[-1]
        self.assertEqual(term[0], writer.BLOCK_TERM)
        self.assertEqual(term[2], sector_count,
                         'TERM 必须收在总扇区数上，全 0 会被判 CORRUPTED')
        self.assertEqual(term[4], data_len,
                         'TERM 必须收在数据叉长度上')
        # Apple 尾部必须有独立的二进制资源叉，且内容与 XML plist 同源
        k2, xml, rsrc = read_tail(path)
        self.assertEqual(k2, koly)
        ro, rl = struct.unpack_from('>QQ', koly, 0x28)
        self.assertGreater(ro, 0)
        self.assertGreaterEqual(rl, 256)
        self.assertEqual(len(rsrc), rl)
        self.assertEqual(ro + rl + 512, os.path.getsize(path))
        data_off, map_off, data_len, map_len = struct.unpack_from(
            '>IIII', rsrc, 0)
        self.assertEqual(data_off, 256)
        self.assertEqual(map_off + map_len, len(rsrc))
        self.assertEqual(map_off, 256 + data_len)
        p = 256
        for key in ('blkx', 'plst'):
            ln = struct.unpack_from('>I', rsrc, p)[0]
            self.assertEqual(rsrc[p + 4:p + 4 + ln], rf[key][0]['Data'])
            p += 4 + ln
        self.assertEqual(p, map_off)
        # Apple CFPropertyList 用 CR 行尾与旧 DTD 名
        self.assertIn(b'-//Apple Computer//DTD PLIST 1.0//EN', xml)
        self.assertNotIn(b'\n', xml)

    def test_create_uses_standard_metadata(self):
        self._check(self.orig)

    def test_expand_uses_standard_metadata(self):
        self._check(self.expanded)

    def test_compress_uses_standard_metadata(self):
        self._check(self.compressed)

    def test_template_is_reusable_from_our_output(self):
        tpl = writer.read_udif_template(self.expanded)
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl['resource_fork']['blkx'][0]['Name'], STD_NAME)
        self.assertIn('plst', tpl['resource_fork'])
        self.assertEqual(tpl['koly'][:4], b'koly')

    def test_comment_runs_reused_from_source_template(self):
        """源 DMG 首尾的 COMMENT run 与闭合的 TERM run 必须原样沿用。"""
        head = bytearray(204)
        head[:4] = b'mish'
        struct.pack_into('>Q', head, 0x10, 8)
        struct.pack_into('>I', head, 200, 4)
        src_runs = [
            (writer.BLOCK_COMMENT, 111, 0, 0, 0, 0),
            (writer.BLOCK_RAW, 0, 8, 0, 4096, 4096),
            (writer.BLOCK_COMMENT, 222, 8, 0, 4096, 0),
            (writer.BLOCK_TERM, 0, 8, 0, 4096, 0),
        ]
        data = bytes(head) + b''.join(writer.RUN.pack(*r) for r in src_runs)
        tpl = {
            'resource_fork': {
                'blkx': [{'Attributes': '0x0050', 'Data': data,
                          'ID': '0', 'Name': STD_NAME}],
                'plst': [{'Attributes': '0x0050', 'Data': bytes(1032),
                          'ID': '0', 'Name': ''}],
            },
            'koly': bytes(512),
        }
        xml = writer._build_xml(
            [(writer.BLOCK_RAW, 0, 8, 0, 4096)], 8,
            tpl, 0x12345678, 4096)
        mish = plistlib.loads(xml)['resource-fork']['blkx'][0]['Data']
        n = struct.unpack_from('>I', mish, 0xC8)[0]
        out = [struct.unpack_from('>IIQQQQ', mish, 204 + 40 * i)
               for i in range(n)]
        self.assertEqual(n, 4)
        self.assertEqual((out[0][0], out[0][1]),
                         (writer.BLOCK_COMMENT, 111))
        self.assertEqual((out[-2][0], out[-2][1], out[-2][2], out[-2][4]),
                         (writer.BLOCK_COMMENT, 222, 8, 4096))
        self.assertEqual((out[-1][0], out[-1][2], out[-1][4]),
                         (writer.BLOCK_TERM, 8, 4096))

    def test_template_koly_writes_real_resource_fork(self):
        """koly 的 RsrcFork 必须指向真实写出的资源叉，过期校验和须清零。"""
        koly = bytearray(512)
        koly[:4] = b'koly'
        struct.pack_into('>QQ', koly, 0x28, 999999999, 404197)
        koly[0x160:0x164] = b'\x00\x00\x00\x02'
        koly[0x164:0x168] = b'\x00\x00\x00\x20'
        koly[0x168:0x16C] = b'\xde\xad\xbe\xef'
        out = writer._koly(4096, 4096, 600, 8, {'koly': bytes(koly)},
                           4096 + 600, 404197)
        self.assertEqual(struct.unpack_from('>QQ', out, 0x28),
                         (4696, 404197))
        self.assertEqual(out[0x160:0x1E8], bytes(0x1E8 - 0x160))
        self.assertEqual(struct.unpack_from('>Q', out, 0x20), (4096,))
        self.assertEqual(struct.unpack_from('>QQ', out, 0xD8), (4096, 600))
        keep = writer._koly(4096, 4096, 600, 8, {'koly': bytes(koly)},
                            4696, 404197, bytes(koly[0x160:0x1E8]))
        self.assertEqual(keep[0x160:0x1E8], bytes(koly[0x160:0x1E8]))

    def test_roundtrip_is_byte_identical(self):
        """create → eXpand → Compress 必须逐字节回到原始 DMG。"""
        with open(self.orig, 'rb') as f:
            before = f.read()
        with open(self.compressed, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_apple_plist_serialization_and_zlib_level(self):
        """Apple 的 CR 行尾 / 旧 DTD 名与 level 7 都要落在产物里。"""
        self.assertEqual(writer.UDIF_ZLIB_LEVEL, 7)
        for path in (self.orig, self.expanded, self.compressed):
            _k, xml, _r = read_tail(path)
            self.assertIn(b'-//Apple Computer//DTD PLIST 1.0//EN', xml)
            self.assertNotIn(b'\n', xml)
        # 每个 ZLIB 段都必须能被 level 7 原样重现（level 6 会多几字节）
        koly, _pl, _blkx, mish = tail_meta(self.orig)
        n = struct.unpack_from('>I', mish, 0xC8)[0]
        import zlib
        with open(self.orig, 'rb') as f:
            for i in range(n):
                typ, _c, _s, _cnt, off, ln = struct.unpack_from(
                    '>IIQQQQ', mish, 204 + 40 * i)
                if typ != writer.BLOCK_ZLIB:
                    continue
                f.seek(off)
                packed = f.read(ln)
                self.assertEqual(zlib.compress(zlib.decompress(packed), 7),
                                 packed)

    def test_resource_fork_generator_matches_written_bytes(self):
        for path in (self.orig, self.expanded, self.compressed):
            _k, xml, rsrc = read_tail(path)
            rf = plistlib.loads(xml)['resource-fork']
            self.assertEqual(writer._build_resource_fork(rf), rsrc)

    def test_master_checksum_reused_only_when_disk_unchanged(self):
        tpl = writer.read_udif_template(self.orig)
        src_crc = struct.unpack_from(
            '>I', tpl['resource_fork']['blkx'][0]['Data'], 0x48)[0]
        self.assertEqual(writer._master_checksum(tpl, src_crc),
                         bytes(tpl['koly'][0x160:0x1E8]))
        self.assertIsNone(writer._master_checksum(tpl, src_crc ^ 1))
        self.assertIsNone(writer._master_checksum(None, src_crc))

    def test_template_not_found_for_raw_image(self):
        raw = os.path.join(self.d, 'plain.img')
        with open(raw, 'wb') as f:
            f.write(b'\0' * 4096)
        self.assertIsNone(writer.read_udif_template(raw))

    def test_all_three_read_back_identical(self):
        with Disk(self.orig) as d:
            expect = d.reader.read(0, 64 * 1024)
        for p in (self.expanded, self.compressed):
            with Disk(p) as d:
                self.assertEqual(d.reader.read(0, 64 * 1024), expect)
            self.assertEqual(tail_meta(p)[2]['Name'], STD_NAME)


class TestDataForkChecksum(unittest.TestCase):
    """回归：源镜像带 type=2 数据叉校验和时，重排后 CRC 必须跟着更新。

    真实 Apple DMG 的 koly 会给出 type=2 + 数据叉 CRC32；以前 eXpand /
    Compress 直接沿用源值，数据叉一旦重排就成了过期 CRC，macOS 挂载时会
    拿这个不匹配的值拒绝打开。
    """

    @staticmethod
    def _typed(base, out):
        with open(base, 'rb') as f:
            data = f.read()
        k = bytearray(data[-512:])
        dl = struct.unpack_from('>Q', k, 0x20)[0]
        crc = zlib.crc32(data[:dl]) & 0xFFFFFFFF
        k[0x50:0xD8] = bytes(0xD8 - 0x50)
        struct.pack_into('>II', k, 0x50, 2, 32)
        struct.pack_into('>I', k, 0x58, crc)
        with open(out, 'wb') as f:
            f.write(data[:-512] + bytes(k))
        return crc

    @staticmethod
    def _crc(path):
        with open(path, 'rb') as f:
            data = f.read()
        k = data[-512:]
        dl = struct.unpack_from('>Q', k, 0x20)[0]
        return (struct.unpack_from('>II', k, 0x50),
                struct.unpack_from('>I', k, 0x58)[0],
                zlib.crc32(data[:dl]) & 0xFFFFFFFF,
                struct.unpack_from('>II', k, 0x160))

    def test_crc_matches_real_data_fork_after_roundtrip(self):
        d = tempfile.mkdtemp(prefix='omd_dfcrc_')
        payload = os.path.join(d, 'payload.bin')
        with open(payload, 'wb') as f:
            f.write(b'CHECKSUM-REGRESSION-' * 6000)
        base = os.path.join(d, 'base.dmg')
        writer.create_dmg(
            base,
            [writer.Entry(path='payload.bin',
                          size=os.path.getsize(payload), src=payload)],
            'CRCVOL', compressed=True)
        typed = os.path.join(d, 'typed.dmg')
        src_crc = self._typed(base, typed)
        self.assertEqual(self._crc(typed)[1], src_crc)

        expanded = os.path.join(d, 'expanded.dmg')
        writer.expand_to_editable_dmg(typed, expanded)
        compressed = os.path.join(d, 'compressed.dmg')
        writer.compress_to_dmg(expanded, compressed)

        for p in (expanded, compressed):
            typ, stored, actual, main = self._crc(p)
            self.assertEqual(typ, (2, 32), 'type=2 的源必须继续提供校验和')
            self.assertEqual(stored, actual,
                             '数据叉 CRC32 必须与实际写出的数据一致')
            self.assertEqual(main, (0, 0),
                             'koly 变了以后源主校验和必须清零')
        self.assertNotEqual(self._crc(expanded)[1], src_crc,
                            'eXpand 后数据叉已重排，CRC 不应还是源值')
        with open(typed, 'rb') as f:
            before = f.read()
        with open(compressed, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after, '无编辑往返仍须逐字节一致')


if __name__ == '__main__':
    unittest.main()
