# -*- coding: utf-8 -*-
"""加密 DMG（encrcdsa v1/v2）解密的**自洽**单元测试。

测试里用公开格式规范独立实现「加密构造器」（与被测解密器不共享私有辅助
函数），把一个由本仓库 ``writer.create_blank_dmg`` 生成的小明文 DMG 按
v2 / v1 格式加密落盘，再用 ``EncryptedUDIF`` 解密，断言逐字节一致。

覆盖：
  * v2 往返 + 明文 DMG 逐字节一致 + is_encrypted + close() 清理临时文件
  * v2 reader 契约（read / size / logical_size / summary / 交给上层解析）
  * v2 「重叠 keyblob」历史写法仍能被自动识别
  * 普通 DMG 的 is_encrypted 为 False、误当加密 DMG 打开抛异常
  * 错误密码抛中文 DMGError（v1 / v2 都测）
  * v1 往返（尾部 cdsaencr 头）

用 tempfile 且自行清理。
"""
import hashlib
import hmac
import os
import shutil
import struct
import tempfile
import unittest

from Crypto.Cipher import AES, DES3

from openmacdisk import partition, writer
from openmacdisk.crypto_dmg import (DMGError, EncryptedUDIF, is_encrypted)
from openmacdisk.image import Disk
from openmacdisk.udif import UDIFImage

PASSWORD = 'p@ssw0rd-加密测试'
BLOCK = 4096
CMS3DES_IV = bytes([0x4A, 0xDD, 0xA2, 0x2C, 0x79, 0xE8, 0x21, 0x05])


# ---------------------------------------------------------------------------
# 独立的加密构造器（净室：只按公开规范/算法描述编写，不 import 被测模块私有函数）
# ---------------------------------------------------------------------------

def _pkcs7_pad(data, block=8):
    n = block - len(data) % block
    return data + bytes([n]) * n


def _kek(password, salt, iters):
    return hashlib.pbkdf2_hmac('sha1', password, salt, iters, dklen=24)


def _chunk_iv(hmac_key, i):
    return hmac.new(hmac_key, struct.pack('>I', i), hashlib.sha1).digest()[:16]


def _des3_enc(key, iv, data):
    return DES3.new(key, DES3.MODE_CBC, iv).encrypt(data)


def _aes_enc(key, iv, data):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(data)


