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
    SEED_MAX, FountainExhausted, FountainEncoder, round_permutation, xor_bytes,
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


def test_round_permutation_reference_vectors():
    print("[TEST] round_permutation reference vectors...")
    cases = {
        (1, 0): [0], (1, 1): [0], (1, 2): [0],
        (2, 0): [0, 1], (2, 1): [0, 1], (2, 2): [1, 0],
        (5, 0): [2, 3, 1, 4, 0], (5, 1): [2, 1, 4, 3, 0], (5, 2): [1, 3, 4, 2, 0],
        (9, 0): [1, 0, 3, 5, 6, 8, 2, 4, 7],
        (9, 1): [2, 4, 3, 0, 6, 8, 1, 7, 5],
        (9, 2): [5, 1, 7, 3, 8, 6, 0, 2, 4],
    }
    for (K, r), want in sorted(cases.items()):
        got = round_permutation(r, K)
        assert got == want, "K=%d round=%d 应为 %r，实得 %r" % (K, r, want, got)
    # 任何轮次都必须是 0..K-1 的一个真置换
    for K in range(1, 10):
        for r in range(50):
            perm = round_permutation(r, K)
            assert sorted(perm) == list(range(K)), "K=%d round=%d 不是合法置换" % (K, r)
    print("  ✅ PASSED")


def test_encoder_small_k_is_pure_systematic():
    print("[TEST] K<=9 只发系统性包，线上 seed 恒 < K...")
    blocks = [bytes([i]) * 8 for i in range(9)]
    enc = FountainEncoder(blocks)
    assert enc.K == 9 and enc.blocklen == 8
    for _ in range(500):
        seed, data = enc.next_packet()
        assert 0 <= seed < 9, "K<=9 时线上 seed 必须恒落在 [0, K)，实得 %d" % seed
        assert data == blocks[seed], "系统性包的 data 必须就是源块 seed 本身"
    print("  ✅ PASSED")


def test_encoder_small_k_covers_every_round():
    print("[TEST] K<=9 每一轮全覆盖...")
    blocks = [bytes([i]) * 4 for i in range(5)]
    enc = FountainEncoder(blocks)
    for _ in range(20):                      # 20 轮
        seeds = [enc.next_packet()[0] for _ in range(5)]
        assert sorted(seeds) == [0, 1, 2, 3, 4], \
            "每轮必须恰好覆盖全部 K 个块一次，实得 %r" % (seeds,)
    print("  ✅ PASSED")


def test_encoder_periodic_loss_regression():
    print("[TEST] 周期丢帧回归：固定顺序永久缺块，随机置换能收齐...")
    K = 9

    def frames_to_collect(use_permutation, max_frames):
        got, n = set(), 0
        while len(got) < K and n < max_frames:
            r, j = divmod(n, K)
            blk = round_permutation(r, K)[j] if use_permutation else j
            if n % K != 0:                   # 每 K 帧丢 1 帧，丢帧率仅 11%
                got.add(blk)
            n += 1
        return n if len(got) == K else None

    assert frames_to_collect(False, 9000) is None, \
        "固定顺序在周期丢帧下必然永久缺块（这正是要修的病态别名）"
    got = frames_to_collect(True, 9000)
    assert got is not None and got <= 30, \
        "随机置换应在 30 帧内收齐，实得 %r" % (got,)
    print("  ✅ PASSED")


def test_encoder_large_k_seed_is_monotonic():
    print("[TEST] K>=10 线上 seed = 单调递增的包序号 n...")
    blocks = [bytes([i]) * 16 for i in range(12)]
    enc = FountainEncoder(blocks)
    for n in range(300):
        seed, data = enc.next_packet()
        assert seed == n, "K>=10 且 M=∞ 时线上 seed 必须等于包序号 %d，实得 %d" % (n, seed)
        assert len(data) == 16
    print("  ✅ PASSED")


def test_encoder_large_k_first_k_are_systematic():
    print("[TEST] K>=10 前 K 个包天然是系统性包...")
    blocks = [bytes([i]) * 16 for i in range(12)]
    enc = FountainEncoder(blocks)
    for n in range(12):
        seed, data = enc.next_packet()
        assert seed == n
        assert data == blocks[n], "前 K 个包的 data 必须就是源块本身"
    print("  ✅ PASSED")


def test_encoder_m_parameter_inserts_systematic_rounds():
    print("[TEST] M 参数：每 M 个 LT 包插一轮 K 个系统性包...")
    K, M = 10, 5
    blocks = [bytes([i]) * 8 for i in range(K)]
    enc = FountainEncoder(blocks, M=M)
    seeds = [enc.next_packet()[0] for _ in range(K + M + K + 3)]
    # 前 K 个：系统性（n = 0..K-1）
    assert seeds[:K] == list(range(K))
    # 接着 M 个 LT 包：seed = K..K+M-1
    assert seeds[K:K + M] == list(range(K, K + M))
    # 然后插入一轮完整的 K 个系统性包，seed 复用块号（因此 < K）
    inserted = seeds[K + M:K + M + K]
    assert sorted(inserted) == list(range(K)), \
        "插入轮必须是完整的 K 个块号，实得 %r" % (inserted,)
    # 插入轮不推进包序号 n，恢复后继续从 K+M 递增
    assert seeds[K + M + K:] == [K + M, K + M + 1, K + M + 2], \
        "插入轮不得推进包序号 n，实得 %r" % (seeds[K + M + K:],)
    print("  ✅ PASSED")


def test_encoder_m_infinity_is_byte_identical():
    print("[TEST] M=None（∞）与不加此机制逐字节相同...")
    K = 12
    blocks = [bytes([i]) * 8 for i in range(K)]
    a = FountainEncoder(blocks, M=None)
    b = FountainEncoder(blocks)
    for _ in range(200):
        assert a.next_packet() == b.next_packet()
    print("  ✅ PASSED")


def test_encoder_seed_does_not_wrap():
    print("[TEST] seed 到 2^32-1 停止，绝不回绕...")
    K = 10
    blocks = [bytes([i]) * 4 for i in range(K)]
    enc = FountainEncoder(blocks)
    enc._n = SEED_MAX                        # 直接推到边界
    seed, _ = enc.next_packet()
    assert seed == SEED_MAX
    try:
        enc.next_packet()
    except FountainExhausted:
        print("  ✅ PASSED")
        return
    raise AssertionError("超过 SEED_MAX 必须抛 FountainExhausted，绝不回绕")


def test_encoder_rejects_ragged_blocks():
    print("[TEST] 源块必须等长...")
    for bad in ([], [b'aaa', b'aa'], [b'', b'']):
        try:
            FountainEncoder(bad)
        except ValueError:
            continue
        raise AssertionError("应当拒绝 %r" % (bad,))
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
    test_round_permutation_reference_vectors()
    test_encoder_small_k_is_pure_systematic()
    test_encoder_small_k_covers_every_round()
    test_encoder_periodic_loss_regression()
    test_encoder_large_k_seed_is_monotonic()
    test_encoder_large_k_first_k_are_systematic()
    test_encoder_m_parameter_inserts_systematic_rounds()
    test_encoder_m_infinity_is_byte_identical()
    test_encoder_seed_does_not_wrap()
    test_encoder_rejects_ragged_blocks()
    print("\n✅ All fountain PRNG tests passed!")
