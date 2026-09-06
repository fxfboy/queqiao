#!/usr/bin/env python3
"""fountain.py 的单元测试：纯字节、零 IO、零依赖。

reference vectors 是跨 Python 版本、跨 Windows/macOS 一致性的唯一硬保障，
比任何统计测试都硬。任何一条断言失败都意味着两端会解不出，不是"精度问题"。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fountain import MASK64, splitmix64_next, unbiased_below


def test_splitmix64_reference_vectors():
    print("[TEST] splitmix64 reference vectors...")
    # 官方 splitmix64 参考实现 seed=0 的前 5 个输出
    expected = [
        0xE220A8397B1DCDAF,
        0x6E789E6AA1B965F4,
        0x06C45D188009454F,
        0xF88BB8A8724C81EC,
        0x1B39896A51A8749B,
    ]
    state = 0
    for i, want in enumerate(expected):
        state, z = splitmix64_next(state)
        assert z == want, "seed=0 第 %d 个输出应为 0x%016X，实得 0x%016X" % (i, want, z)

    # state 演进也要固定：第一步 state 就是加上黄金分割常数
    state, _ = splitmix64_next(0)
    assert state == 0x9E3779B97F4A7C15, "state 演进必须是先加常数再混合"

    # 另一个种子的向量，防止"只对 0 正确"的实现蒙混过关
    expected_12345 = [0x22118258A9D111A0, 0x346EDCE5F713F8ED, 0x1E9A57BC80E6721D]
    state = 12345
    for i, want in enumerate(expected_12345):
        state, z = splitmix64_next(state)
        assert z == want, "seed=12345 第 %d 个输出应为 0x%016X，实得 0x%016X" % (i, want, z)

    # 输出必须落在 64 位无符号范围内
    state = 0
    for _ in range(200):
        state, z = splitmix64_next(state)
        assert 0 <= z <= MASK64
        assert 0 <= state <= MASK64
    print("  ✅ PASSED")


def test_unbiased_below_convention_n1():
    print("[TEST] unbiased_below 约定①：n==1 不推进状态...")
    # n == 1 时区间只有一个值，直接返回 0，不消耗一次 next()。
    # 两端必须一致，否则后续所有抽样错位。
    for state in (0, 999, 2 ** 63, MASK64):
        new_state, value = unbiased_below(state, 1)
        assert value == 0, "n=1 必须返回 0"
        assert new_state == state, "n=1 必须不推进 PRNG 状态（约定①）"
    print("  ✅ PASSED")


def test_unbiased_below_reference_vectors():
    print("[TEST] unbiased_below reference vectors...")
    state = 0
    got = []
    for _ in range(5):
        state, v = unbiased_below(state, 10)
        got.append(v)
    assert got == [5, 0, 9, 4, 7], "unbiased_below(0, 10) 前 5 个应为 [5,0,9,4,7]，实得 %r" % (got,)
    print("  ✅ PASSED")


def test_unbiased_below_range_and_no_modulo_bias():
    print("[TEST] unbiased_below 值域与无取模偏置...")
    for n in (2, 3, 7, 10, 100, 1000):
        state = 42
        counts = [0] * n
        for _ in range(20000):
            state, v = unbiased_below(state, n)
            assert 0 <= v < n, "值必须落在 [0, %d)" % n
            counts[v] += 1
        # 容差取「±25% 与 ±5σ 的较大者」。固定 ±25% 对大 n 站不住：
        # n=1000 时每桶均值仅 20（σ≈4.47），±25% 只有 1.12σ，单桶越界先验
        # 概率 26.9%，1000 个桶必然误报——换成真随机源同样必挂，是测试的
        # 统计设计问题，不是实现的取模偏置。
        # n≤10 时 0.25·mean 恒大于 5·√mean，这几档行为与原计划逐字节相同。
        # 另断言每桶都被取到过：n=1000 下单桶零命中先验概率 2e-9，能抓住
        # 「某些值永远取不到」这类真实缺陷，又不会误报。
        mean = 20000 / n
        tol = max(mean * 0.25, 5 * math.sqrt(mean))
        for i, c in enumerate(counts):
            assert c > 0, "n=%d 桶 %d 一次都没被取到，疑似值域缺口" % (n, i)
            assert abs(c - mean) < tol, \
                "n=%d 桶 %d 计数 %d 偏离均值 %.1f 超出容差 %.1f" % (n, i, c, mean, tol)
    print("  ✅ PASSED")


def test_unbiased_below_rejection_consumes_state():
    print("[TEST] unbiased_below 约定②：被拒的抽样照常消耗一次 next()...")
    # 构造一次必然发生拒绝的抽样：n 取一个 2^64 不整除的值，
    # 手工模拟"拒绝则继续抽"的过程，断言实现与手工模拟逐步一致。
    n = 3
    limit = (1 << 64) - ((1 << 64) % n)
    state = 0
    manual_state = 0
    for _ in range(50):
        # 手工模拟：每次被拒都推进一次
        s = manual_state
        while True:
            s, x = splitmix64_next(s)
            if x < limit:
                break
        manual_state = s
        state, _ = unbiased_below(state, n)
        assert state == manual_state, "被拒的抽样必须照常推进状态（约定②）"
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_splitmix64_reference_vectors()
    test_unbiased_below_convention_n1()
    test_unbiased_below_reference_vectors()
    test_unbiased_below_range_and_no_modulo_bias()
    test_unbiased_below_rejection_consumes_state()
    print("\n✅ All fountain PRNG tests passed!")
