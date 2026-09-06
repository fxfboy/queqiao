#!/usr/bin/env python3
"""发送端：文件 → 喷泉包 → 符号图像，经有界队列交给 GUI 线程。

本模块**不 import tkinter**。GUI 不做自动化测试（§10），所以线程生命周期
必须能在无 GUI 环境下单测——这条是设计约束，不是省事。
"""

import math
import queue
import threading

from fountain import FountainEncoder, FountainExhausted
from stream_packet import MAX_TOTAL_PAYLOAD, build_payload, pack_packet, split_blocks

# 6 fps 下是 10 秒缓冲。它的作用是吸收生成耗时的抖动，不是攒水库——
# 生成 33-105 ms/帧本来就快过播放 167 ms/帧，不需要大缓冲。
QUEUE_MAXSIZE = 60

# 带超时的 put，每轮回来检查 stop。无超时 put 会在关窗时确定性死锁。
PUT_TIMEOUT = 0.1

JOIN_TIMEOUT = 5.0

DEFAULT_BLOCKLEN = 800

MAX_K = 65535               # 头里 K 是 >H


class GeneratorError(RuntimeError):
    """生成线程里的异常，经哨兵送到主线程后重新抛出。原始异常在 __cause__。"""


class Frame:
    __slots__ = ('index', 'seed', 'image')

    def __init__(self, index, seed, image):
        self.index = index
        self.seed = seed
        self.image = image


class _Sentinel:
    """生成线程结束的通知：正常耗尽或异常。"""

    __slots__ = ('exc',)

    def __init__(self, exc):
        self.exc = exc


class StreamEncoder:

    def __init__(self, filename, file_bytes, symbol_encoder,
                 blocklen=DEFAULT_BLOCKLEN, M=None, queue_maxsize=QUEUE_MAXSIZE):
        if blocklen <= 0:
            raise ValueError("blocklen 必须为正，实得 %r" % (blocklen,))

        # §9 早失败之一：算术容量检查
        symbol_encoder.check_capacity(blocklen)

        payload, meta_json, nonce = build_payload(filename, file_bytes)
        K = math.ceil(len(payload) / blocklen)
        if K > MAX_K:
            raise ValueError(
                "K=%d 超出头字段上限 %d（blocklen=%d）。请调大 blocklen 或减小文件。"
                % (K, MAX_K, blocklen)
            )
        if K * blocklen > MAX_TOTAL_PAYLOAD:
            raise ValueError(
                "K×blocklen=%d 超出 MAX_TOTAL_PAYLOAD=%d。"
                % (K * blocklen, MAX_TOTAL_PAYLOAD)
            )

        blocks = split_blocks(payload, blocklen)
        assert len(blocks) == K

        self.meta_json = meta_json
        self.nonce = nonce
        self.K = K
        self.blocklen = blocklen
        self.packets_sent = 0

        self._symbol = symbol_encoder
        self._fountain = FountainEncoder(blocks, M=M)
        self._queue = queue.Queue(maxsize=queue_maxsize)
        self._stop = threading.Event()
        self._thread = None
        self._next_index = 0

        # §9 早失败之二：用最大包真渲染一次。check_capacity 只是算术，
        # qrcode 的 DataOverflowError 只有真跑一次才会暴露。
        probe = pack_packet(nonce, 0, K, blocklen, blocks[0])
        probe_image = self._symbol.encode(probe)
        probe_image.close()

    # ── 生成线程 ──────────────────────────────────────────────

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._generate, name='queqiao-generator', daemon=False)
        self._thread.start()

    def _offer(self, item):
        """带超时循环 put，每轮检查 stop。返回是否真的送达。

        ③ 哨兵也走这条路：不能为了送哨兵去无限等一个再也不会被消费的满队列。
        """
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=PUT_TIMEOUT)
                return True
            except queue.Full:
                continue
        return False

    def _generate(self):
        try:
            while not self._stop.is_set():
                try:
                    seed, data = self._fountain.next_packet()
                except FountainExhausted as e:
                    self._offer(_Sentinel(e))
                    return
                raw = pack_packet(self.nonce, seed, self.K, self.blocklen, data)
                image = self._symbol.encode(raw)
                frame = Frame(self._next_index, seed, image)
                self._next_index += 1
                if not self._offer(frame):
                    image.close()       # 没送出去就是我的，自己关
                    return
        except BaseException as e:               # noqa: BLE001 —— 绝不静默死掉
            self._offer(_Sentinel(e))

    # ── 主线程 ────────────────────────────────────────────────

    def get_frame(self):
        """取一帧。队列空返回 None；生成线程出错则抛 GeneratorError。

        返回的 Frame 所有权归调用方，用完 close 它的 image。
        """
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return None
        if isinstance(item, _Sentinel):
            raise GeneratorError("包生成线程已结束") from item.exc
        self.packets_sent += 1
        return item

    # ── 关停 ──────────────────────────────────────────────────

    def stop(self):
        """① close 回调里只调这个——置 stop、腾空队列。**不在这里 join。**

        join 必须放到 mainloop() 返回之后：close 回调跑在 Tk 事件循环线程上，
        在那里 join 会阻塞事件循环本身。
        """
        self._stop.set()
        self._drain()

    def join(self, timeout=JOIN_TIMEOUT):
        """等生成线程结束。返回 True=已结束，False=超时放弃。

        ④ 超时不是致命错误，但必须放弃等待继续退出流程，否则进程永不退出。
        """
        if self._thread is None:
            return True
        self._thread.join(timeout)
        alive = self._thread.is_alive()
        # ② 最终清理：生成线程可能在"检查 stop"与"put"之间的窗口里再入队一次，
        # 所以不能假设 stop() 里那次 drain 就把队列排空了。
        self._drain()
        return not alive

    def _drain(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, Frame):
                item.image.close()
