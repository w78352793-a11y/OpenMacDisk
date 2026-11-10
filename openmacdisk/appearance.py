# -*- coding: utf-8 -*-
"""外观主题：通过 Windows DWM / 用户32 API 实现 透明玻璃 / 磨砂玻璃 / 暗黑。

本模块只负责「窗口合成层」效果（标题栏暗色、Aero 玻璃模糊、亚克力磨砂），
控件本身的颜色由调用方（desktop.py）设置。所有调用都包在 try/except 中，
在不支持 DWM 的系统上静默降级，绝不让主程序崩溃。

主题键：'light' | 'dark' | 'glass' | 'acrylic'
"""
from __future__ import annotations

import ctypes
from ctypes import (POINTER, Structure, byref, c_int, c_uint, c_void_p)

DWM_AVAILABLE = False
USER_AVAILABLE = False
try:
    _dwmapi = ctypes.windll.dwmapi
    _user32 = ctypes.windll.user32
    DWM_AVAILABLE = True
    USER_AVAILABLE = True
except Exception:  # pragma: no cover - 非 Windows 环境
    _dwmapi = None
    _user32 = None


class _BLURBEHIND(Structure):
    _fields_ = [
        ("dwFlags", c_uint),
        ("fEnable", c_int),
        ("hRgnBlur", c_void_p),
        ("fTransitionOnMaximized", c_int),
    ]


class _ACCENT(Structure):
    _fields_ = [
        ("AccentState", c_int),
        ("AccentFlags", c_int),
        ("GradientColor", c_uint),   # 0xAABBGGRR（高字节为 alpha）
        ("AnimationId", c_int),
    ]


class _WINCOMP(Structure):
    _fields_ = [
        ("Attribute", c_int),
        ("Data", POINTER(_ACCENT)),
        ("SizeOfData", c_uint),
    ]


_DWM_BB_ENABLE = 0x1
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_WCA_ACCENT_POLICY = 19
_ACCENT_DISABLED = 0
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

if DWM_AVAILABLE:
    _dwmapi.DwmEnableBlurBehindWindow.argtypes = [c_void_p, POINTER(_BLURBEHIND)]
    _dwmapi.DwmEnableBlurBehindWindow.restype = c_int
    _dwmapi.DwmSetWindowAttribute.argtypes = [c_void_p, c_int, c_void_p, c_int]
    _dwmapi.DwmSetWindowAttribute.restype = c_int
if USER_AVAILABLE:
    _user32.SetWindowCompositionAttribute.argtypes = [c_void_p,
                                                     POINTER(_WINCOMP)]
    _user32.SetWindowCompositionAttribute.restype = c_int


def _blur(hwnd, enable: bool) -> bool:
    if not DWM_AVAILABLE:
        return False
    bb = _BLURBEHIND(_DWM_BB_ENABLE, 1 if enable else 0, 0, 0)
    try:
        _dwmapi.DwmEnableBlurBehindWindow(int(hwnd), byref(bb))
        return True
    except Exception:  # pragma: no cover
        return False


def _dark_title(hwnd, enable: bool) -> bool:
    if not DWM_AVAILABLE:
        return False
    v = c_int(1 if enable else 0)
    try:
        _dwmapi.DwmSetWindowAttribute(
            int(hwnd), _DWMWA_USE_IMMERSIVE_DARK_MODE, byref(v), 4)
        return True
    except Exception:  # pragma: no cover
        return False


def _acrylic(hwnd, enable: bool, tint: int) -> bool:
    if not USER_AVAILABLE:
        return False
    try:
        pol = _ACCENT()
        if enable:
            pol.AccentState = _ACCENT_ENABLE_ACRYLICBLURBEHIND
            pol.AccentFlags = 0
            pol.GradientColor = tint
        else:
            pol.AccentState = _ACCENT_DISABLED
        # 字段类型是 POINTER(_ACCENT)，必须直接传实例（byref() 的 CArgObject
        # 无法赋给结构体字段，会抛 TypeError: expected LP__ACCENT instance）
        data = _WINCOMP(_WCA_ACCENT_POLICY, pol,
                        ctypes.sizeof(_ACCENT))
        _user32.SetWindowCompositionAttribute(int(hwnd), byref(data))
        return True
    except Exception:  # pragma: no cover
        return False


def apply_dwm(hwnd, theme: str) -> None:
    """重置并应用主题对应的窗口合成效果；失败时静默忽略。"""
    # 先清掉上一次可能残留的效果
    _blur(hwnd, False)
    _acrylic(hwnd, False, 0)
    _dark_title(hwnd, False)

    if theme == 'dark':
        _dark_title(hwnd, True)
    elif theme == 'glass':
        _blur(hwnd, True)
    elif theme == 'acrylic':
        # 浅色亚克力 tint：alpha=0xC8，BGR=0xF2F2F2（近白带蓝）
        _acrylic(hwnd, True, 0xC8F2F2F2)
    # light：保持默认，不做额外处理
