# -*- coding: utf-8 -*-
"""LZFSE 解码（供 UDIF/DMG 块类型 0x80000007 使用）。

依据 Apple 开源 lzfse 项目公开的**格式说明**独立实现，只依赖 Python
标准库，未搬运其源码（该项目源码为 BSD 许可，本实现按格式描述另行编写）。

解码后端按优先级选择：

1. 已安装的 Python 包 ``lzfse``（若提供 ``decompress`` 接口）；
2. ``PATH`` 中的 ``lzfse`` 可执行文件（``lzfse -decode -i ... -o ...``）；
3. 内置纯 Python 解码器。

【能力范围 / 局限 —— 如实标注】
内置纯 Python 解码器只覆盖 LZFSE 容器里的下列块类型：

* ``bvx-`` 未压缩（raw）块；
* ``bvxn`` LZVN 压缩块，完整支持全部 LZVN opcode（sml_d / med_d /
  lrg_d / pre_d、sml_l / lrg_l、sml_m / lrg_m、nop、eos）；
* ``bvx$`` 流结束标记。

它**不支持** ``bvx1`` / ``bvx2``（FSE 熵编码 + LZ 的压缩块）：完整 FSE
解码需要 v1/v2 频率表 Huffman 解码、4 路交织的 literal 位流以及 L/M/D
FSE 状态机，规模与出错风险都超出本模块的合理范围。遇到这两种块、而又
没有外部解码器时，抛出 ``LZFSEError('该 LZFSE 块类型需要外部解码器')``，
**绝不返回错误数据**。

``available()`` 返回是否存在任一可调用的解码后端；返回 ``True`` 并**不**
意味着支持全部块类型。请用 ``full_support()`` / ``capabilities()`` 查询
完整能力；集成方应捕获 ``LZFSEError`` 后回退或提示用户安装外部解码器。

块头（流内多字节字段均为小端）::

    bvx- : magic(4) + n_raw_bytes(4) + raw[n_raw_bytes]
    bvxn : magic(4) + n_raw_bytes(4) + n_payload_bytes(4) + payload[...]
    bvx$ : magic(4)，流结束
    多个块顺序拼接，直到 bvx$ 或输入结束。
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from functools import lru_cache

__all__ = ['LZFSEError', 'decompress', 'available',
           'full_support', 'capabilities']

_u32le = struct.Struct('<I').unpack_from

# 块 magic：流中为小端 uint32，字节序列即下面 4 个 ASCII 字节
MAGIC_EOS = b'bvx$'
MAGIC_RAW = b'bvx-'
MAGIC_V1 = b'bvx1'
MAGIC_V2 = b'bvx2'
MAGIC_LZVN = b'bvxn'

PURE_SUPPORTED = (MAGIC_EOS, MAGIC_RAW, MAGIC_LZVN)
UNSUPPORTED = (MAGIC_V1, MAGIC_V2)


class LZFSEError(Exception):
    """LZFSE 解码失败（含「该 LZFSE 块类型需要外部解码器」）。"""


# ---------------------------------------------------------------------------
# 外部解码器探测
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _external():
    """返回 ``('module', mod)`` / ``('exe', path)`` / ``None``，结果缓存。"""
    try:
        import importlib
        mod = importlib.import_module('lzfse')
        if callable(getattr(mod, 'decompress', None)):
            return ('module', mod)
    except Exception:
        pass
    try:
        exe = shutil.which('lzfse')
        if exe:
            return ('exe', exe)
    except Exception:
        pass
    return None


def _external_decode(backend, data: bytes, expected_size: int) -> bytes:
    """调用外部解码器；失败时抛 LZFSEError，不静默回退到不完整实现。"""
    kind, obj = backend
    if kind == 'module':
        # 不同 Python 绑定的签名不一：有的只收 data，有的还收解压后大小
        try:
            out = obj.decompress(data, expected_size)
        except TypeError:
            out = obj.decompress(data)
        if not isinstance(out, (bytes, bytearray)):
            raise LZFSEError('外部 lzfse Python 包返回了非字节结果')
        return bytes(out)

    with tempfile.TemporaryDirectory(prefix='openmacdisk_lzfse_') as d:
        in_path = os.path.join(d, 'in.bin')
        out_path = os.path.join(d, 'out.bin')
        with open(in_path, 'wb') as f:
            f.write(data)
        try:
            proc = subprocess.run(
                [obj, '-decode', '-i', in_path, '-o', out_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LZFSEError('外部 lzfse 可执行文件调用失败：%s' % exc) from exc
        if proc.returncode != 0:
            raise LZFSEError(
                '外部 lzfse 可执行文件解码失败（返回码 %d）：%s'
                % (proc.returncode,
                   proc.stderr[:200].decode('utf-8', 'replace')))
        with open(out_path, 'rb') as f:
            return f.read()


# ---------------------------------------------------------------------------
# 内置纯 Python 解码器
# ---------------------------------------------------------------------------

def _decode_pure(data: bytes, limit: int) -> bytes:
    """解码 LZFSE 容器（不支持的块类型直接抛 LZFSEError）。"""
    out = bytearray()
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        magic = bytes(data[pos:pos + 4])
        if magic == MAGIC_EOS:
            break
        if magic == MAGIC_RAW:
            if pos + 8 > n:
                raise LZFSEError('LZFSE 未压缩块头截断')
            raw_len = _u32le(data, pos + 4)[0]
            chunk = data[pos + 8:pos + 8 + raw_len]
            if len(chunk) != raw_len:
                raise LZFSEError('LZFSE 未压缩块数据截断（声明 %d 字节，实际 %d）'
                                 % (raw_len, len(chunk)))
            out += chunk
            pos += 8 + raw_len
        elif magic == MAGIC_LZVN:
            if pos + 12 > n:
                raise LZFSEError('LZFSE LZVN 块头截断')
            raw_len = _u32le(data, pos + 4)[0]
            payload_len = _u32le(data, pos + 8)[0]
            payload = data[pos + 12:pos + 12 + payload_len]
            if len(payload) != payload_len:
                raise LZFSEError('LZFSE LZVN 载荷截断（声明 %d 字节，实际 %d）'
                                 % (payload_len, len(payload)))
            out += _lzvn_decode(payload, raw_len)
            pos += 12 + payload_len
        elif magic in UNSUPPORTED:
            raise LZFSEError(
                '该 LZFSE 块类型需要外部解码器（%s：FSE 熵编码 + LZ 压缩块，'
                '内置纯 Python 解码器未实现）' % magic.decode('latin1'))
        else:
            raise LZFSEError('未知 LZFSE 块魔数 %r；该 LZFSE 块类型需要外部解码器'
                             % magic)
        if limit > 0 and len(out) >= limit:
            break
    return bytes(out)


def _lzvn_decode(payload: bytes, raw_len: int) -> bytes:
    """独立实现的 LZVN 解码器（Apple lzfse 公开的 LZVN 指令格式）。

    opcode 字节按 ``h = op >> 4``、``d3 = op & 7`` 分类；``d_prev`` 保存
    上一条显式给出距离的指令的值，块开始时为 0（与 lzfse 官方一致）。
    字面量紧跟在 opcode（含距离扩展字节）之后；匹配从 ``out[-D]`` 逐字节
    复制，以正确处理 D < M 的重叠情形。
    """
    out = bytearray()
    i = 0
    n = len(payload)
    d_prev = 0
    while i < n and len(out) < raw_len:
        op = payload[i]
        h = op >> 4
        d3 = op & 7
        lit = b''
        M = 0
        D = 0

        if op == 0x06:                          # eos
            break
        elif h == 0xE:                          # 仅字面量
            if op == 0xE0:                     # lrg_l: 11100000 LLLLLLLL
                if i + 2 > n:
                    raise LZFSEError('LZVN 大字面量指令截断')
                L = payload[i + 1] + 16
                lit = payload[i + 2:i + 2 + L]
                i += 2 + L
            else:                               # sml_l: 1110LLLL
                L = op & 0xF
                lit = payload[i + 1:i + 1 + L]
                i += 1 + L
            if len(lit) != L:
                raise LZFSEError('LZVN 字面量数据截断')
        elif h == 0xF:                          # 仅匹配，复用 d_prev
            if op == 0xF0:                     # lrg_m: 11110000 MMMMMMMM
                if i + 2 > n:
                    raise LZFSEError('LZVN 大匹配指令截断')
                M = payload[i + 1] + 16
                i += 2
            else:                               # sml_m: 1111MMMM
                M = op & 0xF
                i += 1
            D = d_prev
        elif h in (0xA, 0xB):                   # med_d: 101LLMMM DDDDDDMM DDDDDDDD
            if i + 3 > n:
                raise LZFSEError('LZVN 中等距离指令截断')
            opc23 = payload[i + 1] | (payload[i + 2] << 8)   # load2（小端）
            L = (op >> 3) & 3
            M = (((op & 7) << 2) | (opc23 & 3)) + 3
            D = (opc23 >> 2) & 0x3FFF
            lit = payload[i + 3:i + 3 + L]
            if len(lit) != L:
                raise LZFSEError('LZVN 字面量数据截断')
            i += 3 + L
            d_prev = D
        elif h in (0x7, 0xD) or (d3 == 6 and h in (0x2, 0x3)):
            raise LZFSEError('LZVN 非法 opcode 0x%02X' % op)
        elif op in (0x0E, 0x16):               # nop
            i += 1
            continue
        elif d3 == 7:                           # lrg_d: LLMMM111 DDDDDDDD DDDDDDDD
            if i + 3 > n:
                raise LZFSEError('LZVN 大距离指令截断')
            L = (op >> 6) & 3
            M = ((op >> 3) & 7) + 3
            D = payload[i + 1] | (payload[i + 2] << 8)        # load2（小端）
            lit = payload[i + 3:i + 3 + L]
            if len(lit) != L:
                raise LZFSEError('LZVN 字面量数据截断')
            i += 3 + L
            d_prev = D
        elif d3 == 6:                           # pre_d: LLMMM110，复用 d_prev
            L = (op >> 6) & 3
            M = ((op >> 3) & 7) + 3
            D = d_prev
            lit = payload[i + 1:i + 1 + L]
            if len(lit) != L:
                raise LZFSEError('LZVN 字面量数据截断')
            i += 1 + L
        else:                                   # sml_d: LLMMMDDD DDDDDDDD
            if i + 2 > n:
                raise LZFSEError('LZVN 小距离指令截断')
            L = (op >> 6) & 3
            M = ((op >> 3) & 7) + 3
            D = ((op & 7) << 8) | payload[i + 1]
            lit = payload[i + 2:i + 2 + L]
            if len(lit) != L:
                raise LZFSEError('LZVN 字面量数据截断')
            i += 2 + L
            d_prev = D

        if lit:
            out += lit
        if M:
            room = raw_len - len(out)
            if room <= 0:
                break
            M = min(M, room)
            if D <= 0 or D > len(out):
                raise LZFSEError('LZVN 匹配距离非法（D=%d，已输出 %d 字节）'
                                 % (D, len(out)))
            if D >= M:
                start = len(out) - D
                out += out[start:start + M]
            else:
                for _ in range(M):              # 重叠匹配必须逐字节复制
                    out.append(out[-D])
    return bytes(out[:raw_len])


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def decompress(data: bytes, expected_size: int) -> bytes:
    """把一段 LZFSE 编码流解码为原始字节。

    ``expected_size`` 是调用方期望的输出长度（UDIF run 的 ``count*512``）：

    * 解码结果更长时截断到该长度；
    * 更短时原样返回（由调用方按需补零），**不臆造数据**。

    优先使用外部解码器；否则用内置子集。遇到未内置支持的 ``bvx1``/
    ``bvx2`` 且无外部解码器时抛 ``LZFSEError``。

    **局限（如实标注）**：内置解码器只覆盖 ``bvx-`` / ``bvxn`` / ``bvx$``，
    FSE 熵编码块 ``bvx1`` / ``bvx2`` 需要外部解码器，详见模块 docstring。
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise LZFSEError('LZFSE 输入必须是字节序列')
    data = bytes(data)
    try:
        limit = int(expected_size)
    except (TypeError, ValueError):
        limit = 0
    backend = _external()
    if backend is not None:
        raw = _external_decode(backend, data, limit)
    else:
        raw = _decode_pure(data, limit)
    if limit > 0 and len(raw) > limit:
        return raw[:limit]
    return raw


def available() -> bool:
    """是否存在任一可调用的 LZFSE 解码后端。

    注意：返回 ``True`` **不代表能解码所有块类型**。无外部解码器时仅支持
    ``bvx-`` / ``bvxn`` / ``bvx$``，``bvx1`` / ``bvx2`` 会抛
    ``LZFSEError('该 LZFSE 块类型需要外部解码器')``。完整能力请用
    ``full_support()``，细分能力请用 ``capabilities()``。
    """
    return _external() is not None or bool(PURE_SUPPORTED)


def full_support() -> bool:
    """是否能解码含 ``bvx1`` / ``bvx2`` 在内的全部 LZFSE 块类型。

    只有检测到外部解码器时才返回 ``True``；内置纯 Python 实现为部分支持。
    """
    return _external() is not None


def capabilities() -> dict:
    """如实返回当前解码能力，便于集成方判断。"""
    ext = _external()
    return {
        'external_backend': None if ext is None else ext[0],
        'full_lzfse': ext is not None,
        'pure_python_blocks': [m.decode('latin1') for m in PURE_SUPPORTED],
        'unsupported_without_external': [m.decode('latin1') for m in UNSUPPORTED],
    }
