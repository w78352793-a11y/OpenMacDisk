# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真正实例化主窗口并切换外观主题。

此前几轮只做「编译 + 导入」冒烟，漏掉了 _build_ui 里的未定义变量：
v1.4.0 把 `panel` 改成 `self.panel` 时漏改 11 处引用，程序启动即抛
`NameError: name 'panel' is not defined` 直接闪退；同一批改动里还有
`is_device` 未定义（打开镜像后才触发）。两者都只有真正跑起来才会暴露。

本测试会：
  1) 构造 MainFrame（覆盖 _build_ui / _build_menu / _apply_theme）；
  2) 逐个切换全部外观主题；
  3) 跑一遍 on_loaded 的状态栏分支（含 is_device 判定）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class GuiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wx
        cls.wx = wx
        cls.app = wx.App(False)      # False = 不接管 stdout

    def _frame(self):
        import desktop
        return desktop.MainFrame()

    def test_main_frame_builds(self):
        frame = self._frame()
        try:
            self.assertIsNotNone(frame.panel)
            self.assertIsNotNone(frame.path_txt)
            self.assertIsNotNone(frame.info)
            self.assertTrue(frame.op_buttons, '工具栏按钮列表为空')
            self.assertTrue(frame.list, '文件列表未创建')
        finally:
            frame.Destroy()

    def test_all_themes_apply(self):
        for theme in ('light', 'dark', 'glass', 'acrylic'):
            with self.subTest(theme=theme):
                frame = self._frame()
                try:
                    frame._apply_theme(theme)
                finally:
                    frame.Destroy()

    def test_on_loaded_state_branches(self):
        """真跑 on_loaded，覆盖 is_device 判定与信息栏状态文案。

        on_loaded 里曾使用未定义的 `is_device`（应为 summary['format']），
        只有打开镜像才会触发。这里用合成 summary 覆盖三条分支。
        """
        import desktop
        frame = self._frame()
        orig_recent = list(frame.recent)
        saved = []
        try:
            # 不污染用户的「最近打开」列表
            desktop.save_recent = lambda items: saved.append(list(items))
            frame._render_volume = lambda path, vol: None
            frame._sync_convert_menu = lambda: None
            for fmt, expect in (('DEVICE', '物理磁盘'),
                                ('UDIF', '需先 eXpand'),
                                ('RAW', '需先 eXpand')):
                summary = {'format': fmt, 'file_size': 1, 'logical_size': 1,
                           'partitions': [], 'volumes': [], 'errors': []}
                self.wx.BeginBusyCursor()   # on_loaded 里配对 EndBusyCursor
                frame.on_loaded('/__fake__.dmg', None, summary, None)
                label = frame.info.GetLabel()
                self.assertIn(expect, label,
                              'format=%s 的状态文案不对: %s' % (fmt, label))
        finally:
            frame.recent = orig_recent
            frame.Destroy()

    def test_device_flag_used_in_status(self):
        """确保 on_loaded 用的是 summary['format'] 而不是未定义名。"""
        import desktop
        with open(desktop.__file__, encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotIn('if is_device:', src,
                         'on_loaded 仍在使用未定义的 is_device')
        self.assertIn("summary.get('format') == 'DEVICE'", src)

    def test_settings_switching(self):
        """语言 / 外观切换（v1.4.0 新增，从未被真正执行过）。"""
        import desktop
        from openmacdisk import config as _config

        path = os.path.join(os.environ.get('APPDATA') or
                            os.path.expanduser('~'), 'OpenMacDisk',
                            'config.json')
        backup = None
        if os.path.isfile(path):
            with open(path, 'rb') as fh:
                backup = fh.read()

        class _Ev:
            def __init__(self, mid):
                self._id = mid

            def GetId(self):
                return self._id

        frame = self._frame()
        try:
            # 语言：切到英文再切回中文
            frame.on_lang(_Ev(desktop.ID_LANG_EN))
            self.assertEqual(frame.cfg['language'], 'en_US')
            frame.on_lang(_Ev(desktop.ID_LANG_ZH))
            self.assertEqual(frame.cfg['language'], 'zh_CN')
            # 外观：逐个主题
            for i, key in enumerate(desktop.THEME_ORDER):
                frame.on_theme(_Ev(desktop.ID_THEME_BASE + i))
                self.assertEqual(frame.theme, key)
            frame._sync_settings_checked()
            frame._sync_convert_menu()
        finally:
            frame.Destroy()
            # 还原用户设置
            if backup is not None:
                with open(path, 'wb') as fh:
                    fh.write(backup)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
