# -*- coding: utf-8 -*-
"""加密 UDIF/DMG 解密 —— 旧式（FileVault 之前）``encrcdsa`` v1 / v2 容器。

把带密码的加密 DMG 还原成**完整明文 DMG 字节流**，对外表现为一个 reader，
可像 ``UDIFImage`` / ``RawImage`` 一样交给现有解析层（``image.Disk`` 等）使用。

------------------------------------------------------------------------------
格式依据（均为公开规范 / 开源格式说明，净室独立实现，未复制任何商业软件代码）
------------------------------------------------------------------------------
* dmg2img / vfdecrypt.c（Ralf-Philipp Weinmann、Jacob Appelbaum、Christian
  Fromme，MIT 许可）中的 ``cencrypted_v1_header`` / ``cencrypted_v2_pwheader``
  结构定义与 ``unwrap_v1_header`` / ``unwrap_v2_header`` 流程说明。
* nlitsme/encrypteddmg（readencrcdsa.py，MIT）对 v1 ``cdsaencr`` 尾部 magic、
  v2 ``encrcdsa`` 头、PBKDF2 参数与每块 HMAC-SHA1 IV 的格式说明。
* fstool encrypted.rs、公开逆向文章对 keyblob 明文布局（AES key || HMAC-SHA1
  key）与每块 AES-CBC 解密的描述。
* RFC 2898（PBKDF2）、RFC 3217 / RFC 2630（3DES key unwrap 的 IV 常量与步骤）、
  RFC 2104（HMAC-SHA1）。

净室声明：本文件依据上述**公开字段布局与算法描述**独立编写。所有对称加解密
通过可选的 ``pycryptodome`` 完成，PBKDF2 / HMAC-SHA1 用 Python 标准库实现；
未链接、未包含、未抄写任何 GPL 代码或商业软件实现。

------------------------------------------------------------------------------
v2 头（位于文件开头，大端；采用 dmg2img vfdecrypt.c 的经典 0x340 字节布局）
------------------------------------------------------------------------------
    0x000  8s   magic = b'encrcdsa'
    0x008  I    version = 2
    0x00C  I    enc_iv_size (AES IV 长度，实际取 16)
    0x018  I    keyBits (128 = AES-128；256 暂不支持)
    0x024  16s  uuid
    0x034  I    blocksize (数据分块大小，通常 4096)
    0x038  Q    datasize  (明文 DMG 真实字节数，可非块对齐)
    0x040  Q    dataoffset(密文数据起点绝对偏移)
    0x2A8  I    kdf_algorithm (0x67 = PKCS5_PBKDF2)
    0x2AC  I    kdf_prng_algorithm
    0x2B0  I    kdf_iteration_count
    0x2B4  I    kdf_salt_len
    0x2B8  32s  kdf_salt
    0x2D8  I    blob_enc_iv_size (>= 8，3DES 只取前 8 字节)
    0x2DC  32s  blob_enc_iv
    0x2FC  I    blob_enc_key_bits (= 192，3DES-EDE3 KEK 24 字节)
    0x300  I    blob_enc_algorithm (= 17，3DES_3KEY_EDE)
    0x30C  I    encrypted_keyblob_size (= 48)
    0x310  48s  encrypted_keyblob (3DES-CBC + PKCS#7)

v1 头（尾部 magic ``cdsaencr`` + 尾指针定位；采用 nlitsme 的布局）：
    文件末尾 20 字节 trailer: >QL8s = (offsetToHeader, 1, b'cdsaencr')
    header 从 offsetToHeader 起 0x4E8 字节，关键字段：
        0x10 I bytesPerBlock   0x20 I blobEncKeyBits  0x2C Q kdfIterationCount
        0x34 I kdfSaltLen      0x38 32s kdfSalt       0x58 I blockIvLen
        0x64 I keyBits         0x88 I wrappedAesKeyLen 0x8C wrappedAesKey
        0x1B4 I wrappedHmacKeyLen 0x1B8 wrappedHmacKey
    数据区 = [0, offsetToHeader)，每块 AES-CBC，IV 同 v2。

------------------------------------------------------------------------------
算法（v1 / v2 的数据叉一致，keyblob 解包方式不同）
------------------------------------------------------------------------------
* KEK = PBKDF2-HMAC-SHA1(password, salt[:salt_len], iterations, dkLen=24)。
* v2 keyblob：3DES-EDE3-CBC(KEK, blob_iv[:8]) 解密 + PKCS#7 去填充，
  明文 = AES_key(16) || HMAC-SHA1_key(20) [+ 可忽略的 Apple 尾部标签]。
* v1 两个 wrapped key：RFC 3217 风格两阶段 3DES-EDE3-CBC（固定 IV
  ``4A DD A2 2C 79 E8 21 05``、反转、PKCS#7、跳过 4 字节前缀）分别解出
  AES key 与 HMAC-SHA1 key。
* 数据叉按 blocksize 分块，第 i 块：
      IV  = HMAC-SHA1(hmac_key, struct.pack('>I', i))[:16]
      plain = AES-128-CBC(aes_key, IV).decrypt(cipher_chunk)
  末块按 datasize 截断，拼成完整明文 DMG。

关于 keyblob 明文切分：公开实现存在两种写法 —— 规范的 ``aes||hmac``
（非重叠，fstool / nlitsme）与 vfdecrypt.c 的历史重叠写法（hmac 取明文前
20 字节）。本实现**两种都尝试**：解开 keyblob 后，先用候选密钥解密最后一块
并校验明文 DMG 尾部 ``koly``，选中能还原合法 DMG 的方案，随后才全量解密。
密码错误或格式不支持时抛 :class:`DMGError`，绝不静默返回垃圾数据。

------------------------------------------------------------------------------
支持范围（做不到 / 未覆盖的部分如实说明）
------------------------------------------------------------------------------
* 支持：v1 / v2 + AES-128-CBC 数据叉 + 3DES-EDE3 keyblob + 口令解锁。
* 不支持：AES-256 数据叉、证书/公钥恢复记录、现代 macOS 重新设计的
  v2「key-entry 表 / 多 passphrase 记录」变体（字段偏移与本布局不同），
  以及 v2 头内其它非 3DES 的 keyblob 算法。遇到这类镜像会在解析或
  「明文 DMG 尾部 koly 自检」阶段失败，抛可读异常。
* 明文被假定为标准 UDIF DMG（尾部有合法 ``koly``）。解密产物用
  ``UDIFImage`` 打开以获得与现有栈一致的**逻辑磁盘** ``read`` 视图；
  同时通过 ``decrypted_path`` 暴露临时明文 DMG 路径，供集成方直接
  用 ``Disk(decrypted_path)`` / ``UDIFImage(decrypted_path)`` 打开。

安全：密码与派生密钥仅存在于内存，绝不写入日志、异常信息或磁盘文件。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import tempfile
import threading

from .udif import UDIFImage, DMGError

try:  # 可选依赖：缺库时给出中文说明，而不是 ModuleNotFoundError
    from Crypto.Cipher import AES, DES3
except ImportError:  # pragma: no cover - 取决于运行环境
    AES = None
    DES3 = None

__all__ = ['is_encrypted', 'EncryptedUDIF', 'DMGError']

V2_MAGIC = b'encrcdsa'
V1_MAGIC = b'cdsaencr'
CMS3DES_IV = bytes([0x4A, 0xDD, 0xA2, 0x2C, 0x79, 0xE8, 0x21, 0x05])
V2_HEADER_SIZE = 0x340
V1_HEADER_SIZE = 0x4E8
AES_BLOCK = 16
KOLY_SIZE = 512


# ---------------------------------------------------------------------------
# 密码学小工具（PBKDF2/HMAC 用标准库；对称分组密码用可选依赖）
# ---------------------------------------------------------------------------

def _require_crypto():
    if AES is None or DES3 is None:
        raise DMGError('缺少对称加密依赖：请运行 pip install pycryptodome '
                       '后重试（用于 3DES / AES 解密）')


def _pbkdf2(password: bytes, salt: bytes, iterations: int, dklen: int = 24) -> bytes:
    return hashlib.pbkdf2_hmac('sha1', password, salt, iterations, dklen=dklen)


def _chunk_iv(hmac_key: bytes, index: int) -> bytes:
    """第 index 块的 AES-CBC IV = HMAC-SHA1(hmac_key, >I index)[:16]。"""
    return hmac.new(hmac_key, struct.pack('>I', index & 0xFFFFFFFF),
                    hashlib.sha1).digest()[:AES_BLOCK]


def _pkcs7_strip(data: bytes, block: int = 8):
    if not data or len(data) % block:
        return None
    n = data[-1]
    if n < 1 or n > block or n > len(data):
        return None
    if data[-n:] != bytes([n]) * n:
        return None
    return data[:-n]


def _des3_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return DES3.new(key, DES3.MODE_CBC, iv).decrypt(data)


def _aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    # CBC 要求密文长度是 16 的整数倍
    usable = len(data) - len(data) % AES_BLOCK
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data[:usable])


# ---------------------------------------------------------------------------
# 快速识别
# ---------------------------------------------------------------------------

def is_encrypted(path: str) -> bool:
    """快速判断是否为旧式加密 DMG：v2 头 magic 在开头，或 v1 头在文件尾。

    普通 UDIF / 裸镜像返回 False；文件不存在或过小也返回 False。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < 20:
        return False
    try:
        with open(path, 'rb') as f:
            if f.read(8) == V2_MAGIC:
                return True
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
            if len(tail) == 12:
                version, sig = struct.unpack('>L8s', tail)
                if sig == V1_MAGIC and version == 1:
                    return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# keyblob 解包
