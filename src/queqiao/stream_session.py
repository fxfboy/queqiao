#!/usr/bin/env python3
"""QueQiao v3 接收端会话状态机：候选桶 / lock 门槛 / 解码推进。

会话隔离要挡的不是"网络乱序"，是三件真实的事：屏幕上的旧会话残留画面、
16 位 nonce 在不同文件间的碰撞、以及操作者中途换了个文件重发。
"""

from queqiao.fountain import FountainDecoder
from queqiao.stream_packet import PacketError, unpack_packet

LOCK_MIN_PACKETS = 3


class _Bucket:
    """一个 (nonce, K, blocklen) 候选桶的累积状态。"""

    __slots__ = ('packets', 'seen')

    def __init__(self):
        self.packets = 0
        self.seen = {}                 # seed -> checksum（去重表兼冲突检测表）

    def admit(self, pkt):
        """记入一个通过校验的包。返回 'new' / 'duplicate' / 'conflict'。

        同会话内 nonce/K/blocklen 相同，所以 checksum 不同 ==> data 不同。
        注意这是单向蕴含：data 相同 ==> checksum 相同，其逆否即上式；
        但 checksum 相同不蕴含 data 相同（4 字节摘要有 2^-32 碰撞概率）。
        协议用的恰好是可靠的那一向——检测到 checksum 不同就判会话切换，
        无假阳性；代价只是 2^-32 概率漏判一次，而后续每个包都在继续检测。
        """
        self.packets += 1
        prev = self.seen.get(pkt.seed)
        if prev is None:
            self.seen[pkt.seed] = pkt.checksum
            return 'new'
        if prev == pkt.checksum:
            return 'duplicate'
        return 'conflict'

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


class SessionStats:
    """累计诊断（§9）。

    §7.5 已改为无限循环，所以误接受概率不能再拿"10 万个包"当分母。
    正确表述是 N_corrupt / 2^32，它随运行时长线性增长——所以按累计口径
    打印，让用户对当前运行的暴露量有数，而不是引用一个固定上界。
    """

    __slots__ = ('frames', 'codes', 'foreign', 'corrupt', 'valid',
                 'duplicate', 'other_bucket', 'conflicts', 'switches', 'resets')

    def __init__(self):
        # 显式逐个赋值（不用 setattr 循环）：pylint/IDE 能推断出成员
        self.frames = 0
        self.codes = 0
        self.foreign = 0
        self.corrupt = 0
        self.valid = 0
        self.duplicate = 0
        self.other_bucket = 0
        self.conflicts = 0
        self.switches = 0
        self.resets = 0

    def summary(self):
        return ("帧 %d | 码 %d | 有效 %d | 重复 %d | 损坏 %d | 非本协议 %d | "
                "他会话 %d | 冲突 %d | 切换 %d | 重置 %d"
                % (self.frames, self.codes, self.valid, self.duplicate,
                   self.corrupt, self.foreign, self.other_bucket,
                   self.conflicts, self.switches, self.resets))


class StreamSession:
    """喂原始解码字节进来，吐事件名出去。

    事件名：foreign / corrupt / other_bucket / accumulating / locked /
            progress / redundant / duplicate / complete / conflict / switched
    """

    def __init__(self, stall_threshold=32, switch_idle_packets=30):
        self.stall_threshold = stall_threshold
        self.switch_idle_packets = switch_idle_packets
        self.locked_key = None
        self.K = None
        self.blocklen = None
        self.decoder = None
        self._buckets = {}
        self.stats = SessionStats()
        self._idle = 0                    # locked 桶自上次收到新包以来喂进的包数

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

    def note_frame(self):
        """每抓一帧调一次。帧数与码数分开统计——一帧里可能有 0 个或多个码。"""
        self.stats.frames += 1

    def feed(self, raw):
        self.stats.codes += 1
        pkt = self._parse(raw)
        if pkt == 'foreign':
            self.stats.foreign += 1
            return 'foreign'
        if pkt == 'corrupt':
            self.stats.corrupt += 1
            return 'corrupt'
        self.stats.valid += 1

        if self.locked_key is None:
            return self._accumulate(pkt)
        if pkt.bucket_key != self.locked_key:
            self.stats.other_bucket += 1
            self._idle += 1
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
        verdict = bucket.admit(pkt)
        # 统计口径是全局的，不是"lock 之后才开始记"——lock 前持续收到重复包
        # 正是"画面卡死 / 残留静止"的诊断信号，报 0 会把它藏起来。
        if verdict == 'duplicate':
            self.stats.duplicate += 1
        elif verdict == 'conflict':
            # 只计数，**不重置桶**。§8.6 第 2 条说的是"强制立即重置**解码状态**"，
            # 而 lock 前根本没有解码状态可重置；若在这里把桶清零，两个发送端同时
            # 在播时桶永远达不到门槛 = 死锁。不重置的最坏情况只是 lock 到一个混了
            # 两个会话的桶，由解压后 SHA256 失败的重置（§9）兜住。
            self.stats.conflicts += 1
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
        bucket = self._buckets.setdefault(pkt.bucket_key, _Bucket())
        bucket.admit(pkt)
        # 门槛里的 K 取候选桶自己的 K，与当前 locked 桶无关——两个桶的 K
        # 完全可能不同（桶 A 是 K=1 的会话、桶 B 是 K=100 的），而这个门槛
        # 判定的是"该桶自身是否足够可信"。
        if bucket.can_lock(pkt.K) and self._idle >= self.switch_idle_packets:
            self.stats.switches += 1
            self._switch(pkt.bucket_key, pkt.K, pkt.blocklen)
            return 'switched'
        return 'other_bucket'

    def _switch(self, key, K, blocklen):
        self._buckets.pop(self.locked_key, None)
        self._lock(key, K, blocklen)       # _lock 会新建 FountainDecoder = 清空解码状态

    def _decode(self, pkt):
        bucket = self._buckets[self.locked_key]
        verdict = bucket.admit(pkt)
        if verdict == 'conflict':
            # 优先级最高：强制立即重置，不等门槛。
            self.stats.conflicts += 1
            self._unlock()
            # 触发冲突的这个包作为新桶的第一个包重新计入
            self._buckets.setdefault(pkt.bucket_key, _Bucket()).admit(pkt)
            return 'conflict'
        if verdict == 'duplicate':
            self.stats.duplicate += 1
            self._idle += 1
            return 'duplicate'

        self._idle = 0
        progressed = self.decoder.add_packet(pkt.seed, pkt.data)
        if self.decoder.is_complete:
            return 'complete'
        return 'progress' if progressed else 'redundant'

    def _unlock(self):
        self._buckets.pop(self.locked_key, None)
        self.locked_key = None
        self.K = None
        self.blocklen = None
        self.decoder = None
        self._idle = 0

    def reset_decoding(self, reason):
        """丢弃全部解码状态、从零重新累积包，但保留当前 lock。

        由 stream_decoder.py 在 SHA256 不匹配时调用。这是"重置重来"，不是
        "在现有状态上继续剥离"：一个误过 4 字节 checksum 的坏方程若先被剥离
        成了错误的度 1 块，错误会沿剥离链传播；把同一批方程重新剥离不会自动
        定位坏包。重新累积能得到一组干净方程，因为坏包的 LT seed 几乎不会再出现。
        """
        self.stats.resets += 1
        if self.locked_key is None:
            return
        self._buckets[self.locked_key] = _Bucket()
        self._lock(self.locked_key, self.K, self.blocklen)
        self._idle = 0

    def assemble(self):
        if self.decoder is None:
            raise ValueError("尚未 lock 任何会话")
        return self.decoder.assemble()
