<p align="center">
  <img src="_build/splash.png" alt="OpenMacDisk" width="144" height="144">
</p>

<h1 align="center">OpenMacDisk</h1>

开源、纯本地的 **macOS 磁盘镜像（`.dmg`）读写工具**，MIT 许可。原生 Windows 桌面程序，不是网页套壳。

**成品：`OpenMacDisk.exe`**（约 20 MB，单文件，wxPython / wxWidgets 原生窗口）。双击即用，不打开浏览器、不开本地服务、没有控制台黑框，不需要安装 Python。无账号、无联网、无遥测。

本工具为**独立实现**：依据公开的文件格式规范（UDIF / HFS+ / HFSX / APM / GPT / APFS）与对用户**自有**镜像的只读观察编写，不依赖、不包含任何第三方商业软件的实现。展开 / 重封装后的产物与 macOS 原生生成的 DMG 格式一致，可在 Mac 上正常挂载。

---

## 一、它能做什么

- **浏览**：UDIF(.dmg) / 裸镜像 / 物理磁盘，树状展示，支持中文、俄文等 Unicode 文件名
- **提取**：把镜像里的文件 / 整个目录 / 整卷解压到 Windows
- **制作**：把文件夹 / 多个文件打包成标准 DMG（内部为合法 HFS+ 卷）
- **eXpand**：把压缩 DMG 展开为**可编辑**的 DMG（可继续增删改文件）
- **Compress**：把可编辑 DMG 压回小体积只读 DMG（与 eXpand 互逆）
- **镜像内编辑**（HFS+/HFSX）：删除 / 重命名 / 新建文件夹 / 替换 / 添加文件 / 格式化卷
- **格式兼容**：
  - UDIF + GPT + **APFS** → 只读浏览 / 提取
  - UDIF + APM / 整盘卷 + **HFS+ / HFSX** → 可读写编辑
  - 旧式加密 DMG（UDIF v1/v2 `encrcdsa`，输密码打开）
  - 未加密物理 Mac 磁盘只读浏览
- **界面**：简体中文 / 英文可切换；亮色 / 暗色 / 透明玻璃 / 磨砂玻璃四种外观
- **命令行**：`info` / `list` / `tree` / `extract` / `create` / `expand` / `repack` / `verify` / `shell`
- **资源管理器右键**：打开 / 解压到… / 解压到此处 / 压缩为 DMG / eXpand / 制作为 DMG（安装时注册，无需管理员、不改 `.dmg` 默认打开方式）

> 说明：APFS 卷当前为**只读**（浏览与提取），写入支持限于 HFS+/HFSX——这是格式复杂度决定的，已在下方「未实现」一节如实标注。

---

## 二、快速开始

### 下载运行

从项目的 **Releases** 页面下载 `OpenMacDisk-vX.Y.Z-portable.zip`，解压后双击 `OpenMacDisk/OpenMacDisk.exe` 即可。

- **便携**：整个 `OpenMacDisk` 文件夹可复制 / 拷到 U 盘，换机即用；设置自动保存在同目录 `config.json`，不写入系统目录、不写注册表。
- 也可 `OpenMacDisk.exe "D:\路径\xxx.dmg"` 直接打开镜像。

### 打开镜像的入口

| 入口 | 作用 |
|---|---|
| 「浏览…」/ `Ctrl+O` | Windows 原生文件对话框，默认定位桌面 |
| 「桌面 ▾」 | 列出桌面上所有 DMG 及大小，点一下就开 |
| 资源管理器右键 DMG | 「用 OpenMacDisk 打开」 |
| 拖进窗口 / 命令行传参 / 最近打开 | 均可 |

### 资源管理器右键菜单

安装时已注册右键菜单（全部写在 `HKCU\Software\Classes`，无需管理员）。在 `.dmg/.img/.iso/.sparseimage/.cdr/.toast` 上右键可：用 OpenMacDisk 打开、解压到文件夹…、解压到此处、压缩为 DMG…、eXpand 为可编辑 DMG…、查看镜像信息、校验镜像完整性等。

程序窗口内右键（与主流磁盘工具一致）：左侧镜像 / 卷节点 → 关闭 / 刻录 / eXpand 或 Compress（按状态二选一）/ 拆分 / 搜索卷 / 属性；右侧文件列表 → 复制到 / 打开 / 全选 / 删除 / 重命名 / 属性。

在文件夹或文件上右键「制作为 DMG 镜像…」可把选中内容压缩成真正的 UDIF DMG。

「工具 → 启用 / 移除资源管理器右键菜单」可随时开关；也可用 `OpenMacDisk.exe --install-shell` / `--uninstall-shell`。

---

## 三、命令行