# ---------------------------------------------------------------------------

def _unwrap_v2_keyblob(kek: bytes, blob_iv: bytes, blob: bytes):
    """v2：3DES-EDE3-CBC + PKCS#7。返回 keyblob 明文，失败返回 None。"""
    if len(blob_iv) < 8 or not blob or len(blob) % 8:
        return None
    try:
        plain = _des3_decrypt(kek, blob_iv[:8], blob)
    except (ValueError, KeyError):
        return None
    return _pkcs7_strip(plain, 8)


def _unwrap_v1_key(kek: bytes, blob: bytes):
    """v1：RFC 3217 风格两阶段 3DES 解包，返回 CEKICV[4:]，失败返回 None。"""
    if not blob or len(blob) % 8:
        return None
    try:
        t1 = _pkcs7_strip(_des3_decrypt(kek, CMS3DES_IV, blob), 8)
        if t1 is None or len(t1) < 8:
            return None
        t2 = t1[::-1]
        cekicv = _pkcs7_strip(_des3_decrypt(kek, t2[:8], t2[8:]), 8)
        if cekicv is None or len(cekicv) < 4:
            return None
        return cekicv[4:]
    except (ValueError, KeyError):
        return None


def _candidate_keys(keyblob: bytes):
    """返回 [(aes_key, hmac_key), ...]，覆盖规范切分与 vfdecrypt 历史重叠写法。"""
    out = []
    if len(keyblob) >= 36:
        out.append((keyblob[:16], keyblob[16:36]))      # 规范：aes || hmac
    if len(keyblob) >= 20:
        cand = (keyblob[:16], keyblob[:20])             # 历史：hmac = 明文[:20]
        if cand not in out:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# 明文 DMG 自检：用候选密钥解密最后一块，校验尾部 koly，选出正确方案
