#!/usr/bin/env python3
"""QueQiao v3 喷泉码内核：纯字节、零 IO、零第三方依赖。

设计约束（来自规格 §6.2）：不能用 random.Random(seed)——CPython 的
randint/sample 内部算法在版本间不保证不变，而发送端和接收端必须用同一个
种子推出同一组源块索引，一旦不一致就全盘解不出。所以内嵌 splitmix64，
编解码两端共用，跨 Python 版本、跨 Windows/macOS 完全确定。
"""

from decimal import Decimal, getcontext, ROUND_HALF_EVEN

MASK64 = (1 << 64) - 1


def splitmix64_next(state):
    """splitmix64 的规范变体：先加常数、再混合。

    返回 (新 state, 输出 z)，两者都是 uint64。
    """
    state = (state + 0x9E3779B97F4A7C15) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    z = z ^ (z >> 31)
    return state, z


def unbiased_below(state, n):
    """用拒绝采样从 [0, n) 均匀取一个值，无取模偏置。n >= 1。

    两条消耗约定（两端必须一致，否则后续所有抽样错位）：
    - 约定①：n == 1 时区间只有一个值，直接返回 0，**不**推进 PRNG 状态。
    - 约定②：被拒的抽样**照常消耗一次** next()，即拒绝会推进状态。
    """
    if n == 1:
        return state, 0
    limit = (1 << 64) - ((1 << 64) % n)
    while True:
        state, x = splitmix64_next(state)
        if x < limit:
            return state, x % n


# ──────────────────────────────────────────────────────────────
# 度分布：K >= LT_MIN_K 用 Robust Soliton，K < LT_MIN_K 走纯系统性循环
# ──────────────────────────────────────────────────────────────

# K <= 9 时 Robust Soliton 的 tau 尖峰 floor(K/R) 落在源块数之外
# （K=1 尖峰在第 14 块、K=5 在第 9 块、K=9 在第 10 块），度分布无意义。
# K=10 时 floor(K/R)=10 恰好相等，是可用的最小值。
LT_MIN_K = 10

# 写死，不可配置，两端必须一致（规格 §6.5）
SOLITON_C = Decimal('0.1')
SOLITON_DELTA = Decimal('0.5')

# decimal 上下文：libmpdec 实现 IEEE 754-2008，跨平台位精确。
# 用它算 CDF 再量化到 2^32 整数阈值，消除"浮点差 1 ulp 导致两端落到不同度"的风险。
_DECIMAL_PREC = 50


class DegreeTable:
    """给定 K 的 Robust Soliton 量化阈值表。K 固定，启动时算一次。

    PMF（写死，不可配置）：
        R    = c·sqrt(K)·ln(K/delta)             c=0.1, delta=0.5
        rho(1) = 1/K
        rho(i) = 1/(i·(i-1))                     i = 2..K
        tau(i) = R/(i·K)                         i = 1..floor(K/R)-1
        tau(floor(K/R)) = R·ln(R/delta)/K
        tau(i) = 0                               i > floor(K/R)
        beta   = sum_i(rho(i)+tau(i))
        mu(i)  = (rho(i)+tau(i))/beta
    """

    __slots__ = ('K', 'R', 'spike', 'thresholds')

    def __init__(self, K):
        if K < LT_MIN_K:
            raise ValueError(
                "DegreeTable 要求 K >= %d；K=%d 的 tau 尖峰落在源块数之外，"
                "这一档应走纯系统性循环（见 fountain.round_permutation）" % (LT_MIN_K, K)
            )
        ctx = getcontext()
        old_prec, old_round = ctx.prec, ctx.rounding
        ctx.prec = _DECIMAL_PREC
        ctx.rounding = ROUND_HALF_EVEN
        try:
            Kd = Decimal(K)
            R = SOLITON_C * Kd.sqrt() * (Kd / SOLITON_DELTA).ln()
            spike = int(Kd / R)                       # floor(K/R)

            rho = {1: Decimal(1) / Kd}
            for i in range(2, K + 1):
                rho[i] = Decimal(1) / (Decimal(i) * Decimal(i - 1))

            tau = {}
            for i in range(1, K + 1):
                if i < spike:
                    tau[i] = R / (Decimal(i) * Kd)
                elif i == spike:
                    tau[i] = R * (R / SOLITON_DELTA).ln() / Kd
                else:
                    tau[i] = Decimal(0)

            beta = sum(rho[i] + tau[i] for i in range(1, K + 1))

            thresholds = [0] * (K + 1)                # 1-based，[0] 是占位
            acc = Decimal(0)
            for i in range(1, K + 1):                 # CDF 累加顺序：i 从 1 到 K
                acc += (rho[i] + tau[i]) / beta
                thresholds[i] = int((acc * (1 << 32)).to_integral_value())
            thresholds[K] = 1 << 32                   # 收尾，兜住边界
        finally:
            ctx.prec, ctx.rounding = old_prec, old_round

        self.K = K
        self.R = R
        self.spike = spike
        self.thresholds = thresholds


