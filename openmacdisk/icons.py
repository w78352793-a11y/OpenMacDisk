# -*- coding: utf-8 -*-
"""界面图标：直接使用 Windows 系统已注册的文件类型图标。

不再自绘任何图标。``.dmg`` 等扩展名交给 SHGetFileInfoW，取到的就是
资源管理器里显示的同一枚图标（若本机装有 Mac 镜像软件，即显示其
注册的 Apple DMG 图标）；工具栏用 wx.ArtProvider 的系统标准图标。

非 Windows 环境退化为 wx 内置图标。
"""
from __future__ import annotations

import os

import wx

IDX_DISK = 0
IDX_VOLUME = 1
IDX_FOLDER = 2
IDX_FILE = 3
IDX_APP = 4
IDX_DOC = 5
IDX_IMAGE = 6
IDX_MEDIA = 7
IDX_ARCHIVE = 8
IDX_EXEC = 9
IDX_SYSTEM = 10

SHGFI_ICON = 0x000000100
SHGFI_SMALLICON = 0x000000001
SHGFI_USEFILEATTRIBUTES = 0x000000010
FILE_ATTRIBUTE_DIRECTORY = 0x00000010

_BASE_SAMPLES = [
    ('a.dmg', False),          # 0  光盘 / Apple DMG 图标
    ('a.iso', False),          # 1  卷
    ('folder', True),          # 2  文件夹
    ('a.omd_unknown', False),  # 3  普通文件
    ('App.app', True),         # 4  应用程序包
    ('a.pdf', False),          # 5  文档
    ('a.png', False),          # 6  图片
    ('a.mp4', False),          # 7  音视频
    ('a.zip', False),          # 8  压缩包
    ('a.exe', False),          # 9  可执行
    ('a.dll', False),          # 10 系统文件
]

try:
    import ctypes
    from ctypes import wintypes

    class _SHFILEINFO(ctypes.Structure):
        _fields_ = [('hIcon', wintypes.HICON),
                    ('iIcon', ctypes.c_int),
                    ('dwAttributes', wintypes.DWORD),
                    ('szDisplayName', ctypes.c_wchar * 260),
                    ('szTypeName', ctypes.c_wchar * 80)]

    _sfi_size = ctypes.sizeof(_SHFILEINFO)
    _shell32 = ctypes.windll.shell32
    _user32 = ctypes.windll.user32
except Exception:                                        # pragma: no cover
    ctypes = None


def _system_bitmap(sample: str, directory: bool = False) -> wx.Bitmap | None:
    """取系统为某个文件名/扩展名注册的 16x16 图标，转成带 alpha 的位图。"""
    if ctypes is None:
        return None
    try:
        info = _SHFILEINFO()
        attr = FILE_ATTRIBUTE_DIRECTORY if directory else 0x80
        ret = _shell32.SHGetFileInfoW(
            sample, attr, ctypes.byref(info), _sfi_size,
            SHGFI_ICON | SHGFI_SMALLICON | SHGFI_USEFILEATTRIBUTES)
        if not ret or not info.hIcon:
            return None
        icon = wx.Icon()
        if not icon.CreateFromHICON(info.hIcon):
            _user32.DestroyIcon(info.hIcon)
            return None
        bmp = wx.Bitmap()
        ok = bmp.CopyFromIcon(icon)
        _user32.DestroyIcon(info.hIcon)
        if not ok or not bmp.IsOk():
            return None
        if bmp.GetWidth() != 16 or bmp.GetHeight() != 16:
            img = bmp.ConvertToImage()
            img.Rescale(16, 16, wx.IMAGE_QUALITY_HIGH)
            bmp = wx.Bitmap(img)
        return bmp
    except Exception:
        return None


def _fallback_bitmap(kind: str) -> wx.Bitmap:
    art = {
        'disk': wx.ART_CDROM, 'folder': wx.ART_FOLDER,
        'file': wx.ART_NORMAL_FILE, 'exec': wx.ART_EXECUTABLE_FILE,
    }.get(kind, wx.ART_NORMAL_FILE)
    b = wx.ArtProvider.GetBitmap(art, wx.ART_OTHER, (16, 16))
    if b.IsOk():
        return b
    return wx.Bitmap(16, 16)


class IconProvider:
    """按需缓存的系统图标表；每个扩展名都取系统自己的图标。"""

    def __init__(self):
        self.il = wx.ImageList(16, 16, False)
        self._cache: dict[tuple, int] = {}
        for i, (sample, is_dir) in enumerate(_BASE_SAMPLES):
            bmp = _system_bitmap(sample, is_dir) or _fallback_bitmap('file')
            idx = self.il.Add(bmp)
            self._cache[(sample, is_dir)] = idx
            assert idx == i, '基础图标顺序异常'

    def _get(self, sample: str, directory: bool, kind_hint: str) -> int:
        key = (sample, directory)
        if key in self._cache:
            return self._cache[key]
        bmp = (_system_bitmap(sample, directory) or
               _fallback_bitmap(kind_hint))
        idx = self.il.Add(bmp)
        self._cache[key] = idx
        return idx

    def icon_for(self, name: str, kind: str = 'file') -> int:
        low = (name or '').lower()
        if kind == 'folder':
            if low.endswith('.app'):
                return self._get('App.app', True, 'folder')
            if '\x00' in (name or '') or 'private data' in low:
                return self._get('a.dll', False, 'file')
            return IDX_FOLDER
        ext = os.path.splitext(low)[1]
        if not ext or ext == '.':
            return IDX_FILE
        return self._get('a' + ext, False, 'file')


_PROVIDER: IconProvider | None = None


def provider() -> IconProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = IconProvider()
    return _PROVIDER


def build_image_list() -> wx.ImageList:
    return provider().il


def icon_for(name: str, kind: str = 'file') -> int:
    return provider().icon_for(name, kind)


def bitmap_for(name: str, kind: str = 'file') -> wx.Bitmap:
    return provider().il.GetBitmap(icon_for(name, kind))


def art(name: str, size=(16, 16)) -> wx.Bitmap:
    """工具栏 / 菜单图标：wx 提供的系统标准图标。"""
    return wx.ArtProvider.GetBitmap(
        getattr(wx, name, wx.ART_NORMAL_FILE), wx.ART_BUTTON, size)