def _encrypt_fork(plain, aes_key, hmac_key, block=BLOCK):
    """明文按 block 补零对齐后，逐块 AES-128-CBC 加密。"""
    padded = plain + b'\x00' * (-len(plain) % block)
    out = bytearray()
    for i in range(len(padded) // block):
        out += _aes_enc(aes_key, _chunk_iv(hmac_key, i),
                        padded[i * block:(i + 1) * block])
    return bytes(out)


def build_v2(plain, password=PASSWORD, scheme='split', iters=1000,
             block=BLOCK, data_offset=0x1000):
    """构造 encrcdsa v2 加密 DMG 字节。

    scheme='split'  : keyblob 明文 = aes(16) || hmac(20) || b'CKIE\\x00'（规范切分）
    scheme='overlap': keyblob 明文 = hmac(20) || b'CKIE\\x00'，且 aes = hmac[:16]
                      （对应 vfdecrypt.c 的历史重叠写法）
    """
    pw = password.encode('utf-8') if isinstance(password, str) else bytes(password)
    hmac_key = os.urandom(20)
    if scheme == 'split':
        aes_key = os.urandom(16)
        keyblob_plain = aes_key + hmac_key + b'CKIE\x00'
    else:
        aes_key = hmac_key[:16]
        keyblob_plain = hmac_key + b'CKIE\x00'
        hmac_key = hmac_key           # hmac 仍是完整 20 字节

    salt = os.urandom(20)
    blob_iv = os.urandom(8)
    kek = _kek(pw, salt, iters)
    keyblob_enc = _des3_enc(kek, blob_iv, _pkcs7_pad(keyblob_plain, 8))
    assert len(keyblob_enc) <= 0x30

    ct = _encrypt_fork(plain, aes_key, hmac_key, block)

    hdr = bytearray(0x340)
    struct.pack_into('>8s', hdr, 0x00, b'encrcdsa')
    struct.pack_into('>I', hdr, 0x08, 2)         # version
    struct.pack_into('>I', hdr, 0x0C, 16)        # enc_iv_size
    struct.pack_into('>I', hdr, 0x10, 0)         # encryption_mode: AES-128
    struct.pack_into('>I', hdr, 0x14, 1)         # AES_CBC
    struct.pack_into('>I', hdr, 0x18, 128)       # keyBits
    struct.pack_into('>I', hdr, 0x34, block)     # blocksize
    struct.pack_into('>Q', hdr, 0x38, len(plain))  # datasize（明文真实长度）
    struct.pack_into('>Q', hdr, 0x40, data_offset) # dataoffset
    struct.pack_into('>I', hdr, 0x2A8, 0x67)     # kdf_algorithm
    struct.pack_into('>I', hdr, 0x2B0, iters)
    struct.pack_into('>I', hdr, 0x2B4, len(salt))
    hdr[0x2B8:0x2B8 + len(salt)] = salt
    struct.pack_into('>I', hdr, 0x2D8, 8)        # blob_enc_iv_size
    hdr[0x2DC:0x2DC + 8] = blob_iv
    struct.pack_into('>I', hdr, 0x2FC, 192)      # blob_enc_key_bits
    struct.pack_into('>I', hdr, 0x300, 17)       # 3DES_3KEY_EDE
    struct.pack_into('>I', hdr, 0x304, 7)        # PKCS7
    struct.pack_into('>I', hdr, 0x308, 6)        # CBCPadIV8
    struct.pack_into('>I', hdr, 0x30C, len(keyblob_enc))
    hdr[0x310:0x310 + len(keyblob_enc)] = keyblob_enc

    out = bytearray(data_offset)
    out[:0x340] = hdr
    return bytes(out) + ct


def _wrap_v1_key(kek, key):
    """RFC 3217 风格两阶段 3DES 包装（解密器 _unwrap_v1_key 的逆过程）。"""
    cekicv = b'\x00' * 4 + key
    iv2 = os.urandom(8)
    t2 = iv2 + _des3_enc(kek, iv2, _pkcs7_pad(cekicv, 8))
    t1 = t2[::-1]
    return _des3_enc(kek, CMS3DES_IV, _pkcs7_pad(t1, 8))


def build_v1(plain_aligned, password=PASSWORD, iters=1000, block=BLOCK):
    """构造 cdsaencr v1 加密 DMG 字节（plain_aligned 长度须为 block 倍数）。"""
    assert len(plain_aligned) % block == 0
    pw = password.encode('utf-8') if isinstance(password, str) else bytes(password)
    aes_key = os.urandom(16)
    hmac_key = os.urandom(20)
    salt = os.urandom(20)
    kek = _kek(pw, salt, iters)
    w_aes = _wrap_v1_key(kek, aes_key)     # 40 字节
    w_hmac = _wrap_v1_key(kek, hmac_key)   # 48 字节

    ct = _encrypt_fork(plain_aligned, aes_key, hmac_key, block)
    offset_to_header = len(ct)

    hdr = bytearray(0x4E8)
    struct.pack_into('>I', hdr, 0x10, block)      # bytesPerBlock
    struct.pack_into('>I', hdr, 0x20, 192)        # blobEncKeyBits
    struct.pack_into('>Q', hdr, 0x2C, iters)      # kdfIterationCount
    struct.pack_into('>I', hdr, 0x34, len(salt))
    hdr[0x38:0x38 + len(salt)] = salt
    struct.pack_into('>I', hdr, 0x58, 16)         # blockIvLen
    struct.pack_into('>I', hdr, 0x64, 128)        # keyBits
    struct.pack_into('>I', hdr, 0x88, len(w_aes))
    hdr[0x8C:0x8C + len(w_aes)] = w_aes
    struct.pack_into('>I', hdr, 0x1B4, len(w_hmac))
    hdr[0x1B8:0x1B8 + len(w_hmac)] = w_hmac

    trailer = struct.pack('>QL8s', offset_to_header, 1, b'cdsaencr')
    return bytes(ct) + bytes(hdr) + trailer


def align_plain_dmg(dmg_bytes, block=BLOCK):
    """把明文 DMG 调整成 block 对齐、且 koly 仍在最末尾 512 字节的合法 DMG。

    做法：在 plist 之后、koly 之前插零填充。koly 里的 xml_off / xml_len 与
    blkx run 偏移都指向前半部分，不受影响，UDIFImage 仍能正常解析。
    """
    koly = dmg_bytes[-512:]
    body = dmg_bytes[:-512]
    pad = (-len(dmg_bytes)) % block
    return body + b'\x00' * pad + koly


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestEncryptedDMG(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_enc_test_')
        self.plain_path = os.path.join(self.tmp, 'plain.dmg')
        writer.create_blank_dmg(self.plain_path, '加密样本', 2)
        with open(self.plain_path, 'rb') as f:
            self.plain = f.read()
        self.plain_aligned = align_plain_dmg(self.plain)
        self._opened = []

    def tearDown(self):
        for enc in self._opened:
            try:
                enc.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data, name):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def _open(self, path, password=PASSWORD):
        enc = EncryptedUDIF(path, password)
        self._opened.append(enc)
        return enc

    def test_v2_roundtrip_bytes_and_cleanup(self):
        path = self._write(build_v2(self.plain), 'enc_v2.dmg')
        self.assertTrue(is_encrypted(path))

        enc = self._open(path)
        # 解出的「明文 DMG 字节流」与原始明文逐字节一致
        self.assertEqual(enc.plain_read(0, enc._data_len), self.plain)
        with open(enc.decrypted_path, 'rb') as f:
            self.assertEqual(f.read(), self.plain)
        tmp = enc.decrypted_path
        self.assertTrue(os.path.exists(tmp))
        enc.close()
        self.assertFalse(os.path.exists(tmp), 'close() 应删除临时明文 DMG')
        self._opened.remove(enc)

    def test_v2_reader_contract(self):
        path = self._write(build_v2(self.plain), 'enc_v2b.dmg')
        enc = self._open(path)

        # 与直接用 UDIFImage 打开明文 DMG 的逻辑磁盘视图一致
        with UDIFImage(self.plain_path) as u:
            self.assertEqual(enc.read(0, 4096), u.read(0, 4096))
            self.assertEqual(enc.read(1024, 512), u.read(1024, 512))
            self.assertEqual(enc.logical_size, u.logical_size)
            self.assertEqual(enc.size, u.size)
            # reader 可直接给分区解析层使用
            self.assertEqual(partition.parse(enc), partition.parse(u))

        s = enc.summary()
        self.assertTrue(s['encrypted'])
        self.assertEqual(s['encryption_version'], 2)
        self.assertIn('PBKDF2', s['kdf'])

        # 集成方也可用 decrypted_path 走现有 Disk
        with Disk(enc.decrypted_path) as d:
            self.assertGreaterEqual(len(d.volumes), 1)

    def test_v2_overlap_keyblob_scheme(self):
        # vfdecrypt.c 历史重叠写法：解密器应自动识别正确密钥方案
        path = self._write(build_v2(self.plain, scheme='overlap'),
                           'enc_v2_overlap.dmg')
        self.assertTrue(is_encrypted(path))
        enc = self._open(path)
        self.assertEqual(enc.plain_read(0, enc._data_len), self.plain)

    def test_is_encrypted_and_plain_dmg(self):
        self.assertFalse(is_encrypted(self.plain_path))  # 普通 DMG
        # 小文件 / 不存在文件
        tiny = self._write(b'\x00' * 10, 'tiny.dmg')
        self.assertFalse(is_encrypted(tiny))
        self.assertFalse(is_encrypted(os.path.join(self.tmp, 'nope.dmg')))

        v2 = self._write(build_v2(self.plain), 'enc_v2c.dmg')
        v1 = self._write(build_v1(self.plain_aligned), 'enc_v1c.dmg')
        self.assertTrue(is_encrypted(v2))
        self.assertTrue(is_encrypted(v1))

        # 把普通 DMG 误当加密 DMG 打开：抛可读异常，不返回垃圾
        with self.assertRaises(DMGError):
            EncryptedUDIF(self.plain_path, PASSWORD)

    def test_wrong_password(self):
        v2 = self._write(build_v2(self.plain), 'enc_v2d.dmg')
        with self.assertRaises(DMGError) as cm:
            EncryptedUDIF(v2, 'wrong-password')
        self.assertIn('密码错误', str(cm.exception))

        v1 = self._write(build_v1(self.plain_aligned), 'enc_v1d.dmg')
        with self.assertRaises(DMGError) as cm:
            EncryptedUDIF(v1, 'another-wrong-password')
        self.assertIn('密码错误', str(cm.exception))

    def test_v1_roundtrip(self):
        path = self._write(build_v1(self.plain_aligned), 'enc_v1.dmg')
        self.assertTrue(is_encrypted(path))
        enc = self._open(path)
        self.assertEqual(enc.summary()['encryption_version'], 1)
        # 解出的明文 DMG 字节流与构造时的对齐明文逐字节一致
        self.assertEqual(enc.plain_read(0, enc._data_len), self.plain_aligned)
        with open(enc.decrypted_path, 'rb') as f:
            self.assertEqual(f.read(), self.plain_aligned)
        aligned_path = self._write_align()
        with UDIFImage(aligned_path) as u:
            self.assertEqual(enc.read(0, 4096), u.read(0, 4096))

    def _write_align(self):
        p = os.path.join(self.tmp, 'plain_aligned.dmg')
        with open(p, 'wb') as f:
            f.write(self.plain_aligned)
        return p


if __name__ == '__main__':
    unittest.main()