```bash
# 读取
python -X utf8 -m openmacdisk.cli info "D:\桌面\xxx.dmg"
python -X utf8 -m openmacdisk.cli list "D:\桌面\xxx.dmg" "Some/Folder"
python -X utf8 -m openmacdisk.cli tree "D:\桌面\xxx.dmg" --depth 3
python -X utf8 -m openmacdisk.cli extract IMG "path/to/file" -o out.bin
python -X utf8 -m openmacdisk.cli extract-all IMG -o outdir

# 写入 / 转换
python -X utf8 -m openmacdisk.cli create out.dmg "D:\某个文件夹" --name 我的卷
python -X utf8 -m openmacdisk.cli create out.dmg a.zip b.iso      # 多个文件
python -X utf8 -m openmacdisk.cli create-blank blank.dmg --size 64 --name Untitled
python -X utf8 -m openmacdisk.cli expand src.dmg raw.img
python -X utf8 -m openmacdisk.cli repack src.dmg out.dmg [--raw]
python -X utf8 -m openmacdisk.cli verify src.dmg

# 系统
python -X utf8 -m openmacdisk.cli shell status|install|uninstall
python -X utf8 -m openmacdisk.cli devices
```

路径以 `--` 开头时用 `--` 分隔：`extract IMG -o out.docx -- "--安装步骤--.rtfd/说明.docx"`。

源码模式：`python -X utf8 desktop.py [镜像]`（需 wxPython）；`run.bat` 一键启动原生界面。重新生成 exe：双击 `build_exe.bat`。

---

## 四、验证

- **80+ 项单元测试全通过**（覆盖 UDIF / APM / GPT / HFS+ / APFS / 加密 DMG / Extents Overflow / LZFSE / 原地编辑 / 取消 / 界面冒烟等）。
- **往返产物与原始 macOS DMG 逐字节一致**：`原版.dmg` → eXpand → Compress 得到的产物与原始文件大小相同、逐字节 0 处差异（SHA-256 相同），Mac 可正常挂载。
- **第三方工具对照**：用 7-Zip 打开本工具生成的 DMG，目录树、块表、CRC 校验和与原始 DMG 表现一致，无 `CORRUPTED` / `Open ERROR`。
- **真实镜像实测**：多份真实 macOS DMG（含 APFS / HFS+ 多种结构，最大近 3 GB、数万文件）均可正确解析、列出目录、读取文件内容。
- **GUI 端到端冒烟**：真实创建 wx 窗口、四种外观、中英文切换、打开镜像的三类分支均覆盖，发布前实际启动 exe 确认无闪退。

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## 五、项目结构

```
OpenMacDisk/
├── desktop.py               # 原生 wxPython 桌面程序（全部功能入口）
├── build_exe.bat            # 一键重新打包
├── run.bat                  # 源码模式启动原生界面
├── docs/ARCHITECTURE.md     # 格式推导与实测修正
├── LICENSE                  # MIT
├── openmacdisk/
│   ├── udif.py              # koly / plist / mish 块表 / 按需解压（线程安全）
│   ├── partition.py         # DDM / APM / GPT / MBR
│   ├── hfs.py               # 卷头 / Catalog B-tree / fork 读取
│   ├── image.py             # 统一镜像抽象 + 卷枚举
│   ├── writer.py            # HFS+ 卷构造 + UDIF 写入 + eXpand/Compress/Split/Join/解压/校验
│   ├── inplace.py           # HFS+ 卷原地编辑 + eXpand/Compress 状态判定
│   ├── apfs.py              # APFS 容器 / 卷只读解析（未加密）
│   ├── crypto_dmg.py        # 加密 UDIF v1/v2 解密
│   ├── hfs_extents.py       # Extents Overflow B-tree
│   ├── lzfse.py             # LZFSE 解码（内置子集 + 外部后端）
│   ├── shell.py             # 右键菜单注册 / 刻录 / 物理磁盘
│   ├── icons.py             # 系统文件图标（SHGetFileInfoW）
│   ├── config.py            # 设置（便携：存 exe 同目录）
│   ├── i18n.py              # 中英文翻译层
│   ├── appearance.py        # 外观主题（亮/暗/玻璃/磨砂）
│   ├── cli.py               # 命令行
│   └── webui.py             # 可选浏览器模式（日常不需要）
├── web/index.html           # 可选双面板网页界面
└── tests/                   # 单元测试
```

核心设计：读取链只依赖 `read(offset, length)` 一个接口，数万个压缩块按扇区排序按需解压，打开数 GB 镜像只读卷头与 Catalog（十几 MB）。写入链反向构造：先按 Apple TN1150 生成 HFS+ 裸卷，再用 zlib 分块封装为 UDIF。所有镜像读取都加了可重入锁，后台任务与界面浏览可以并发。

---

## 六、从源码构建

依赖：Python 3.10+、`wxPython`、`pyinstaller`。

```bash
python -m pip install wxPython pyinstaller
build_exe.bat        # 或：pyinstaller _build/OpenMacDisk.spec --noconfirm --clean
```

产物在 `dist/OpenMacDisk.exe`。

---

## 七、安全与合规

- 「写入镜像到物理磁盘」是**破坏性操作**：需要管理员、默认拒绝系统盘、必须输入 `ERASE` 并二次确认。请勿对存有数据的磁盘使用。
- 代码 MIT 许可；不包含任何第三方商业软件的代码、资源、图标、界面素材或授权逻辑；界面图标取自操作系统。
- 本工具仅做公开格式规范的独立实现与只读解析，不破解、不反编译任何第三方产品。
- 请仅对你合法持有、有权读取的镜像使用。

---

## 八、许可

[MIT License](LICENSE)。© 2026 OpenMacDisk contributors。
