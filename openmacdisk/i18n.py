# -*- coding: utf-8 -*-
"""极简国际化层（i18n）。

设计：以原始中文串作为翻译键（key）。当语言为简体中文（zh_CN）时，
_() 直接返回键本身，因此未提供翻译的字符串保持中文，不会出现空白；
当语言为 English（en_US）时，按 key 查表返回英文，查不到则回退到中文。

调用方在需要本地化的字符串处用 _('某中文串') 包裹即可，无需改动逻辑。
"""
from __future__ import annotations

_LANG = 'zh_CN'

LANGUAGES = [
    ('zh_CN', '简体中文'),
    ('en_US', 'English'),
]

# key = 原始中文串；value = 英文翻译。仅 zh_CN 以外的语言查这张表。
STRINGS = {
    # ---- 窗口/状态 ----
    ' - Mac 磁盘镜像工具': ' - Mac Disk Image Tool',
    '尚未打开镜像': 'No image opened',
    '就绪 - 点「浏览…」打开镜像，或在资源管理器里右键 DMG 直接解压':
    'Ready - click "Browse..." to open an image, or right-click a DMG in '
    'Explorer to extract it',
    '镜像文件：': 'Image file:',
    '搜索：': 'Search:',
    # ---- 工具栏按钮 ----
    '浏览…': 'Browse...',
    '打开': 'Open',
    '桌面 ▾': 'Desktop ▾',
    '解压全部': 'Extract All',
    '解压选中': 'Extract Selected',
    '制作 DMG': 'Make DMG',
    '添加文件': 'Add Files',
    'eXpand 可编辑 DMG': 'eXpand Editable DMG',
    '校验': 'Verify',
    '属性': 'Properties',
    '刷新': 'Refresh',
    '查找': 'Find',
    # ---- 文件菜单 ----
    '&打开镜像…\tCtrl+O': '&Open Image...\tCtrl+O',
    '浏览&桌面…': 'Browse &Desktop...',
    '浏览&下载…': 'Browse &Downloads...',
    '浏览&此电脑…': 'Browse &This PC...',
    '最近打开': 'Recent',
    '关闭镜像 (Close)\tCtrl+W': 'Close Image (Close)\tCtrl+W',
    '退出\tAlt+F4': 'Exit\tAlt+F4',
    '&文件': '&File',
    # ---- 操作菜单 ----
    '解压整个镜像到…\tCtrl+E': 'Extract Whole Image...\tCtrl+E',
    '解压选中项到…': 'Extract Selected...',
    '导出选中文件…': 'Export Selected File...',
    '导出当前目录…': 'Export Current Folder...',
    '复制选中项路径\tCtrl+C': 'Copy Path\tCtrl+C',
    '删除选中项…': 'Delete Selected...',
    '重命名选中项…': 'Rename Selected...',
    '新建文件夹…': 'New Folder...',
    '用本地文件替换选中项…': 'Replace with Local File...',
    '刷新\tF5': 'Refresh\tF5',
    '&操作': '&Operations',
    # ---- 工具菜单 ----
    '制作压缩 DMG 镜像…\tCtrl+N': 'Create Compressed DMG...\tCtrl+N',
    '复制到 Mac 卷… (Copy to Mac Volume)': 'Copy to Mac Volume...',
    '创建空白 HFS+ DMG…': 'Create Blank HFS+ DMG...',
    '把选中目录制作为 DMG…': 'Make DMG from Selected Folder...',
    'eXpand 为可编辑 DMG…': 'eXpand to Editable DMG...',
    '重新封装为压缩 / 未压缩 DMG…':
    'Repack as Compressed/Uncompressed DMG...',
    '校验镜像完整性…': 'Verify Image Integrity...',
    '镜像信息 / 属性…': 'Image Info / Properties...',
    '烧录镜像到光盘…': 'Burn Image to Disc...',
    '搜索卷… (Search for Volumes)': 'Search for Volumes...',
    '格式化当前镜像（清空 HFS+ 卷）…':
    'Format Current Image (Erase HFS+ Volume)...',
    '打开物理 Mac 磁盘…（只读）': 'Open Physical Mac Disk... (read-only)',
    '格式化物理磁盘为 Mac 卷…（危险）':
    'Format Physical Disk as Mac Volume... (dangerous)',
    '查看物理磁盘…': 'View Physical Disks...',
    '写入镜像到物理磁盘…（危险）':
    'Restore Image to Physical Disk... (dangerous)',
    '从物理磁盘保存镜像… (Save Image of Drive)': 'Save Image of Drive...',
    '启用资源管理器右键菜单': 'Enable Explorer Context Menu',
    '移除资源管理器右键菜单': 'Remove Explorer Context Menu',
    '&工具': '&Tools',
    '关于 OpenMacDisk': 'About OpenMacDisk',
    '&帮助': '&Help',
    # ---- 关于对话框 ----
    '开源 Mac 磁盘 / DMG 工具，原生 Windows 桌面程序。':
    'Open-source Mac disk / DMG tool, a native Windows desktop app.',
    '纯本地运行，不联网、不上传数据，界面图标取自系统。':
    'Runs fully locally: no network access, no uploads; UI icons '
    'come from the system.',
    '读取：UDIF(zlib/bzip2/ADC/RAW)、裸镜像、APM/GPT/MBR、HFS+/HFSX':
    'Read: UDIF (zlib/bzip2/ADC/RAW), raw images, APM/GPT/MBR, '
    'HFS+/HFSX',
    '写入：制作压缩 DMG、空白 HFS+ 卷、添加文件重建、eXpand 可编辑 DMG、重封装':
    'Write: create compressed DMG, blank HFS+ volume, rebuild with added '
    'files, eXpand editable DMG, repack',
    '其它：整卷解压、校验、刻录、物理磁盘、资源管理器右键集成':
    'More: full-volume extract, verify, burn, physical disks, '
    'Explorer context menu',
    '外观：明亮 / 暗黑 / 透明玻璃 / 磨砂玻璃，可自由切换':
    'Appearance: Light / Dark / Transparent Glass / Frosted Glass, '
    'freely switchable',
    'MIT License': 'MIT License',
    # ---- 设置菜单（本版新增）----
    '设置': '&Settings',
    '语言': 'Language',
    '外观': 'Appearance',
    '明亮': 'Light',
    '暗黑': 'Dark',
    '透明玻璃': 'Transparent Glass',
    '磨砂玻璃': 'Frosted Glass',
    # ---- 动态项 ----
    'eXpand（展开为可编辑 DMG）': 'eXpand (expand to editable DMG)',
    'Compress（压缩为只读 DMG）': 'Compress (compress to read-only DMG)',
    # ---- 动态转换菜单 / 右键菜单 ----
    '压缩镜像… (Compress)': 'Compress Image... (Compress)',
    'eXpand / Compress（先打开镜像）': 'eXpand / Compress (open an image first)',
    '关闭镜像 (Close)': 'Close Image (Close)',
    '刻录到 CD/DVD… (Burn to CD/DVD)': 'Burn to CD/DVD... (Burn to CD/DVD)',
    'eXpand 为可编辑 DMG… (Expand)': 'eXpand to Editable DMG... (Expand)',
    '拆分为多个分片… (Split)': 'Split Image... (Split)',
    '搜索卷… (Search for Volumes)': 'Search for Volumes...',
    '属性 (Properties)': 'Properties',
    '合并分片为镜像… (Join)': 'Join Image... (Join)',
    '重新封装 DMG…': 'Repack DMG...',
    '校验镜像完整性…': 'Verify Image Integrity...',
    '解压整个镜像到…': 'Extract Whole Image...',
    '更多操作': 'More Actions',
    '解压此目录到…': 'Extract This Folder...',
    '把此目录制作为 DMG…': 'Make DMG from This Folder...',
    '复制路径': 'Copy Path',
    '属性': 'Properties',
    # ---- 常见提示 ----
    '请先打开一个镜像。': 'Open an image first.',
    '请先打开一个镜像或物理磁盘。': 'Open an image or physical disk first.',
    '正在处理上一个镜像，请稍候。': 'Processing previous image, please wait.',
    '已取消。': 'Cancelled.',
    '操作失败：': 'Operation failed:',
}


def set_language(lang: str) -> None:
    global _LANG
    _LANG = lang if lang in dict(LANGUAGES) else 'zh_CN'


def get_language() -> str:
    return _LANG


def available() -> list:
    return list(LANGUAGES)


def _(s: str) -> str:
    """翻译：当前语言有对应条目则返回，否则（含 zh_CN）原样返回。"""
    if _LANG == 'zh_CN':
        return s
    return STRINGS.get(s, s)
