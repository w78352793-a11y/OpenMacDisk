# -*- coding: utf-8 -*-
"""OpenMacDisk 本地图形界面服务（纯标准库，只监听 127.0.0.1）。

浏览器端为 Explorer 风格双面板：左侧镜像/卷/目录树，右侧文件列表。
所有解析都发生在本机，不上传任何数据、不访问网络。

用法：python -m openmacdisk.webui [--port 8770] [--no-browser]
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .image import Disk
from .hfs import HFSError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'web'))


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.disks: dict[str, Disk] = {}

    def get(self, path: str) -> Disk:
        with self.lock:
            d = self.disks.get(path)
            if d is None:
                d = Disk(path)
                # 只保留最近 3 个，避免大镜像长期占用内存
                if len(self.disks) >= 3:
                    _, old = self.disks.popitem()
                    old.close()
                self.disks[path] = d
            return d

    def close_all(self):
        with self.lock:
            for d in self.disks.values():
                d.close()
            self.disks.clear()


STATE = AppState()


def _hum(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.2f %s' % (n, u)
        n /= 1024.0


def _safe_name(s):
    return ''.join(c if c.isprintable() else '\\x%02x' % ord(c) for c in s)


def api_info(q):
    image = q.get('image', [''])[0]
    if not image or not os.path.isfile(image):
        raise ValueError('镜像文件不存在：%s' % image)
    d = STATE.get(image)
    s = d.summary()
    return {
        'ok': True, 'image': image, 'format': s['format'],
        'file_size': s['file_size'], 'file_size_h': _hum(s['file_size']),
        'logical_size_h': _hum(s['logical_size']),
        'udif': {k: s['udif'][k] for k in
                 ('version', 'flags', 'segment', 'runs', 'run_types')}
                if 'udif' in s else None,
        'partitions': [{'i': p['index'], 'name': _safe_name(p['name']),
                        'type': p['type'], 'size': _hum(p['length'])}
                       for p in s['partitions']],
        'volumes': [{'name': _safe_name(v['name']), 'fs': v['fs'],
                     'size': _hum(v['volume_bytes']),
                     'files': v['files'], 'folders': v['folders']}
                    for v in s['volumes']],
        'errors': s['errors'],
    }


def api_list(q):
    image = q.get('image', [''])[0]
    dirp = q.get('dir', [''])[0]
    d = STATE.get(image)
    vol = d.first_volume
    node = vol.find(dirp)
    if node is None:
        raise HFSError('目录不存在：%s' % dirp)
    items = node.children if node.kind == 'folder' else [node]
    entries = [{
        'name': _safe_name(c.name), 'raw': c.name,
        'kind': c.kind, 'size': c.size, 'size_h': _hum(c.size),
        'path': c.path, 'children': len(c.children),
    } for c in items]
    return {'ok': True, 'dir': node.path, 'count': len(entries),
            'items': entries}


def api_search(q):
    image = q.get('image', [''])[0]
    term = q.get('q', [''])[0].lower()
    limit = int(q.get('limit', ['200'])[0])
    d = STATE.get(image)
    vol = d.first_volume
    out = []
    for n in vol.walk():
        if term in n.name.lower():
            out.append({'name': _safe_name(n.name), 'kind': n.kind,
                        'size_h': _hum(n.size), 'path': n.path})
            if len(out) >= limit:
                break
    return {'ok': True, 'count': len(out), 'items': out}


class Handler(BaseHTTPRequestHandler):
    server_version = 'OpenMacDisk/1.0'

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8',
              extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False,
                                   default=str).encode('utf-8'))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if route.startswith('/api/'):
                if route == '/api/info':
                    return self._json(api_info(q))
                if route == '/api/list':
                    return self._json(api_list(q))
                if route == '/api/search':
                    return self._json(api_search(q))
                if route == '/api/drives':
                    drives = []
                    for i in range(12):
                        p = r'\\.\PhysicalDrive%d' % i
                        try:
                            f = open(p, 'rb')
                            sig = f.read(2)
                            f.close()
                            drives.append({'index': i, 'sig': repr(sig)})
                        except OSError:
                            pass
                    return self._json({'ok': True, 'drives': drives})
                if route == '/api/download':
                    image = q.get('image', [''])[0]
                    path = q.get('path', [''])[0]
                    d = STATE.get(image)
                    n = d.first_volume.find(path)
                    if n is None or n.kind != 'file':
                        raise HFSError('文件不存在：%s' % path)
                    data = d.first_volume.read_file(n)
                    fn = os.path.basename(path) or 'file.bin'
                    return self._send(
                        200, data, 'application/octet-stream',
                        {'Content-Disposition': 'attachment; filename="%s"'
                         % urllib.parse.quote(fn)})
                return self._json({'ok': False, 'error': '未知接口'}, 404)

            # 静态文件
            rel = 'index.html' if route in ('/', '') else route.lstrip('/')
            full = os.path.abspath(os.path.join(ROOT, rel))
            if not full.startswith(ROOT) or not os.path.isfile(full):
                return self._send(404, b'Not Found', 'text/plain')
            ctype = ('text/html; charset=utf-8' if full.endswith('.html')
                     else 'text/css; charset=utf-8' if full.endswith('.css')
                     else 'application/javascript; charset=utf-8')
            with open(full, 'rb') as f:
                self._send(200, f.read(), ctype)
        except Exception as e:
            self._json({'ok': False, 'error': '%s: %s' %
                        (type(e).__name__, e)}, 500)

    def do_HEAD(self):
        self.do_GET()


def main() -> int:
    ap = argparse.ArgumentParser(description='OpenMacDisk 图形界面')
    ap.add_argument('--port', type=int, default=8770)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    if not os.path.isfile(os.path.join(ROOT, 'index.html')):
        print('找不到 web/index.html：%s' % ROOT)
        return 1
    handler = functools.partial(Handler)
    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(('127.0.0.1', a.port), handler) as srv:
        url = 'http://127.0.0.1:%d/' % a.port
        print('OpenMacDisk 界面：%s' % url)
        print('只监听本机；Ctrl+C 退出。')
        if not a.no_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print('\n已停止。')
        finally:
            STATE.close_all()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
