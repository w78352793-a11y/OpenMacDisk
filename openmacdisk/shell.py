# -*- coding: utf-8 -*-
"""Windows 资源管理器右键集成、刻录与物理磁盘辅助。

右键菜单全部写在 HKCU\\Software\\Classes 下，不需要管理员权限，
也不会改动 .dmg 的默认打开方式；uninstall() 可完整撤销。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

try:
    import winreg
except ImportError:                                   # pragma: no cover
    winreg = None

APP = 'OpenMacDisk'
IMAGE_EXTS = ('.dmg', '.img', '.iso', '.sparseimage', '.cdr', '.toast')

# .dmg 文件图标：与 exe 同目录的持久 ico（透明背景的 DMG 插画）
DMG_ICON_FILE = 'dmg_file.ico'
DMG_ICON_KEYS = (
    r'Software\Classes\.dmg\DefaultIcon',
    r'Software\Classes\SystemFileAssociations\.dmg\DefaultIcon',
)

# 镜像文件上的右键动作
IMAGE_VERBS = [
    ('OMD_Open',       '用 &OpenMacDisk 打开',        '--open'),
    ('OMD_Extract',    '解压到文件夹…(&X)',           '--extract'),
    ('OMD_ExtractHere','解压到此处(&E)',              '--extract-here'),
    ('OMD_Create',     '压缩为 DMG… (Compress)(&C)',  '--compress'),
    ('OMD_Expand',     'eXpand 为可编辑 DMG…(&R)',    '--expand'),
    ('OMD_Info',       '查看镜像信息(&I)',            '--info'),
    ('OMD_Verify',     '校验镜像完整性…(&V)',         '--verify'),
    ('OMD_Burn',       '烧录到光盘…(&B)',             '--burn'),
]
# 文件夹 / 任意文件上的右键动作
CREATE_VERBS = [
    ('OMD_MakeDMG',    '制作为 DMG 镜像…(&D)',        '--create'),
]


def launch_prefix(exe: str | None = None) -> str:
    """返回可直接拼到命令行前的、已正确加引号的程序前缀。"""
    if exe:
        return '"%s"' % exe
    if getattr(sys, 'frozen', False):
        return '"%s"' % sys.executable
    py = '"%s"' % sys.executable
    script = '"%s"' % os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'desktop.py')
    return py + ' ' + script


def _icon_value() -> str:
    if getattr(sys, 'frozen', False):
        return '"%s",0' % sys.executable
    dll = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                       'System32', 'imageres.dll')
    return '"%s",-102' % dll if os.path.isfile(dll) else ''


def _keys(exe: str | None = None):
    """产出 (注册表路径, 显示文字, 命令行) 列表。"""
    pre = launch_prefix(exe)
    out = []
    for ext in IMAGE_EXTS:
        for verb, label, flag in IMAGE_VERBS:
            base = (r'Software\Classes\SystemFileAssociations\%s\shell\%s'
                    % (ext, verb))
            out.append((base, label, '%s %s "%%1"' % (pre, flag)))
    for root, arg in ((r'Software\Classes\Directory\shell', '%1'),
                      (r'Software\Classes\*\shell', '%1'),
                      (r'Software\Classes\Directory\Background\shell', '%V')):
        for verb, label, flag in CREATE_VERBS:
            base = r'%s\%s' % (root, verb)
            out.append((base, label, '%s %s "%s"' % (pre, flag, arg)))
    return out


def dmg_icon_path(exe: str | None = None) -> str:
    """与 exe 同目录的持久 ico 路径；打包版首次从内嵌资源释放一份。

    DefaultIcon 必须指向一个长期存在的文件，不能指向 PyInstaller 的
    临时解压目录，所以 ico 单独放在 exe 旁边。
    """
    if exe or getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(exe or sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, DMG_ICON_FILE)
    if not os.path.isfile(path) and getattr(sys, 'frozen', False):
        bundled = os.path.join(getattr(sys, '_MEIPASS', ''), '_build',
                               DMG_ICON_FILE)
        if os.path.isfile(bundled):
            try:
                shutil.copyfile(bundled, path)
            except OSError:
                pass
    return path


def _notify_assoc_changed() -> None:
    """通知资源管理器刷新文件关联与图标缓存。"""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


def dmg_icon_registry_keys() -> tuple[str, ...]:
    """要写 DefaultIcon 的 HKCU 键。

    Windows 解析图标的优先级是「ProgID 的 DefaultIcon > 扩展名的
    DefaultIcon > SystemFileAssociations」，所以还要覆盖 .dmg 当前
    ProgID（本机装了 TransMac 时是 MacDiskImageFile）下的图标值；
    只覆盖图标，不改 (Default) 和 shell，打开方式保持原样。
    """
    keys = list(DMG_ICON_KEYS)
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, '.dmg') as k:
            progid, _ = winreg.QueryValueEx(k, '')
        if progid and progid != APP and '\\' not in progid:
            keys.append(r'Software\Classes\%s\DefaultIcon' % progid)
    except OSError:
        pass
    return tuple(keys)


def install_dmg_icon(exe: str | None = None) -> bool:
    """让 .dmg 在资源管理器里显示透明 DMG 图标，不改默认打开方式。"""
    if winreg is None:
        return False
    ico = dmg_icon_path(exe)
    if not os.path.isfile(ico):
        return False
    value = '"%s",0' % ico
    for base in dmg_icon_registry_keys():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, '', 0, winreg.REG_SZ, value)
    _notify_assoc_changed()
    return True


def install(exe: str | None = None) -> int:
    """注册右键菜单，返回写入的项数；exe 可指定要写入的程序绝对路径。"""
    if winreg is None:
        raise RuntimeError('仅支持 Windows')
    icon = ('"%s",0' % exe) if exe else _icon_value()
    n = 0
    for base, label, cmd in _keys(exe):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, '', 0, winreg.REG_SZ, label)
            if icon:
                winreg.SetValueEx(k, 'Icon', 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              base + r'\command') as k:
            winreg.SetValueEx(k, '', 0, winreg.REG_SZ, cmd)
        n += 1
    install_dmg_icon(exe)
    return n


def uninstall() -> int:
    """移除所有由 install() 创建的键，返回删除的项数。"""
    if winreg is None:
        raise RuntimeError('仅支持 Windows')
    n = 0
    for base, _label, _cmd in _keys():
        for sub in (base + r'\command', base):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                n += 1
            except OSError:
                pass
    # 清掉可能残留的空 shell 容器键（忽略失败）
    parents = set()
    for base, _label, _cmd in _keys():
        parents.add(base.rsplit('\\shell\\', 1)[0] + '\\shell')
    for p in parents:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p)
        except OSError:
            pass
    for base in dmg_icon_registry_keys():
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base)
        except OSError:
            pass
    for base in (r'Software\Classes\.dmg',):
        try:                       # 仅当该键已被清空时才删，避免误删他处配置
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base)
        except OSError:
            pass
    _notify_assoc_changed()
    return n


def status() -> bool:
    if winreg is None:
        return False
    base = (r'Software\Classes\SystemFileAssociations\.dmg\shell\%s'
            % IMAGE_VERBS[0][0])
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- 刻录

def isoburn_path() -> str | None:
    p = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                     'System32', 'isoburn.exe')
    return p if os.path.isfile(p) else None


def burn_image(image_path: str, cb=None) -> str:
    """调用 Windows 自带「光盘映像刻录程序」。

    DMG 会先展开为裸镜像再交给刻录器（Windows 只认 ISO 风格镜像文件）。
    返回实际传给刻录器的文件路径。
    """
    exe = isoburn_path()
    if not exe:
        raise RuntimeError('本机未找到 Windows 光盘映像刻录程序 '
                           '（%SystemRoot%\\System32\\isoburn.exe）')
    target = image_path
    if image_path.lower().endswith('.dmg'):
        from .writer import expand_dmg
        tmp = os.path.join(tempfile.gettempdir(),
                           os.path.splitext(os.path.basename(image_path))[0]
                           + '_burn.iso')
        expand_dmg(image_path, tmp, cb)
        target = tmp
    subprocess.Popen([exe, target], close_fds=True)
    return target


# ---------------------------------------------------------------- 物理磁盘

def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def list_devices() -> list[dict]:
    """枚举 \\\\.\\PhysicalDriveN 及容量（只读，不写入）。"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    out = []
    for i in range(0, 16):
        path = r'\\.\PhysicalDrive%d' % i
        h = k32.CreateFileW(path, 0, 3, None, 3, 0x80, None)
        if h in (0, -1, 0xFFFFFFFF):
            continue
        size = 0
        try:
            buf = ctypes.create_string_buffer(8)
            ret = wintypes.DWORD()
            # IOCTL_DISK_GET_LENGTH_INFO = CTL_CODE(7, 0x17, 0, 0)
            if k32.DeviceIoControl(h, 0x0007005C, None, 0, buf, 8,
                                   ctypes.byref(ret), None):
                size = struct_unpack_q(buf.raw[:8])
            else:
                geo = ctypes.create_string_buffer(24)
                if k32.DeviceIoControl(h, 0x00070000, None, 0, geo, 24,
                                       ctypes.byref(ret), None):
                    cyl, media, tpc, spt, bps = struct_unpack_geo(geo.raw)
                    size = cyl * tpc * spt * bps
        except Exception:
            pass
        k32.CloseHandle(h)
        out.append({'index': i, 'path': path, 'size': size})
    return out


