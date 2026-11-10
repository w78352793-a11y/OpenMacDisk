# -*- coding: utf-8 -*-
"""HFS+ Extents Overflow B-tree 读取：碎片文件的第 9 个及以后的 extent。

依据 Apple TN1150 公开格式说明独立实现，与 openmacdisk.hfs 的 B-tree
节点约定保持一致，只依赖 Python 标准库。

HFS+ 的 ForkData 只能内嵌 8 个 extent 描述符；文件碎片化超过 8 段时，
后续记录保存在 Extents Overflow B-tree 中。本模块把两者合并，供
HFSVolume.read_fork 使用。

设计上完全向后兼容：树为空、读不到、头节点异常或记录损坏时，一律原样
返回 fork 自带的 extent 列表，绝不把异常或半解析的脏数据抛给调用方。

节点约定（与 hfs.py 一致，TN1150 kBTNodeKind 的 uint8 视图）::

    BTNodeDescriptor = >IIBBHH  (14 字节)
        fLink(4) bLink(4) kind(1) height(1) numRecords(2) reserved(2)
    kind: 0=索引 1=头 2=位图 0xFF=叶 0xFE=空闲
    记录偏移表: 节点尾 (nrec+1) 个大端 uint16；从低地址读回来即
                [rec0, rec1, ..., recN-1, freeStart]（存储上是倒序）。
"""
from __future__ import annotations

import struct

# --- 与 openmacdisk/hfs.py 一致的结构 ---
NODE_DESC = struct.Struct('>IIBBHH')       # fLink, bLink, kind, height, numRecords, reserved
EXTENT = struct.Struct('>II')              # startBlock, blockCount（均为大端 uint32）
EXT_KEY = struct.Struct('>HBBII')          # keyLen, forkType, pad, fileID, startBlock

KIND_INDEX = 0
KIND_HEADER = 1
KIND_MAP = 2
KIND_FREE = 0xFE
KIND_LEAF = 0xFF

_RECORD_BYTES = 8 * EXTENT.size            # 一条 overflow 记录 = 8 个 extent = 64 字节
_HEADER_NODESIZE_OFFSET = 32               # 14 字节描述符 + BTHeaderRec.nodeSize(+18)
_EXTENT_KEY_LENGTH = 10                    # HFSPlusExtentKey 的 keyLength 恒为 10
_MIN_NODE = 512
_MAX_NODE = 65535


def _header_node_size(tree: bytes) -> int:
    """从 extents 树**自己**的头节点取 nodeSize。

    头节点固定为节点 0；BTHeaderRec.nodeSize 位于 header record 内偏移 +18，
    而 header record 紧跟 14 字节节点描述符，故从节点起始算为绝对偏移 +32
    （与 hfs.py ``_load_catalog`` 的 ``unpack_from('>H', cat, 32)`` 完全一致）。

    **不能**使用 ``volume.node_size``：那是 Catalog B-tree 的节点大小，
    与 Extents Overflow B-tree 的节点大小通常不同。
    """
    if len(tree) < _HEADER_NODESIZE_OFFSET + 2:
        return 0
    try:
        desc = NODE_DESC.unpack_from(tree, 0)
    except struct.error:
        return 0
    # 头节点固定含 header / user data / map 三条记录
    if desc[2] != KIND_HEADER or desc[4] < 3:
        return 0
    try:
        n = struct.unpack_from('>H', tree, _HEADER_NODESIZE_OFFSET)[0]
    except struct.error:
        return 0
    if _MIN_NODE <= n <= _MAX_NODE and len(tree) >= n:
        return n
    return 0


def _leaf_records(tree: bytes, node_size: int):
    """遍历全部叶节点，逐个 yield ``(node_bytes, record_offset)``。

    索引节点、头节点、位图节点、空闲节点一律跳过——按 TN1150 的做法，
    直接扫描所有叶节点即可取到全部记录，无需沿索引树下降。
    """
    total = len(tree) // node_size
    for idx in range(total):
        node = tree[idx * node_size:(idx + 1) * node_size]
        if len(node) < NODE_DESC.size:
            continue
        try:
            desc = NODE_DESC.unpack_from(node, 0)
        except struct.error:
            continue
        if desc[2] != KIND_LEAF:
            continue
        nrec = desc[4]
        # 一条记录至少占 2 字节数据 + 2 字节偏移表项，挡住损坏的计数
        if nrec <= 0 or nrec > (node_size - NODE_DESC.size) // 4:
            continue
        off_start = node_size - 2 * (nrec + 1)
        if off_start < NODE_DESC.size:
            continue
        try:
            offsets = struct.unpack_from('>%dH' % (nrec + 1), node, off_start)
            if len(offsets) > 1 and offsets[0] > offsets[-1]:
                offsets = tuple(reversed(offsets))
        except struct.error:
            continue
        for i in range(nrec):
            pos = offsets[i]
            if NODE_DESC.size <= pos < node_size:
                yield node, pos