# ---------------------------------------------------------------------------

def _probe_scheme(fh, data_offset: int, data_len: int, block_size: int,
                  candidates):
    """用候选密钥解密「koly 所在块 → 最后一块」，校验能否还原合法明文 DMG。

    明文 DMG 大小常不是 block_size 的整数倍，尾部 512 字节的 ``koly``
    trailer 可能横跨两个加密块（例如 4135 字节明文 / 4096 块大小时，
    koly 从第 0 块 3623 偏移起、延续到第 1 块）。因此这里从 koly 起始块
    一直解密到最后一块并拼接，再按绝对偏移校验 magic。
    """
    nblocks = (data_len + block_size - 1) // block_size
    if nblocks <= 0:
        return None
    koly_pos = max(0, data_len - KOLY_SIZE)
    start = koly_pos // block_size

    cipher_chunks = []
    for i in range(start, nblocks):
        fh.seek(data_offset + i * block_size)
        ct = fh.read(block_size)
        if len(ct) < block_size:
            ct += b'\x00' * (block_size - len(ct))
        cipher_chunks.append(ct)

    rel_koly = koly_pos - start * block_size
    for aes_key, hmac_key in candidates:
        try:
            plain = b''.join(
                _aes_decrypt(aes_key, _chunk_iv(hmac_key, start + j), ct)
                for j, ct in enumerate(cipher_chunks))
        except (ValueError, KeyError):
            continue
        if (0 <= rel_koly <= len(plain) - 4
                and plain[rel_koly:rel_koly + 4] == b'koly'):
            return (aes_key, hmac_key)
        if b'koly' in plain:  # 宽松兜底
            return (aes_key, hmac_key)
    return None


