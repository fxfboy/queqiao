#!/usr/bin/env python3
"""`queqiao` 统一入口：把 encode/decode/diff/stream/receive 五个 CLI 挂成子命令。

分发策略是**各模块自治**：顶层 parser 只认出子命令名，剩余参数原样交给
各模块自己的 argparse（add_help=False 避免顶层抢走 `queqiao encode --help`；
parse_known_args 把未知参数留在 rest 里）。这样每个模块的 --help 文本、
报错习惯、默认值逻辑完全不动——模块文件里的 main() 依然可以单独测试。

模块侧需要遵守的唯一约定：`main(argv=None)` 且 `parse_args(argv)`。
"""

import sys

from queqiao import decoder, encoder, make_diff, player, stream_decoder

COMMANDS = {
    'encode': (encoder.main, '任意文件 → 二维码 HTML 网格'),
    'decode': (decoder.main, '截图/照片 → 还原字节一致的原文件'),
    'diff': (make_diff.main, '目录对比 → patch 文件（可选的辅助工具）'),
    'stream': (player.main, '流式发送：喷泉码播放窗（配合 receive）'),
    'receive': (stream_decoder.main, '流式接收：抓屏解码喷泉码'),
}


def build_parser():
    import argparse

    names = ' '.join(COMMANDS)
    parser = argparse.ArgumentParser(
        prog='queqiao',
        description='QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件')
    sub = parser.add_subparsers(dest='command', required=True, metavar=names)
    for name, (_, help_text) in COMMANDS.items():
        # add_help=False：`queqiao encode --help` 由子命令自己的 parser 接管
        sub.add_parser(name, add_help=False, description=help_text)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    func = COMMANDS[args.command][0]
    # 交给子命令后 sys.argv 不再是它的视角，直接传 rest；
    # 各模块 main(argv) 里的 parse_args(argv) 会重新给出完整报错/--help。
    return func(rest) or 0


if __name__ == '__main__':
    sys.exit(main())
