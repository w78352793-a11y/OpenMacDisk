# -*- coding: utf-8 -*-
"""OpenMacDisk 命令行：镜像信息、目录浏览、提取、压缩制作与系统右键集成。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .image import Disk
from .hfs import HFSError
from . import writer

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def _display(name: str) -> str:
    """HFS+ 硬链接私有目录名带 NUL 前缀，控制字符转义后显示，不改动实际路径。"""
    return ''.join(c if c.isprintable() else '\\x%02x' % ord(c) for c in name)


def _hum(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.2f %s' % (n, u)
        n /= 1024.0


def cmd_info(a):
    with Disk(a.image) as d:
        s = d.summary()
        print('文件：%s' % s['path'])
        print('容器：%s   文件大小：%s   逻辑容量：%s' % (
            s['format'], _hum(s['file_size']), _hum(s['logical_size'])))
        if 'udif' in s:
            u = s['udif']
            print('UDIF：v%d  flags=%s  分段=%s  数据叉=%s  XML=%s B  块运行=%d' % (
                u['version'], u['flags'], u['segment'], _hum(u['data_fork']),
                _hum(u['xml_length']), u['runs']))
            print('压缩块：' + ', '.join('%s×%d' % (k, v)
                                     for k, v in u['run_types'].items()))
        print('\n分区表：')
        if not s['partitions']:
            print('  （无分区表，整盘卷）')
        for p in s['partitions']:
            print('  #%-2d %-28s %-20s 偏移=%-12s 长度=%s' % (
                p['index'], p['name'][:28], p['type'],
                _hum(p['start']), _hum(p['length'])))
        print('\n卷：')
        if not s['volumes']:
            print('  （无 HFS+/HFSX 卷）')
        for v in s['volumes']:
            print('  %s  文件系统=%s  块大小=%d  容量=%s  文件数=%d  目录数=%d'
                  '  Catalog=%s B  节点=%d  根目录条目=%d' % (
                      v['name'], v['fs'], v['block_size'],
                      _hum(v['volume_bytes']), v['files'], v['folders'],
                      _hum(v['catalog_bytes']), v['node_size'],
                      v['root_children']))
        for e in s['errors']:
            print('  ! ' + e)
        if a.json:
            print('\n' + json.dumps(s, ensure_ascii=False, indent=2, default=str))


def cmd_list(a):
    with Disk(a.image) as d:
        vol = d.first_volume
        node = vol.find(a.path or '')
        if node is None:
            print('路径不存在：%s' % a.path, file=sys.stderr)
            return 1
        items = node.children if node.kind == 'folder' else [node]
        print('目录：/%s   (%d 项)' % (node.path, len(items)))
        print('%-6s %-14s %s' % ('类型', '大小', '名称'))
        print('-' * 60)
        for c in items[:a.limit]:
            print('%-6s %-14s %s' % (
                '目录' if c.kind == 'folder' else '文件',
                '<DIR>' if c.kind == 'folder' else _hum(c.size),
                _display(c.name)))
        if len(items) > a.limit:
            print('... 还有 %d 项（用 --limit 调整）' % (len(items) - a.limit))
        return 0


def cmd_tree(a):
    with Disk(a.image) as d:
        vol = d.first_volume
        root = vol.find(a.path or '')
        if root is None:
            print('路径不存在：%s' % a.path, file=sys.stderr)
            return 1

        def walk(n, depth):
            if depth > a.depth:
                return
            print('%s%s%s' % ('  ' * depth, _display(n.name) or '/',
                             '/' if n.kind == 'folder' else ''))
            for c in n.children:
                walk(c, depth + 1)

        walk(root, 0)
        return 0


def _write_out(data, out, label):
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
        with open(out, 'wb') as f:
            f.write(data)
        print('已导出 %s → %s (%s)' % (label, out, _hum(len(data))))
    else:
        sys.stdout.buffer.write(data)


def cmd_extract(a):
    with Disk(a.image) as d:
        vol = d.first_volume
        node = vol.find(a.path)
        if node is None:
            print('路径不存在：%s' % a.path, file=sys.stderr)
            return 1
        if node.kind != 'file':
            print('不是文件：%s' % a.path, file=sys.stderr)
            return 1
        data = vol.read_file(node)
        if a.resource and node.resource_fork:
            data = vol.read_fork(node.resource_fork)
        _write_out(data, a.out, a.path)
        return 0


def cmd_extract_all(a):
    with Disk(a.image) as d:
        vol = d.first_volume
        root = vol.find(a.path or '')
        if root is None:
            print('路径不存在：%s' % a.path, file=sys.stderr)
            return 1
        total = 0
        count = 0
        for n in vol.walk(root):
            if n.kind != 'file':
                continue
            if n.size > a.max_size * 1024 * 1024:
                print('跳过（超过 --max-size）：%s (%s)' % (n.path, _hum(n.size)))
                continue
            dest = os.path.join(a.out, n.path.replace('/', os.sep))
            os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(vol.read_file(n))
            total += n.size
            count += 1
            if count % 50 == 0:
                print('  已导出 %d 个文件 / %s' % (count, _hum(total)))
        print('完成：%d 个文件，共 %s → %s' % (count, _hum(total), a.out))
        return 0


def cmd_create(a):
    name = a.name or os.path.splitext(os.path.basename(a.out))[0]
    size = writer.create_from_paths(a.out, a.sources, name, not a.raw)
    print('已生成 %s（%s，压缩=%s）' % (a.out, _hum(size), not a.raw))
    return 0


def cmd_create_blank(a):
    size = writer.create_blank_dmg(a.out, a.name, a.size)
    print('已创建空白 HFS+ 卷 %s（逻辑 %d MB，文件 %s）'
          % (a.out, a.size, _hum(size)))
    return 0


def cmd_expand(a):
    if a.raw:
        n = writer.expand_dmg(a.image, a.out)
        print('已展开为裸镜像 %s 字节 → %s（仅供刻录等底层用途）'
              % (n, a.out))
    else:
        n = writer.expand_to_editable_dmg(a.image, a.out)
        print('已 eXpand 为可编辑 DMG → %s（%s）' % (a.out, _hum(n)))
    return 0


def cmd_compress(a):
    n = writer.compress_to_dmg(a.image, a.out)
    print('已 Compress 压缩 → %s（%s）' % (a.out, _hum(n)))
    return 0


def cmd_repack(a):
    n = writer.repack_dmg(a.image, a.out, not a.raw)
    print('已重新封装 → %s（%s）' % (a.out, _hum(n)))
    return 0


def cmd_verify(a):
    r = writer.verify_image(a.image)
    print('文件：%s' % r['path'])
    print('文件大小：%s   逻辑容量：%s'
          % (_hum(r['file_size']), _hum(r['logical_size'])))
    print('文件 SHA-256      ：%s' % r['file_sha256'])
    print('逻辑磁盘 SHA-256 ：%s' % r['logical_sha256'])
    print('分区 %d 个，卷：%s' % (r['partitions'], ', '.join(r['volumes'])))
    return 0


def cmd_shell(a):
    from . import shell as sh
    if a.action == 'status':
        print('资源管理器右键菜单：%s' %
              ('已启用' if sh.status() else '未启用'))
        return 0
    n = sh.install() if a.action == 'install' else sh.uninstall()
    print('%s资源管理器右键菜单（%d 项）'
          % ('已启用' if a.action == 'install' else '已移除', n))
    print('当前状态：%s' % ('已启用' if sh.status() else '未启用'))
    return 0


def cmd_devices(a):
    print('Windows 物理磁盘（读取需要管理员权限）：')
    for i in range(16):
        p = r'\\.\PhysicalDrive%d' % i
        try:
            f = open(p, 'rb')
            f.seek(0)
            sig = f.read(512)
            f.close()
            print('  PhysicalDrive%-2d 可访问  DDM/签名=%r' % (i, sig[:2]))
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(prog='openmacdisk',
                                 description='OpenMacDisk — 开源 Mac 磁盘/DMG 读取工具')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('info', help='显示镜像、分区与卷信息')
    p.add_argument('image')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_info)

    p = sub.add_parser('list', help='列出卷内目录')
    p.add_argument('image')
    p.add_argument('path', nargs='?', default='')
    p.add_argument('--limit', type=int, default=200)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('tree', help='显示目录树')
    p.add_argument('image')
    p.add_argument('path', nargs='?', default='')
    p.add_argument('--depth', type=int, default=3)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser('extract', help='提取单个文件')
    p.add_argument('image')
    p.add_argument('path')
    p.add_argument('--out', '-o')
    p.add_argument('--resource', action='store_true', help='提取资源叉')
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser('extract-all', help='批量导出目录')
    p.add_argument('image')
    p.add_argument('--out', '-o', default='extracted')
    p.add_argument('--path', default='')
    p.add_argument('--max-size', type=float, default=64, help='单文件上限 MB')
    p.set_defaults(func=cmd_extract_all)

    p = sub.add_parser('create', help='从文件夹/文件制作 DMG（压缩）')
    p.add_argument('out')
    p.add_argument('sources', nargs='+')
    p.add_argument('--name', help='卷名称')
    p.add_argument('--raw', action='store_true', help='不压缩')
    p.set_defaults(func=cmd_create)

    p = sub.add_parser('create-blank', help='创建空白 HFS+ DMG')
    p.add_argument('out')
    p.add_argument('--size', type=int, default=16, help='容量 MB')
    p.add_argument('--name', default='Untitled')
    p.set_defaults(func=cmd_create_blank)

    p = sub.add_parser('expand', help='eXpand 为未压缩、可编辑的 DMG')
    p.add_argument('image')
    p.add_argument('out')
    p.add_argument('--raw', action='store_true',
                   help='改为输出裸 IMG（仅供刻录 / 重封装等底层用途）')
    p.set_defaults(func=cmd_expand)

    p = sub.add_parser('compress', help='Compress：压缩为体积更小的 DMG')
    p.add_argument('image')
    p.add_argument('out')
    p.set_defaults(func=cmd_compress)

    p = sub.add_parser('repack', help='重新封装为压缩/未压缩 DMG')
    p.add_argument('image')
    p.add_argument('out')
    p.add_argument('--raw', action='store_true')
    p.set_defaults(func=cmd_repack)

    p = sub.add_parser('verify', help='计算文件与逻辑磁盘 SHA-256')
    p.add_argument('image')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('shell', help='启用/移除资源管理器右键菜单')
    p.add_argument('action', choices=('install', 'uninstall', 'status'))
    p.set_defaults(func=cmd_shell)

    p = sub.add_parser('devices', help='列出物理磁盘')
    p.set_defaults(func=cmd_devices)

    a = ap.parse_args()
    try:
        r = a.func(a)
        sys.exit(r or 0)
    except (HFSError, OSError, ValueError, RuntimeError) as e:
        print('错误：%s' % e, file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
