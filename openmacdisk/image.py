# -*- coding: utf-8 -*-
"""统一镜像抽象：UDIF/DMG、加密 DMG、裸磁盘镜像、Windows 物理磁盘。

对外只暴露 read(offset, length) 与 size，使分区/文件系统层完全解耦。
卷层支持 HFS+/HFSX 与 APFS（未加密、无快照），并可打开加密 UDIF。
"""
from __future__ import annotations

import os
import struct

from .udif import UDIFImage, DMGError
from .partition import parse as parse_partitions
from .hfs import HFSVolume, HFSError

SECTOR = 512


class RawImage:
    """裸镜像 / ISO / 展开后的 DMG。"""

    def __init__(self, path: str):
        import threading
        self.path = path
        self._io_lock = threading.RLock()
        self.fh = open(path, 'rb')
        self._size = os.path.getsize(path)

    @property
    def size(self):
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        with self._io_lock:
            if offset >= self._size or length <= 0:
                return b''
            self.fh.seek(offset)
            return self.fh.read(min(length, self._size - offset))

    def close(self):
        if not self.fh.closed:
            self.fh.close()


class PhysicalDrive:
    """Windows 物理磁盘（需管理员权限）。路径形如 \\\\.\\PhysicalDrive1。"""

    def __init__(self, index: int):
        import threading
        self.path = r'\\.\PhysicalDrive%d' % index
        self.index = index
        self._io_lock = threading.RLock()
        self.fh = open(self.path, 'rb', buffering=0)
        self._size = 0
        try:
            import ctypes
            from ctypes import wintypes
            h = ctypes.windll.kernel32.CreateFileW(
                self.path, 0, 3, None, 3, 0, None)
            if h and h != -1:
                disk_geometry = ctypes.create_string_buffer(24)
                returned = wintypes.DWORD()
                ok = ctypes.windll.kernel32.DeviceIoControl(
                    h, 0x00070000, None, 0, disk_geometry, 24,
                    ctypes.byref(returned), None)
                if ok:
                    cyl, media, tpc, spt, bps = struct.unpack_from(
                        '<QIIII', disk_geometry)
                    self._size = cyl * tpc * spt * bps
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
        try:
            from .shell import list_devices
            for d in list_devices():
                if d['index'] == index and d.get('size'):
                    self._size = d['size']
                    break
        except Exception:
            pass
        if not self._size:
            self._size = 2 * 1024 ** 4  # 无法探测时的保守上限

    @property
    def size(self):
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        with self._io_lock:
            try:
                self.fh.seek(offset & ~511)  # 原始设备要求扇区对齐
                skip = offset & 511
                data = self.fh.read(length + skip)
                return data[skip:skip + length]
            except OSError:
                return b''

    def close(self):
        if not self.fh.closed:
            self.fh.close()


def is_device_path(path: str) -> bool:
    return path.lower().replace('/', '\\').startswith('\\\\.\\physicaldrive')


def device_index(path: str) -> int:
    return int(path.lower().split('physicaldrive')[-1].strip('\\/'))


def open_image(path: str, password: str | None = None):
    """自动识别加密 DMG / UDIF / 裸镜像。返回 (reader, kind)。"""
    if is_device_path(path):
        return PhysicalDrive(device_index(path)), 'DEVICE'
    from .crypto_dmg import is_encrypted
    if is_encrypted(path):
        if password is None:
            raise DMGError('该 DMG 已加密，需要输入密码')
        from .crypto_dmg import EncryptedUDIF
        return EncryptedUDIF(path, password), 'UDIF-ENC'
    with open(path, 'rb') as f:
        f.seek(max(0, os.path.getsize(path) - 512))
        tail = f.read(512)
    if tail[:4] == b'koly':
        return UDIFImage(path), 'UDIF'
    return RawImage(path), 'RAW'


class VolumeInfo(dict):
    """分区 + 卷的描述。"""


