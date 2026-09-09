#!/usr/bin/env python3
"""QueQiao v3 接收端：屏幕帧 → 喷泉包 → 原文件。

用法:
    python stream_decoder.py -o ./out [--backend zxing] [--reselect]

与 v1/v2 的两处**策略相反**（§9）：
  · lzma 解压失败 → 报错、不写文件（decoder.py:362 是降级写明文）
  · SHA256 不符   → 报错、不写文件，并**丢弃全部解码状态重置**
半确定的方程集比空集更坏——它会让后续所有包都算错。
"""

import argparse
import base64
import sys
import time
from pathlib import Path

from queqiao.frame_source import (
    BLACK_FRAME_ALERT, NO_CODE_ALERT, ScreenSource, is_black_frame, resolve_region,
)
from queqiao.qr_backends import DEFAULT_BACKEND, available_backends, get_backend
from queqiao.stream_packet import PayloadError, parse_payload, safe_output_name
from queqiao.stream_profile import (
    CALIBRATION_MATRIX, CONSERVATIVE_DEFAULTS, CalibrationCollector,
    MIN_ACCEPTABLE_RATE, save_profile,
)
from queqiao.stream_session import StreamSession


class ReceiveResult:
    __slots__ = ('meta', 'file_bytes', 'stats')

    def __init__(self, meta, file_bytes, stats):
        self.meta = meta
        self.file_bytes = file_bytes
        self.stats = stats


def decode_frame(image, backend):
    """一帧 → 若干 raw 包。后端异常、b85 解码失败都在这里吞掉。

    这一层**永不抛**：单帧解码失败是常态（画面正在刷新、码被挡住半个），
    让它冒泡会把整场接收打断。
    """
    try:
        results = backend.decode_image(image)
    except Exception:
        return []
    encoding = getattr(backend, 'payload_encoding', 'base85')
    out = []
    for r in results:
        data = r.data
        if encoding == 'base85':
            try:
                data = base64.b85decode(data)
            except Exception:
                continue        # 不是 base85 → 多半是别人的码
        out.append(data)
    return out


def _default_on_event(session, event, raw):
    if event == 'locked':
        print("\n  🔒 已锁定会话: K=%d blocklen=%d" % (session.K, session.blocklen))
    elif event in ('progress', 'redundant'):
        print("\r  进度 %d/%d 块 │ %s"
              % (session.solved_count, session.K, session.stats.summary()),
              end='', flush=True)