def struct_unpack_q(b: bytes) -> int:
    import struct
    return struct.unpack('<Q', b)[0]


def struct_unpack_geo(b: bytes):
    import struct
    return struct.unpack_from('<QIIII', b, 0)


def save_drive_image(index: int, out_path: str, cb=None) -> int:
    """Saving Image of Drive...：把物理磁盘原样保存为镜像文件。

    与 restore_to_drive 互为反向；只读磁盘、写普通文件，绝不改设备。
    """
    cb = cb or (lambda *_: None)
    if not is_admin():
        raise PermissionError('读取物理磁盘需要以管理员身份运行本程序')
    devs = {d['index']: d for d in list_devices()}
    if index not in devs:
        raise OSError('找不到 PhysicalDrive%d' % index)
    total = devs[index].get('size') or 0
    src_path = r'\\.\PhysicalDrive%d' % index
    done = 0
    with open(src_path, 'rb', buffering=0) as src, \
            open(out_path, 'wb') as dst:
        while done < total:
            want = min(1 << 20, total - done)
            want = (want + 511) // 512 * 512     # 设备要求扇区对齐
            buf = src.read(want)
            if not buf:
                break
            take = min(len(buf), total - done)
            dst.write(buf[:take])
            done += take
            cb(100 * done / max(1, total),
               'Saving Image of Drive... %s / %s'
               % (human_bytes(done), human_bytes(total)))
        dst.flush()
    cb(100, '镜像保存完成')
    return done


