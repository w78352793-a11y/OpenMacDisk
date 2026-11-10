# -*- coding: utf-8 -*-
"""UDIF / DMG 解析：koly trailer → XML plist → blkx(mish) 块表 → 按需解压。

只依赖 Python 标准库（struct / plistlib / zlib / bz2 / bisect）。
实现依据为公开描述的 UDIF 容器格式，未使用任何商业软件代码。

设计要点：镜像可达数 GB，绝不整体展开；按扇区范围定位到 run，
只解压被请求的那一段，并用 LRU 缓存最近解压的 run。
"""
from __future__ import annotations

import bisect
import bz2
import plistlib
import struct
import zlib
from collections import OrderedDict

SECTOR = 512
TRAILER_SIZE = 512

# UDIF 块类型（大端 uint32）
BLOCK_ZERO = 0x00000000
BLOCK_RAW = 0x00000001
BLOCK_FREE = 0x00000002
BLOCK_ADC = 0x80000004
BLOCK_ZLIB = 0x80000005
BLOCK_BZLIB = 0x80000006
BLOCK_LZFSE = 0x80000007
BLOCK_LZMA = 0x80000008
BLOCK_COMMENT = 0x7FFFFFFE
BLOCK_TERM = 0xFFFFFFFF

BLOCK_NAMES = {
    BLOCK_ZERO: 'zero', BLOCK_RAW: 'raw', BLOCK_FREE: 'free',
    BLOCK_ADC: 'adc', BLOCK_ZLIB: 'zlib', BLOCK_BZLIB: 'bzip2',
    BLOCK_LZFSE: 'lzfse', BLOCK_LZMA: 'lzma',
    BLOCK_COMMENT: 'comment', BLOCK_TERM: 'term',
}

_KOLY = struct.Struct('>4sIIIQQQQQII16s')
_MISH = struct.Struct('>4sIQQQII')
_RUN = struct.Struct('>IIQQQQ')


class DMGError(Exception):
    pass


class Run:
    __slots__ = ('type', 'sector', 'count', 'offset', 'length', 'data_start')

    def __init__(self, typ, sector, count, offset, length, data_start):
        self.type = typ
        self.sector = sector
        self.count = count
        self.offset = offset
        self.length = length
        self.data_start = data_start

    @property
    def out_size(self):
        return self.count * SECTOR

    @property
    def type_name(self):
        return BLOCK_NAMES.get(self.type, hex(self.type))


def _adc_decompress(src: bytes, expected: int) -> bytes:
    """Apple ADC 压缩：字面量 / 回指两种 opcode。"""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n and len(out) < expected:
        c = src[i]
        i += 1
        if c & 0x80:  # 回指：3 字节一组，长度 = (c & 0x7F) + 3
            run = (c & 0x7F) + 3
            if i + 1 >= n:
                break
            off = (src[i] << 8) | src[i + 1]
            i += 2
            if off == 0:
                b = src[i] if i < n else 0
                i += 1
                out.extend(bytes([b]) * run)
            else:
                start = len(out) - off
                if start < 0:
                    raise DMGError('ADC 回指越界')
                for k in range(run):
                    out.append(out[start + k])
        else:  # 字面量：长度 = c + 1
            run = c + 1
            out.extend(src[i:i + run])
            i += run
    return bytes(out[:expected])


