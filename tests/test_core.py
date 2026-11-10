# -*- coding: utf-8 -*-
"""不依赖真实 DMG 的合成样本单元测试：UDIF/mish、分区表、HFS+ fork。"""
import bz2
import os
import plistlib
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from openmacdisk.udif import UDIFImage, _adc_decompress, _MISH, _RUN  # noqa: E402
from openmacdisk import partition  # noqa: E402
from openmacdisk.hfs import ForkData  # noqa: E402

SECTOR = 512


class MemReader:
    def __init__(self, data):
        self.data = data
        self.size = len(data)

    def read(self, off, length):
        return self.data[off:off + length]


def make_mish(runs, sector_number=0, data_start=0):
    """按 UDIF blkx 规范构造 mish 二进制。"""
    body = bytearray(204)
    _MISH.pack_into(body, 0, b'mish', 1, sector_number,
                    sum(r[2] for r in runs), data_start, 1, 0)
    struct.pack_into('>I', body, 200, len(runs) + 1)
    for typ, sector, count, coff, clen in runs:
        body += _RUN.pack(typ, 0, sector, count, coff, clen)
    body += _RUN.pack(0xFFFFFFFF, 0, 0, 0, 0, 0)
    return bytes(body)


def make_dmg(chunks, runs, sector_number=0):
    """chunks: 依次拼接的数据叉字节；runs: (type, sector, count, offset, length)。"""
    fork = b''.join(chunks)
    mish = make_mish(runs, sector_number=sector_number)
    pl = {'resource-fork': {'blkx': [
        {'Name': 'test', 'CFName': 'test', 'ID': '0', 'Attributes': '0x0050',
         'Data': mish}]}}
    xml = plistlib.dumps(pl, fmt=plistlib.FMT_XML)
    trailer = bytearray(512)
    struct.pack_into('>4sIIIQQQQQII16s', trailer, 0, b'koly', 4, 512, 1,
                     0, 0, len(fork), 0, 0, 1, 1, b'\x00' * 16)
    struct.pack_into('>QQ', trailer, 0xD8, len(fork), len(xml))
    with tempfile.NamedTemporaryFile(delete=False, suffix='.dmg') as f:
        f.write(fork + xml + trailer)
        return f.name


class TestUDIF(unittest.TestCase):
    def test_raw_and_zero_runs(self):
        a = b'A' * SECTOR
        b = b'B' * SECTOR
        runs = [(0x00000001, 0, 1, 0, len(a)),
                (0x00000000, 1, 1, 0, 0),
                (0x80000005, 2, 1, len(a), 0)]  # 替换为 zlib 段
        c = b'C' * SECTOR
        z = zlib.compress(c)
        runs[2] = (0x80000005, 2, 1, len(a), len(z))
        path = make_dmg([a, z], runs)
        try:
            with UDIFImage(path) as d:
                self.assertEqual(len(d.runs), 3)
                self.assertEqual(d.logical_size, 3 * SECTOR)
                self.assertEqual(d.read(0, SECTOR), a)
                self.assertEqual(d.read(SECTOR, SECTOR), b'\x00' * SECTOR)
                self.assertEqual(d.read(2 * SECTOR, SECTOR), c)
                self.assertEqual(d.read(SECTOR - 4, 8), b'A' * 4 + b'\x00' * 4)
                self.assertEqual(d.read(2 * SECTOR - 4, 8),
                                 b'\x00' * 4 + b'C' * 4)
                self.assertEqual(d.read(3 * SECTOR, 10), b'')
                s = d.summary()
                self.assertEqual(s['run_types'], {'raw': 1, 'zero': 1, 'zlib': 1})
        finally:
            os.unlink(path)

    def test_bzip2_run(self):
        data = b'Z' * SECTOR
        z = bz2.compress(data)
        path = make_dmg([z], [(0x80000006, 0, 1, 0, len(z))])
        try:
            with UDIFImage(path) as d:
                self.assertEqual(d.read(0, SECTOR), data)
        finally:
            os.unlink(path)

    def test_multi_blkx_sector_base(self):
        # mish 起始扇区 64，run 内扇区 0 → 整盘扇区 64
        data = b'Q' * SECTOR
        path = make_dmg([data], [(0x00000001, 0, 1, 0, len(data))],
                        sector_number=64)
        try:
            with UDIFImage(path) as d:
                self.assertEqual(d.runs[0].sector, 64)
                self.assertEqual(d.read(63 * SECTOR, SECTOR), b'\x00' * SECTOR)
                self.assertEqual(d.read(64 * SECTOR, SECTOR), data)
        finally:
            os.unlink(path)

    def test_adc_decompress(self):
        # 字面量 3 字节 + 回指
        src = bytes([2]) + b'abc' + bytes([0x80, 0x00, 0x03, ord('x')])
        out = _adc_decompress(src, 3 + 3)
        self.assertEqual(out[:3], b'abc')

    def test_not_udif(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dmg',
                                         mode='wb') as f:
            f.write(b'\x00' * 1024)
            p = f.name
        try:
            from openmacdisk.udif import DMGError
            with self.assertRaises(DMGError):
                UDIFImage(p)
        finally:
            os.unlink(p)