def _decrypt_to_temp(fh, out_path: str, data_offset: int, data_len: int,
                     block_size: int, aes_key: bytes, hmac_key: bytes):
    """流式逐块解密，写入临时文件；末块按 data_len 截断。"""
    nblocks = (data_len + block_size - 1) // block_size
    with open(out_path, 'wb') as w:
        remaining = data_len
        for i in range(nblocks):
            fh.seek(data_offset + i * block_size)
            ct = fh.read(block_size)
            if len(ct) < block_size:  # 末块不足则补零（正常样本不会触发）
                ct += b'\x00' * (block_size - len(ct))
            pt = _aes_decrypt(aes_key, _chunk_iv(hmac_key, i), ct)
            take = min(block_size, remaining)
            w.write(pt[:take])
            remaining -= take


# ---------------------------------------------------------------------------
# 头解析
# ---------------------------------------------------------------------------

def _parse_v2(fh, file_size: int) -> dict:
    fh.seek(0)
    head = fh.read(V2_HEADER_SIZE)
    if len(head) < V2_HEADER_SIZE or head[:8] != V2_MAGIC:
        raise DMGError('加密 DMG v2 头不完整或 magic 不符')

    def u32(o):
        return struct.unpack_from('>I', head, o)[0]

    def u64(o):
        return struct.unpack_from('>Q', head, o)[0]

    if u32(0x08) != 2:
        raise DMGError('不支持的 encrcdsa 版本（仅支持 v2）')
    key_bits = u32(0x18)
    if key_bits == 256:
        raise DMGError('暂不支持 AES-256 加密的 DMG（本实现支持 AES-128）')
    if key_bits != 128:
        raise DMGError('不支持的加密位数 %d（仅支持 AES-128）' % key_bits)

    block_size = u32(0x34)
    data_size = u64(0x38)
    data_offset = u64(0x40)
    iterations = u32(0x2B0)
    salt_len = u32(0x2B4)
    salt = head[0x2B8:0x2B8 + min(salt_len, 32)]
    blob_iv_size = u32(0x2D8)
    blob_iv = head[0x2DC:0x2DC + min(blob_iv_size, 32)]
    blob_key_bits = u32(0x2FC)
    blob_alg = u32(0x300)
    keyblob_size = u32(0x30C)
    keyblob = head[0x310:0x310 + min(keyblob_size, 0x30)]

    _validate_geometry(block_size, data_size, data_offset, file_size)
    if not iterations or iterations > 10_000_000:
        raise DMGError('加密 DMG 的 PBKDF2 迭代次数非法')
    if not salt:
        raise DMGError('加密 DMG 的 PBKDF2 salt 为空')
    if blob_key_bits != 192 or blob_alg not in (3, 17):
        raise DMGError('不支持的 keyblob 算法（仅支持 3DES-EDE3）')
    if len(blob_iv) < 8 or not keyblob or len(keyblob) % 8:
        raise DMGError('加密 DMG 的 keyblob / IV 非法')

    return {
        'version': 2,
        'block_size': block_size,
        'data_size': data_size,
        'data_offset': data_offset,
        'iterations': iterations,
        'salt': salt,
        'blob_iv': blob_iv,
        'keyblob': keyblob,
    }


