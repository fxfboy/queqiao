#!/usr/bin/env python3
"""QueQiao v3 接收端会话状态机：候选桶 / lock 门槛 / 解码推进。

会话隔离要挡的不是"网络乱序"，是三件真实的事：屏幕上的旧会话残留画面、
16 位 nonce 在不同文件间的碰撞、以及操作者中途换了个文件重发。
"""

from fountain import FountainDecoder
from stream_packet import PacketError, unpack_packet

LOCK_MIN_PACKETS = 3


class _Bucket:
    """一个 (nonce, K, blocklen) 候选桶的累积状态。"""

    __slots__ = ('packets', 'seen')

    def __init__(self):
        self.packets = 0
        self.seen = {}                 # seed -> checksum（去重表兼冲突检测表）

    def admit(self, pkt):
        """记入一个通过校验的包。返回 True 表示是本桶没见过的 (seed, checksum)。"""
        self.packets += 1
        prev = self.seen.get(pkt.seed)
        if prev == pkt.checksum:
            return False
        self.seen[pkt.seed] = pkt.checksum
        return True

    def can_lock(self, K):
        """lock 门槛：两个独立条件的合取。

        - 累计合法包数 >= 3：K=1 时线上 seed 恒为 0，只靠 seed 数会退化成
          "首包即锁"，而残留画面正好是 1 帧。这一半把抗残留强度恢复到 3 帧。
        - 不同 seed 数 >= min(3, K)：K<=2 的会话总共只有 K 个不同 seed，
          写死 3 会让它们永远不 lock，小文件必然死锁。

        K >= 3 时"3 个不同 seed"必然蕴含">= 3 个包"，与只写 seed 条件等价。
        min 是函数不是分支——接收端仍然一行 K 的 if 特例都不用写。
        """
        return self.packets >= LOCK_MIN_PACKETS and len(self.seen) >= min(LOCK_MIN_PACKETS, K)


class StreamSession:
    """喂原始解码字节进来，吐事件名出去。

    事件名：foreign / corrupt / other_bucket / accumulating / locked /
            progress / redundant / duplicate / complete
    """

    def __init__(self, stall_threshold=32, switch_idle_packets=30):
        self.stall_threshold = stall_threshold
        self.switch_idle_packets = switch_idle_packets
        self.locked_key = None
        self.K = None
        self.blocklen = None
        self.decoder = None
        self._buckets = {}

    @property
    def is_complete(self):
        return self.decoder is not None and self.decoder.is_complete

    @property
    def is_stalled(self):
        return self.decoder is not None and self.decoder.is_stalled

    @property
    def solved_count(self):
        return self.decoder.solved_count if self.decoder else 0

    @property
    def progress(self):
        if not self.K:
            return 0.0
        return self.solved_count / self.K

    def feed(self, raw):
        pkt = self._parse(raw)
        if isinstance(pkt, str):
            return pkt                        # 'foreign' / 'corrupt'
        if self.locked_key is None:
            return self._accumulate(pkt)
        if pkt.bucket_key != self.locked_key:
            return self._on_other_bucket(pkt)
        return self._decode(pkt)

    def _parse(self, raw):
        try:
            pkt = unpack_packet(raw)
        except PacketError:
            return 'corrupt'                  # 是本协议的包但坏了，计入诊断
        if pkt is None:
            return 'foreign'                  # 画面里的其他码，不算损坏
        return pkt

    def _accumulate(self, pkt):
        bucket = self._buckets.setdefault(pkt.bucket_key, _Bucket())
        bucket.admit(pkt)
        if not bucket.can_lock(pkt.K):
            return 'accumulating'
        # lock 前的包只记了 (seed, checksum) 不留 data，故不回灌——
        # 留存 data 会让每个候选桶都能被畸形包用来放大内存，丢 3 个包更划算。
        # 这个"丢 3 个包划算"的论证只在 K>=LT_MIN_K 成立：LT 路径上 lock 后
        # 的包 seed 递增、checksum 各异，永远是新信息。K<LT_MIN_K 的纯系统性
        # 循环路径上，后续包按定义就是前面包的逐字节重放（seed 恒在 0..K-1、
        # 块内容不变、checksum 不变）——若不重建去重表，_lock() 里新建的
        # decoder 会被 lock 前的记录永久拦在门外，见 _lock() 的注释。
        self._lock(pkt.bucket_key, pkt.K, pkt.blocklen)
        return 'locked'

    def _lock(self, key, K, blocklen):
        self.locked_key = key
        self.K = K
        self.blocklen = blocklen
        self.decoder = FountainDecoder(K, blocklen, stall_threshold=self.stall_threshold)
        # 去重表必须与 decoder 同生共死。lock 前 seen 回答的是"这个 seed 统计过没有"
        # （只服务于门槛计数），lock 后回答的是"这个方程喂给 decoder 没有"——
        # 两段语义不同。共用一张跨越 lock 的永久表会让 K <= 9 的纯系统性路径
        # 永久死锁：那条路径上后续包按定义就是前面包的逐字节重放（seed 恒在
        # 0..K-1、块内容不变、checksum 不变），lock 前记下的条目会把它们全判成
        # duplicate，新建的 decoder 一个包都吃不到。
        self._buckets[key] = _Bucket()

    def _on_other_bucket(self, pkt):
        self._buckets.setdefault(pkt.bucket_key, _Bucket()).admit(pkt)
        return 'other_bucket'

    def _decode(self, pkt):
        bucket = self._buckets[self.locked_key]
        if not bucket.admit(pkt):
            return 'duplicate'                # (seed, checksum) 完全相同，去重丢弃
        progressed = self.decoder.add_packet(pkt.seed, pkt.data)
        if self.decoder.is_complete:
            return 'complete'
        return 'progress' if progressed else 'redundant'

    def assemble(self):
        if self.decoder is None:
            raise ValueError("尚未 lock 任何会话")
        return self.decoder.assemble()
