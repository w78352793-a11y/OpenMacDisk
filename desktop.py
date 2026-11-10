# -*- coding: utf-8 -*-
"""OpenMacDisk 原生 Windows 桌面程序（wxWidgets，无浏览器、无本地服务）。

功能：打开/浏览 DMG·IMG·ISO、整卷解压、按目录解压、导出、搜索、
制作压缩 DMG、添加文件重建镜像、创建空白 HFS+ 卷、eXpand 可编辑 DMG、
重新封装、校验、属性、刻录、物理磁盘查看与恢复、资源管理器右键集成。

用法：OpenMacDisk.exe [镜像路径 | --动词 路径]
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback

import wx

from openmacdisk.image import Disk
from openmacdisk.hfs import HFSError
from openmacdisk import icons, writer, shell, inplace
from openmacdisk import config as _config
from openmacdisk import i18n, appearance
from openmacdisk.i18n import _

APP_NAME = 'OpenMacDisk'
VERSION = '1.4.3'


class TaskCancelled(Exception):
    """用户在进度对话框点了「取消」，后台任务应立即停止写盘。"""
WILDCARD = ('Mac 磁盘镜像 (*.dmg;*.img;*.iso;*.sparseimage)|'
            '*.dmg;*.img;*.iso;*.sparseimage|'
            '镜像与光盘 (*.dmg;*.img;*.iso;*.sparseimage;*.cdr;*.toast)|'
            '*.dmg;*.img;*.iso;*.sparseimage;*.cdr;*.toast|'
            '所有文件 (*.*)|*.*')
DMG_WILDCARD = 'Mac DMG 镜像 (*.dmg)|*.dmg|所有文件 (*.*)|*.*'
RAW_WILDCARD = '裸磁盘镜像 (*.img;*.iso)|*.img;*.iso|所有文件 (*.*)|*.*'
IMAGE_EXTS = ('.dmg', '.img', '.iso', '.sparseimage', '.cdr', '.toast')

# 文件菜单
ID_OPEN = 10001
ID_BROWSE_DESKTOP = 10002
ID_BROWSE_DOWNLOAD = 10003
ID_BROWSE_COMPUTER = 10004
ID_EXIT = 10005
ID_RECENT = 10100
ID_QUICK = 10200
# 操作菜单
ID_EXPORT_SEL = 11001
ID_EXPORT_ALL = 11002
ID_EXTRACT_ALL = 11003
ID_EXTRACT_SEL = 11004
ID_EXTRACT_TREE = 11005
ID_REFRESH = 11006
ID_COPY_PATH = 11007
ID_PROPS = 11008
# 工具菜单
ID_CREATE = 12001
ID_ADD = 12002
ID_BLANK = 12003
ID_EXPAND = 12004
ID_REPACK = 12005
ID_MAKE_FROM_SEL = 12006
ID_VERIFY = 12007
ID_INFO = 12008
ID_BURN = 12009
ID_DEVICES = 12010
ID_RESTORE = 12011
ID_SHELL_ON = 12012
ID_SHELL_OFF = 12013
ID_ABOUT = 12014
# 编辑（操作菜单）
ID_DELETE_SEL = 11009
ID_RENAME_SEL = 11010
ID_MKDIR = 11011
ID_REPLACE_SEL = 11012
# 磁盘与格式化（工具菜单）
ID_FORMAT_IMG = 12015
ID_OPEN_DRIVE = 12016
ID_FORMAT_DRIVE = 12017
# 程序内右键菜单 / 卷操作
ID_CLOSE_IMG = 12018
ID_SPLIT = 12019
ID_JOIN = 12020
ID_SEARCH_VOLUMES = 12021
ID_OPEN_NODE = 11013
ID_SELECT_ALL = 11014
# 磁盘镜像保存
ID_SAVE_DRIVE = 12022
ID_COMPRESS = 12023
ID_CONVERT = 12024        # 工具菜单动态项：eXpand / Compress 二选一
# 设置菜单（语言 / 外观）
ID_SETTINGS = 13000
ID_LANG_ZH = 13001
ID_LANG_EN = 13002
ID_THEME_BASE = 13010     # +0 明亮, +1 暗黑, +2 透明玻璃, +3 磨砂玻璃

THEME_ORDER = ['light', 'dark', 'glass', 'acrylic']
THEME_LABELS = {'light': '明亮', 'dark': '暗黑',
                'glass': '透明玻璃', 'acrylic': '磨砂玻璃'}


def resource_path(rel: str) -> str:
    base = getattr(sys, '_MEIPASS',
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def appdata_dir() -> str:
    p = os.path.join(os.environ.get('APPDATA') or
                     os.path.expanduser('~'), APP_NAME)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        p = os.path.join(os.path.expanduser('~'), '.' + APP_NAME)
    return p


def desktop_dir() -> str:
    home = os.path.expanduser('~')
    candidates = [r'D:\桌面', os.path.join(home, 'Desktop'),
                  os.path.join(home, '桌面'), r'C:\Users\Public\Desktop']
    try:
        candidates.insert(0, wx.StandardPaths.Get().GetDesktopDir())
    except Exception:
        pass
    for p in candidates:
        if os.path.isdir(p):
            return p
    return home


def downloads_dir() -> str:
    p = os.path.join(os.path.expanduser('~'), 'Downloads')
    return p if os.path.isdir(p) else desktop_dir()


def clean_name(s: str) -> str:
    return ''.join(c if c.isprintable() else '\\x%02x' % ord(c)
                   for c in s) or '(无名)'


def human(n) -> str:
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.2f %s' % (n, u)
        n /= 1024.0


def unquote(s: str) -> str:
    return (s or '').strip().strip('"')


def load_recent() -> list:
    try:
        with open(os.path.join(appdata_dir(), 'recent.json'), 'r',
                  encoding='utf-8') as f:
            return [p for p in json.load(f) if os.path.isfile(p)][:8]
    except Exception:
        return []


def save_recent(items: list):
    try:
        with open(os.path.join(appdata_dir(), 'recent.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(items[:8], f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class ImageDropTarget(wx.FileDropTarget):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    def OnDropFiles(self, x, y, filenames):
        if filenames:
            wx.CallAfter(self.frame.open_image, filenames[0])
        return True


class LoadThread(threading.Thread):
    daemon = True

    def __init__(self, frame, path, password=None):
        super().__init__()
        self.frame, self.path, self.password = frame, path, password

    def run(self):
        d = None
        try:
            d = Disk(self.path, self.password)
            s = d.summary()
            vol = d.first_volume
            wx.CallAfter(self.frame.on_loaded, self.path, d, s, vol)
        except Exception as e:
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
            wx.CallAfter(self.frame.on_load_failed, self.path,
                         '%s: %s' % (type(e).__name__, e))


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME + _(' - Mac 磁盘镜像工具'),
                         size=(1220, 780))
        self.SetMinSize((940, 580))
        try:
            self.SetIcon(wx.Icon(resource_path(os.path.join('_build',
                                                            'icon.ico')),
                                 wx.BITMAP_TYPE_ICO))
        except Exception:
            pass

        self.disk = None
        self.vol = None
        self.summary = None
        self.password = None
        self.current = None
        self.node_list = []
        self.list_nodes = []
        self.recent = load_recent()
        self.busy = False
        self.task_open = False
        self._theme_widgets = []
        self.op_buttons = []

        # 加载用户设置（语言 + 外观），先于界面构建以让菜单/工具栏使用正确语言
        self.cfg = _config.load_config()
        i18n.set_language(self.cfg['language'])
        self.theme = self.cfg['theme']

        self._build_ui()
        self._apply_theme(self.theme)
        self.SetDropTarget(ImageDropTarget(self))
        self.Centre()
        self.set_status(_('就绪 - 点「浏览…」打开镜像，'
                         '或在资源管理器里右键 DMG 直接解压'))

    # ---------------- 界面 ----------------
    @staticmethod
    def _set_icon(btn, art_name):
        try:
            btn.SetBitmap(icons.art(art_name))
        except Exception:
            pass

    @staticmethod
    def _menu_icon(item, art_name):
        try:
            item.SetBitmap(icons.art(art_name))
        except Exception:
            pass

    def _build_ui(self):
        self.panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self.panel, label=_('镜像文件：')), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.path_txt = wx.TextCtrl(self.panel, style=wx.TE_PROCESS_ENTER)
        self.path_txt.Bind(wx.EVT_TEXT_ENTER,
                           lambda e: self.open_image(self.path_txt.GetValue()))
        row.Add(self.path_txt, 1, wx.EXPAND | wx.RIGHT, 6)
        b_browse = wx.Button(self.panel, label=_('浏览…'))
        self._set_icon(b_browse, 'ART_FILE_OPEN')
        b_browse.Bind(wx.EVT_BUTTON, lambda e: self.browse())
        row.Add(b_browse, 0, wx.RIGHT, 6)
        b_open = wx.Button(self.panel, label=_('打开'))
        self._set_icon(b_open, 'ART_CDROM')
        b_open.Bind(wx.EVT_BUTTON,
                    lambda e: self.open_image(self.path_txt.GetValue()))
        row.Add(b_open, 0, wx.RIGHT, 6)
        self.b_quick = wx.Button(self.panel, label=_('桌面 ▾'))
        self._set_icon(self.b_quick, 'ART_FOLDER')
        self.b_quick.Bind(wx.EVT_BUTTON, self.on_quick_menu)
        row.Add(self.b_quick, 0)
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        self.info = wx.StaticText(self.panel, label=_('尚未打开镜像'))
        self.info.SetForegroundColour(wx.Colour(60, 90, 130))
        root.Add(self.info, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        ops = wx.BoxSizer(wx.HORIZONTAL)
        self.op_specs = [
            ('解压全部', self.on_extract_all, 'ART_FILE_SAVE'),
            ('解压选中', self.on_extract_sel, 'ART_FILE_SAVE'),
            ('制作 DMG', self.on_create, 'ART_NEW'),
            ('添加文件', self.on_add_files, 'ART_FILE_OPEN'),
            ('eXpand 可编辑 DMG', self.on_expand, 'ART_CDROM'),
            ('校验', self.on_verify, 'ART_INFORMATION'),
            ('属性', self.on_info, 'ART_INFORMATION'),
            ('刷新', self.on_refresh, 'ART_REDO')]
        for label, handler, aid in self.op_specs:
            b = wx.Button(self.panel, label=_(label))
            self._set_icon(b, aid)
            b.Bind(wx.EVT_BUTTON, handler)
            ops.Add(b, 0, wx.RIGHT, 5)
            self.op_buttons.append((label, b))
        ops.AddStretchSpacer(1)
        ops.Add(wx.StaticText(self.panel, label=_('搜索：')), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.search_txt = wx.TextCtrl(self.panel, size=(190, -1),
                                      style=wx.TE_PROCESS_ENTER)
        self.search_txt.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        ops.Add(self.search_txt, 0, wx.RIGHT, 5)
        b_search = wx.Button(self.panel, label=_('查找'))
        self._set_icon(b_search, 'ART_FIND')
        b_search.Bind(wx.EVT_BUTTON, self.on_search)
        ops.Add(b_search, 0)
        root.Add(ops, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        split = wx.SplitterWindow(self.panel, style=wx.SP_LIVE_UPDATE)
        self.tree = wx.TreeCtrl(split, style=wx.TR_DEFAULT_STYLE |
                                wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT)
        self.list = wx.ListCtrl(split, style=wx.LC_REPORT)
        for i, (cap, w) in enumerate(((_('名称'), 350), (_('类型'), 90),
                                      (_('大小'), 110), (_('路径'), 340))):
            self.list.InsertColumn(i, cap, width=w)
        self.il = icons.build_image_list()
        self.tree.SetImageList(self.il)
        self.list.SetImageList(self.il, wx.IMAGE_LIST_SMALL)
        split.SplitVertically(self.tree, self.list, 340)
        split.SetMinimumPaneSize(200)
        root.Add(split, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.panel.SetSizer(root)

        self.tree.Bind(wx.EVT_TREE_ITEM_EXPANDING, self.on_expanding)
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_tree_sel)
        self.tree.Bind(wx.EVT_TREE_ITEM_RIGHT_CLICK, self.on_tree_right)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_list_activate)
        self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_list_right)

        # 收集需要随主题变化背景色的控件
        self._theme_widgets = [
            self.path_txt, self.search_txt, b_browse, b_open,
            self.b_quick, b_search,
        ] + [b for _, b in self.op_buttons]

        self._build_menu()
        self.CreateStatusBar(2)
        self.SetStatusWidths([-2, -1])
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _build_menu(self):
        mb = wx.MenuBar()
        fm = wx.Menu()
        self._menu_icon(fm.Append(ID_OPEN, _('&打开镜像…\tCtrl+O')),
                        'ART_FILE_OPEN')
        self._menu_icon(fm.Append(ID_BROWSE_DESKTOP, _('浏览&桌面…')),
                        'ART_FOLDER')
        fm.Append(ID_BROWSE_DOWNLOAD, _('浏览&下载…'))
        fm.Append(ID_BROWSE_COMPUTER, _('浏览&此电脑…'))
        fm.AppendSeparator()
        self.recent_menu = wx.Menu()
        fm.AppendSubMenu(self.recent_menu, _('最近打开'))
        fm.AppendSeparator()
        fm.Append(ID_CLOSE_IMG, _('关闭镜像 (Close)\tCtrl+W'))
        fm.Append(ID_EXIT, _('退出\tAlt+F4'))
        mb.Append(fm, _('&文件'))

        am = wx.Menu()
        self._menu_icon(am.Append(ID_EXTRACT_ALL,
                                  _('解压整个镜像到…\tCtrl+E')),
                        'ART_FILE_SAVE')
        am.Append(ID_EXTRACT_SEL, _('解压选中项到…'))
        am.AppendSeparator()
        am.Append(ID_EXPORT_SEL, _('导出选中文件…'))
        am.Append(ID_EXPORT_ALL, _('导出当前目录…'))
        am.AppendSeparator()
        am.Append(ID_COPY_PATH, _('复制选中项路径\tCtrl+C'))
        am.Append(ID_PROPS, _('属性…'))
        am.AppendSeparator()
        am.AppendSeparator()
        am.Append(ID_DELETE_SEL, _('删除选中项…'))
        am.Append(ID_RENAME_SEL, _('重命名选中项…'))
        am.Append(ID_MKDIR, _('新建文件夹…'))
        am.Append(ID_REPLACE_SEL, _('用本地文件替换选中项…'))
        am.AppendSeparator()
        am.Append(ID_REFRESH, _('刷新\tF5'))
        mb.Append(am, _('&操作'))

        tm = wx.Menu()
        self._menu_icon(tm.Append(ID_CREATE,
                                  _('制作压缩 DMG 镜像…\tCtrl+N')),
                        'ART_NEW')
        tm.Append(ID_ADD, _('复制到 Mac 卷… (Copy to Mac Volume)'))
        tm.Append(ID_BLANK, _('创建空白 HFS+ DMG…'))
        tm.Append(ID_MAKE_FROM_SEL, _('把选中目录制作为 DMG…'))
        tm.AppendSeparator()
        tm.Append(ID_CONVERT, _('eXpand 为可编辑 DMG…'))
        self.tools_menu = tm
        tm.Append(ID_SPLIT, _('拆分为多个分片… (Split)'))
        tm.Append(ID_JOIN, _('合并分片为镜像… (Join)'))
        tm.Append(ID_REPACK, _('重新封装为压缩 / 未压缩 DMG…'))
        tm.AppendSeparator()
        tm.Append(ID_VERIFY, _('校验镜像完整性…'))
        tm.Append(ID_INFO, _('镜像信息 / 属性…'))
        tm.Append(ID_BURN, _('烧录镜像到光盘…'))
        tm.Append(ID_SEARCH_VOLUMES, _('搜索卷… (Search for Volumes)'))
        tm.AppendSeparator()
        tm.Append(ID_FORMAT_IMG, _('格式化当前镜像（清空 HFS+ 卷）…'))
        tm.AppendSeparator()
        tm.Append(ID_OPEN_DRIVE, _('打开物理 Mac 磁盘…（只读）'))
        tm.Append(ID_FORMAT_DRIVE, _('格式化物理磁盘为 Mac 卷…（危险）'))
        tm.Append(ID_DEVICES, _('查看物理磁盘…'))
        tm.Append(ID_RESTORE, _('写入镜像到物理磁盘…（危险）'))
        tm.Append(ID_SAVE_DRIVE, _('从物理磁盘保存镜像…'
                  ' (Save Image of Drive)'))
        tm.AppendSeparator()
        tm.Append(ID_SHELL_ON, _('启用资源管理器右键菜单'))
        tm.Append(ID_SHELL_OFF, _('移除资源管理器右键菜单'))
        mb.Append(tm, _('&工具'))

        hm = wx.Menu()
        hm.Append(ID_ABOUT, _('关于 OpenMacDisk'))
        mb.Append(hm, _('&帮助'))

        # 设置：语言 + 外观（本版新增）
        sm = wx.Menu()
        lang_sub = wx.Menu()
        lang_sub.Append(ID_LANG_ZH, _('简体中文'), '', wx.ITEM_RADIO)
        lang_sub.Append(ID_LANG_EN, _('English'), '', wx.ITEM_RADIO)
        sm.AppendSubMenu(lang_sub, _('语言'))
        theme_sub = wx.Menu()
        for i, key in enumerate(THEME_ORDER):
            theme_sub.Append(ID_THEME_BASE + i, _(THEME_LABELS[key]),
                             '', wx.ITEM_RADIO)
        sm.AppendSubMenu(theme_sub, _('外观'))
        mb.Append(sm, _('设置'))

        self.SetMenuBar(mb)
        self._sync_settings_checked(lang_sub, theme_sub)

        acc = wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord('O'), ID_OPEN),
            (wx.ACCEL_CTRL, ord('E'), ID_EXTRACT_ALL),
            (wx.ACCEL_CTRL, ord('N'), ID_CREATE),
            (wx.ACCEL_CTRL, ord('C'), ID_COPY_PATH),
            (wx.ACCEL_NORMAL, wx.WXK_F5, ID_REFRESH),
        ])
        self.SetAcceleratorTable(acc)

        bind = {
            ID_OPEN: lambda e: self.browse(),
            ID_BROWSE_DESKTOP: lambda e: self.browse(desktop_dir()),
            ID_BROWSE_DOWNLOAD: lambda e: self.browse(downloads_dir()),
            ID_BROWSE_COMPUTER: lambda e: self.browse(os.path.expanduser('~')),
            ID_EXIT: lambda e: self.Close(),
            ID_EXTRACT_ALL: self.on_extract_all,
            ID_EXTRACT_SEL: self.on_extract_sel,
            ID_EXTRACT_TREE: self.on_extract_tree,
            ID_EXPORT_SEL: self.on_export_sel,
            ID_EXPORT_ALL: self.on_export_all,
            ID_COPY_PATH: self.on_copy_path,
            ID_PROPS: self.on_props,
            ID_REFRESH: self.on_refresh,
            ID_DELETE_SEL: self.on_delete_sel,
            ID_RENAME_SEL: self.on_rename_sel,
            ID_MKDIR: self.on_mkdir,
            ID_REPLACE_SEL: self.on_replace_sel,
            ID_CREATE: self.on_create,
            ID_ADD: self.on_add_files,
            ID_BLANK: self.on_blank,
            ID_EXPAND: self.on_expand,
            ID_COMPRESS: self.on_compress,
            ID_CONVERT: self.on_convert,
            ID_REPACK: self.on_repack,
            ID_MAKE_FROM_SEL: self.on_make_from_sel,
            ID_VERIFY: self.on_verify,
            ID_INFO: self.on_info,
            ID_BURN: self.on_burn,
            ID_DEVICES: self.on_devices,
            ID_FORMAT_IMG: self.on_format_image,
            ID_OPEN_DRIVE: self.on_open_drive,
            ID_FORMAT_DRIVE: self.on_format_drive,
            ID_RESTORE: self.on_restore,
            ID_SAVE_DRIVE: self.on_save_drive,
            ID_CLOSE_IMG: self.on_close_image,
            ID_SPLIT: self.on_split,
            ID_JOIN: self.on_join,
            ID_SEARCH_VOLUMES: self.on_search_volumes,
            ID_OPEN_NODE: self.on_open_node,
            ID_SELECT_ALL: self.on_select_all,
            ID_SHELL_ON: self.on_shell_on,
            ID_SHELL_OFF: self.on_shell_off,
            ID_ABOUT: self.on_about,
            ID_LANG_ZH: self.on_lang,
            ID_LANG_EN: self.on_lang,
            ID_THEME_BASE: self.on_theme,
            ID_THEME_BASE + 1: self.on_theme,
            ID_THEME_BASE + 2: self.on_theme,
            ID_THEME_BASE + 3: self.on_theme,
        }
        for i, fn in bind.items():
            self.Bind(wx.EVT_MENU, fn, id=i)
        self._refresh_recent_menu()
        self._sync_convert_menu()

    def _refresh_recent_menu(self):
        for mi in list(self.recent_menu.GetMenuItems()):
            self.recent_menu.Delete(mi.GetId())
        if not self.recent:
            self.recent_menu.Append(wx.ID_ANY, '(暂无记录)').Enable(False)
            return
        for i, p in enumerate(self.recent):
            it = self.recent_menu.Append(ID_RECENT + i,
                                         '%d  %s' % (i + 1, p))
            self.Bind(wx.EVT_MENU,
                      lambda e, path=p: self.open_image(path), id=it.GetId())

    # ---------------- 通用对话框 / 后台任务 ----------------
    def ask_dir(self, title='选择目标文件夹') -> str | None:
        dlg = wx.DirDialog(self, title, defaultPath=desktop_dir(),
                           style=getattr(wx, 'DD_DEFAULT_STYLE', 0) |
                           getattr(wx, 'DD_DIR_MUST_EXIST', 0))
        try:
            return dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
        finally:
            dlg.Destroy()

    def ask_save(self, title, wildcard, default_name) -> str | None:
        dlg = wx.FileDialog(self, title, defaultDir=desktop_dir(),
                            defaultFile=default_name, wildcard=wildcard,
                            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            return dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
        finally:
            dlg.Destroy()

    def show_text(self, title, text):
        dlg = wx.Dialog(self, title=title, size=(680, 460),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        box = wx.BoxSizer(wx.VERTICAL)
        tc = wx.TextCtrl(dlg, value=text,
                         style=wx.TE_MULTILINE | wx.TE_READONLY |
                         wx.TE_DONTWRAP | wx.HSCROLL)
        tc.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                           wx.FONTWEIGHT_NORMAL))
        box.Add(tc, 1, wx.EXPAND | wx.ALL, 10)
        btn = wx.Button(dlg, wx.ID_OK, '关闭')
        box.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)
        dlg.SetSizer(box)
        dlg.CentreOnParent()
        dlg.ShowModal()
        dlg.Destroy()

    def run_task(self, title, fn, done=None, out_path=None):
        """后台执行耗时任务；进度框「取消」可真正中止并清理半成品。

        out_path 是本次新建的输出文件（目录类任务不传，避免误删用户文件）。
        """
        if self.task_open:
            wx.MessageBox('已有任务正在运行，请稍候。', APP_NAME, wx.OK)
            return
        self.task_open = True
        cancel = threading.Event()
        preexisting = bool(out_path and os.path.exists(out_path))
        state = {'closed': False, 'finished': False, 'pct': 0,
                 'msg': '准备中…'}
        dlg = wx.ProgressDialog(
            title, '准备中…', 100, self,
            wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_SMOOTH |
            wx.PD_CAN_ABORT)          # 没有这个样式，取消按钮永远是灰的
        dlg.SetSize((480, -1))
        dlg.CentreOnParent()

        def mark_cancel():
            if cancel.is_set():
                return
            cancel.set()
            try:
                dlg.Update(state['pct'], '正在取消，请稍候…')
            except Exception:
                pass

        def pump(pct, msg):
            """主线程刷新进度，并检查用户是否点了取消。"""
            if state['closed']:
                return
            state['pct'] = max(0, min(100, int(pct)))
            state['msg'] = msg
            try:
                r = dlg.Update(state['pct'], msg)
                if (r[0] if isinstance(r, tuple) else r) is False:
                    mark_cancel()
            except Exception:
                pass

        def cb(pct, msg):
            """后台线程每次报进度都检查取消，立即停止继续写盘。"""
            if cancel.is_set():
                raise TaskCancelled()
            wx.CallAfter(pump, pct, msg)

        def tick():
            """心跳：即使任务暂时不报进度，取消按钮也随时能点。"""
            if state['closed']:
                return
            if not cancel.is_set():
                try:
                    r = dlg.Update(state['pct'], state['msg'])
                    if (r[0] if isinstance(r, tuple) else r) is False:
                        mark_cancel()
                except Exception:
                    pass
            wx.CallLater(150, tick)

        def worker():
            try:
                res = fn(cb)
                state['finished'] = True
            except TaskCancelled:
                res = ('CANCEL', '')
            except Exception as e:
                res = ('ERROR', '%s: %s' % (type(e).__name__, e))
            wx.CallAfter(finish, res)

        def cleanup():
            """只删本次新建且未完成的文件；已存在的目标一律不动。"""
            if not out_path or preexisting:
                return False
            root, ext = os.path.splitext(out_path)
            cands = [out_path]
            if ext.lower() != '.dmg':
                cands.append(root + '.dmg')
            for q in cands:
                try:
                    if os.path.isfile(q):
                        os.remove(q)
                        return True
                except OSError:
                    pass
            return False

        def finish(res):
            state['closed'] = True
            cancelled = (not state['finished'] and
                         (cancel.is_set() or
                          (isinstance(res, tuple) and res and
                           res[0] == 'CANCEL')))
            cancel.set()
            try:
                dlg.Destroy()
            except Exception:
                pass
            self.task_open = False
            if cancelled:
                removed = cleanup()
                msg = ('操作已取消，未完成的输出文件已删除。' if removed else
                       '操作已取消。已写出的部分文件会保留，可自行删除。')
                self.set_status(msg)
                wx.MessageBox(msg, APP_NAME, wx.OK | wx.ICON_INFORMATION)
                return
            if isinstance(res, tuple) and res and res[0] == 'ERROR':
                wx.MessageBox('操作失败：\n%s' % res[1], APP_NAME,
                              wx.OK | wx.ICON_ERROR)
                self.set_status('操作失败：%s' % res[1][:100])
                return
            if done:
                done(res)

        wx.CallLater(150, tick)
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 打开 ----------------
    def browse(self, start_dir=None):
        if start_dir is None:
            old = self.path_txt.GetValue().strip()
            start_dir = (os.path.dirname(old) if old and os.path.isdir(
                os.path.dirname(old)) else desktop_dir())
        dlg = wx.FileDialog(self, '选择 Mac 磁盘镜像',
                            defaultDir=start_dir, wildcard=WILDCARD,
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self.open_image(dlg.GetPath())
        finally:
            dlg.Destroy()

    def on_quick_menu(self, event):
        m = wx.Menu()
        m.Append(ID_BROWSE_DESKTOP, '浏览桌面…')
        m.Append(ID_BROWSE_DOWNLOAD, '浏览下载…')
        m.AppendSeparator()
        found = []
        try:
            for name in sorted(os.listdir(desktop_dir())):
                if name.lower().endswith(IMAGE_EXTS):
                    p = os.path.join(desktop_dir(), name)
                    if os.path.isfile(p):
                        found.append((name, p))
        except OSError:
            pass
        if found:
            m.AppendSeparator()
            for i, (name, p) in enumerate(found[:14]):
                it = m.Append(ID_QUICK + i,
                              '%s   (%s)' % (name, human(os.path.getsize(p))))
                self._menu_icon(it, 'ART_CDROM')
                self.Bind(wx.EVT_MENU,
                          lambda e, path=p: self.open_image(path),
                          id=it.GetId())
        else:
            m.Append(wx.ID_ANY, '桌面上没有找到镜像文件').Enable(False)
        self.PopupMenu(m)
        m.Destroy()

    def open_image(self, path):
        path = unquote(path)
        if not path:
            self.browse()
            return
        is_device = path.lower().replace('/', '\\').startswith(
            '\\\\.\\physicaldrive')
        if not os.path.isfile(path) and not is_device:
            wx.MessageBox('找不到文件：\n%s' % path, APP_NAME,
                          wx.OK | wx.ICON_WARNING)
            return
        password = None
        if os.path.isfile(path):
            try:
                from openmacdisk.crypto_dmg import is_encrypted
                if is_encrypted(path):
                    dlg = wx.TextEntryDialog(
                        self, '该 DMG 已加密，请输入密码：', '加密镜像', '',
                        style=wx.OK | wx.CANCEL | wx.TE_PASSWORD)
                    try:
                        if dlg.ShowModal() != wx.ID_OK:
                            return
                        password = dlg.GetValue()
                    finally:
                        dlg.Destroy()
                    if not password:
                        return
            except Exception as e:
                wx.MessageBox('加密镜像检测失败：%s' % e, APP_NAME,
                              wx.OK | wx.ICON_ERROR)
                return
        if self.busy or self.task_open:
            wx.MessageBox('正在处理上一个镜像，请稍候。', APP_NAME, wx.OK)
            return
        self.password = password
        self.busy = True
        self.path_txt.SetValue(path)
        self.set_status('正在解析镜像，请稍候…')
        self.info.SetLabel('正在读取：%s' % os.path.basename(path))
        wx.BeginBusyCursor()
        LoadThread(self, path, password).start()

    def on_loaded(self, path, disk, summary, vol):
        wx.EndBusyCursor()
        self.busy = False
        if self.disk:
            try:
                self.disk.close()
            except Exception:
                pass
        self.disk, self.summary, self.vol = disk, summary, vol
        self.recent = [p for p in self.recent if p != path]
        self.recent.insert(0, path)
        save_recent(self.recent)
        self._refresh_recent_menu()

        parts, vols = summary['partitions'], summary['volumes']
        has_hfs = any(v.get('fs') in ('HFS+', 'HFSX') for v in vols)
        if summary.get('format') == 'DEVICE':
            state_txt, state_color = '只读（物理磁盘）', wx.Colour(200, 120, 30)
        elif has_hfs and inplace.container_writable(path):
            state_txt, state_color = '可写（未压缩，可直接编辑）', \
                wx.Colour(40, 140, 70)
        else:
            state_txt, state_color = '只读（压缩镜像，需先 eXpand 才能编辑）', \
                wx.Colour(200, 120, 30)
        self.info.SetLabel(
            '%s  |  文件大小 %s  |  逻辑容量 %s  |  分区 %d  |  卷 %d  |  %s'
            % (summary['format'], human(summary['file_size']),
               human(summary['logical_size']), len(parts), len(vols),
               state_txt))
        self.info.SetForegroundColour(state_color)
        self.set_status(('注意：' + '；'.join(summary['errors'])[:160])
                        if summary['errors'] else
                        '已打开：%s' % os.path.basename(path))

        self._sync_convert_menu()
        self._render_volume(path, vol)

    def _render_volume(self, path, vol):
        """重建目录树与文件列表（打开镜像 / 切换卷共用）。"""
        root_node = vol.find('') or vol.find('/')
        self.current = root_node
        self.node_list = []
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot(clean_name(os.path.basename(path)),
                                 icons.IDX_DISK)
        self.tree.SetItemImage(root, icons.IDX_DISK,
                               wx.TreeItemIcon_Expanded)
        if root_node is not None:
            self.tree.SetItemData(root, self._add_node_index(root_node))
            self._fill_tree_children(root, root_node)
            self.tree.Expand(root)
        self._show_dir(root_node)

    def on_load_failed(self, path, msg):
        wx.EndBusyCursor()
        self.busy = False
        self.info.SetLabel('打开失败：%s' % os.path.basename(path))
        self.set_status('打开失败')
        wx.MessageBox('无法读取镜像：\n%s\n\n%s' % (path, msg),
                      APP_NAME, wx.OK | wx.ICON_ERROR)

    # ---------------- 树与列表 ----------------
    def _add_node_index(self, node):
        self.node_list.append(node)
        return len(self.node_list) - 1

    def _fill_tree_children(self, item, node):
        if self.tree.GetChildrenCount(item, False) > 0:
            return
        for c in sorted((x for x in node.children if x.kind == 'folder'),
                        key=lambda n: clean_name(n.name).lower()):
            child = self.tree.AppendItem(item, clean_name(c.name))
            self.tree.SetItemData(child, self._add_node_index(c))
            img = icons.icon_for(c.name, 'folder')
            self.tree.SetItemImage(child, img)
            self.tree.SetItemImage(child, img, wx.TreeItemIcon_Expanded)
            if any(g.kind == 'folder' for g in c.children):
                self.tree.AppendItem(child, '…')   # 展开箭头占位

    def on_expanding(self, event):
        item = event.GetItem()
        if not item.IsOk():
            return
        if self.tree.GetChildrenCount(item, False) == 1:
            first, _ = self.tree.GetFirstChild(item)
            if first.IsOk() and self.tree.GetItemText(first) == '…':
                self.tree.Delete(first)
        idx = self.tree.GetItemData(item)
        if idx is not None and idx < len(self.node_list):
            self._fill_tree_children(item, self.node_list[idx])

    def on_tree_sel(self, event):
        item = event.GetItem()
        idx = self.tree.GetItemData(item) if item.IsOk() else None
        if idx is not None and idx < len(self.node_list):
            self._show_dir(self.node_list[idx])
            self.tree.Expand(item)

    def _row(self, c):
        i = self.list.InsertItem(self.list.GetItemCount(),
                                 clean_name(c.name),
                                 icons.icon_for(c.name, c.kind))
        self.list.SetItem(i, 1, '文件夹' if c.kind == 'folder' else '文件')
        self.list.SetItem(i, 2, '' if c.kind == 'folder' else human(c.size))
        self.list.SetItem(i, 3, clean_name(c.path))
        self.list_nodes.append(c)

    def _show_dir(self, node):
        self.current = node
        self.list.DeleteAllItems()
        self.list_nodes = []
        if node is None:
            return
        items = node.children if node.kind == 'folder' else [node]
        for c in sorted(items, key=lambda n: (n.kind != 'folder',
                                              clean_name(n.name).lower())):
            self._row(c)
        self.set_status('%s  -  %d 个项目'
                        % (clean_name(node.path) or '/', len(items)))

    def on_list_activate(self, event):
        i = event.GetIndex()
        if i >= len(self.list_nodes):
            return
        node = self.list_nodes[i]
        if node.kind == 'folder':
            self._show_dir(node)
        else:
            self._extract_nodes([node], '解压文件')

    def _selected_nodes(self):
        out, i = [], self.list.GetFirstSelected()
        while i != -1:
            if i < len(self.list_nodes):
                out.append(self.list_nodes[i])
            i = self.list.GetNextSelected(i)
        return out

    def on_list_right(self, event):
        """文件/文件夹右键：与 TransMac 的 Copy to / Open / Select all /
        Delete / Rename / Properties 对齐，额外功能收进「更多操作」。"""
        i = event.GetIndex()
        if 0 <= i < len(self.list_nodes):
            self.list.SetItemState(i, wx.LIST_STATE_SELECTED,
                                   wx.LIST_STATE_SELECTED)
        m = wx.Menu()
        self._menu_icon(m.Append(ID_EXTRACT_SEL, '复制到… (Copy to)'),
                        'ART_FILE_SAVE')
        m.Append(ID_OPEN_NODE, '打开 (Open)')
        m.Append(ID_SELECT_ALL, '全选 (Select all)')
        m.AppendSeparator()
        m.Append(ID_DELETE_SEL, '删除 (Delete)')
        m.Append(ID_RENAME_SEL, '重命名 (Rename)')
        m.AppendSeparator()
        m.Append(ID_PROPS, '属性 (Properties)')
        m.AppendSeparator()
        more = wx.Menu()
        more.Append(ID_EXPORT_SEL, '导出选中文件…')
        more.Append(ID_MAKE_FROM_SEL, '把选中项制作为 DMG…')
        more.Append(ID_REPLACE_SEL, '用本地文件替换选中项…')
        more.Append(ID_MKDIR, '新建文件夹…')
        more.Append(ID_COPY_PATH, '复制路径')
        m.AppendSubMenu(more, '更多操作')
        self.PopupMenu(m)
        m.Destroy()

    def on_tree_right(self, event):
        """镜像/卷根节点右键：Close / Burn / (Expand 或 Compress 二选一) /
        Split / Search for Volumes / Properties，与 TransMac 卷菜单一致。
        压缩卷只显示 eXpand，展开后的可写卷只显示 Compress。"""
        item = event.GetItem()
        if not item.IsOk():
            return
        self.tree.SelectItem(item)
        is_root = not self.tree.GetItemParent(item).IsOk()
        m = wx.Menu()
        if is_root and self.disk:
            m.Append(ID_CLOSE_IMG, _('关闭镜像 (Close)'))
            m.Append(ID_BURN, _('刻录到 CD/DVD… (Burn to CD/DVD)'))
            state = self._current_convert_state()
            if state == 'compress':
                m.Append(ID_COMPRESS, _('压缩镜像… (Compress)'))
            elif state == 'expand':
                m.Append(ID_EXPAND, _('eXpand 为可编辑 DMG… (Expand)'))
            m.Append(ID_SPLIT, _('拆分为多个分片… (Split)'))
            m.Append(ID_SEARCH_VOLUMES, _('搜索卷… (Search for Volumes)'))
            m.AppendSeparator()
            m.Append(ID_INFO, _('属性 (Properties)'))
            m.AppendSeparator()
            more = wx.Menu()
            more.Append(ID_JOIN, _('合并分片为镜像… (Join)'))
            more.Append(ID_REPACK, _('重新封装 DMG…'))
            more.Append(ID_VERIFY, _('校验镜像完整性…'))
            more.Append(ID_EXTRACT_ALL, _('解压整个镜像到…'))
            m.AppendSubMenu(more, _('更多操作'))
        else:
            self._menu_icon(m.Append(ID_EXTRACT_TREE, _('解压此目录到…')),
                            'ART_FILE_SAVE')
            m.Append(ID_MAKE_FROM_SEL, _('把此目录制作为 DMG…'))
            m.AppendSeparator()
            m.Append(ID_COPY_PATH, _('复制路径'))
            m.Append(ID_PROPS, _('属性'))
        self.PopupMenu(m)
        m.Destroy()

    def _current_convert_state(self):
        """当前镜像该显示 eXpand 还是 Compress（底层纯函数，两个命令互斥）。"""
        return inplace.convert_action(
            unquote(self.path_txt.GetValue()),
            bool(getattr(self, 'password', None)))

    def _sync_convert_menu(self):
        """工具菜单只保留一个动态项，标签与可用性随镜像状态变化。"""
        menu = getattr(self, 'tools_menu', None)
        txt = getattr(self, 'path_txt', None)
        if menu is None or txt is None:
            return
        state = self._current_convert_state()
        self.convert_is_compress = (state == 'compress')
        label = {'compress': _('压缩镜像… (Compress)'),
                 'expand': _('eXpand 为可编辑 DMG…')}.get(
                     state, _('eXpand / Compress（先打开镜像）'))
        menu.SetLabel(ID_CONVERT, label)
        menu.Enable(ID_CONVERT, state is not None)

    # ---------------- 设置：语言 / 外观 ----------------
    def _sync_settings_checked(self, lang_sub=None, theme_sub=None):
        """勾选当前生效的语言与外观项。"""
        if lang_sub is None or theme_sub is None:
            # 从已建好的菜单栏里取子菜单
            # 注意：wx.Menu 没有 GetMenu()/GetSubMenuCount()（那是 wx.MenuBar
            # 的 API），子菜单只能用 GetSubMenu(项序号) 取：0=语言，1=外观
            bar = self.GetMenuBar()
            for i in range(bar.GetMenuCount()):
                m = bar.GetMenu(i)
                if m.GetTitle() in (_('设置'), '&Settings'):
                    try:
                        lang_sub = m.GetSubMenu(0)
                        theme_sub = m.GetSubMenu(1)
                    except Exception:
                        pass
                    break
        if lang_sub is None or theme_sub is None:
            return
        lang_sub.Check(ID_LANG_ZH if self.cfg['language'] == 'zh_CN'
                       else ID_LANG_EN, True)
        for i, key in enumerate(THEME_ORDER):
            theme_sub.Check(ID_THEME_BASE + i, key == self.theme)

    def on_lang(self, event):
        lang = 'zh_CN' if event.GetId() == ID_LANG_ZH else 'en_US'
        if lang == self.cfg['language']:
            return
        i18n.set_language(lang)
        self.cfg['language'] = lang
        _config.save_config(self.cfg)
        self._apply_language()

    def on_theme(self, event):
        idx = event.GetId() - ID_THEME_BASE
        if 0 <= idx < len(THEME_ORDER):
            self._apply_theme(THEME_ORDER[idx])

    def _apply_language(self):
        """重建菜单、刷新工具栏/标题/状态文本以反映新语言。"""
        self._build_menu()
        for label, b in self.op_buttons:
            b.SetLabel(_(label))
        self.SetTitle(APP_NAME + _(' - Mac 磁盘镜像工具'))
        self.set_status(_('就绪 - 点「浏览…」打开镜像，'
                         '或在资源管理器里右键 DMG 直接解压'))

    def _apply_theme(self, key):
        """应用外观主题：控件配色 + DWM 合成效果，并持久化。"""
        self.theme = key
        self.cfg['theme'] = key
        _config.save_config(self.cfg)
        # 先恢复完全不透明，避免跨主题残留半透明
        try:
            self.SetTransparent(255)
        except Exception:
            pass
        if key == 'dark':
            for w in self._theme_widgets:
                try:
                    w.SetBackgroundColour(wx.Colour(37, 37, 38))
                    w.SetForegroundColour(wx.Colour(230, 230, 230))
                except Exception:
                    pass
            self.info.SetForegroundColour(wx.Colour(127, 176, 224))
            self.tree.SetBackgroundColour(wx.Colour(37, 37, 38))
            self.tree.SetForegroundColour(wx.Colour(230, 230, 230))
            self.list.SetBackgroundColour(wx.Colour(37, 37, 38))
            self.list.SetForegroundColour(wx.Colour(230, 230, 230))
            self.panel.SetBackgroundColour(wx.Colour(30, 30, 30))
        else:
            pane = (wx.Colour(240, 240, 240) if key == 'light'
                    else wx.Colour(245, 247, 250))
            for w in self._theme_widgets:
                try:
                    if key == 'light':
                        w.SetBackgroundColour(wx.NullColour)
                        w.SetForegroundColour(wx.NullColour)
                    else:
                        w.SetBackgroundColour(wx.Colour(255, 255, 255))
                        w.SetForegroundColour(wx.Colour(0, 0, 0))
                except Exception:
                    pass
            self.info.SetForegroundColour(wx.Colour(60, 90, 130))
            self.tree.SetBackgroundColour(wx.Colour(255, 255, 255))
            self.tree.SetForegroundColour(wx.Colour(0, 0, 0))
            self.list.SetBackgroundColour(wx.Colour(255, 255, 255))
            self.list.SetForegroundColour(wx.Colour(0, 0, 0))
            self.panel.SetBackgroundColour(pane)
        try:
            appearance.apply_dwm(self.GetHandle(), key)
        except Exception:
            pass
        # 玻璃 / 磨砂玻璃：整窗轻微半透明以保证可见的玻璃质感
        if key in ('glass', 'acrylic'):
            try:
                self.SetTransparent(235 if key == 'glass' else 220)
            except Exception:
                pass
        try:
            self.Refresh()
        except Exception:
            pass

    def on_convert(self, event):
        """工具菜单动态项：按当前状态分发到 Compress 或 eXpand。"""
        if self.convert_is_compress:
            self.on_compress(event)
        else:
            self.on_expand(event)

    # ------------- 程序内右键菜单命令 -------------
    def on_select_all(self, event):
        for i in range(self.list.GetItemCount()):
            self.list.SetItemState(i, wx.LIST_STATE_SELECTED,
                                   wx.LIST_STATE_SELECTED)
        self.set_status('已选中 %d 个项目' % self.list.GetItemCount())

    def on_open_node(self, event):
        """文件夹进入目录；文件先提取到临时目录再用系统默认程序打开。"""
        nodes = self._selected_nodes()
        if not nodes:
            return
        n = nodes[0]
        if n.kind == 'folder':
            self._show_dir(n)
            return
        if not self.vol:
            return
        temp = os.path.join(appdata_dir(), 'Opened')
        try:
            os.makedirs(temp, exist_ok=True)
        except OSError:
            import tempfile
            temp = tempfile.gettempdir()
        safe = ''.join(c for c in clean_name(n.name)
                       if c not in '\\/:*?"<>|').strip() or 'file'
        target = os.path.join(temp, safe)
        stem, ext = os.path.splitext(target)
        k = 1
        while os.path.exists(target):
            target = '%s (%d)%s' % (stem, k, ext)
            k += 1
        self.run_task('正在提取并打开',
                      lambda cb: self._extract_for_open(n, target, cb),
                      self._launch_opened, out_path=target)

    def _extract_for_open(self, node, target, cb):
        off, size = 0, node.size
        with open(target, 'wb') as f:
            while off < size:
                buf = writer.read_file_range(
                    self.vol, node, off, min(writer.CHUNK, size - off))
                if not buf:
                    break
                f.write(buf)
                off += len(buf)
                cb(100 * off / max(1, size),
                   '提取 %s / %s' % (human(off), human(size)))
        return target

    def _launch_opened(self, target):
        try:
            os.startfile(target)
            self.set_status('已提取并打开：%s' % target)
        except Exception as e:
            wx.MessageBox('已提取到：\n%s\n\n系统无法直接打开：%s'
                          % (target, e), APP_NAME, wx.OK)

    def on_close_image(self, event):
        if self.busy or self.task_open:
            wx.MessageBox('正在处理镜像，请稍候。', APP_NAME, wx.OK)
            return
        if self.disk:
            try:
                self.disk.close()
            except Exception:
                pass
        self.disk = self.vol = self.summary = self.current = None
        self.password = None
        self._sync_convert_menu()
        self.node_list, self.list_nodes = [], []
        self.tree.DeleteAllItems()
        self.list.DeleteAllItems()
        self.path_txt.SetValue('')
        self.info.SetLabel('未打开镜像')
        self.set_status('已关闭镜像')

    def on_split(self, event):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像文件。', APP_NAME, wx.OK)
            return
        choices = ['650 MB（一张 CD）', '700 MB（一张 CD）',
                   '4096 MB（FAT32 单文件上限）', '自定义大小…']
        dlg = wx.SingleChoiceDialog(
            self, '把镜像切成固定大小的分片，便于刻录到光盘或复制到 FAT32 设备。\n'
                  '生成 .001/.002… 与 .split.json 清单，可用「合并分片」无损还原。',
            '拆分镜像 (Split)', choices)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            sel = dlg.GetSelection()
        finally:
            dlg.Destroy()
        if sel == 3:
            d2 = wx.TextEntryDialog(self, '每个分片大小（MB）：',
                                    '拆分镜像', '700')
            try:
                if d2.ShowModal() != wx.ID_OK:
                    return
                raw = d2.GetValue().strip()
            finally:
                d2.Destroy()
            try:
                mb = float(raw)
                if mb <= 0:
                    raise ValueError
            except ValueError:
                wx.MessageBox('请输入大于 0 的数字（MB）。', APP_NAME,
                              wx.OK | wx.ICON_WARNING)
                return
        else:
            mb = (650, 700, 4096)[sel]
        dd = wx.DirDialog(self, '选择分片保存位置（会在其中新建子文件夹）',
                          defaultPath=os.path.dirname(os.path.abspath(path)))
        try:
            out_dir = dd.GetPath() if dd.ShowModal() == wx.ID_OK else None
        finally:
            dd.Destroy()
        if not out_dir:
            return
        self.run_task('正在拆分镜像',
                      lambda cb: writer.split_image(path, out_dir, mb, cb),
                      self._split_done)

    def _split_done(self, m):
        wx.MessageBox('拆分完成：%d 个分片，每片 %s。\n\n目录：%s\n'
                      '清单：%s\n\n用「合并分片…」可按清单无损还原为原镜像。'
                      % (len(m['parts']), human(m['chunk_bytes']),
                         m['directory'], os.path.basename(m['manifest'])),
                      APP_NAME, wx.OK | wx.ICON_INFORMATION)

    def on_join(self, event):
        dlg = wx.FileDialog(
            self, '选择分片清单 (.split.json) 或第一个分片 (.001)',
            defaultDir=desktop_dir(),
            wildcard='分片清单 (*.split.json)|*.split.json|'
                     '分片 (*.001)|*.001|所有文件 (*.*)|*.*',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            p = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
        finally:
            dlg.Destroy()
        if not p:
            return
        try:
            m = writer.read_manifest(p)
        except Exception as e:
            wx.MessageBox(str(e), APP_NAME, wx.OK | wx.ICON_ERROR)
            return
        out = self.ask_save('合并分片为镜像', DMG_WILDCARD, m['source'])
        if not out:
            return
        self.run_task('正在合并分片',
                      lambda cb: writer.join_parts(p, out, cb),
                      lambda n: wx.MessageBox(
                          '合并完成，逐片与整体 SHA-256 校验通过。\n\n%s\n%s'
                          % (out, human(n)), APP_NAME,
                          wx.OK | wx.ICON_INFORMATION),
                      out_path=out)

    def on_search_volumes(self, event):
        """重新扫描当前镜像/物理磁盘的分区与卷，多卷时可选择显示哪一个。"""
        path = unquote(self.path_txt.GetValue())
        if not path:
            wx.MessageBox('请先打开一个镜像或物理磁盘。', APP_NAME, wx.OK)
            return
        self.run_task('正在搜索卷',
                      lambda cb: self._scan_volumes(path, cb),
                      self._volumes_found)

    def _scan_volumes(self, path, cb):
        from openmacdisk.image import Disk
        cb(15, '重新扫描分区表…')
        d = Disk(path, self.password)
        try:
            s = d.summary()
            cb(95, '找到 %d 个卷' % len(s['volumes']))
            return d, s
        except Exception:
            d.close()
            raise

    def _volumes_found(self, res):
        d, s = res
        lines = ['文件：%s' % s['path'], '容器：%s' % s['format'],
                 '', '=== 分区 (%d) ===' % len(s['partitions'])]
        for p in s['partitions']:
            lines.append('  %-28s %-24s 起始 %-12d 长度 %d'
                         % (p.get('name', ''), p.get('type', ''),
                            p.get('start', 0), p.get('length', 0)))
        lines += ['', '=== 可读取的卷 (%d) ===' % len(s['volumes'])]
        for i, v in enumerate(s['volumes']):
            lines.append('  [%d] %s  %s  %s'
                         % (i + 1, v['name'], v['fs'], v['scheme']))
        if s['errors']:
            lines += ['', '=== 提示 ==='] + ['  ' + e for e in s['errors']]
        if not d.volumes:
            d.close()
            lines += ['', '未找到可读取的 HFS+/APFS 卷，当前视图保持不变。']
            self.show_text('搜索卷', '\n'.join(lines))
            return
        idx = 0
        if len(d.volumes) > 1:
            names = ['%s  %s  [%s]' % (v['name'], v['fs'], v['scheme'])
                     for v in d.volumes]
            dlg = wx.SingleChoiceDialog(self, '选择要在主窗口显示的卷：',
                                        '搜索卷', names)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    d.close()
                    return
                idx = dlg.GetSelection()
            finally:
                dlg.Destroy()
        if self.disk and self.disk is not d:
            try:
                self.disk.close()
            except Exception:
                pass
        self.disk, self.summary = d, s
        self.vol = d.volumes[idx]['volume']
        self._render_volume(unquote(self.path_txt.GetValue()), self.vol)
        self.set_status('搜索卷：找到 %d 个，当前显示「%s」'
                        % (len(d.volumes), d.volumes[idx]['name']))
        lines += ['', '当前显示：%s' % d.volumes[idx]['name']]
        self.show_text('搜索卷', '\n'.join(lines))

    def on_search(self, event):
        if not self.vol:
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        term = self.search_txt.GetValue().strip().lower()
        if not term:
            return
        self.list.DeleteAllItems()
        self.list_nodes = []
        count = 0
        for n in self.vol.walk():
            if term in n.name.lower():
                self._row(n)
                count += 1
                if count >= 500:
                    break
        self.set_status('搜索「%s」：%d 条结果%s'
                        % (term, count, '（最多 500 条）' if count >= 500 else ''))

    # ---------------- 解压 / 导出 ----------------
    def _extract_nodes(self, nodes, label):
        if not self.vol or not nodes:
            wx.MessageBox('请先打开镜像并选中文件。', APP_NAME, wx.OK)
            return
        dest = self.ask_dir('把「%s」解压到…' % label)
        if not dest:
            return
        vol = self.vol
        self.run_task('正在解压',
                      lambda cb: writer.extract_selected(vol, nodes, dest, cb),
                      lambda r: self._done_extract(r, dest))

    def _done_extract(self, result, dest):
        ok, fail = result
        self.set_status('解压完成：成功 %d，失败 %d → %s' % (ok, fail, dest))
        wx.MessageBox('解压完成\n成功 %d 个，失败 %d 个\n位置：%s'
                      % (ok, fail, dest), APP_NAME, wx.OK)

    def on_extract_all(self, event):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        dest = self.ask_dir('把整个镜像解压到…')
        if not dest:
            return
        self.run_task('正在解压整个镜像',
                      lambda cb: writer.extract_all(path, dest, cb),
                      lambda r: self._done_extract(r, dest))

    def on_extract_sel(self, event):
        nodes = self._selected_nodes()
        if not nodes:
            wx.MessageBox('请先在右侧列表选中文件或文件夹。', APP_NAME, wx.OK)
            return
        self._extract_nodes(nodes, '选中项')

    def on_extract_tree(self, event):
        item = self.tree.GetSelection()
        idx = self.tree.GetItemData(item) if item.IsOk() else None
        if idx is None or idx >= len(self.node_list):
            return
        self._extract_nodes([self.node_list[idx]], '此目录')

    def on_export_sel(self, event):
        # 「导出」= 只导出文件本身（不保留子目录），与 TransMac 的 Extract 一致
        nodes = [n for n in self._selected_nodes() if n.kind == 'file']
        if not nodes:
            wx.MessageBox('请先在右侧列表选中一个文件。', APP_NAME, wx.OK)
            return
        dest = self.ask_dir('选择保存位置')
        if not dest:
            return
        vol = self.vol
        self.run_task('正在导出',
                      lambda cb: writer.extract_selected(vol, nodes, dest, cb),
                      lambda r: self._done_extract(r, dest))

    def on_export_all(self, event):
        if not self.current:
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        self._extract_nodes(list(self.current.children), '当前目录')

    # ---------------- 制作 / 转换 ----------------
    def _choose_sources(self):
        ch = wx.SingleChoiceDialog(
            self, '要从什么内容制作 DMG？', '制作 DMG 镜像',
            ['从文件夹制作（含全部子目录）', '从多个文件制作',
             '创建空白 HFS+ 卷'])
        ch.SetSelection(0)
        if ch.ShowModal() != wx.ID_OK:
            ch.Destroy()
            return None, None
        pick = ch.GetSelection()
        ch.Destroy()
        if pick == 0:
            d = self.ask_dir('选择要打包的文件夹')
            return (['FOLDER', d] if d else None), 0
        if pick == 1:
            dlg = wx.FileDialog(self, '选择要打包的文件（可多选）',
                                defaultDir=desktop_dir(),
                                wildcard='所有文件 (*.*)|*.*',
                                style=wx.FD_OPEN | wx.FD_MULTIPLE |
                                wx.FD_FILE_MUST_EXIST)
            try:
                paths = dlg.GetPaths() if dlg.ShowModal() == wx.ID_OK else []
            finally:
                dlg.Destroy()
            return (['FILES'] + list(paths) if paths else None), 1
        dlg = wx.NumberEntryDialog(self, '容量（MB）', '空白卷大小：',
                                   '创建空白 DMG', 16, 4, 4096)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return None, 2
        size = dlg.GetValue()
        dlg.Destroy()
        return ['BLANK', size], 2

    def _default_name(self, spec, default):
        if spec and spec[0] == 'FOLDER':
            return os.path.basename(spec[1].rstrip('\\/')) + '.dmg'
        if spec and spec[0] == 'FILES' and len(spec) > 1:
            return os.path.splitext(os.path.basename(spec[1]))[0] + '.dmg'
        return default

    def on_create(self, event):
        spec, _kind = self._choose_sources()
        if not spec:
            return
        out = self.ask_save('保存 DMG 镜像', DMG_WILDCARD,
                            self._default_name(spec, 'NewImage.dmg'))
        if not out:
            return
        name = os.path.splitext(os.path.basename(out))[0]
        if spec[0] == 'BLANK':
            work = lambda cb: writer.create_blank_dmg(out, name, spec[1], cb)
        else:
            paths = [spec[1]] if spec[0] == 'FOLDER' else spec[1:]
            work = lambda cb: writer.create_from_paths(out, paths, name,
                                                       True, cb)
        self.run_task('正在制作 DMG', work,
                      lambda size: self._done_create(out, size),
                      out_path=out)

    def _done_create(self, out, size):
        self.set_status('已生成 %s（%s）' % (os.path.basename(out), human(size)))
        r = wx.MessageBox('DMG 已生成：\n%s\n大小 %s\n\n现在打开看看吗？'
                          % (out, human(size)), APP_NAME,
                          wx.YES_NO | wx.ICON_INFORMATION)
        if r == wx.YES:
            self.open_image(out)

    def on_blank(self, event):
        dlg = wx.NumberEntryDialog(self, '容量（MB）', '空白卷大小：',
                                   '创建空白 HFS+ DMG', 16, 4, 4096)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        size = dlg.GetValue()
        dlg.Destroy()
        dlg = wx.TextEntryDialog(self, '卷名称：', '空白 HFS+ 卷',
                                'Untitled')
        try:
            if dlg.ShowModal() != wx.ID_OK:   # 取消就真的退出，不再继续生成
                return
            name = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        out = self.ask_save('保存空白 DMG', DMG_WILDCARD,
                            (name or 'Untitled') + '.dmg')
        if not out:
            return
        self.run_task('正在创建空白卷',
                      lambda cb: writer.create_blank_dmg(
                          out, name or 'Untitled', size, cb),
                      lambda s: self._done_create(out, s), out_path=out)

    def on_add_files(self, event):
        """Copy to Mac Volume：把 PC 文件直接写入当前 HFS+ 卷（原地）。"""
        if not self.vol or not self.disk:
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        dlg = wx.FileDialog(self, '选择要复制到 Mac 卷的文件（可多选）',
                            defaultDir=desktop_dir(),
                            wildcard='所有文件 (*.*)|*.*',
                            style=wx.FD_OPEN | wx.FD_MULTIPLE |
                            wx.FD_FILE_MUST_EXIST)
        try:
            paths = list(dlg.GetPaths()) if dlg.ShowModal() == wx.ID_OK else []
        finally:
            dlg.Destroy()
        if not paths:
            return
        cur = self.current.path if self.current else ''
        ops, skipped = [], 0
        for local in paths:
            name = writer._safe_name(os.path.basename(local))
            dest = (cur + '/' if cur else '') + name
            if self.vol.find(dest):
                ch = wx.SingleChoiceDialog(
                    self, '文件 <b>%s</b> 已存在于 Mac 卷，如何处理？'
                    % name, 'Mac File Name Exists',
                    ['替换现有文件（删除原文件）',
                     '两者都保留（自动加序号）', '跳过此文件'])
                ch.SetSelection(0)
                try:
                    if ch.ShowModal() != wx.ID_OK:
                        skipped += 1
                        continue
                    pick = ch.GetSelection()
                finally:
                    ch.Destroy()
                if pick == 2:
                    skipped += 1
                    continue
                if pick == 1:
                    stem, ext = os.path.splitext(dest)
                    k = 1
                    while self.vol.find('%s (%d)%s' % (stem, k, ext)):
                        k += 1
                    dest = '%s (%d)%s' % (stem, k, ext)
            ops.append({'op': 'replace' if self.vol.find(dest) else 'add',
                        'src': local, 'path': dest})
        if not ops:
            wx.MessageBox('没有需要复制的文件（跳过 %d 个）。' % skipped,
                          APP_NAME, wx.OK)
            return
        self._edit(ops, '正在复制到 Mac 卷',
                   'Copying %d File(s) to Mac Volume...\n跳过 %d 个。\n'
                   '将直接写入当前卷，继续吗？' % (len(ops), skipped))

    # ---------------- 镜像编辑（重建卷） ----------------
    def _sel_nodes(self):
        out, nodes = [], getattr(self, 'list_nodes', [])
        i = self.list.GetFirstSelected()
        while i != -1:
            if 0 <= i < len(nodes):
                out.append(nodes[i])
            i = self.list.GetNextSelected(i)
        return out

    def _need_image(self):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像文件。', APP_NAME, wx.OK)
            return None
        return path

    # ---------------- TransMac 流程：卷内原地写入 ----------------
    def release_image(self):
        """释放当前镜像句柄；覆盖/替换文件前必须调用，避免 WinError 5。"""
        try:
            if self.disk:
                self.disk.close()
        except Exception:
            pass
        self.disk = None
        self.vol = None
        self.current = None

    def _editable_target(self, path, why):
        """压缩 DMG 只读：按 TransMac 流程先 eXpand 成未压缩副本再写。

        返回 (目标文件, 是否新副本)；用户取消则 (None, False)。
        """
        r = wx.MessageBox(
            '%s\n\n压缩 DMG 是只读的，需要先 eXpand 成未压缩副本才能写入。\n'
            '是否现在生成可编辑副本，并在副本上完成本次操作？'
            % why, 'Volume is read only',
            wx.YES_NO | wx.ICON_QUESTION)
        if r != wx.YES:
            return None, False
        stem = os.path.splitext(path)[0]
        out, k = stem + '_editable.dmg', 1
        while os.path.exists(out):
            out = '%s (%d).dmg' % (stem, k)
            k += 1
        return out, True

    def _inplace_work(self, src, target, ops, is_copy, cb):
        from openmacdisk import inplace
        if is_copy:
            writer.expand_to_editable_dmg(
                src, target, lambda p, m: cb(p * 0.5, m))
        base = target if is_copy else src
        try:
            return inplace.edit_inplace(
                base, ops,
                lambda p, m: (cb(50 + p * 0.5, m) if is_copy else cb(p, m)))
        except inplace.InplaceError as e:
            # 卷空间 / 目录区不足：原地写不下了，兜底重建整卷（必成功且保持可编辑）
            if inplace.is_capacity_error(str(e)):
                return self._rebuild_edit(
                    base, ops,
                    lambda p, m: (cb(60 + p * 0.4, m) if is_copy else cb(p, m)),
                    str(e))
            raise

    def _rebuild_edit(self, base, ops, cb, reason):
        """原地写不下时的兜底：重建整卷并应用编辑，产物仍可继续编辑。

        - 关闭 GUI 当前持有的镜像句柄，否则 edit_image 内部的 os.replace
          会因 WinError 5 失败；
        - 未压缩（compressed=False）以保持「可编辑」；
        - 按卷大小预留更多空闲块，确保本次及后续编辑都有空间。
        """
        from openmacdisk import writer
        import os
        self.release_image()
        try:
            from openmacdisk.image import Disk
            with Disk(base) as d:
                tb = d.first_volume.total_blocks
        except Exception:
            tb = 0
        grow = max(16384, tb // 16) if tb else 16384   # 至少 64MB 空闲
        writer.edit_image(base, base, ops, compressed=False,
                          extra_blocks=grow, cb=cb)
        added = sum(1 for o in ops if o.get('op') in ('add', 'replace'))
        return {'mode': 'rebuild', 'added': added,
                'size': os.path.getsize(base), 'reason': reason}

    def _edit(self, ops, title,
              confirm='将直接写入 Mac 卷（原地修改目录），继续吗？'):
        path = self._need_image()
        if not path:
            return
        if wx.MessageBox('%s\n\n%s' % (confirm, path), title,
                         wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return
        from openmacdisk import inplace
        ok, why = inplace.is_writable(path)
        target, is_copy = path, False
        if not ok:
            target, is_copy = self._editable_target(path, why)
            if not target:
                return
        # 原地写不替换文件本身，绝不传 out_path（否则取消时会误删用户镜像）
        self.run_task(
            title,
            lambda cb: self._inplace_work(path, target, ops, is_copy, cb),
            lambda r: self._after_edit(target, r))

    def _after_edit(self, path, result):
        if isinstance(result, dict):
            size = result.get('size') or os.path.getsize(path)
            if result.get('mode') == 'rebuild':
                self.set_status(
                    '卷空间不足，已自动重建镜像并把文件加入（已预留更多空闲空间）：'
                    '删除 %d / 改名 %d / 新建 %d / 加入 %d。'
                    '之后可直接继续编辑。'
                    % (result.get('deleted', 0), result.get('renamed', 0),
                       result.get('created', 0), result.get('added', 0)))
            else:
                self.set_status(
                    '已原地写入 %s：删除 %d / 改名 %d / 新建 %d / 加入 %d'
                    % (os.path.basename(path), result.get('deleted', 0),
                       result.get('renamed', 0), result.get('created', 0),
                       result.get('added', 0)))
        else:
            size = result
            self.set_status('镜像已更新：%s（%s）' %
                            (os.path.basename(path), human(size)))
        self.release_image()
        self.open_image(path)

    def on_delete_sel(self, event):
        nodes = self._sel_nodes()
        if not nodes:
            wx.MessageBox('请先在右侧列表中选中文件/文件夹。', APP_NAME, wx.OK)
            return
        names = '、'.join(clean_name(n.name) for n in nodes[:5])
        self._edit([{'op': 'delete', 'path': n.path} for n in nodes],
                   '正在删除',
                   '确定删除 %d 项吗？\n%s%s' %
                   (len(nodes), names, '…' if len(nodes) > 5 else ''))

    def on_rename_sel(self, event):
        nodes = self._sel_nodes()
        if len(nodes) != 1:
            wx.MessageBox('请选中一项进行重命名。', APP_NAME, wx.OK)
            return
        dlg = wx.TextEntryDialog(self, '新名称：', '重命名',
                                 clean_name(nodes[0].name))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            new = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        if not new or '/' in new or '\\' in new:
            wx.MessageBox('名称不能为空或包含路径分隔符。', APP_NAME, wx.OK)
            return
        self._edit([{'op': 'rename', 'path': nodes[0].path, 'name': new}],
                   '正在重命名', '确定重命名吗？')

    def on_mkdir(self, event):
        if not self.vol:
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        dlg = wx.TextEntryDialog(self, '在当前目录下新建文件夹：',
                                 '新建文件夹', '新建文件夹')
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        if not name:
            return
        base = self.current.path if self.current else ''
        rel = (base + '/' if base else '') + name
        self._edit([{'op': 'mkdir', 'path': rel}], '正在新建文件夹',
                   '确定新建文件夹「%s」吗？' % name)

    def on_replace_sel(self, event):
        nodes = self._sel_nodes()
        if len(nodes) != 1 or nodes[0].kind != 'file':
            wx.MessageBox('请选中一个文件。', APP_NAME, wx.OK)
            return
        dlg = wx.FileDialog(self, '选择用于替换的本地文件',
                            defaultDir=desktop_dir(),
                            wildcard='所有文件 (*.*)|*.*',
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            src = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else ''
        finally:
            dlg.Destroy()
        if not src:
            return
        self._edit([{'op': 'replace', 'path': nodes[0].path, 'src': src}],
                   '正在替换文件', '确定用 %s 覆盖选中的文件吗？' %
                   os.path.basename(src))

    def on_format_image(self, event):
        path = self._need_image()
        if not path:
            return
        dlg = wx.TextEntryDialog(self, '卷名称：', '格式化镜像', 'Untitled')
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue().strip() or 'Untitled'
        finally:
            dlg.Destroy()
        if wx.MessageBox('Formatting will destroy existing data on this '
                         'volume!\n%s\n\n卷容量保持不变，将原地重建目录结构。'
                         '\n确定格式化吗？' % path, '格式化镜像',
                         wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return
        from openmacdisk import inplace
        ok, why = inplace.is_writable(path)
        target, is_copy = path, False
        if not ok:
            target, is_copy = self._editable_target(path, why)
            if not target:
                return

        def work(cb):
            if is_copy:
                writer.expand_to_editable_dmg(
                    path, target, lambda p, m: cb(p * 0.5, m))
                return inplace.format_inplace(
                    target, name, lambda p, m: cb(50 + p * 0.5, m))
            return inplace.format_inplace(path, name, cb)

        self.run_task('正在格式化镜像', work,
                      lambda r: self._after_edit(target, r))

    # ---------------- 物理 Mac 磁盘 ----------------
    def on_open_drive(self, event):
        devs = shell.list_devices()
        if not devs:
            wx.MessageBox('未发现物理磁盘。读取物理磁盘通常需要管理员权限。',
                          APP_NAME, wx.OK | wx.ICON_WARNING)
            return
        choices = ['PhysicalDrive%d   %s%s' %
                   (d['index'], human(d['size']),
                    '  （系统盘，只读）' if d['index'] == 0 else '')
                   for d in devs]
        dlg = wx.SingleChoiceDialog(self, '选择要浏览的物理磁盘（只读）',
                                    '打开物理 Mac 磁盘', choices)
        dlg.SetSelection(0)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            idx = devs[dlg.GetSelection()]['index']
        finally:
            dlg.Destroy()
        self.open_image(r'\\.\PhysicalDrive%d' % idx)

    def on_format_drive(self, event):
        devs = [d for d in shell.list_devices() if d['index'] != 0]
        if not devs:
            wx.MessageBox('未发现可格式化的非系统磁盘。出于安全考虑，'
                          '拒绝格式化 PhysicalDrive0。', APP_NAME,
                          wx.OK | wx.ICON_WARNING)
            return
        choices = ['PhysicalDrive%d  %s' % (d['index'], human(d['size']))
                   for d in devs]
        dlg = wx.SingleChoiceDialog(self, '目标磁盘的全部数据将被覆盖！',
                                    '格式化为 Mac 卷', choices)
        dlg.SetSelection(0)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            idx = devs[dlg.GetSelection()]['index']
        finally:
            dlg.Destroy()
        ed = wx.TextEntryDialog(
            self, '将抹掉 PhysicalDrive%d 上的全部数据。\n请输入大写 ERASE 确认：'
            % idx, '危险操作确认', '')
        try:
            if ed.ShowModal() != wx.ID_OK or ed.GetValue().strip() != 'ERASE':
                wx.MessageBox('确认文字不匹配，操作已取消。', APP_NAME, wx.OK)
                return
        finally:
            ed.Destroy()
        if not shell.is_admin():
            wx.MessageBox('写入物理磁盘需要以管理员身份运行本程序。',
                          APP_NAME, wx.OK | wx.ICON_WARNING)
            return
        self.run_task('正在格式化物理磁盘',
                      lambda cb: writer.format_drive(idx, 'Macintosh HD', 16, cb),
                      lambda n: self._done_simple('格式化完成',
                                                  'PhysicalDrive%d' % idx, n))

    def on_make_from_sel(self, event):
        nodes = self._selected_nodes()
        if not nodes and self.tree.GetSelection().IsOk():
            idx = self.tree.GetItemData(self.tree.GetSelection())
            if idx is not None and idx < len(self.node_list):
                nodes = [self.node_list[idx]]
        if not nodes or not self.vol:
            wx.MessageBox('请先选中要打包的文件或目录。', APP_NAME, wx.OK)
            return
        out = self.ask_save('保存 DMG 镜像', DMG_WILDCARD,
                            'Selected.dmg')
        if not out:
            return
        vol, name = self.vol, os.path.splitext(os.path.basename(out))[0]
        dest = out

        def work(cb):
            import tempfile
            tmp = tempfile.mkdtemp(prefix='omd_sel_')
            writer.extract_selected(vol, nodes, tmp, cb)
            sources = [os.path.join(tmp, x) for x in os.listdir(tmp)]
            entries = writer.collect_paths(sources, cb)
            size = writer.create_dmg(dest, entries, name, True, cb)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            return size

        self.run_task('正在打包选中项', work,
                      lambda s: self._done_create(out, s), out_path=out)

    def on_compress(self, event):
        """Compress：把当前镜像压成更小的标准 DMG（TransMac 卷右键同名命令）。"""
        src = unquote(self.path_txt.GetValue())
        if not src or not os.path.isfile(src):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        out = self.ask_save(
            'Compress 压缩镜像', DMG_WILDCARD,
            os.path.splitext(os.path.basename(src))[0] + '_compressed.dmg')
        if not out:
            return
        self._run_compress(src, out)

    def _run_compress(self, src, out):
        """右键 / 菜单 / 命令行共用的压缩链路，可取消并清理半成品。"""
        self.run_task(
            '正在 Compress 压缩镜像',
            lambda cb: writer.compress_to_dmg(src, out, cb),
            lambda n: self._done_compress(out, n),
            out_path=out)

    def _done_compress(self, out, size):
        real = out if out.lower().endswith('.dmg') else (
            os.path.splitext(out)[0] + '.dmg')
        self.set_status('Compress 完成：%s（%s）'
                        % (os.path.basename(real), human(size)))
        # TransMac 完成转换后是「Would you like to open it?」而不是直接打开。
        r = wx.MessageBox(
            '已压缩为 DMG：\n%s\n大小 %s\n\n'
            '压缩卷为只读；需要继续编辑时对它执行 eXpand 即可。\n\n'
            '现在打开看看吗？' % (real, human(size)), APP_NAME,
            wx.YES_NO | wx.ICON_INFORMATION)
        if r == wx.YES:
            self.open_image(real)

    def on_expand(self, event):
        """菜单入口：让用户选择 eXpand 后 DMG 的保存位置。"""
        src = unquote(self.path_txt.GetValue())
        if not src or not os.path.isfile(src):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        out = self.ask_save(
            'eXpand 为可编辑 DMG', DMG_WILDCARD,
            os.path.splitext(os.path.basename(src))[0] + '_expanded.dmg')
        if not out:
            return
        self._run_expand(src, out)

    def _run_expand(self, src, out):
        """把镜像展开成未压缩、可编辑的 DMG（右键与菜单共用）。"""
        self.run_task(
            '正在 eXpand 镜像',
            lambda cb: writer.expand_to_editable_dmg(src, out, cb),
            lambda n: self._done_expand(out, n),
            out_path=out)

    def _done_expand(self, out, size):
        real = out if out.lower().endswith('.dmg') else (
            os.path.splitext(out)[0] + '.dmg')
        self.set_status('eXpand 完成：%s（%s）'
                        % (os.path.basename(real), human(size)))
        # 直接切换到可编辑副本：原压缩 DMG 保持只读，编辑操作必须在
        # 展开后的新文件上进行，否则会误以为原文件已经可写而报「只读」。
        self.open_image(real)

    def on_repack(self, event):
        src = unquote(self.path_txt.GetValue())
        if not src or not os.path.isfile(src):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        ch = wx.SingleChoiceDialog(self, '重新封装为：', '重新封装 DMG',
                                   ['压缩（zlib，体积小）',
                                    '未压缩（RAW，读写快）'])
        ch.SetSelection(0)
        if ch.ShowModal() != wx.ID_OK:
            ch.Destroy()
            return
        compressed = ch.GetSelection() == 0
        ch.Destroy()
        out = self.ask_save('保存重新封装的 DMG', DMG_WILDCARD,
                            os.path.splitext(os.path.basename(src))[0]
                            + '_repack.dmg')
        if not out:
            return
        self.run_task('正在重新封装',
                      lambda cb: writer.repack_dmg(src, out, compressed, cb),
                      lambda n: self._done_simple('重新封装完成', out, n),
                      out_path=out)

    def _done_simple(self, title, out, size):
        self.set_status('%s：%s（%s）' % (title, os.path.basename(out),
                                         human(size)))
        wx.MessageBox('%s\n%s\n大小 %s' % (title, out, human(size)),
                      APP_NAME, wx.OK | wx.ICON_INFORMATION)

    # ---------------- 信息 / 校验 / 刻录 / 磁盘 ----------------
    def on_info(self, event):
        if not self.summary:
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        s = self.summary
        lines = ['文件：%s' % s['path'],
                 '容器：%s' % s['format'],
                 '文件大小：%s (%d B)' % (human(s['file_size']), s['file_size']),
                 '逻辑容量：%s (%d B)'
                 % (human(s['logical_size']), s['logical_size']),
                 '', '=== 分区表 (%d) ===' % len(s['partitions'])]
        for p in s['partitions']:
            lines.append('  %-28s %-22s 起始 %-12d 长度 %d'
                         % (p.get('name', ''), p.get('type', ''),
                            p.get('start', 0), p.get('length', 0)))
        lines += ['', '=== 卷 (%d) ===' % len(s['volumes'])]
        for v in s['volumes']:
            lines.append('  %s  %s  %s' % (v['name'], v['fs'], v['scheme']))
            for k in ('signature', 'block_size', 'total_blocks',
                      'free_blocks', 'files', 'folders', 'catalog_bytes',
                      'node_size'):
                if k in v:
                    lines.append('    %-16s %s' % (k, v[k]))
        if s.get('udif'):
            lines += ['', '=== UDIF ===']
            for k, v in s['udif'].items():
                if k != 'blkx':
                    lines.append('  %-14s %s' % (k, v))
        if s['errors']:
            lines += ['', '=== 提示 ==='] + ['  ' + e for e in s['errors']]
        from openmacdisk.inplace import container_writable
        writable = container_writable(s['path'])
        lines = [
            '=== Disk Image Properties ===',
            'Image Path:    %s' % s['path'],
            'Image Size:    %s (%d B)' % (human(s['file_size']),
                                          s['file_size']),
            'Uncompressed Size: %s' % human(s['logical_size']),
            'Image Type:   %s / %s' % (
                s['format'], 'uncompressed' if writable else 'compressed'),
            'Volume Access: %s' % ('Read/Write' if writable else 'Read only'),
            '',
            '=== Disk Properties ===',
            'Disk Type:    %s' % ('Physical Drive' if s['path'].lower()
                                  .startswith('\\\\.\\physicaldrive')
                                  else 'Disk Image'),
            'Partition Map: %s' % (s['partitions'][0].get('scheme', 'none')
                                   if s['partitions'] else 'none'),
            'Partition Info: %d entries' % len(s['partitions']),
            '',
            '=== Volume Properties ===',
        ] + ['', '=== 详细信息 ==='] + lines
        lines += ['', '=== 写入能力 ===',
                  '模式：%s' % ('原地读写（Catalog / 位图 / 卷头）'
                             if writable else '只读；需先 eXpand 才能编辑')]
        self.show_text('Disk Image / Volume Properties', '\n'.join(lines))

    def on_props(self, event):
        nodes = self._selected_nodes()
        if not nodes:
            self.on_info(event)
            return
        n = nodes[0]
        lines = ['名称：%s' % clean_name(n.name),
                 '路径：%s' % clean_name(n.path),
                 '类型：%s' % ('文件夹' if n.kind == 'folder' else '文件'),
                 'CNID：%d' % n.cnid,
                 '父目录 CNID：%d' % n.parent,
                 '大小：%s (%d B)' % (human(n.size), n.size),
                 '资源分支：%s' % human(n.resource_size),
                 '子项：%d' % len(n.children)]
        if n.data_fork:
            lines += ['数据分支块数：%d' % n.data_fork.blocks,
                      'extents：%s' % (n.data_fork.extents[:8],)]
        lines += ['',
                  'Type / Creator: %s' % (
                      n.dates.get('type', '----') if n.dates else '----'),
                  'Volume Type: Mac OS Extended (HFS+)',
                  'Volume Access: Read/Write (image must be uncompressed)']
        self.show_text('Mac %s Properties' %
                       ('Folder' if n.kind == 'folder' else 'File'),
                       '\n'.join(lines))

    def on_copy_path(self, event):
        nodes = self._selected_nodes()
        if not nodes and self.tree.GetSelection().IsOk():
            idx = self.tree.GetItemData(self.tree.GetSelection())
            if idx is not None and idx < len(self.node_list):
                nodes = [self.node_list[idx]]
        if not nodes:
            return
        text = '\n'.join(clean_name(n.path) for n in nodes)
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            self.set_status('已复制 %d 条路径' % len(nodes))

    def on_verify(self, event):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        self.run_task('正在校验镜像',
                      lambda cb: writer.verify_image(path, cb),
                      self._done_verify)

    def _done_verify(self, r):
        text = ('文件：%s\n'
                '文件大小：%s\n'
                '逻辑容量：%s\n\n'
                '文件 SHA-256：\n%s\n\n'
                '逻辑磁盘 SHA-256：\n%s\n\n'
                '分区：%d\n卷：%s\n'
                % (r['path'], human(r['file_size']),
                   human(r['logical_size']), r['file_sha256'],
                   r['logical_sha256'], r['partitions'],
                   ', '.join(r['volumes']) or '（无）'))
        self.show_text('校验结果', text)
        self.set_status('校验完成')

    def on_burn(self, event):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        if wx.MessageBox(
                '将调用 Windows 自带「光盘映像刻录程序」。\n'
                'DMG 会先展开为裸镜像再交给刻录器。\n\n继续？',
                APP_NAME, wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        self.run_task('正在准备刻录镜像',
                      lambda cb: shell.burn_image(path, cb),
                      lambda target: wx.MessageBox(
                          '已打开系统刻录程序。\n镜像：%s\n\n'
                          '请在弹出的窗口中放入空白光盘并开始刻录。' % target,
                          APP_NAME, wx.OK | wx.ICON_INFORMATION))

    def on_save_drive(self, event):
        """Saving Image of Drive...：把物理磁盘完整保存为镜像文件。"""
        devs = shell.list_devices()
        if not devs:
            wx.MessageBox('未发现物理磁盘。读取物理磁盘通常需要管理员权限。',
                          APP_NAME, wx.OK | wx.ICON_WARNING)
            return
        choices = ['PhysicalDrive%d   %s' % (d['index'], human(d['size']))
                   for d in devs]
        dlg = wx.SingleChoiceDialog(self, '选择要保存为镜像的磁盘：',
                                    'Save Image of Drive', choices)
        dlg.SetSelection(0)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            idx = devs[dlg.GetSelection()]['index']
        finally:
            dlg.Destroy()
        if not shell.is_admin():
            wx.MessageBox('读取物理磁盘需要以管理员身份运行本程序。',
                          APP_NAME, wx.OK | wx.ICON_WARNING)
            return
        out = self.ask_save('保存磁盘镜像', RAW_WILDCARD,
                            'PhysicalDrive%d.img' % idx)
        if not out:
            return
        self.run_task('正在保存磁盘镜像',
                      lambda cb: shell.save_drive_image(idx, out, cb),
                      lambda n: self._done_simple('镜像已保存', out, n),
                      out_path=out)

    def on_devices(self, event):
        devs = shell.list_devices()
        lines = ['本机物理磁盘（只读枚举）', '']
        for d in devs:
            lines.append('PhysicalDrive%d   %s   %s'
                         % (d['index'], d['path'], human(d['size'])))
        lines += ['', '提示：写入镜像到物理磁盘会抹掉该磁盘上的全部数据。']
        self.show_text('物理磁盘', '\n'.join(lines))

    def on_restore(self, event):
        path = unquote(self.path_txt.GetValue())
        if not path or not os.path.isfile(path):
            wx.MessageBox('请先打开一个镜像。', APP_NAME, wx.OK)
            return
        devs = [d for d in shell.list_devices() if d['index'] != 0]
        if not devs:
            wx.MessageBox('未发现可写入的非系统物理磁盘。出于安全考虑，'
                          '本功能拒绝写入 PhysicalDrive0。', APP_NAME,
                          wx.OK | wx.ICON_WARNING)
            return
        choices = ['PhysicalDrive%d  %s' % (d['index'], human(d['size']))
                   for d in devs]
        ch = wx.SingleChoiceDialog(self,
                                   '选择目标磁盘（该磁盘所有数据将被覆盖！）',
                                   '写入镜像到物理磁盘', choices)
        ch.SetSelection(0)
        if ch.ShowModal() != wx.ID_OK:
            ch.Destroy()
            return
        idx = devs[ch.GetSelection()]['index']
        ch.Destroy()
        dlg = wx.TextEntryDialog(
            self,
            '此操作会永久抹掉 PhysicalDrive%d 上的全部数据！\n'
            '请输入大写 ERASE 确认：' % idx, '危险操作确认', '')
        if dlg.ShowModal() != wx.ID_OK or dlg.GetValue().strip() != 'ERASE':
            dlg.Destroy()
            wx.MessageBox('确认文字不匹配，操作已取消。', APP_NAME, wx.OK)
            return
        dlg.Destroy()
        self.run_task('正在写入物理磁盘',
                      lambda cb: shell.restore_to_drive(path, idx, cb, True),
                      lambda n: self._done_simple('写入完成',
                                                  'PhysicalDrive%d' % idx, n))

    # ---------------- 右键菜单集成 ----------------
    def on_shell_on(self, event):
        try:
            n = shell.install()
            wx.MessageBox('已启用资源管理器右键菜单（%d 项）。\n\n'
                          '在 DMG 上右键：打开 / 解压到文件夹 / 解压到此处 /\n'
                          '制作压缩副本 / eXpand 可编辑 DMG / 信息 / 校验 / 烧录；\n'
                          '在文件夹或文件上右键：制作为 DMG 镜像。\n\n'
                          '可能需要重启资源管理器或重新打开窗口才显示。' % n,
                          APP_NAME, wx.OK | wx.ICON_INFORMATION)
            self.set_status('右键菜单已启用（%d 项）' % n)
        except Exception as e:
            wx.MessageBox('注册失败：%s' % e, APP_NAME, wx.OK | wx.ICON_ERROR)

    def on_shell_off(self, event):
        try:
            n = shell.uninstall()
            wx.MessageBox('已移除 %d 项右键菜单。' % n, APP_NAME, wx.OK)
            self.set_status('右键菜单已移除')
        except Exception as e:
            wx.MessageBox('移除失败：%s' % e, APP_NAME, wx.OK | wx.ICON_ERROR)

    def on_refresh(self, event):
        p = self.path_txt.GetValue().strip()
        if p:
            self.open_image(p)

    def on_about(self, event):
        text = '\n'.join([
            '%s %s' % (APP_NAME, VERSION),
            '',
            _('开源 Mac 磁盘 / DMG 工具，原生 Windows 桌面程序。'),
            _('纯本地运行，不联网、不上传数据，界面图标取自系统。'),
            '',
            _('读取：UDIF(zlib/bzip2/ADC/RAW)、裸镜像、APM/GPT/MBR、HFS+/HFSX'),
            _('写入：制作压缩 DMG、空白 HFS+ 卷、添加文件重建、'
              'eXpand 可编辑 DMG、重封装'),
            _('其它：整卷解压、校验、刻录、物理磁盘、资源管理器右键集成'),
            _('外观：明亮 / 暗黑 / 透明玻璃 / 磨砂玻璃，可自由切换'),
            '',
            _('MIT License'),
        ])
        wx.MessageBox(text, _('关于 OpenMacDisk'),
                      wx.OK | wx.ICON_INFORMATION)

    def set_status(self, text):
        try:
            self.SetStatusText(text, 0)
        except Exception:
            pass

    def on_close(self, event):
        if self.disk:
            try:
                self.disk.close()
            except Exception:
                pass
        event.Skip()

    # ---------------- 命令行动词（供右键菜单调用） ----------------
    def run_verb(self, verb, path):
        path = unquote(path)
        if not path or not os.path.exists(path):
            wx.MessageBox('路径不存在：\n%s' % path, APP_NAME,
                          wx.OK | wx.ICON_WARNING)
            return
        if verb == 'open':
            self.open_image(path)
        elif verb == 'extract-here':
            dest = os.path.splitext(path)[0]
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError:
                dest = os.path.dirname(path)
            self.path_txt.SetValue(path)
            self.run_task('正在解压到此处',
                          lambda cb: writer.extract_all(path, dest, cb),
                          lambda r: self._done_extract(r, dest))
        elif verb == 'extract':
            dest = self.ask_dir('把镜像解压到…')
            if not dest:
                return
            self.path_txt.SetValue(path)
            self.run_task('正在解压镜像',
                          lambda cb: writer.extract_all(path, dest, cb),
                          lambda r: self._done_extract(r, dest))
        elif verb == 'create':
            self.on_create_from_path(path)
        elif verb == 'repack':
            self.path_txt.SetValue(path)
            self.on_repack(None)
        elif verb == 'compress':
            # 资源管理器右键 Compress：直接在原目录生成同名 _compressed.dmg
            self.path_txt.SetValue(path)
            folder = os.path.dirname(os.path.abspath(path))
            stem = os.path.splitext(os.path.basename(path))[0]
            out = os.path.join(folder, stem + '_compressed.dmg')
            i = 2
            while os.path.exists(out):
                out = os.path.join(folder, '%s_compressed_%d.dmg' % (stem, i))
                i += 1
            self._run_compress(path, out)
        elif verb == 'expand':
            # 右键 eXpand：不再弹保存框，直接在原目录生成同名 _expanded.dmg
            self.path_txt.SetValue(path)
            folder = os.path.dirname(os.path.abspath(path))
            stem = os.path.splitext(os.path.basename(path))[0]
            out = os.path.join(folder, stem + '_expanded.dmg')
            i = 2
            while os.path.exists(out):
                out = os.path.join(folder, '%s_expanded_%d.dmg' % (stem, i))
                i += 1
            self._run_expand(path, out)
        elif verb == 'info':
            self.open_image(path)
            wx.CallLater(1200, self.on_info, None)
        elif verb == 'verify':
            self.path_txt.SetValue(path)
            self.on_verify(None)
        elif verb == 'burn':
            self.path_txt.SetValue(path)
            self.on_burn(None)

    def on_create_from_path(self, path):
        out = self.ask_save('保存 DMG 镜像', DMG_WILDCARD,
                            os.path.basename(path.rstrip('\\/')) + '.dmg')
        if not out:
            return
        name = os.path.splitext(os.path.basename(out))[0]
        self.run_task('正在制作 DMG',
                      lambda cb: writer.create_from_paths(out, [path],
                                                          name, True, cb),
                      lambda s: self._done_create(out, s), out_path=out)


class SplashFrame(wx.Frame):
    # 圆形异形启动画面：徽章以外区域真正透明（窗口形状按 alpha 裁剪）

    def __init__(self):
        style = (wx.FRAME_SHAPED | wx.FRAME_NO_TASKBAR |
                 wx.STAY_ON_TOP | wx.NO_BORDER)
        super().__init__(None, style=style, title=APP_NAME)
        png = resource_path(os.path.join('_build', 'splash.png'))
        img = wx.Image(png, wx.BITMAP_TYPE_PNG)
        if not img.IsOk():
            raise RuntimeError('缺少启动画面素材 splash.png')
        self.bmp = wx.Bitmap(img)
        w, h = self.bmp.GetWidth(), self.bmp.GetHeight()
        self.SetClientSize((w, h))
        self.SetBackgroundColour(wx.Colour(76, 91, 212))
        self._region = None
        try:
            alpha = img.GetAlpha() if img.HasAlpha() else None
            if alpha:
                # 用不带 alpha 的纯色遮罩构造 region（带 alpha 的位图会被
                # wx 当成整块矩形，导致四角无法透明）
                pix = bytearray(b'\x00\x00\x00' * (w * h))
                for i, a in enumerate(alpha):
                    if not a:
                        pix[i * 3:i * 3 + 3] = b'\xff\x00\xff'
                mask = wx.Image(w, h)
                mask.SetData(bytes(pix))
                self._region = wx.Region(wx.Bitmap(mask),
                                         wx.Colour(255, 0, 255))
                self.SetShape(self._region)
        except Exception:
            self._region = None
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Centre(wx.BOTH)

    def apply_shape(self):
        """窗口显示后再应用一次形状，确保 Windows 上裁剪生效。"""
        if self._region is not None:
            try:
                self.SetShape(self._region)
            except Exception:
                pass

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(76, 91, 212)))
        dc.Clear()
        dc.DrawBitmap(self.bmp, 0, 0, True)
        bw, bh, by = 280, 60, int(h * 0.785)
        bx = (w - bw) // 2
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(45, 56, 145)))
        dc.DrawRoundedRectangle(bx, by, bw, bh, 8)
        f = wx.Font(17, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                    wx.FONTWEIGHT_BOLD, faceName='Microsoft YaHei')
        dc.SetFont(f)
        dc.SetTextForeground(wx.WHITE)
        tw, th = dc.GetTextExtent(APP_NAME)
        dc.DrawText(APP_NAME, (w - tw) // 2, by + 5)
        f2 = wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                     wx.FONTWEIGHT_NORMAL, faceName='Microsoft YaHei')
        dc.SetFont(f2)
        dc.SetTextForeground(wx.Colour(205, 215, 255))
        sub = 'Mac 磁盘镜像工具  ·  正在启动…'
        tw2, _ = dc.GetTextExtent(sub)
        dc.DrawText(sub, (w - tw2) // 2, by + 33)


VERBS = ('--open', '--extract', '--extract-here', '--create', '--repack',
         '--compress', '--expand', '--info', '--verify', '--burn')


def parse_args(argv):
    for i, a in enumerate(argv):
        if a in VERBS:
            return a[2:], unquote(argv[i + 1] if i + 1 < len(argv) else '')
    for a in argv:
        p = unquote(a)
        if os.path.isfile(p):
            return 'open', p
    return None, None


def main() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    argv = sys.argv[1:]
    app = wx.App(False)
    app.SetAppName(APP_NAME)

    if '--install-shell' in argv or '--uninstall-shell' in argv:
        on = '--install-shell' in argv
        try:
            n = shell.install() if on else shell.uninstall()
            msg = ('已启用资源管理器右键菜单（%d 项）。' % n if on else
                   '已移除 %d 项右键菜单。' % n)
        except Exception as e:
            msg = '操作失败：%s' % e
        wx.MessageBox(msg, APP_NAME,
                      wx.OK | (wx.ICON_ERROR if '失败' in msg else
                               wx.ICON_INFORMATION))
        return 0

    frame = MainFrame()
    verb, path = parse_args(argv)
    splash = None
    if not argv:                       # 只有双击启动才显示启动画面
        try:
            splash = SplashFrame()
            splash.Show()
            splash.apply_shape()
        except Exception:
            splash = None
    if splash is None:
        frame.Show()
        if verb:
            wx.CallAfter(frame.run_verb, verb, path)
    else:
        def _finish():
            try:
                splash.Destroy()
            except Exception:
                pass
            frame.Show()
            if verb:
                wx.CallAfter(frame.run_verb, verb, path)

        wx.CallLater(2300, _finish)
    app.MainLoop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
