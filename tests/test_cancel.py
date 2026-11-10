# -*- coding: utf-8 -*-
"""「取消」按钮的回归测试。

覆盖两件事：
1. 耗时任务中途取消后，Split 产生的分片必须被清理，不留垃圾；
2. 进度对话框必须启用 wx.PD_CAN_ABORT（否则取消按钮永远是灰的），
   且 run_task 带 out_path 用于删除未完成的输出文件。
"""
import io
import os
import shutil
import tempfile
import unittest

from openmacdisk import writer


class Cancelled(Exception):
    """模拟进度回调里抛出的取消异常（单测不依赖 GUI 模块）。"""


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='omd_cancel_')
        self.src = os.path.join(self.tmp, '大镜像.dmg')
        with open(self.src, 'wb') as f:
            f.write((b'OpenMacDisk cancel test\n' * 120000)[:2_500_000])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_split_cancel_removes_parts(self):
        calls = [0]

        def cb(_pct, _msg):
            calls[0] += 1
            if calls[0] >= 2:      # 写到一半时用户点取消（每 1 MiB 报一次）
                raise Cancelled()

        with self.assertRaises(Cancelled):
            writer.split_image(self.src, chunk_mb=1, cb=cb)
        d = os.path.join(self.tmp, '大镜像.dmg_split')
        left = os.listdir(d) if os.path.isdir(d) else []
        self.assertEqual(left, [], '取消后不应留下分片或空目录：%s' % left)

    def test_split_without_cancel_still_works(self):
        m = writer.split_image(self.src, chunk_mb=1)
        self.assertEqual(len(m['parts']), 3)
        self.assertTrue(os.path.isfile(m['manifest']))

    def test_progress_dialog_cancel_enabled(self):
        path = os.path.join(os.path.dirname(__file__), os.pardir, 'desktop.py')
        with io.open(os.path.normpath(path), encoding='utf-8') as f:
            src = f.read()
        self.assertIn('wx.PD_CAN_ABORT', src)
        self.assertIn('class TaskCancelled', src)
        self.assertIn('def run_task(self, title, fn, done=None, out_path=None)',
                      src)
        self.assertIn('raise TaskCancelled()', src)
        self.assertIn('wx.PD_CAN_ABORT', src)
        # 取消后不再执行完成回调，也不弹「操作失败」
        self.assertIn('if cancelled:', src)


if __name__ == '__main__':
    unittest.main()
