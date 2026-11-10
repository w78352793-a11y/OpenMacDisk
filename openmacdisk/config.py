# -*- coding: utf-8 -*-
"""用户设置持久化：语言与外观主题。

配置保存在 %APPDATA%/OpenMacDisk/config.json，键名：
    language : 'zh_CN' | 'en_US'
    theme    : 'light' | 'dark' | 'glass' | 'acrylic'
"""
import json
import os
import sys

APP_NAME = 'OpenMacDisk'

DEFAULTS = {
    'language': 'zh_CN',
    'theme': 'light',
}

_LANG_KEYS = ('zh_CN', 'en_US')
_THEME_KEYS = ('light', 'dark', 'glass', 'acrylic')


def _exe_dir() -> str:
    """可执行文件所在目录；未打包时回退到本模块目录。"""
    if getattr(sys, 'frozen', False) and getattr(sys, 'executable', ''):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _appdata() -> str:
    p = os.path.join(os.environ.get('APPDATA') or
                     os.path.expanduser('~'), APP_NAME)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        p = os.path.join(os.path.expanduser('~'), '.' + APP_NAME)
    return p


def _local_config() -> str:
    return os.path.join(_exe_dir(), 'config.json')


def _load_path() -> str:
    """读取路径：优先 exe 同目录（便携），否则回退 AppData。"""
    local = _local_config()
    if os.path.exists(local):
        return local
    return os.path.join(_appdata(), 'config.json')


def _save_path() -> str:
    """保存路径：固定写到 exe 同目录，保证便携版配置随身。"""
    return _local_config()


def load_config() -> dict:
    """读取配置，缺少或非法字段用默认值补位。"""
    cfg = dict(DEFAULTS)
    try:
        with open(_load_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            if data.get('language') in _LANG_KEYS:
                cfg['language'] = data['language']
            if data.get('theme') in _THEME_KEYS:
                cfg['theme'] = data['theme']
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    """写回配置；任何失败都静默忽略，不影响主流程。"""
    safe = {
        'language': cfg.get('language', DEFAULTS['language'])
        if cfg.get('language') in _LANG_KEYS else DEFAULTS['language'],
        'theme': cfg.get('theme', DEFAULTS['theme'])
        if cfg.get('theme') in _THEME_KEYS else DEFAULTS['theme'],
    }
    try:
        path = _save_path()
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(safe, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