def _validate_geometry(block_size, data_size, data_offset, file_size):
    if block_size < 512 or block_size > (1 << 20) or block_size % AES_BLOCK:
        raise DMGError('加密 DMG 的数据块大小非法：%d' % block_size)
    if data_size <= 0:
        raise DMGError('加密 DMG 的明文长度非法')
    nblocks = (data_size + block_size - 1) // block_size
    cipher_len = nblocks * block_size
    if data_offset + cipher_len > file_size:
        raise DMGError('加密 DMG 的数据叉越界（头字段与文件大小不符，'
                       '可能是现代 v2 变体，本实现不支持）')


def _parse_v1(fh, file_size: int) -> dict:
    fh.seek(-12, os.SEEK_END)
    ver, sig = struct.unpack('>L8s', fh.read(12))
    if sig != V1_MAGIC or ver != 1:
        raise DMGError('加密 DMG v1 尾部 magic 不符')
    fh.seek(-20, os.SEEK_END)
    offset_to_header, ver2, sig2 = struct.unpack('>QL8s', fh.read(20))
    if sig2 != V1_MAGIC or offset_to_header + V1_HEADER_SIZE + 20 > file_size:
        raise DMGError('加密 DMG v1 头指针非法')

    fh.seek(offset_to_header)
    hdr = fh.read(V1_HEADER_SIZE)
    if len(hdr) < V1_HEADER_SIZE:
        raise DMGError('加密 DMG v1 头不完整')

    def u32(o):
        return struct.unpack_from('>I', hdr, o)[0]

    block_size = u32(0x10)
    blob_key_bits = u32(0x20)
    iterations = struct.unpack_from('>Q', hdr, 0x2C)[0]
    salt_len = u32(0x34)
    salt = hdr[0x38:0x38 + min(salt_len, 32)]
    w_aes_len = u32(0x88)
    w_aes = hdr[0x8C:0x8C + w_aes_len] if 0x8C + w_aes_len <= V1_HEADER_SIZE else b''
    w_hmac_len = u32(0x1B4)
    w_hmac = (hdr[0x1B8:0x1B8 + w_hmac_len]
              if 0x1B8 + w_hmac_len <= V1_HEADER_SIZE else b'')

    data_size = offset_to_header
    _validate_geometry(block_size, data_size, 0, file_size)
    if blob_key_bits != 192 or not iterations or iterations > 10_000_000 or not salt:
        raise DMGError('加密 DMG v1 的 KDF 参数非法')
    if not w_aes or not w_hmac or len(w_aes) % 8 or len(w_hmac) % 8:
        raise DMGError('加密 DMG v1 的 wrapped key 非法')

    return {
        'version': 1,
        'block_size': block_size,
        'data_size': data_size,
        'data_offset': 0,
        'iterations': iterations,
        'salt': salt,
        'w_aes': w_aes,
        'w_hmac': w_hmac,
    }


# ---------------------------------------------------------------------------
# 对外 reader
# ---------------------------------------------------------------------------

