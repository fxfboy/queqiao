#!/usr/bin/env python3
"""顺序跑完全部测试脚本（纯 assert 风格，非 pytest）。任一失败立即停止。

用法：
    uv run python tests/run_all.py

pyzbar 相关用例在 macOS 上需要 DYLD_LIBRARY_PATH 指向 Homebrew 的 lib，
这里统一补上（不存在该目录则跳过）。
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 依赖顺序：纯函数 → 单元 → 集成 → 子进程冒烟（最慢、最容易受环境影响，放最后）
SUITES = [
    'test_fountain.py',
    'test_fountain_acceptance.py',
    'test_stream_packet.py',
    'test_stream_session.py',
    'test_stream_profile.py',
    'test_frame_source.py',
    'test_make_diff.py',
    'test_roundtrip.py',
    'test_stream_lifecycle.py',
    'test_stream_roundtrip.py',
    'test_stream_qr_roundtrip.py',      # 需要 pyzbar（render + 读回）
    'test_qr_roundtrip.py',             # 需要 pyzbar
    'test_stream_degraded.py',
    'test_jab_backend.py',              # 不需要原生 JAB 工具（假实现注入）
    'test_cli.py',                      # 子进程冒烟，含 pyzbar 后端
]


def make_env():
    env = dict(os.environ)
    brew_lib = '/opt/homebrew/lib'
    if sys.platform == 'darwin' and os.path.isdir(brew_lib):
        env['DYLD_LIBRARY_PATH'] = os.pathsep.join(
            p for p in (brew_lib, env.get('DYLD_LIBRARY_PATH')) if p)
    return env


def main():
    failed = []
    t0 = time.monotonic()
    for name in SUITES:
        path = os.path.join(HERE, name)
        dt = time.monotonic()
        r = subprocess.run([sys.executable, path], env=make_env())
        dt = time.monotonic() - dt
        mark = '✅' if r.returncode == 0 else '❌'
        print('%s %s (%.1fs)' % (mark, name, dt))
        if r.returncode != 0:
            failed.append(name)
            break               # 失败即停，后面的用例大概率连锁失败
    total = time.monotonic() - t0
    print()
    if failed:
        print('❌ 未通过: %s（%.0fs）' % (', '.join(failed), total))
        return 1
    print('✅ 全部 %d 个测试套件通过（%.0fs）' % (len(SUITES), total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
