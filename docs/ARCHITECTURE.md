# OpenMacDisk 格式推导与实现说明

本文记录解析链路、关键结构偏移，以及用真实 DMG 验证时发现并修正的两个易错点。所有依据为公开格式说明与对用户自有镜像的只读二进制观察。

---

## 一、分层

```
┌────────────────────────────────────────────────────────┐
│  web/index.html（双面板） / cli.py                       │
├────────────────────────────────────────────────────────┤
│  webui.py：本地 HTTP 服务 + 镜像会话缓存                  │
├────────────────────────────────────────────────────────┤
│  image.py：Disk → 分区枚举 → 卷枚举                      │
├────────────────────────────────────────────────────────┤
│  partition.py：DDM / APM / GPT / MBR                     │
│  hfs.py：卷头 / Catalog B-tree / fork 读取                │
├────────────────────────────────────────────────────────┤
│  udif.py / RawImage / PhysicalDrive：read(offset,length) │
└────────────────────────────────────────────────────────┘
```

各层只依赖 `read(offset, length)`，因此 UDIF 压缩、裸镜像、物理磁盘对上层完全透明。

---

## 二、UDIF / DMG

### `koly` trailer（文件尾 512 字节，全大端）

| 偏移 | 字段 |
|---|---|
| 0x00 | `4s 'koly'`、Version(4)、HeaderSize(512)、Flags |
| 0x10 | RunningDataForkOffset / DataForkOffset / DataForkLength / RsrcForkOffset / RsrcForkLength（各 8 字节） |
| 0x38 | SegmentNumber、SegmentCount、SegmentID[16] |
| 0x50 | 数据校验和（type + size + 128 B） |
| **0xD8** | **XMLOffset(Q)、XMLLength(Q)** |
| 0x160 | trailer 校验和、ImageVariant、SectorCount |

XML 是标准 plist：`resource-fork → blkx[] → Data`，其中 `Data` 经 `plistlib` 解析后已是 `bytes`（**不要再 base64 解码一次**，这是本次调试中踩过的坑）。

### `mish` 块表（大端）

| 偏移 | 字段 |
|---|---|
| 0x00 | `'mish'`、Version(1)、SectorNumber(Q)、SectorCount(Q)、DataStart(Q)、BuffersNeeded、BlockDescriptors |
| 0x28 | reserved[6]，0x40 起 136 B 校验和 |
| **0xC8 (200)** | BlocksRunCount |
| **0xCC (204)** | 每个 run 40 B：`type, comment, sectorNumber, sectorCount, compOffset, compLength` |

块类型：`0x00000000` ZERO、`0x00000001` RAW、`0x00000002` FREE、`0x80000004` ADC、`0x80000005` ZLIB、`0x80000006` BZLIB、`0x80000007` LZFSE、`0x80000008` LZMA、`0x7FFFFFFE` COMMENT、`0xFFFFFFFF` TERM。

### 修正点 1：多 blkx 的扇区基准

`mish` 头的 `SectorNumber` 是**该分区段在整盘中的绝对起始扇区**，而每个 run 的 `sectorNumber` 是**相对该段**的。单 blkx 镜像两者都是 0，容易漏掉；一旦遇到 4 段镜像（DDM / APM / Apple_HFS / Apple_Free），若不加基准，所有 run 都堆在扇区 0，分区表与卷头全部读成零。

```python
Run(typ, sec_num + s_num, s_cnt, data_start + c_off, c_len, ...)
```

实测：`Cinema 4D 2024.2.0 Mac.dmg` 的 HFS 段 `sec_num=64`，修正前识别不到任何分区，修正后正常读出 23 文件 / 7 目录。

### 压缩与缓存

`read(offset, length)` 用 `bisect` 定位覆盖的 run，只解压命中段；`OrderedDict` 做 32 MB LRU。ZLIB/BZLIB 用标准库，ADC 自行实现（字面量 + 回指两种 opcode）。未映射的空洞补零，越过逻辑末尾返回空。

---

## 三、分区表

- **DDM**：扇区 0 签名 `ER`（0x4552），`1.dmg` 即为含 DDM 的裸镜像。
- **APM**：扇区 1 起每条 512 B，签名 `PM`；`pmPyPartStart` / `pmPartBlkCnt` 在 +8/+12，名称 +16..48、类型 +48..80。
- **GPT**：扇区 1 `EFI PART`，分区项 128 B；Apple_HFS / Apple_APFS 用 GUID 判定。
- **MBR**：`0x55AA`，类型 `0xAF` 为 Apple HFS。
- 无分区表时按整盘卷处理（`whole disk (Apple_HFS : 0)`）。

