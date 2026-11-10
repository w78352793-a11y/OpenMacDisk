# -*- mode: python ; coding: utf-8 -*-
# 可移植构建配置：所有资源路径相对本 spec 所在目录，不含任何本机绝对路径。
# 用法（在项目根目录执行）：pyinstaller _build/OpenMacDisk.spec --noconfirm --clean
import os

_BUILD = os.path.abspath(SPECPATH)          # 本 spec 所在目录（_build/）
_PROJ = os.path.dirname(_BUILD)             # 项目根目录

a = Analysis(
    [os.path.join(_PROJ, 'desktop.py')],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(_BUILD, 'icon.ico'), '_build'),
        (os.path.join(_BUILD, 'splash.png'), '_build'),
        (os.path.join(_PROJ, 'dmg_file.ico'), '_build'),
    ],
    hiddenimports=['openmacdisk.image', 'openmacdisk.udif', 'openmacdisk.partition', 'openmacdisk.hfs', 'openmacdisk.icons', 'openmacdisk.writer', 'openmacdisk.inplace', 'openmacdisk.shell', 'openmacdisk.apfs', 'openmacdisk.crypto_dmg', 'openmacdisk.hfs_extents', 'openmacdisk.lzfse', 'openmacdisk.config', 'openmacdisk.i18n', 'openmacdisk.appearance', 'Crypto.Cipher.AES', 'Crypto.Cipher.DES3'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OpenMacDisk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(_BUILD, 'version_info.txt'),
    icon=[os.path.join(_BUILD, 'icon.ico')],
)