class UDIFImage:
    """一个 UDIF DMG 的逻辑磁盘视图，支持任意 offset/length 随机读取。"""

    def __init__(self, path: str, cache_bytes: int = 32 * 1024 * 1024):
        import threading
        self.path = path
        self._io_lock = threading.RLock()
        self.fh = open(path, 'rb')
        self.size = self.fh.seek(0, 2)
        if self.size < TRAILER_SIZE:
            self.fh.close()
            raise DMGError('文件过小，不是 UDIF 镜像')
        self.fh.seek(self.size - TRAILER_SIZE)
        t = self.fh.read(TRAILER_SIZE)
        if t[:4] != b'koly':
            self.fh.close()
            raise DMGError('尾部无 koly 签名（可能是裸镜像或下载未完成）')
        (sig, self.version, self.header_size, self.flags,
         self.running_fork_off, self.fork_off, self.fork_len,
         self.rsrc_off, self.rsrc_len, self.seg_no, self.seg_count,
         self.seg_id) = _KOLY.unpack_from(t, 0)
        self.xml_off, self.xml_len = struct.unpack_from('>QQ', t, 0xD8)
        if not self.xml_len or self.xml_off + self.xml_len > self.size:
            raise DMGError('XML plist 偏移非法')
        self.fh.seek(self.xml_off)
        self.plist = plistlib.loads(self.fh.read(self.xml_len))

        self.runs: list[Run] = []
        self.blkx: list[dict] = []
        self._parse_blkx()
        self.runs.sort(key=lambda r: r.sector)
        self._starts = [r.sector for r in self.runs]
        self.logical_size = max((r.sector + r.count) for r in self.runs) * SECTOR if self.runs else 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_limit = int(cache_bytes)
        self._cache_bytes = 0

    # ---------- 容器解析 ----------

    def _parse_blkx(self):
        rf = self.plist.get('resource-fork', {})
        for entry in rf.get('blkx', []):
            data = entry.get('Data', b'')
            raw = bytes(data) if isinstance(data, (bytes, bytearray)) else b''
            info = {
                'name': entry.get('CFName') or entry.get('Name', ''),
                'id': entry.get('ID'),
                'attributes': entry.get('Attributes', ''),
                'runs': 0,
            }
            self.blkx.append(info)
            if raw[:4] != b'mish' or len(raw) < 204:
                continue
            (msig, mver, sec_num, sec_count, data_start,
             buffers, descriptors) = _MISH.unpack_from(raw, 0)
            run_count = struct.unpack_from('>I', raw, 200)[0]
            off = 204
            for _ in range(min(run_count, (len(raw) - off) // 40)):
                typ, comment, s_num, s_cnt, c_off, c_len = _RUN.unpack_from(raw, off)
                off += 40
                if typ in (BLOCK_TERM, BLOCK_COMMENT):
                    continue
                if typ in (BLOCK_ZERO, BLOCK_FREE):
                    c_len = 0
                # mish 的 SectorNumber 是该分区段的绝对起始扇区；
                # run 的 sectorNumber 则相对该段，必须相加才是整盘逻辑扇区号。
                self.runs.append(Run(typ, sec_num + s_num, s_cnt,
                                     data_start + c_off, c_len, data_start))
                info['runs'] += 1

    # ---------- 解压 ----------

    def _decode(self, r: Run) -> bytes:
        key = id(r)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        expected = r.out_size
        if r.type in (BLOCK_ZERO, BLOCK_FREE):
            out = b'\x00' * expected
        elif r.type in (BLOCK_RAW, BLOCK_ZLIB, BLOCK_BZLIB, BLOCK_ADC,
                        BLOCK_LZFSE, BLOCK_LZMA):
            self.fh.seek(self.fork_off + r.offset)
            src = self.fh.read(r.length)
            if r.type == BLOCK_RAW:
                out = src
            elif r.type == BLOCK_ZLIB:
                out = zlib.decompress(src)
            elif r.type == BLOCK_BZLIB:
                out = bz2.decompress(src)
            elif r.type == BLOCK_ADC:
                out = _adc_decompress(src, expected)
            elif r.type == BLOCK_LZFSE:
                from . import lzfse
                try:
                    out = lzfse.decompress(src, expected)
                except lzfse.LZFSEError as exc:
                    raise DMGError('LZFSE 压缩段解码失败：%s' % exc) from exc
            else:  # BLOCK_LZMA：UDIF 使用 raw LZMA1，前 5 字节为属性头
                import lzma
                try:
                    out = lzma.decompress(src)
                except Exception:
                    if len(src) < 6:
                        raise DMGError('LZMA 压缩段数据过短')
                    prop = src[0]
                    lc, lp, pb = prop % 9, (prop // 9) % 5, prop // 45
                    dict_size = struct.unpack_from('<I', src, 1)[0]
                    try:
                        out = lzma.decompress(
                            src[5:], format=lzma.FORMAT_RAW,
                            filters=[{'id': lzma.FILTER_LZMA1,
                                      'dict_size': dict_size,
                                      'lc': lc, 'lp': lp, 'pb': pb}])
                    except Exception as exc:
                        raise DMGError('LZMA 压缩段解码失败：%s' % exc) from exc
            if len(out) < expected:  # 某些镜像压缩段不足整扇区
                out += b'\x00' * (expected - len(out))
            elif len(out) > expected:
                out = out[:expected]
        else:
            raise DMGError('未知块类型 %s' % hex(r.type))
        self._cache[key] = out
        self._cache_bytes += len(out)
        self._cache.move_to_end(key)
        while self._cache_bytes > self._cache_limit and self._cache:
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)
        return out

    # ---------- 随机读取 ----------

    def read(self, offset: int, length: int) -> bytes:
        """逻辑磁盘偏移读取（线程安全，供 GUI 后台任务并发使用）。"""
        with self._io_lock:
            return self._read_unlocked(offset, length)

    def _read_unlocked(self, offset: int, length: int) -> bytes:
        """逻辑磁盘偏移读取；越界返回较短结果，与文件对象语义一致。"""
        if length <= 0:
            return b''
        offset = max(0, offset)
        end = min(offset + length, self.logical_size)
        if end <= offset:
            return b''
        out = bytearray()
        pos = offset
        i = bisect.bisect_right(self._starts, pos // SECTOR) - 1
        if i < 0:
            i = 0
        while pos < end and i < len(self.runs):
            r = self.runs[i]
            r_start = r.sector * SECTOR
            r_end = r_start + r.out_size
            if r_end <= pos:
                i += 1
                continue
            if r_start > pos:  # 未映射的空洞
                gap = min(r_start, end) - pos
                out.extend(b'\x00' * gap)
                pos += gap
                continue
            data = self._decode(r)
            local = pos - r_start
            take = min(end - pos, len(data) - local)
            if take <= 0:
                i += 1
                continue
            out.extend(data[local:local + take])
            pos += take
            i += 1
        if len(out) < end - offset:
            out.extend(b'\x00' * (end - offset - len(out)))
        return bytes(out)

    def close(self):
        if not self.fh.closed:
            self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- 摘要 ----------

    def summary(self) -> dict:
        types = {}
        for r in self.runs:
            n = r.type_name
            types[n] = types.get(n, 0) + 1
        return {
            'format': 'UDIF/DMG',
            'file_size': self.size,
            'logical_size': self.logical_size,
            'version': self.version,
            'flags': '0x%X' % self.flags,
            'segment': '%d/%d' % (self.seg_no, self.seg_count),
            'data_fork': self.fork_len,
            'xml_length': self.xml_len,
            'blkx': self.blkx,
            'runs': len(self.runs),
            'run_types': types,
        }
