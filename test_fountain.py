#!/usr/bin/env python3
"""fountain.py 的单元测试：纯字节、零 IO、零依赖。

reference vectors 是跨 Python 版本、跨 Windows/macOS 一致性的唯一硬保障，
比任何统计测试都硬。任何一条断言失败都意味着两端会解不出，不是"精度问题"。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fountain import (
    MASK64, splitmix64_next, unbiased_below,
    LT_MIN_K, DegreeTable, sample_degree, sample_indices, lt_indices,
)


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


def test_degree_table_reference_values():
    print("[TEST] DegreeTable R 值与尖峰位置...")
    # R 的定点参考值：floor(R * 2^20)。用定点比对而不是浮点相等，
    # 既能钉死数值又不受 Decimal repr 影响。
    cases = {
        10: (993351, 10),        # (floor(R*2^20), spike=floor(K/R))
        100: (5555688, 18),
        1000: (25203744, 41),
    }
    for K, (want_fixed, want_spike) in cases.items():
        t = DegreeTable(K)
        got_fixed = int(t.R * (1 << 20))
        assert got_fixed == want_fixed, \
            "K=%d 的 floor(R*2^20) 应为 %d，实得 %d" % (K, want_fixed, got_fixed)
        assert t.spike == want_spike, \
            "K=%d 的 spike 应为 %d，实得 %d" % (K, want_spike, t.spike)
    print("  ✅ PASSED")


def test_degree_table_thresholds():
    print("[TEST] DegreeTable 量化阈值表...")
    cases = {
        10: [629544998, 2399102804, 3039998635],
        100: [206922051, 1936633538, 2542215452],
        1000: [89913269, 1928742066, 2556071987],
    }
    for K, want_head in cases.items():
        t = DegreeTable(K)
        assert len(t.thresholds) == K + 1, "thresholds 是 1-based，长度应为 K+1"
        assert t.thresholds[0] == 0, "thresholds[0] 是占位，恒为 0"
        for i, want in enumerate(want_head, start=1):
            assert t.thresholds[i] == want, \
                "K=%d 的 T[%d] 应为 %d，实得 %d" % (K, i, want, t.thresholds[i])
        # 收尾必须强制到 2^32，兜住浮点边界
        assert t.thresholds[K] == (1 << 32), \
            "K=%d 的 T[K] 必须强制等于 2^32，实得 %d" % (K, t.thresholds[K])
        # CDF 必须单调不减
        for i in range(1, K):
            assert t.thresholds[i] <= t.thresholds[i + 1], "CDF 必须单调不减"
    print("  ✅ PASSED")


def test_degree_table_rejects_small_k():
    print("[TEST] DegreeTable 拒绝 K < LT_MIN_K...")
    # K <= 9 时 tau 尖峰 floor(K/R) 落在源块数之外（K=1 尖峰在第 14 块，
    # K=9 在第 10 块），度分布无意义。这一档走纯系统性循环，不建表。
    assert LT_MIN_K == 10
    for K in (1, 2, 5, 9):
        try:
            DegreeTable(K)
        except ValueError:
            continue
        raise AssertionError("K=%d 应当拒绝建表（尖峰越界）" % K)
    print("  ✅ PASSED")


def test_lt_indices_reference_vectors():
    print("[TEST] lt_indices reference vectors...")
    # 这是跨平台一致性的核心断言：同一个 (K, seed) 必须推出同一组索引。
    cases = [
        (10, 10, 1, [4]),
        (10, 11, 2, [4, 9]),
        (10, 17, 2, [1, 6]),
        (10, 1000000, 2, [3, 6]),
        (100, 100, 2, [36, 77]),
        (100, 101, 11, [71, 37, 3, 91, 93, 24, 43, 96, 8, 39, 7]),
        (100, 107, 2, [79, 28]),
        (100, 1000000, 2, [30, 96]),
        (1000, 1000, 2, [997, 121]),
        (1000, 1001, 2, [727, 297]),
        (1000, 1000000, 2, [210, 696]),
    ]
    tables = {K: DegreeTable(K) for K in (10, 100, 1000)}
    for K, seed, want_d, want_idxs in cases:
        idxs = lt_indices(seed, tables[K])
        assert len(idxs) == want_d, \
            "K=%d seed=%d 的度应为 %d，实得 %d" % (K, seed, want_d, len(idxs))
        assert idxs == want_idxs, \
            "K=%d seed=%d 的索引应为 %r，实得 %r" % (K, seed, want_idxs, idxs)
    print("  ✅ PASSED")


def test_sample_indices_no_duplicates():
    print("[TEST] sample_indices 无放回、值域正确...")
    K = 100
    state = 7
    for d in (1, 2, 5, 50, 99, 100):
        state, idxs = sample_indices(state, d, K)
        assert len(idxs) == d, "应取到 %d 个索引，实得 %d" % (d, len(idxs))
        assert len(set(idxs)) == d, "索引必须两两不同（Floyd 无放回）"
        assert all(0 <= i < K for i in idxs), "索引必须落在 [0, K)"
    print("  ✅ PASSED")


def test_sample_degree_bounds():
    print("[TEST] sample_degree 度恒落在 [1, K]...")
    for K in (10, 100, 1000):
        table = DegreeTable(K)
        state = 3
        for _ in range(5000):
            state, d = sample_degree(state, table)
            assert 1 <= d <= K, "K=%d 的度 %d 越界" % (K, d)
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_splitmix64_reference_vectors()
    test_unbiased_below_convention_n1()
    test_unbiased_below_reference_vectors()
    test_unbiased_below_range_and_no_modulo_bias()
    test_unbiased_below_rejection_consumes_state()
    test_degree_table_reference_values()
    test_degree_table_thresholds()
    test_degree_table_rejects_small_k()
    test_lt_indices_reference_vectors()
    test_sample_indices_no_duplicates()
    test_sample_degree_bounds()
    print("\n✅ All fountain PRNG tests passed!")