def receive_stream(source, session, backend, on_event=None):
    """帧循环。收齐并通过校验后返回 ReceiveResult；帧源耗尽则抛 RuntimeError。"""
    on_event = on_event or _default_on_event
    consecutive_black = 0
    black_warned = False
    consecutive_no_code = 0
    no_code_warned = False
    last_solved = 0
    last_progress_at = time.monotonic()
    frames = 0
    last_heartbeat = time.monotonic()

    for image in source:
        try:
            if is_black_frame(image):
                consecutive_black += 1
                if consecutive_black >= BLACK_FRAME_ALERT and not black_warned:
                    black_warned = True
                    print("\n  ⚠️  连续 %d 帧全黑。macOS 未授屏幕录制权限时，截图 API "
                          "不报错、只返回纯黑图。\n"
                          "     去「系统设置 → 隐私与安全性 → 屏幕录制」勾上当前终端，"
                          "然后**重启终端**再试。" % consecutive_black)
            else:
                consecutive_black = 0

            session.note_frame()
            frame_raws = decode_frame(image, backend)
            # 非黑但连续 N 帧一个码都读不出 —— 和"没有屏幕录制权限"表现不同，
            # 成因多半是播放窗被遮挡、区域选错、或发送端没开（见 Task 16 的诊断表）。
            # is_black_frame 抓不到这种情况：遮挡窗口五颜六色、完全不黑。
            if not frame_raws and not is_black_frame(image):
                consecutive_no_code += 1
                if consecutive_no_code >= NO_CODE_ALERT and not no_code_warned:
                    no_code_warned = True
                    print("\n  ⚠️  连续 %d 帧非黑但解不出任何码。"
                          "检查播放窗是否在最前、圈选区域是否选对、发送端是否在跑。"
                          % consecutive_no_code)
            else:
                consecutive_no_code = 0
                no_code_warned = False

            for raw in frame_raws:
                event = session.feed(raw)
                on_event(session, event, raw)
        finally:
            image.close()       # §8.1：帧的所有权在我们手里，必须关

        # 心跳：每 5 秒报一次"还活着、抓了多少帧"。进度行只在解出包时打印，
        # 一个码都解不出时（窗口悬出屏幕/被遮挡/区域偏了）终端会长时间静默，
        # 看起来像卡死——2026-09-08 用户因此 Force Quit 过一次。
        frames += 1
        if time.monotonic() - last_heartbeat >= 5.0:
            last_heartbeat = time.monotonic()
            print("\r  已抓 %d 帧 │ %s" % (frames, session.stats.summary()),
                  end='', flush=True)

        if session.solved_count != last_solved:
            last_solved = session.solved_count
            last_progress_at = time.monotonic()
        elif session.locked_key is not None and \
                time.monotonic() - last_progress_at > 20:
            # §9：零新包只警告，不退出——发送端可能刚被切到后台，会回来
            print("\n  ⚠️  20 秒没有新块了。检查播放窗是否被遮挡/最小化，"
                  "或圈选区域是否偏了。")
            last_progress_at = time.monotonic()

        if session.is_complete:
            print()
            assembled = session.assemble()
            try:
                meta, file_bytes = parse_payload(
                    assembled, session.K, session.blocklen)
            except PayloadError as e:
                # §9：不写文件，丢弃全部状态重置，让发送端的后续包重建一份干净的
                print("  ❌ 载荷校验失败（%s）。不写文件，已重置解码状态继续接收。"
                      % e.reason)
                session.reset_decoding('payload:%s' % e.reason)
                continue
            return ReceiveResult(meta, file_bytes, session.stats)

        # 注意这里是 **return 而不是 continue**：一次 receive 只收一个文件，收完
        # 就退出，连传第二个文件要重新跑一次命令。这不只是 UX 选择，它还顺手绕开了
        # §8.6 的一个真实边界：`_idle` 只在 `admit()` 返回 'new' 时清零，而 K>=10 的
        # 会话 seed 是严格递增的包序号，**每个包都是 'new'**——哪怕文件早已解完，
        # `_idle` 也永远涨不上去，该会话就再也不会被切换让位。K<=9 的纯系统性循环
        # 没这个问题（重放包全判 duplicate，_idle 正常累积）。
        # 将来若要加 `--loop` 连收模式，**必须在这里新建一个 StreamSession**，
        # 而不是继续 feed 同一个——否则 K>=10 的第二个文件永远收不到。

        if session.is_stalled:
            print("\n  ⚠️  剥离停滞：待定方程堆积但解不出新块。"
                  "多半是丢包太密，继续接收即可。")

    raise RuntimeError("帧源已耗尽但未收齐（%s）" % session.stats.summary())


def write_output(meta, file_bytes, out_dir=None, out_path=None):
    """写文件。显式 out_path 优先；否则用 meta 里的文件名的 **basename**。"""
    if out_path is not None:
        target = Path(out_path)
    else:
        base = Path(out_dir or '.')
        base.mkdir(parents=True, exist_ok=True)
        target = base / safe_output_name(meta.get('filename', ''))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(file_bytes)
    return target