def sample_degree(state, table):
    """只在 seed >= K 的 LT 包上调用。返回 (新 state, 度 d)。"""
    state, r = splitmix64_next(state)
    u32 = r >> 32                                  # 取高 32 位
    thresholds = table.thresholds
    for i in range(1, table.K + 1):                # CDF 从 i=1 累加向上
        if u32 < thresholds[i]:                    # 边界：严格小于，取第一个满足的 i
            return state, i                        # i 天然 <= K，无需再截断
    return state, table.K                          # 理论不可达（T[K] = 2^32 > 任意 u32）


def sample_indices(state, d, K):
    """Floyd 无放回抽样：取 d 个 [0, K) 内两两不同的索引。"""
    chosen = set()
    result = []
    for j in range(K - d, K):
        state, t = unbiased_below(state, j + 1)    # t 属于 [0, j]
        v = t if t not in chosen else j
        chosen.add(v)
        result.append(v)
    return state, result


def lt_indices(seed, table):
    """一个 LT 包参与 XOR 的源块索引集合。data = XOR(源块[i] for i in 返回值)。"""
    state = seed & MASK64
    state, d = sample_degree(state, table)
    state, idxs = sample_indices(state, d, table.K)
    return idxs


# ──────────────────────────────────────────────────────────────
# 编码器
# ──────────────────────────────────────────────────────────────

SEED_MAX = (1 << 32) - 1


class FountainExhausted(Exception):
    """包序号已达 2^32-1。绝不回绕——回绕会重新发出已用过的 seed，
    被接收端按 (seed, checksum) 误判成重复包而静默丢弃，丢失新信息。"""


def xor_bytes(dst, src):
    """把 src 原地 XOR 进 dst（bytearray）。两者必须等长。"""
    for i, b in enumerate(src):
        dst[i] ^= b