class Disk:
    """打开一个镜像并枚举其中的 Mac 卷。"""

    def __init__(self, path: str, password: str | None = None,
                 reader=None):
        self.path = path
        self.password = password
        if reader is not None:
            self.reader, self.kind = reader, 'DEVICE'
        else:
            self.reader, self.kind = open_image(path, password)
        self.file_size = (os.path.getsize(path)
                          if os.path.isfile(path) else self.reader.size)
        self.logical_size = getattr(self.reader, 'logical_size',
                                    self.reader.size)
        self.partitions = parse_partitions(self.reader)
        self.volumes: list[dict] = []
        self.errors: list[str] = []
        self._detect_volumes()

    def _detect_volumes(self):
        if not self.partitions:
            self._try_volume(0, 'whole disk', 'none')
            # 整盘无分区表且不是 APFS/HFS 时，再试常见分区起点
            if not self.volumes:
                for base in (0x100000, 0x1000):
                    self._try_volume(base, 'offset %d' % base, 'raw')
            return
        for p in self.partitions:
            t = p['type']
            skip = ('partition_map' in t.lower() or t == 'Apple_Free'
                    or t in ('GPT Protective', 'Microsoft Basic Data', 'NTFS',
                             'FAT32', 'Linux') or t.startswith('0x'))
            if not skip:
                self._try_volume(p['start'], p['name'] or t, t, p)

    def _try_volume(self, base, name, ptype, part=None):
        scheme = part['scheme'] if part else 'none'
        magic = self.reader.read(base + 0x20, 4)
        if magic == b'NXSB' or 'APFS' in ptype.upper():
            try:
                from .apfs import APFSContainer
                cont = APFSContainer(self.reader, base)
                for v in cont.volumes:
                    self.volumes.append(VolumeInfo(
                        name=v.volume_name or name, type=ptype,
                        scheme=scheme, base=base, fs='APFS',
                        volume=v, summary=v.summary(), partition=part))
                if not cont.volumes:
                    self.errors.append('%s: APFS 容器中没有可读取的卷' % name)
            except Exception as e:
                self.errors.append('%s: APFS %s' % (name, e))
            return
        try:
            vol = HFSVolume(self.reader, base)
            self.volumes.append(VolumeInfo(
                name=name, type=ptype, scheme=scheme,
                base=base, fs='HFS+' if vol.signature == 'H+' else 'HFSX',
                volume=vol, summary=vol.summary(),
                partition=part))
        except HFSError as e:
            if part and part['type'].startswith('Apple_'):
                self.errors.append('%s: %s' % (name, e))

    def close(self):
        self.reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def summary(self) -> dict:
        out = {
            'path': self.path, 'format': self.kind,
            'file_size': self.file_size, 'logical_size': self.logical_size,
            'partitions': [{k: v for k, v in p.items() if k != 'volume'}
                           for p in self.partitions],
            'volumes': [], 'errors': self.errors,
        }
        if self.kind.startswith('UDIF') and hasattr(self.reader, 'summary'):
            s = self.reader.summary()
            keys = ('version', 'flags', 'segment', 'data_fork',
                    'xml_length', 'runs', 'run_types', 'blkx')
            out['udif'] = {k: s[k] for k in keys if k in s}
        for v in self.volumes:
            d = {'name': v['name'], 'fs': v['fs'], 'scheme': v['scheme']}
            d.update({k: val for k, val in v['summary'].items()
                      if k not in ('name', 'fs', 'scheme')})
            out['volumes'].append(d)
        return out

    @property
    def first_volume(self):
        if not self.volumes:
            raise HFSError('未发现可读取的 HFS+/HFSX/APFS 卷'
                           + ('；' + '；'.join(self.errors) if self.errors else ''))
        return self.volumes[0]['volume']

    @property
    def volume_names(self):
        return [(v['fs'], v['name']) for v in self.volumes]