class TestPartition(unittest.TestCase):
    def test_apm(self):
        buf = bytearray(16 * SECTOR)
        buf[0:2] = b'ER'
        for i, (name, typ, start, count) in enumerate([
                (b'Apple', b'Apple_partition_map', 1, 63),
                (b'disk image', b'Apple_HFS', 64, 1000),
                (b'', b'Apple_Free', 1064, 10)], start=1):
            blk = bytearray(SECTOR)
            struct.pack_into('>2sHIII', blk, 0, b'PM', 0, 3, start, count)
            blk[16:16 + len(name)] = name
            blk[48:48 + len(typ)] = typ
            buf[i * SECTOR:(i + 1) * SECTOR] = blk
        ps = partition.parse(MemReader(bytes(buf)))
        self.assertEqual(len(ps), 3)
        self.assertEqual(ps[1]['type'], 'Apple_HFS')
        self.assertEqual(ps[1]['start'], 64 * SECTOR)
        self.assertEqual(ps[1]['length'], 1000 * SECTOR)

    def test_no_partition_table(self):
        self.assertEqual(partition.parse(MemReader(b'\x00' * 16 * SECTOR)), [])

    def test_gpt_little_endian(self):
        """GPT 规范所有数值字段一律小端（UEFI 2.x §5.3）。回归：曾误用大端
        读取 part_lba/count/entry_size 与 first/last_lba，导致 GPT+APFS 的
        DMG 分区表完全解析不出来（真实 Lightroom DMG 即此结构）。"""
        APFS_GUID = bytes.fromhex('ef57347c0000aa11aa1100306543ecac')
        buf = bytearray(16 * SECTOR)
        buf[510:512] = b'\x55\xaa'                 # 保护性 MBR
        buf[446 + 4] = 0xEE
        # GPT 头（LBA1）：分区表在 LBA2，2 项，每项 128 字节
        buf[SECTOR:SECTOR + 8] = b'EFI PART'
        struct.pack_into('<QII', buf, SECTOR + 72, 2, 2, 128)   # 小端
        # 分区项 0：Apple_APFS，LBA 40960..(40960+10922224-1)
        e0 = 2 * SECTOR
        buf[e0:e0 + 16] = APFS_GUID
        struct.pack_into('<QQ', buf, e0 + 32, 40960, 40960 + 10922224 - 1)
        nm = 'disk image'.encode('utf-16-le')
        buf[e0 + 56:e0 + 56 + len(nm)] = nm
        ps = partition.parse(MemReader(bytes(buf)))
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]['type'], 'Apple_APFS')
        self.assertEqual(ps[0]['start'], 40960 * SECTOR)
        self.assertEqual(ps[0]['length'], 10922224 * SECTOR)
        self.assertEqual(ps[0]['name'], 'disk image')


class TestHFSFork(unittest.TestCase):
    def test_fork_parse(self):
        buf = bytearray(80)
        struct.pack_into('>QII', buf, 0, 12345, 4096, 4)
        struct.pack_into('>II', buf, 16, 100, 3)
        struct.pack_into('>II', buf, 24, 200, 1)
        f = ForkData.parse(bytes(buf))
        self.assertEqual(f.size, 12345)
        self.assertEqual(f.blocks, 4)
        self.assertEqual(f.extents, [(100, 3), (200, 1)])


if __name__ == '__main__':
    unittest.main()
