#!/usr/bin/env python3
"""发送端线程生命周期。不依赖 tkinter，因此可在 CI / 无头环境跑。

规格 §7.1 列了关停必须精确到的五点，缺一条都可能卡死或进程不退出。
这个文件就是那五点各自的回归用例。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image

from stream_encoder import (
    JOIN_TIMEOUT, QUEUE_MAXSIZE, Frame, GeneratorError, StreamEncoder,
)
from symbol_encoder import SymbolEncoder


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


if __name__ == '__main__':
    test_start_and_consume()
    test_stop_while_queue_full_does_not_deadlock()
    test_generator_exception_reaches_main_thread_with_full_queue()
    test_drain_tolerates_producer_race()
    test_join_timeout_gives_up_and_reports()
    test_repeated_stop_and_join_are_idempotent()
    test_start_rejects_oversized_blocklen()
    test_start_rejects_oversized_file()
    test_generation_actually_renders_once_at_startup()
    print("\n✅ All lifecycle tests passed!")