def round_permutation(round_no, K):
    """第 round_no 轮的 0..K-1 随机置换（Fisher-Yates 自后向前）。

    只影响发送端选块顺序——接收端根本不重算它，线上 seed 字段直接就是块编号。
    写进 reference vectors 是为了让标定和测试可复现。
    """
    state = round_no & MASK64
    perm = list(range(K))
    for i in range(K - 1, 0, -1):
        state, j = unbiased_below(state, i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    return perm


class FountainEncoder:
    """把 K 个等长源块变成无限的包生成器。

    发送端视角（接收端不需要知道这些分支，见 fountain.FountainDecoder）：
    - K < LT_MIN_K：不发任何 LT 包，只循环发系统性包，每轮一个随机置换。
      线上 seed = 块编号，恒落在 [0, K)。
    - K >= LT_MIN_K：线上 seed = 单调递增的包序号 n。前 K 个（n = 0..K-1）
      天然是系统性包，其后 n >= K 全是 LT 包。
    - M：K >= LT_MIN_K 时每发 M 个 LT 包插入一轮完整的 K 个系统性包，
      复用块号作为线上 seed（因此 < K），接收端零改动。
      M=None 表示无穷大（关闭），行为与不加此机制逐字节相同。
    """

    def __init__(self, blocks, M=None):
        if not blocks:
            raise ValueError("blocks 不能为空")
        blocklen = len(blocks[0])
        if blocklen == 0:
            raise ValueError("blocklen 不能为 0")
        if any(len(b) != blocklen for b in blocks):
            raise ValueError("所有源块必须等长（XOR 的前提）")
        if M is not None and M < 1:
            raise ValueError("M 必须 >= 1，或用 None 表示无穷大")

        self.blocks = [bytes(b) for b in blocks]
        self.K = len(self.blocks)
        self.blocklen = blocklen
        self.M = M
        self._n = 0                  # 包序号；K >= LT_MIN_K 时即线上 seed
        self._lt_since = 0           # 距上一轮系统性重发已发了多少个 LT 包
        self._resend = []            # 待插入的系统性块号队列
        self._perm_round = -1
        self._perm = None
        self._table = DegreeTable(self.K) if self.K >= LT_MIN_K else None

    def next_packet(self):
        """返回 (线上 seed, data)。data 长度恒为 blocklen。"""
        if self.K < LT_MIN_K:
            return self._next_systematic_cycle()
        return self._next_lt()

    def _next_systematic_cycle(self):
        round_no, j = divmod(self._n, self.K)
        if round_no != self._perm_round:
            self._perm = round_permutation(round_no, self.K)
            self._perm_round = round_no
        seed = self._perm[j]
        self._n += 1
        return seed, self.blocks[seed]

    def _next_lt(self):
        # 插入轮优先，且不推进包序号 n
        if self._resend:
            block = self._resend.pop(0)
            return block, self.blocks[block]

        n = self._n
        if n > SEED_MAX:
            raise FountainExhausted(
                "包序号已达 2^32-1（%d），停止发送。绝不回绕。" % SEED_MAX
            )
        self._n = n + 1

        if n < self.K:
            return n, self.blocks[n]

        idxs = lt_indices(n, self._table)
        data = bytearray(self.blocks[idxs[0]])
        for i in idxs[1:]:
            xor_bytes(data, self.blocks[i])

        if self.M is not None:
            self._lt_since += 1
            if self._lt_since >= self.M:
                self._lt_since = 0
                self._resend = list(range(self.K))

        return n, bytes(data)


# ──────────────────────────────────────────────────────────────
# 解码器：朴素剥离（peeling），不做高斯消元
# ──────────────────────────────────────────────────────────────

# 未剥离方程的存储上限（规格 §9）。超限时停止接纳新方程并计数，不无限增长。
MAX_PENDING_EQUATIONS = 4096
MAX_PENDING_INDEX_TOTAL = 1 << 20


class FountainDecoder:
    """增量喂包，解出 K 个源块。

    接收端的 seed 判定是唯一权威，且没有任何 K<=9 / K>=10 的模式分支：
        seed <  K  -> 系统性包，data 直接就是源块 seed 本身（度 1）
        seed >= K  -> LT 包，以 seed 为 splitmix64 种子推出度与索引
    """

    def __init__(self, K, blocklen, stall_threshold=32):
        if K < 1:
            raise ValueError("K 必须 >= 1")
        if blocklen < 1:
            raise ValueError("blocklen 必须 >= 1")
        self.K = K
        self.blocklen = blocklen
        self.stall_threshold = stall_threshold
        self._table = DegreeTable(K) if K >= LT_MIN_K else None
        self.reset()

    def reset(self):
        """清空全部解码状态。SHA256 不匹配时由调用方触发（规格 §9）。"""
        self._solved = {}                 # idx -> bytes
        self._pending = []                # [[set(indices), bytearray(data)], ...]
        self._pending_index_total = 0
        self._since_progress = 0
        self.equations_dropped = 0

    @property
    def solved_count(self):
        return len(self._solved)

    @property
    def pending_count(self):
        return len(self._pending)

    @property
    def is_complete(self):
        return len(self._solved) == self.K

    @property
    def is_stalled(self):
        """连续 stall_threshold 个包没推进任何块。进度条不能继续假涨。"""
        return not self.is_complete and self._since_progress >= self.stall_threshold

    def add_packet(self, seed, data):
        """喂一个包。返回 True 表示本包推进了至少一块。"""
        if len(data) != self.blocklen:
            raise ValueError(
                "data 长度必须恒等于 blocklen=%d，实得 %d" % (self.blocklen, len(data))
            )
        if seed < self.K:
            idxs = {seed}
        else:
            idxs = set(lt_indices(seed, self._table))

        before = len(self._solved)
        self._absorb(idxs, bytearray(data))
        progressed = len(self._solved) > before
        self._since_progress = 0 if progressed else self._since_progress + 1
        return progressed

    def _absorb(self, idxs, data):
        queue = [(idxs, data)]
        while queue:
            idxs, data = queue.pop()
            # 先用已解出的块把度降下来
            known = idxs & self._solved.keys()
            if known:
                for i in known:
                    xor_bytes(data, self._solved[i])
                idxs = idxs - known
            if not idxs:
                continue                          # 冗余包，信息量为零，丢弃
            if len(idxs) > 1:
                self._park(idxs, data)
                continue

            i = idxs.pop()
            if i in self._solved:
                continue
            self._solved[i] = bytes(data)

            # 用新解出的块化简所有未剥离方程
            still = []
            for p_idx, p_data in self._pending:
                if i in p_idx:
                    self._pending_index_total -= len(p_idx)
                    xor_bytes(p_data, self._solved[i])
                    p_idx = p_idx - {i}
                    if not p_idx:
                        continue                  # 化简成空，丢弃
                    if len(p_idx) == 1:
                        queue.append((p_idx, p_data))
                        continue
                    self._pending_index_total += len(p_idx)
                still.append([p_idx, p_data])
            self._pending = still

    def _park(self, idxs, data):
        if (len(self._pending) >= MAX_PENDING_EQUATIONS
                or self._pending_index_total + len(idxs) > MAX_PENDING_INDEX_TOTAL):
            self.equations_dropped += 1           # 停止接纳，计数，不无限增长
            return
        self._pending.append([idxs, data])
        self._pending_index_total += len(idxs)

    def assemble(self):
        """按块号拼出完整 payload（含尾部填充）。未完成时抛 ValueError。"""
        if not self.is_complete:
            raise ValueError(
                "只解出 %d/%d 块，不能拼装。收够包数不等于能解出（本设计不做高斯消元）"
                % (len(self._solved), self.K)
            )
        return b''.join(self._solved[i] for i in range(self.K))