def run_calibration(source, backend, duration):
    """标定接收：不做剥离，只按 (K, blocklen) 分档统计 seed 缺号。

    §7.4：报的是**单帧解出率**，不是整文件恢复率。喷泉码会把丢包补回来，
    整文件恢复率在很宽的参数区间里都是 100%，对区分档位毫无分辨力。
    """
    collector = CalibrationCollector()
    deadline = time.monotonic() + duration
    frames = 0
    for image in source:
        try:
            for raw in decode_frame(image, backend):
                collector.feed(raw)
        finally:
            image.close()
        frames += 1
        if frames % 20 == 0:
            stages = len(collector.report())
            print("\r  已抓 %d 帧，识别到 %d 档..." % (frames, stages),
                  end='', flush=True)
        if time.monotonic() >= deadline:
            break
    print()
    return collector, frames


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='queqiao receive',
        description='QueQiao (鹊桥) v3: 从屏幕区域接收流式喷泉码')
    parser.add_argument('-o', '--output', default=None,
                        help='输出文件路径（默认用发送端的文件名写到 --out-dir）')
    parser.add_argument('--out-dir', default='.',
                        help='未指定 -o 时的输出目录 (默认: 当前目录)')
    # §4.1 的命令行形式是 `./run.sh receive --screen`，必须能原样跑通。
    # 本轮只有 ScreenSource 一种帧源，所以它是默认且唯一——**接受但不分支**。
    # 它存在的理由是 §8.1："receive 接的不是视频，是帧源"：将来加 --video FILE
    # 时，用户既有的命令不用改，而现在写下这个标志的成本是两行。
    parser.add_argument('--screen', action='store_true',
                        help='从屏幕区域抓帧（当前唯一的帧源，可省略）')
    parser.add_argument('--backend', choices=available_backends(),
                        default=DEFAULT_BACKEND,
                        help='解码后端 (默认: %s)' % DEFAULT_BACKEND)
    parser.add_argument('--reselect', action='store_true',
                        help='重新圈选区域（默认复用 ~/.queqiao/last_region.json）')
    parser.add_argument('--interval', type=float, default=0.0,
                        help='两帧之间的额外等待秒数 (默认: 0，尽快抓)')
    parser.add_argument('--calibrate', action='store_true',
                        help='标定模式: 配合 stream --calibrate 使用，测各档单帧解出率')
    parser.add_argument('--calibrate-seconds', type=int, default=None,
                        help='标定采集总秒数 (默认: 档数 × 20 + 10)')
    args = parser.parse_args(argv)

    try:
        backend = get_backend(args.backend)
    except RuntimeError as e:
        print("❌ %s" % e)
        return 1

    try:
        bbox = resolve_region(reselect=args.reselect)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 1
    except ValueError as e:
        print("❌ %s" % e)
        return 1

    source = ScreenSource(bbox, interval=args.interval)

    if args.calibrate:
        duration = args.calibrate_seconds or (len(CALIBRATION_MATRIX) * 20 + 10)
        print("=" * 60)
        print("  QueQiao (鹊桥) - 标定接收")
        print("=" * 60)
        print("  区域:   %s" % source.describe)
        print("  采集 %d 秒。发送端现在就运行: ./run.sh stream --calibrate" % duration)
        print("=" * 60)
        try:
            collector, frames = run_calibration(source, backend, duration)
        except KeyboardInterrupt:
            print("\n  已中止。")
            return 130

        reports = collector.report()
        if not reports:
            print("\n❌ 一个包都没收到。检查：")
            print("   · 发送端是否已在 --calibrate 模式播放")
            print("   · 圈选区域是否覆盖了播放窗（--reselect 重选）")
            print("   · macOS 是否已授予屏幕录制权限")
            return 1

        print("\n  抓了 %d 帧，识别到 %d 档：\n" % (frames, len(reports)))
        for r in reports:
            mark = '✅' if r.decode_rate >= MIN_ACCEPTABLE_RATE else '❌'
            print("   %s %s" % (mark, r.line()))
        if len(reports) < len(CALIBRATION_MATRIX):
            print("\n  ⚠️  只识别到 %d/%d 档。采集时间可能不够，或高密度档一个都没解出来。"
                  % (len(reports), len(CALIBRATION_MATRIX)))

        best = collector.best(CALIBRATION_MATRIX)
        if best is None:
            print("\n  ⚠️  没有任何一档达到 %.0f%% 单帧解出率。保守默认不变。"
                  "可以试试调大 --box-size、把 RDP 画质调到最高、或关掉动态分辨率。"
                  % (MIN_ACCEPTABLE_RATE * 100))
            return 1

        chosen = collector.report()
        picked = [r for r in chosen if r.blocklen == best['blocklen']][0]
        best = dict(best)
        # 矩阵条目只有 blocklen/ecc/box_size，validate_settings 要求四项齐全。
        # fps 不是标定维度（见 Step 5 的说明），补基准值。
        best['fps'] = CONSERVATIVE_DEFAULTS['fps']
        p = save_profile(best, measurements={
            'decode_rate': round(picked.decode_rate, 4),
            'max_gap': picked.max_gap,
            'frames': frames,
            'stages_seen': len(reports),
        })
        print("\n" + "=" * 60)
        print("✅ 标定完成，已选 blocklen=%d ecc=%s box_size=%d"
              % (best['blocklen'], best['ecc'], best['box_size']))
        print("  单帧解出率 %.2f%%，最长连丢 %d 帧"
              % (picked.decode_rate * 100, picked.max_gap))
        print("  已写入 %s" % p)
        print("=" * 60)
        return 0

    session = StreamSession()

    print("=" * 60)
    print("  QueQiao (鹊桥) - 流式接收")
    print("=" * 60)
    print("  区域:   %s" % source.describe)
    print("  后端:   %s (payload_encoding=%s)"
          % (backend.name, backend.payload_encoding))
    print("  Ctrl-C 随时中止。")
    print("=" * 60)

    try:
        result = receive_stream(source, session, backend)
    except KeyboardInterrupt:
        print("\n\n  已中止。%s" % session.stats.summary())
        return 130
    except RuntimeError as e:
        print("\n❌ %s" % e)
        return 1

    target = write_output(result.meta, result.file_bytes,
                          out_dir=args.out_dir, out_path=args.output)
    print()
    print("=" * 60)
    print("✅ 收齐并通过 SHA256 校验")
    print("  文件:   %s (%d 字节)" % (target, len(result.file_bytes)))
    print("  统计:   %s" % result.stats.summary())
    print("=" * 60)
    print("\n  现在可以到发送端按 Esc 停止播放了。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
