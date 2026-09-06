#!/usr/bin/env python3
"""QueQiao v3 喷泉码内核：纯字节、零 IO、零第三方依赖。

设计约束（来自规格 §6.2）：不能用 random.Random(seed)——CPython 的
randint/sample 内部算法在版本间不保证不变，而发送端和接收端必须用同一个
种子推出同一组源块索引，一旦不一致就全盘解不出。所以内嵌 splitmix64，
编解码两端共用，跨 Python 版本、跨 Windows/macOS 完全确定。
"""

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
