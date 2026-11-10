# -*- coding: utf-8 -*-
"""OpenMacDisk — 开源、纯本地的 Mac 磁盘 / DMG 读取工具。

MIT 许可。不含任何 TransMac / Acute Systems 的代码、图标、界面素材或许可逻辑。
实现依据为公开的文件系统格式说明（UDIF、APM/GPT、HFS+ / TN1150）。
"""
__version__ = '1.0.0'

from .udif import UDIFImage, DMGError
from .image import Disk, RawImage, PhysicalDrive, open_image
from .hfs import HFSVolume, HFSError

__all__ = ['UDIFImage', 'DMGError', 'Disk', 'RawImage', 'PhysicalDrive',
           'open_image', 'HFSVolume', 'HFSError', '__version__']
