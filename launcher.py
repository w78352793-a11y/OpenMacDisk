# -*- coding: utf-8 -*-
"""OpenMacDisk 桌面程序入口。

打包成单个 exe 后双击即用：
  1. 在本机 127.0.0.1 启动只读解析服务；
  2. 用 Edge/Chrome 的 --app 模式打开一个无地址栏的独立窗口；
  3. 关闭窗口即退出程序，不留后台进程。

开发时也可直接 `python launcher.py` 运行，行为一致。
"""
from __future__ import annotations

import ctypes
import functools
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

APP_NAME = 'OpenMacDisk'
PREFERRED_PORT = 8770


def resource_path(rel: str) -> str:
    """兼容源码运行与 PyInstaller 单文件解压目录。"""
    base = getattr(sys, '_MEIPASS',
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def message_box(text: str, title: str = APP_NAME, icon: int = 0x10) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, str(text), title, icon)
    except Exception:
        pass


def _browser_exe() -> str | None:
    candidates = [
        r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
        r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe',
        r'%LocalAppData%\Microsoft\Edge\Application\msedge.exe',
        r'%ProgramFiles%\Google\Chrome\Application\chrome.exe',
        r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe',
        r'%LocalAppData%\Google\Chrome\Application\chrome.exe',
    ]
    for c in candidates:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return None


def open_window(url: str):
    """以独立应用窗口打开界面；返回浏览器进程（无浏览器时返回 None）。"""
    exe = _browser_exe()
    if not exe:
        webbrowser.open(url)
        return None
    profile = os.path.join(tempfile.gettempdir(), APP_NAME, 'ui-profile')
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        profile = os.path.join(tempfile.gettempdir(), APP_NAME + '-profile')
    cmd = [exe,
           '--app=' + url,
           '--window-size=1320,880',
           '--user-data-dir=' + profile,
           '--no-first-run',
           '--no-default-browser-check',
           '--disable-extensions',
           '--disable-background-networking',
           '--disable-component-update']
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        webbrowser.open(url)
        return None


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


def is_our_instance(port: int) -> bool:
    """通过 Server 响应头判断占用端口的是不是本程序，避免误连别的服务。"""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/' % port,
                                    timeout=1.5) as r:
            return (r.headers.get('Server') or '').startswith(APP_NAME)
    except Exception:
        return False


def free_port() -> int:
    for p in range(PREFERRED_PORT, PREFERRED_PORT + 20):
        if not port_in_use(p):
            return p
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def main() -> int:
    # --noconsole 打包后 stdout/stderr 为 None，避免第三方库 print 报错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')

    try:
        import openmacdisk.webui as ui
    except Exception as e:
        message_box('程序模块加载失败：%s' % e)
        return 1

    ui.ROOT = resource_path('web')
    if not os.path.isfile(os.path.join(ui.ROOT, 'index.html')):
        message_box('缺少界面文件 web/index.html，安装包可能不完整。')
        return 1

    port = PREFERRED_PORT
    if port_in_use(port):
        if is_our_instance(port):
            open_window('http://127.0.0.1:%d/' % port)
            return 0
        port = free_port()

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port),
                                  functools.partial(ui.Handler))
    except OSError as e:
        message_box('无法启动本地服务：%s' % e)
        return 1

    url = 'http://127.0.0.1:%d/' % port
    started = time.time()
    proc = open_window(url)

    if proc is not None:
        def watch():
            proc.wait()
            if time.time() - started < 3.0:
                # 浏览器异常闪退：退回普通浏览器，保持服务可用
                webbrowser.open(url)
                return
            srv.shutdown()
        threading.Thread(target=watch, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ui.STATE.close_all()
        srv.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
