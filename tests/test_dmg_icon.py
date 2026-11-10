# -*- coding: utf-8 -*-
"""透明 DMG 文件图标的产物校验（只读，不写注册表）。"""
from __future__ import annotations

import io
import os
import struct
import unittest

from PIL import Image

from openmacdisk import shell

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICO = os.path.join(ROOT, 'dmg_file.ico')
ART = os.path.join(ROOT, '_build', 'dmg_art.png')


def read_ico_frames(path):
    data = open(path, 'rb').read()
    reserved, typ, count = struct.unpack_from('<HHH', data, 0)
    assert (reserved, typ) == (0, 1)
    out = []
    for i in range(count):
        w, h, colors, rsv, planes, bits, length, off = struct.unpack_from(
            '<BBBBHHII', data, 6 + 16 * i)
        out.append((w or 256, data[off:off + length]))
    return out


class TestDmgIcon(unittest.TestCase):

    def test_icon_exists_and_has_all_sizes(self):
        self.assertTrue(os.path.isfile(ICO))
        frames = read_ico_frames(ICO)
        self.assertEqual([s for s, _ in frames], [256, 128, 64, 48, 32, 16])

    def test_every_frame_is_transparent_rgba(self):
        for size, blob in read_ico_frames(ICO):
            im = Image.open(io.BytesIO(blob)).convert('RGBA')
            self.assertEqual(im.size, (size, size))
            a = im.getchannel('A')
            self.assertEqual(a.getpixel((0, 0)), 0, '左上角必须透明')
            self.assertEqual(a.getpixel((size - 1, size - 1)), 0, '右下角必须透明')
            self.assertGreater(max(a.getdata()), 0, '主体必须保留')

    def test_art_master_is_transparent_and_watermark_removed(self):
        if not os.path.isfile(ART):            # 母版不随交付强制存在
            self.skipTest('透明母版未生成')
        im = Image.open(ART).convert('RGBA')
        a = im.getchannel('A')
        for xy in ((0, 0), (im.width - 1, 0),
                   (0, im.height - 1), (im.width - 1, im.height - 1)):
            self.assertEqual(a.getpixel(xy), 0)
        self.assertEqual(a.getpixel((int(im.width * .88),
                                     int(im.height * .96))), 0, '水印区须透明')
        self.assertEqual(a.getpixel((im.width // 2, im.height // 2)), 255)

    def test_icon_path_is_persistent_and_next_to_program(self):
        p = shell.dmg_icon_path()
        self.assertEqual(os.path.basename(p), 'dmg_file.ico')
        self.assertTrue(os.path.isabs(p))
        self.assertEqual(os.path.normcase(p), os.path.normcase(ICO))

    def test_icon_registry_keys_target_hkcu_only(self):
        self.assertEqual(len(shell.DMG_ICON_KEYS), 2)
        for k in shell.DMG_ICON_KEYS:
            self.assertTrue(k.lower().startswith('software\\classes'))
            self.assertTrue(k.endswith('DefaultIcon'))

    def test_progid_key_included_without_touching_open_with(self):
        """Windows 优先读 ProgID 的 DefaultIcon，必须一并覆盖；但不能写
        扩展名键本身，否则会顶掉本机原有的 .dmg 默认打开方式。"""
        if shell.winreg is None:
            self.skipTest('仅 Windows 有注册表')
        keys = shell.dmg_icon_registry_keys()
        self.assertTrue(all(k.endswith('DefaultIcon') for k in keys))
        self.assertNotIn(r'Software\Classes\.dmg', keys)
        try:
            with shell.winreg.OpenKey(shell.winreg.HKEY_CLASSES_ROOT,
                                      '.dmg') as k:
                progid, _ = shell.winreg.QueryValueEx(k, '')
            if progid:
                self.assertIn(r'Software\Classes\%s\DefaultIcon' % progid,
                              keys)
        except OSError:
            pass


if __name__ == '__main__':
    unittest.main()