class EncryptedUDIF:
    """把加密 DMG 解密成完整明文 DMG，并作为逻辑磁盘 reader 对外服务。

    ``__init__(path, password)``：验证密码、解包 keyblob、逐块解密到临时
    明文 DMG 文件，并用 ``UDIFImage`` 打开得到逻辑磁盘视图。

    对外契约（与 ``UDIFImage`` / ``RawImage`` 一致，可直接给 ``image.Disk``）：
        read(offset, length) / size / logical_size / close() / summary()
    另有：
        decrypted_path : 临时明文 DMG 路径（集成方也可直接 Disk(path) 打开）
        plain_read(offset, length) : 直接读取明文 DMG **文件字节**

    ``close()`` 会关闭内部 reader 并删除临时文件；支持 with 上下文管理。
    """

    def __init__(self, path: str, password):
        _require_crypto()
        self.path = path
        self._lock = threading.RLock()
        self._tmp_path = None
        self._inner = None
        self._version = None
        self._plain_fh = None

        if isinstance(password, str):
            pw = password.encode('utf-8')
        else:
            pw = bytes(password)
        # 注意：pw / 派生密钥仅在内存中使用，绝不写日志或文件。

        file_size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            head = fh.read(8)
            if head == V2_MAGIC:
                info = _parse_v2(fh, file_size)
            else:
                fh.seek(-12, os.SEEK_END)
                tail = fh.read(12)
                if len(tail) == 12 and struct.unpack('>L8s', tail)[1] == V1_MAGIC:
                    info = _parse_v1(fh, file_size)
                else:
                    raise DMGError('不是受支持的加密 DMG'
                                   '（未发现 encrcdsa / cdsaencr 头）')
            self._version = info['version']
            self._data_len = info['data_size']

            kek = _pbkdf2(pw, info['salt'], info['iterations'], dklen=24)
            keyblob = self._unwrap_keyblob(kek, info)
            if keyblob is None or len(keyblob) < 20:
                raise DMGError('密码错误，或 keyblob 解包失败'
                               '（不支持的加密格式）')

            candidates = _candidate_keys(keyblob)
            if not candidates:
                raise DMGError('密码错误，或 keyblob 明文过短')
            scheme = _probe_scheme(fh, info['data_offset'], info['data_size'],
                                   info['block_size'], candidates)
            if scheme is None:
                raise DMGError('密码错误，或不支持的加密格式'
                               '（解密出的明文 DMG 校验失败，已拒绝返回垃圾数据）')

            fd, tmp = tempfile.mkstemp(prefix='omd_enc_', suffix='.dmg')
            os.close(fd)
            self._tmp_path = tmp
            try:
                _decrypt_to_temp(fh, tmp, info['data_offset'],
                                 info['data_size'], info['block_size'],
                                 scheme[0], scheme[1])
                # 最终校验：明文必须是可被现有解析器读取的 UDIF DMG
                self._inner = UDIFImage(tmp)
            except DMGError:
                self.close()
                raise
            except Exception as e:  # pragma: no cover
                self.close()
                raise DMGError('密码错误，或明文 DMG 无法解析：%s' % type(e).__name__)

        self._plain_fh = open(self._tmp_path, 'rb')

    # ---- keyblob 解包（v1 / v2 分派） ----

    def _unwrap_keyblob(self, kek: bytes, info: dict):
        if info['version'] == 2:
            return _unwrap_v2_keyblob(kek, info['blob_iv'], info['keyblob'])
        aes = _unwrap_v1_key(kek, info['w_aes'])
        hm = _unwrap_v1_key(kek, info['w_hmac'])
        if aes is None or hm is None or len(aes) < 16 or len(hm) < 20:
            return None
        # 归一成与 v2 相同的 "aes || hmac" 明文，便于统一的候选切分 + 自检
        return aes[:16] + hm[:20]

    # ---- reader 契约 ----

    @property
    def decrypted_path(self):
        return self._tmp_path

    @property
    def size(self):
        return self._inner.size

    @property
    def logical_size(self):
        return self._inner.logical_size

    def read(self, offset: int, length: int) -> bytes:
        """逻辑磁盘字节读取（委托给内部 UDIFImage，可直接给 image.Disk）。"""
        return self._inner.read(offset, length)

    def plain_read(self, offset: int, length: int) -> bytes:
        """读取明文 DMG 的**文件字节**（非逻辑磁盘视图）。"""
        if length <= 0 or offset >= self._data_len:
            return b''
        with self._lock:
            self._plain_fh.seek(offset)
            return self._plain_fh.read(min(length, self._data_len - offset))

    def close(self):
        with self._lock:
            if self._plain_fh is not None:
                try:
                    self._plain_fh.close()
                except OSError:
                    pass
                self._plain_fh = None
            if self._inner is not None:
                try:
                    self._inner.close()
                except Exception:
                    pass
                self._inner = None
            if self._tmp_path:
                try:
                    os.remove(self._tmp_path)
                except OSError:
                    pass
                self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

    def summary(self) -> dict:
        out = {}
        if self._inner is not None and hasattr(self._inner, 'summary'):
            try:
                out.update(self._inner.summary())
            except Exception:
                pass
        out.update({
            'format': 'UDIF/DMG (encrcdsa)',
            'encrypted': True,
            'encryption_version': self._version,
            'encryption': 'AES-128-CBC + HMAC-SHA1 IV',
            'keyblob': '3DES-EDE3-CBC',
            'kdf': 'PBKDF2-HMAC-SHA1',
            'plain_dmg_size': self._data_len,
            'decrypted_path': self._tmp_path,
        })
        return out