---

## 四、HFS+ / HFSX

卷头位于分区起点 +1024，签名 `H+` 或 `HX`。`blockSize@0x28`、`totalBlocks@0x2C`、`fileCount@0x20`、`folderCount@0x24`。其后 5 个 `HFSPlusForkData`（各 80 B）：`logicalSize(Q)`、`clumpSize`、`totalBlocks`、8 个 extent（startBlock, blockCount）。第 3 个是 Catalog File。

Catalog B-tree 的 header node（kind=1）里，`nodeSize` 位于节点绝对偏移 32；叶节点（kind=0xFF）末尾是 `numRecords+1` 个 UInt16 记录偏移表。Catalog key 由 `keyLength(2) + parentID(4) + HFSUniStr255(length(2) + UTF-16BE)` 组成，记录数据按 2 字节对齐。

目录树由每条记录的 `key.parentID` 与 `name` 直接构建，根 CNID=2；thread record 仅用于名称规范化，v1 跳过。

### 修正点 2：`HFSPlusCatalogFile` 的 fork 偏移

`userInfo` 是 `FndrExtendedFileInfo`（**32 字节**，不是 16），因此记录前缀为 88 字节：

```
recordType(2) flags(2) reserved1(4) fileID(4)
createDate … backupDate (5×4 = 20)
HFSPlusBSDInfo(16) + FndrExtendedFileInfo(32) + textEncoding(4) + reserved2(4)
= 88
→ dataFork @ +88，resourceFork @ +168（各 80 B）
```

按 +72 解析会读到错位字段：文件大小看似正常，但 extents 变成垃圾值，提取出来的是**卷头附近的数据**而不是文件内容。修正后实测 `--安装步骤--.rtfd/说明.docx` = 4405 B、`rhino_9.0.26300.09402.dmg` = 582098251 B（3 个 extents），字节数与 fork 声明完全吻合。

### 只读边界

超过 8 个 extents 的碎片文件需要 Extents Overflow B-tree，v1.1 仍未实现；APFS 文件浏览与加密卷不在范围。

---

## 五、v1.1 写入链（writer.py）

「压缩」不是把文件塞进 ZIP，而是**反向构造一个真实可挂载的 HFS+ 卷再封装成 UDIF**：

```
源文件夹 / 文件
  → collect_paths() → Entry(path, is_dir, size, src)
  → 布局计算：blockSize=4096，nodeSize=4096
       [0 保留+卷头][allocation bitmap][extents B-tree][catalog B-tree]
       [attributes B-tree][文件数据块…][空闲块][备用卷头]
  → build_catalog()：按 (parentID, 折叠大小写名称) 排序生成
       root folder / root thread / 每目录 folder+thread / 每文件 file+thread
       叶节点按容量切分 → 逐层建索引节点（分隔键取子节点最大 key）
       → 头节点(BTHeaderRec@14 + UserData128 + 节点位图) + 空闲节点
  → 写卷头（5 个 fork、干净卸载位 0x100、HFS 时间戳 = Unix+2082844800）
  → write_udif()：1 MiB 分块，zlib 压缩优则 BLOCK_ZLIB，否则 BLOCK_RAW
       → mish 块表(204 B 头 + 40 B/项) → XML plist 的 blkx → koly trailer
```

两个容易踩的坑：

1. **根记录的 key.name 必须为空**，不能套用「空名补缺省名」的清洗函数，否则根目录变成一个叫 `untitled` 的普通目录。
2. **索引节点的 key 是子节点的最大 key**（不含 4 字节子节点指针）；头节点里 `bthRoot` 指向最上层索引节点，`treeDepth` = 叶层 1 + 索引层数。

验证方式：生成的 DMG 立刻用本项目的 `Disk` 回读，逐字节比对原文件；120 文件样本强制产生多个叶节点与索引节点，检查 Catalog 里同时存在 header / index / leaf / free 四类节点。

`expand_dmg()` 走反向：把逻辑磁盘按 8 MiB 流式写成裸 IMG（仅供刻录、重封装等底层用途）；`repack_dmg()` = 展开 + 重新封装，可在压缩/未压缩之间互转。所有解压/展开都按 fork extents 分段读取，不把大文件整体载入内存。