def extra_extents(volume, cnid: int, fork_kind: int,
                  start_block: int) -> list[tuple[int, int]]:
    """读取某文件某 fork 在 Extents Overflow B-tree 中的后续 extent 记录。

    参数:
        volume      HFSVolume 实例；用 ``volume.read_fork(volume.extents_file)``
                    读整棵 B-tree。
        cnid        文件的 catalog node ID（对应 key.fileID）。
        fork_kind   0 = data fork，0xFF = resource fork（对应 key.forkType）。
        start_block 只取 ``key.startBlock >= start_block`` 的记录，即前 8 个
                    extent 已覆盖的逻辑块数；用于排除重复/前置记录。

    key 为 ``keyLen(2) + forkType(1) + pad(1) + fileID(4) + startBlock(4)``
    共 12 字节；记录数据为 8 个 ``(startBlock, blockCount)`` 大端 uint32 对
    （64 字节）。

    返回按 key.startBlock 升序拼接的 ``(startBlock, blockCount)`` 列表；
    任何解析异常都返回空列表，绝不抛异常。
    """
    try:
        tree = volume.read_fork(volume.extents_file)
    except Exception:
        return []
    if not tree:
        return []
    node_size = _header_node_size(tree)
    if not node_size:
        return []
    want_fork = fork_kind & 0xFF
    found: list[tuple[int, list[tuple[int, int]]]] = []
    try:
        for node, pos in _leaf_records(tree, node_size):
            if pos + EXT_KEY.size > len(node):
                continue
            klen, ftype, _pad, file_id, sb = EXT_KEY.unpack_from(node, pos)
            # extent key 定长 12 字节；keyLength 恒为 10
            if klen != _EXTENT_KEY_LENGTH or ftype != want_fork or file_id != cnid:
                continue
            if sb < start_block:
                continue
            dpos = pos + EXT_KEY.size
            if dpos + _RECORD_BYTES > len(node):
                continue
            exts = []
            for j in range(8):
                s, c = EXTENT.unpack_from(node, dpos + j * EXTENT.size)
                if c:
                    exts.append((s, c))
            if exts:
                found.append((sb, exts))
    except Exception:
        # 树损坏时宁可少读，也不把半解析的脏数据交给调用方
        return []
    found.sort(key=lambda item: item[0])
    result: list[tuple[int, int]] = []
    for _sb, exts in found:
        result.extend(exts)
    return result


def all_extents(volume, fork, cnid: int,
                fork_kind: int = 0) -> list[tuple[int, int]]:
    """合并 fork 内嵌的前 8 个 extent 与 Extents Overflow 中的后续记录。

    按 fork 的逻辑块累加顺序返回 ``(startBlock, blockCount)``，直到覆盖
    ``fork.blocks``。解析不到 overflow 时原样返回 ``fork.extents``
    （向后兼容，绝不抛异常）。
    """
    try:
        base = [(int(s), int(c)) for s, c in (fork.extents or ()) if c > 0]
    except Exception:
        return list(getattr(fork, 'extents', []) or [])
    try:
        blocks = int(fork.blocks)
    except Exception:
        blocks = sum(c for _s, c in base)
    if not base or blocks <= 0:
        return base
    covered = sum(c for _s, c in base)
    if covered >= blocks:
        return base                         # 前 8 个已覆盖，无需读 overflow 树
    try:
        extra = extra_extents(volume, cnid, fork_kind, covered)
    except Exception:
        extra = []
    result = list(base)
    for s, c in extra:
        if covered >= blocks:
            break
        if c <= 0:
            continue
        result.append((s, c))
        covered += c
    return result
