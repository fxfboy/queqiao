#!/usr/bin/env python3
"""发送端线程生命周期。不依赖 tkinter，因此可在 CI / 无头环境跑。

规格 §7.1 列了关停必须精确到的五点，缺一条都可能卡死或进程不退出。
这个文件就是那五点各自的回归用例。
"""
import os
import subprocess
import sys
import threading
import time


from PIL import Image

from queqiao.stream_encoder import (
    JOIN_TIMEOUT, QUEUE_MAXSIZE, Frame, GeneratorError, StreamEncoder,
)
from queqiao.symbol_encoder import SymbolEncoder


class FakeEncoder(SymbolEncoder):
    """可控的假符号编码器：能拖慢、能抛异常、能被外部释放。"""

    name = 'fake'
    payload_encoding = 'raw'
    max_payload_bytes = 4096

    def __init__(self, delay=0.0, fail_at=None, gate=None):
        self.delay = delay
        self.fail_at = fail_at
        self.gate = gate            # threading.Event：置位前一直阻塞
        self.calls = 0

    def encode(self, payload):
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise RuntimeError("boom in generator thread")
        if self.gate is not None:
            self.gate.wait()        # 不检查 stop，模拟"不可中断的生成调用"
        if self.delay:
            time.sleep(self.delay)
        return Image.new('RGB', (8, 8), (255, 255, 255))


def make(**kw):
    enc = kw.pop('symbol_encoder', None) or FakeEncoder()
    return StreamEncoder('x.bin', b'payload bytes ' * 400,
                         symbol_encoder=enc, blocklen=200, **kw)


def test_start_and_consume():
    print("[TEST] 基本产出：帧带 index/seed/image...")
    se = make()
    se.start()
    try:
        seen = []
        deadline = time.monotonic() + 5
        while len(seen) < 5 and time.monotonic() < deadline:
            f = se.get_frame()
            if f is None:
                time.sleep(0.01)
                continue
            assert isinstance(f, Frame)
            assert isinstance(f.image, Image.Image)
            seen.append(f)
            f.image.close()
        assert len(seen) == 5, "5 秒内应至少产出 5 帧，实得 %d" % len(seen)
        assert [f.index for f in seen] == [0, 1, 2, 3, 4], "index 必须从 0 连续递增"
        assert se.packets_sent == 5
    finally:
        se.stop()
        se.join()
    print("  ✅ PASSED")


def test_stop_while_queue_full_does_not_deadlock():
    print("[TEST] ① 队列满时关窗不死锁（主线程不消费）...")
    se = make(queue_maxsize=4)
    se.start()
    # 故意完全不消费，等队列填满、生成线程阻塞在 put 上
    time.sleep(0.5)
    t0 = time.monotonic()
    se.stop()
    ok = se.join(timeout=3.0)
    elapsed = time.monotonic() - t0
    assert ok, "生成线程未在 3 秒内退出——阻塞的 put 没有响应 stop"
    assert elapsed < 3.0, "关停耗时 %.2fs，应远小于 3s" % elapsed
    print("  ✅ PASSED (%.3fs)" % elapsed)


def test_generator_exception_reaches_main_thread_with_full_queue():
    print("[TEST] ③ 生成线程异常且队列已满时，哨兵仍能送达...")
    se = make(queue_maxsize=2, symbol_encoder=FakeEncoder(fail_at=5))
    se.start()
    time.sleep(0.5)          # 让队列先满
    raised = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            f = se.get_frame()
        except GeneratorError as e:
            raised = e
            break
        if f is not None:
            f.image.close()
        else:
            time.sleep(0.01)
    assert raised is not None, "生成线程的异常必须以 GeneratorError 抵达主线程"
    assert 'boom' in str(raised.__cause__), "原始异常必须挂在 __cause__ 上"
    se.stop()
    assert se.join(timeout=3.0)
    print("  ✅ PASSED")


def test_drain_tolerates_producer_race():
    print("[TEST] ② drain 后生产者竞态再入队，最终清理能兜住...")
    se = make(queue_maxsize=8, symbol_encoder=FakeEncoder(delay=0.005))
    se.start()
    time.sleep(0.3)
    se.stop()                      # 内部 drain 一次；生成线程可能在此后再 put 一次
    assert se.join(timeout=3.0)
    # join 内部必须再清理一次；此时队列必须真的空了
    assert se.get_frame() is None, \
        "join 之后仍有残留项——drain 只做了一次，没有容忍 stop 与 put 之间的竞态窗口"
    print("  ✅ PASSED")