def human_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f TB' % n


def restore_to_drive(image_path: str, index: int, cb=None,
                     confirm: bool = False) -> int:
    """把镜像的逻辑磁盘逐扇区写入物理磁盘（破坏性，需显式确认）。

    安全约束：必须管理员权限；默认拒绝 PhysicalDrive0；confirm 必须为 True。
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from .image import Disk
    if not confirm:
        raise RuntimeError('缺少显式确认，操作已取消')
    if index == 0:
        raise RuntimeError('拒绝写入系统盘 PhysicalDrive0')
    if not is_admin():
        raise RuntimeError('写入物理磁盘需要以管理员身份运行')
    cb = cb or (lambda *_: None)
    d = Disk(image_path)
    try:
        path = r'\\.\PhysicalDrive%d' % index
        h = ctypes.windll.kernel32.CreateFileW(
            path, 0xC0000000, 0, None, 3, 0x80, None)
        if h in (0, -1, 0xFFFFFFFF):
            raise RuntimeError('无法打开 %s（可能被占用或权限不足）' % path)
        fd = msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY)
        reader, total = d.reader, d.logical_size
        pos = 0
        try:
            while pos < total:
                buf = reader.read(pos, min(8 << 20, total - pos))
                if not buf:
                    break
                os.lseek(fd, pos, os.SEEK_SET)
                written = os.write(fd, buf)
                if written != len(buf):
                    raise RuntimeError('扇区写入不完整（%d/%d）'
                                       % (written, len(buf)))
                pos += written
                cb(100 * pos / total, '写入 %s' % path)
            return pos
        finally:
            os.close(fd)
    finally:
        d.close()
