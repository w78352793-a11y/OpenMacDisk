# -*- coding: utf-8 -*-
"""分区表解析：DDM / Apple Partition Map / GPT / MBR。

所有输入都是「逻辑磁盘」视图（实现了 read(offset,length) 的对象）。
"""
from __future__ import annotations

import struct

SECTOR = 512

# GPT 分区类型 GUID（混合字节序，按字节比较）
GUID_APPLE_HFS = b'\x00\x53\x46\x48\x00\x00\xaa\x11\xaa\x11\x00\x30\x65\x43\xec\xac'
GUID_APPLE_APFS = b'\xef\x57\x34\x7c\x00\x00\xaa\x11\xaa\x11\x00\x30\x65\x43\xec\xac'
GUID_BASIC = b'\xa2\xa0\xd0\xeb\xe5\xb9\x33\x44\x87\xc0\x68\xb6\xb7\x26\x99\xc7'


class Partition(dict):
    """index / name / type / start / length / scheme。start、length 为字节。"""


def _pm_type_name(raw: bytes) -> str:
    return raw.split(b'\x00', 1)[0].decode('latin1', 'replace')


def parse(reader) -> list[Partition]:
    """返回分区列表；若无分区表则返回空列表（调用方按整盘卷处理）。"""
    first = reader.read(0, 16 * SECTOR)
    if len(first) < SECTOR:
        return []

    # --- Apple Partition Map（DDM 签名 ER）---
    if first[:2] == b'ER':
        out = []
        # APM 条目自 1 号扇区开始，每个 512 字节
        for i in range(1, 64):
            blk = reader.read(i * SECTOR, SECTOR)
            if len(blk) < SECTOR or blk[:2] != b'PM':
                break
            map_count = struct.unpack_from('>I', blk, 4)[0]
            start, count = struct.unpack_from('>II', blk, 8)
            name = _pm_type_name(blk[16:48])
            ptype = _pm_type_name(blk[48:80])
            out.append(Partition(index=i - 1, name=name, type=ptype,
                                 start=start * SECTOR, length=count * SECTOR,
                                 scheme='APM'))
            if i >= map_count:
                break
        return out

    # --- GPT（保护性 MBR + EFI PART）---
    # 注意：GPT 规范中所有数值字段一律为**小端**（UEFI 2.x §5.3），
    # 之前误用大端读取导致分区表起点 / 条目数全部错位，GPT 镜像打不开。
    if len(first) >= 2 * SECTOR and first[SECTOR:SECTOR + 8] == b'EFI PART':
        hdr = first[SECTOR:SECTOR + 92]
        part_lba, part_count, entry_size = struct.unpack_from('<QII', hdr, 72)
        out = []
        table = reader.read(part_lba * SECTOR, part_count * entry_size)
        for i in range(min(part_count, 128)):
            e = table[i * entry_size:(i + 1) * entry_size]
            if len(e) < entry_size:
                break
            guid = e[:16]
            if guid == b'\x00' * 16:
                continue
            first_lba, last_lba = struct.unpack_from('<QQ', e, 32)
            name = e[56:128].decode('utf-16-le', 'replace').split('\x00', 1)[0]
            if guid == GUID_APPLE_HFS:
                ptype = 'Apple_HFS'
            elif guid == GUID_APPLE_APFS:
                ptype = 'Apple_APFS'
            elif guid == GUID_BASIC:
                ptype = 'Microsoft Basic Data'
            else:
                ptype = 'GPT-' + guid.hex()[:8]
            out.append(Partition(index=i, name=name, type=ptype,
                                 start=first_lba * SECTOR,
                                 length=(last_lba - first_lba + 1) * SECTOR,
                                 scheme='GPT'))
        return out

    # --- MBR ---
    if first[510:512] == b'\x55\xaa':
        out = []
        for i in range(4):
            e = first[446 + i * 16:462 + i * 16]
            ptype_byte, start, count = e[4], *struct.unpack_from('<II', e, 8)
            if ptype_byte == 0 or count == 0:
                continue
            names = {0xAF: 'Apple_HFS', 0xEE: 'GPT Protective', 0x07: 'NTFS',
                     0x0B: 'FAT32', 0x83: 'Linux'}
            out.append(Partition(index=i, name='partition %d' % (i + 1),
                                 type=names.get(ptype_byte, hex(ptype_byte)),
                                 start=start * SECTOR, length=count * SECTOR,
                                 scheme='MBR'))
        return out

    return []  # 无分区表：whole-disk 卷