def test_join_timeout_gives_up_and_reports():
    print("[TEST] ④ join 超时后放弃等待，不无限阻塞...")
    gate = threading.Event()
    # StreamEncoder.__init__ 会同步做一次"探测渲染"（§9 早失败之二，见
    # test_generation_actually_renders_once_at_startup），用的就是这个
    # FakeEncoder 实例。gate 此时若不放行，探测渲染会在主线程里卡死在
    # gate.wait() 上，构造函数本身永远不返回——连 se.start() 都到不了。
    # 先放行让探测渲染过去，再关上闸门，让*生成线程*的第一次真实 encode
    # 卡住，这才是本用例想测的场景。
    gate.set()
    se = make(symbol_encoder=FakeEncoder(gate=gate))
    gate.clear()
    se.start()
    time.sleep(0.2)                # 卡在 encode 里，不响应 stop
    se.stop()
    t0 = time.monotonic()
    ok = se.join(timeout=0.3)
    elapsed = time.monotonic() - t0
    assert ok is False, "join 应返回 False 表示线程仍未结束"
    assert elapsed < 1.0, "join 超时后必须立刻返回，实测 %.2fs" % elapsed
    gate.set()                     # 放行，让线程自己结束，别留给解释器退出时挂着
    se.join(timeout=3.0)
    print("  ✅ PASSED")


# 子进程脚本：生成线程永久卡在 encode() 里，join 超时放弃后主逻辑正常结束。
# 探测渲染（__init__ 里那次）必须放过去，否则卡的是构造函数，验的不是同一件事。
_HANG_SCRIPT = r'''
import sys, time
from queqiao.stream_encoder import StreamEncoder


class _Img:
    def close(self):
        pass


class ProbeThenBlock:
    def __init__(self):
        self.calls = 0

    def check_capacity(self, blocklen):
        return True

    def encode(self, raw):
        self.calls += 1
        if self.calls == 1:
            return _Img()
        time.sleep(3600)


se = StreamEncoder('x.bin', b'A' * 4000,
                   symbol_encoder=ProbeThenBlock(), blocklen=800)
se.start()
time.sleep(0.5)
se.stop()
assert se.join(timeout=0.3) is False
'''


def test_stuck_generator_does_not_block_process_exit():
    """④ 的后半句：放弃等待之后进程必须**真的能退出**。

    上一个用例只验了 join 立即返回。它测不到真正的危险：生成线程若不是
    daemon，解释器退出时会无条件 join 它，"放弃等待继续退出流程"就变成
    "进程永不退出，只能外部强杀"。这只能在子进程里验——本进程一旦复现
    就再也退不出来，测试自己会挂死。
    """
    print("[TEST] ④ 生成线程卡死时进程仍能退出...")
    p = subprocess.Popen([sys.executable, '-c', _HANG_SCRIPT],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors='replace')
    try:
        out = p.communicate(timeout=20)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate(timeout=10)
        raise AssertionError(
            "主逻辑已结束但进程 20s 未退出——生成线程被解释器无条件 join 了，"
            "检查 StreamEncoder.start() 的 daemon 标志")
    assert p.returncode == 0, "子进程退出码 %r，输出:\n%s" % (p.returncode, out)
    print("  ✅ PASSED")


def test_repeated_stop_and_join_are_idempotent():
    print("[TEST] 重复 stop / join 幂等...")
    se = make()
    se.start()
    time.sleep(0.1)
    se.stop()
    se.stop()
    assert se.join(timeout=3.0)
    assert se.join(timeout=3.0)
    se.stop()
    assert se.get_frame() is None
    print("  ✅ PASSED")


def test_start_rejects_oversized_blocklen():
    print("[TEST] §9 早失败：blocklen 超符号容量时构造即拒...")
    small = FakeEncoder()
    small.max_payload_bytes = 100
    try:
        StreamEncoder('x.bin', b'z' * 5000, symbol_encoder=small, blocklen=200)
    except ValueError as e:
        assert 'blocklen' in str(e)
    else:
        raise AssertionError("blocklen=200 + 16 > 100，必须在构造时就拒绝")
    print("  ✅ PASSED")


def test_start_rejects_oversized_file():
    print("[TEST] §9 早失败：K 超 65535 时构造即拒...")
    try:
        StreamEncoder('x.bin', os.urandom(200_000),
                      symbol_encoder=FakeEncoder(), blocklen=1)
    except ValueError as e:
        msg = str(e)
        assert 'K' in msg or '65535' in msg or 'blocklen' in msg
    else:
        raise AssertionError("blocklen=1 会让 K 远超 65535，必须拒绝")
    print("  ✅ PASSED")