用户可见的 **eXpand** 由 `expand_to_editable_dmg()` 实现：直接从镜像 reader 按 1 MiB 扇区块流式写出全 RAW 段的 UDIF（blkx plist + koly），不产生临时裸 IMG。解压的是容器压缩段而不是卷内文件，分区表与 HFS+ 卷逐字节保留，因此产物仍是单个 `.dmg`，打开后可继续用 `edit_image()` 编辑。


## 程序内右键菜单与 Split / Join（v1.2.2）

`desktop.py` 中窗口内部右键与资源管理器右键（`shell.py`，HKCU 51 项）是两条独立链路。内部菜单按被点对象分派：`on_tree_right()` 判断点击的是否为根节点，根节点给卷级菜单（Close / Burn / Expand / Split / Search for Volumes / Properties），子目录给目录菜单；`on_list_right()` 给文件级菜单（Copy to / Open / Select all / Delete / Rename / Properties）。所有菜单 ID 都在 `_build_menu()` 的事件表里绑定，额外能力收进「更多操作」子菜单，不丢功能。

`Search for Volumes` 会在后台用 `image.Disk(path, password)` 重新扫描分区表与卷，列出分区/卷/错误；多卷时用单选框切换，切换后复用 `_render_volume()` 重建目录树与文件列表。`Open` 对文件夹进入目录，对文件用 `read_file_range()` 分块提取到 `%APPDATA%/Opened` 后 `os.startfile()`；`Close` 释放 reader 并清空视图。

`writer.split_image()` 按 1 MiB 流式把镜像切成 `.001/.002…` 分片，输出 `<原名>.split.json`（源文件大小、整体 SHA-256、每片大小与 SHA-256）。清单已存在时自动换用带序号的新目录，绝不覆盖已有分片。`writer.join_parts()` 先逐片校验大小与哈希再写目标文件，最后校验整体哈希与总大小，任何一片损坏都会在写出目标文件之前抛 `WriteError`。


## HFS+ 原地卷编辑（v1.3.0，原地写入流程）

`openmacdisk/inplace.py` 是与 `writer.py` 并列的第二条写入链路：`writer.py` 负责**从零生成**卷，`inplace.py` 负责**在已有卷上改**。

- `WritableImage` 把 UDIF 的每个 RAW 段映射成 `(逻辑扇区 → 文件偏移)`，提供 `read/write`；含压缩段时直接抛 `InplaceError`（对应 macOS 磁盘工具的通用提示：压缩 DMG 需先展开才能写入）。裸 IMG 则直接映射。
- `HFSInplaceEditor` 复用 `image.Disk` 定位分区与 `HFSVolume`，dump Catalog B-tree 的全部叶记录到 `{原始 key: 记录字节}`。**不重建文件记录**，因此每个文件的 data fork extents（数据块位置）完全不变；删除/改名/新建/加入只改目录记录。
- 新记录需要数据块时，`_alloc()` 扫描 allocation bitmap 找连续空闲块，写数据并置位；删除则释放块。空间不足抛 `Not enough space on Mac volume`。
- `_build_tree()` 用原卷的 `nodeSize` 重新拼装 header/index/leaf/free 节点，保留原 header 的 `BTHeaderRec` 其它字段并只更新 treeDepth/rootNode/leafRecords/totalNodes/freeNodes 与节点占用位图；节点总数不变，catalog fork 的容量不变。
- `_commit_metadata()` 按原 extents 原位写回 Catalog、位图、卷头与备用卷头，递增 writeCount、关闭 journal（避免 macOS 回放旧日志撤销修改）。所有元数据写入先记入 `undo`，异常时逆序回滚。
- 界面层 `desktop.py` 的 `_edit()` / `on_add_files()` / `on_format_image()` 全部走这条链路；`run_task()` 的取消事件通过进度回调抛 `TaskCancelled`，`edit_inplace()` 捕获后回滚。原地写**不传 `out_path`**，避免取消清理误删用户镜像。

### B-tree 节点偏移表（TN1150）

节点末尾 `numRecords+1` 个 uint16 是**倒序**的：`[free_space, last_record, …, first_record]`。`hfs.py`、`hfs_extents.py`、`inplace.py` 读取时按 `offsets[0] > offsets[-1]` 自动判别并反转；`writer._pack_node()` 统一按规范倒序写出，因此自造卷与真实 macOS 卷使用同一套解析逻辑。