def test_generation_actually_renders_once_at_startup():
    print("[TEST] §9 早失败：构造时用最大包真渲染一次...")
    fake = FakeEncoder()
    StreamEncoder('x.bin', b'z' * 5000, symbol_encoder=fake, blocklen=200)
    assert fake.calls >= 1, \
        "check_capacity 只是算术检查；规格 §9 要求启动时用最大包真跑一次 encode"
    print("  ✅ PASSED")


def test_interval_ms_for_fps():
    print("[TEST] 帧率 → 毫秒节拍...")
    from queqiao.player import interval_ms_for_fps
    assert interval_ms_for_fps(6) == 167, "§7.3 的初始默认 6 fps 必须是 167 ms"
    assert interval_ms_for_fps(1) == 1000
    assert interval_ms_for_fps(60) == 17
    assert interval_ms_for_fps(10000) >= 1, "节拍不得为 0，否则 after 会退化成忙循环"
    for bad in (0, -1, -0.5):
        try:
            interval_ms_for_fps(bad)
        except ValueError:
            continue
        raise AssertionError("fps=%r 应被拒绝" % (bad,))
    print("  ✅ PASSED")


def test_fit_box_size():
    print("[TEST] 整数倍缩放：不让任何一层做非整数缩放（§7.2）...")
    from queqiao.player import STREAM_BORDER, fit_box_size
    # v40 是 177 模块；加 4 模块 quiet zone × 2 边 = 185
    assert fit_box_size(177, 1080) == 1080 // 185
    assert fit_box_size(177, 1080) * 185 <= 1080, "缩放后不得超出屏幕"
    assert (fit_box_size(177, 1080) + 1) * 185 > 1080, "必须取最大的整数倍"
    assert fit_box_size(21, 1080, border=STREAM_BORDER) == 1080 // 29
    try:
        fit_box_size(177, 100)          # 屏幕比码还小
    except ValueError as e:
        assert '屏幕' in str(e) or 'box_size' in str(e)
    else:
        raise AssertionError("屏幕装不下时必须报错，而不是返回 0 让 PIL 崩")
    print("  ✅ PASSED")


def test_format_status():
    print("[TEST] 状态行是纯函数...")
    from queqiao.player import format_status
    s = format_status(120, 20.0, K=40)
    assert '120' in s and '40' in s
    assert '6.0' in s, "应显示实际帧率 120/20.0 = 6.0"
    assert format_status(0, 0.0, K=40), "零耗时不得除零"
    print("  ✅ PASSED")


def test_image_to_tk_data_is_base64_png():
    print("[TEST] 帧图转 Tk 可吃的 base64 PNG（不走 PIL.ImageTk）...")
    import base64
    from PIL import Image
    from queqiao.player import image_to_tk_data
    data = image_to_tk_data(Image.new('RGB', (64, 64), 'white'))
    assert isinstance(data, bytes), "tk.PhotoImage(data=) 收 bytes 或 str"
    raw = base64.b64decode(data)
    assert raw[:8] == b'\x89PNG\r\n\x1a\n', "必须是 PNG；Tk 8.6 认 PNG，不认 PIL 写的 P6 PPM"
    print("  ✅ PASSED")


def test_ensure_tcl_env_is_idempotent_and_safe():
    print("[TEST] TCL_LIBRARY 修补幂等且不误伤...")
    import os
    from queqiao.player import ensure_tcl_env
    saved = os.environ.get('TCL_LIBRARY')
    try:
        # 已设值时必须原样不动——用户/run.sh 显式指定的路径优先级最高
        os.environ['TCL_LIBRARY'] = '/nonexistent/sentinel'
        assert ensure_tcl_env() is None, "已有 TCL_LIBRARY 时不得覆盖"
        assert os.environ['TCL_LIBRARY'] == '/nonexistent/sentinel'
    finally:
        if saved is None:
            os.environ.pop('TCL_LIBRARY', None)
        else:
            os.environ['TCL_LIBRARY'] = saved
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_start_and_consume()
    test_stop_while_queue_full_does_not_deadlock()
    test_generator_exception_reaches_main_thread_with_full_queue()
    test_drain_tolerates_producer_race()
    test_join_timeout_gives_up_and_reports()
    test_stuck_generator_does_not_block_process_exit()
    test_repeated_stop_and_join_are_idempotent()
    test_start_rejects_oversized_blocklen()
    test_start_rejects_oversized_file()
    test_generation_actually_renders_once_at_startup()
    test_interval_ms_for_fps()
    test_fit_box_size()
    test_format_status()
    test_image_to_tk_data_is_base64_png()
    test_ensure_tcl_env_is_idempotent_and_safe()
    print("\n✅ All lifecycle tests passed!")
