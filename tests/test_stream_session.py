#!/usr/bin/env python3
"""stream_session.py：接收端会话状态机。

K=1 / K=2 的死锁回归是本文件最重要的用例——这两档对应原文件约 5-17 KB
（配置补丁、单个 patch），是主路径而不是边角。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fountain import FountainEncoder, LT_MIN_K
from stream_packet import build_payload, pack_packet, split_blocks, unpack_packet
from stream_session import LOCK_MIN_PACKETS, SessionStats, StreamSession


def incompressible(n, seed=20260905):
    """生成 n 字节确定性高熵数据（lzma 压不动）。

    b'x' * n 这类重复串会被 lzma 压到几十字节，payload 撞不满一个 block，
    K 恒等于 1——而 §8.6 lock 规则的两个合取项（包数≥3 / 不同 seed 数≥min(3,K)）
    只有在 K>=3 时才分得开。

    这里用 random.Random 只是造测试数据，**不是协议 PRNG**——协议里的选块与
    度分布抽样一律走 fountain.splitmix64，两者不能混。
    """
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


def make_stream(filename, data, blocklen, nonce_override=None):
    """产出一个无限的 (raw_packet_bytes,) 生成器，模拟发送端。"""
    payload, _, nonce = build_payload(filename, data)
    if nonce_override is not None:
        nonce = nonce_override
    blocks = split_blocks(payload, blocklen)
    enc = FountainEncoder(blocks)
    K = len(blocks)

    def gen():
        while True:
            seed, chunk = enc.next_packet()
            yield pack_packet(nonce, seed, K, blocklen, chunk)

    return gen(), K, data


def test_lock_requires_three_packets():
    print("[TEST] lock 需要累计 >= 3 个合法包...")
    stream, K, _ = make_stream("a.txt", incompressible(5000), 800)
    assert K >= 3
    s = StreamSession()
    events = [s.feed(next(stream)) for _ in range(LOCK_MIN_PACKETS)]
    assert events[:2] == ['accumulating', 'accumulating'], \
        "前两个包只能累积不能 lock，实得 %r" % (events[:2],)
    assert events[2] == 'locked', "第 3 个包应触发 lock，实得 %r" % events[2]
    assert s.locked_key is not None and s.K == K
    print("  ✅ PASSED")


def test_k1_does_not_deadlock():
    print("[TEST] K=1 不死锁（P1 回归）...")
    # K=1 时线上 seed 恒为 0，只有一个不同 seed。
    # 若门槛写成"3 个不同 seed"，这里会永远 lock 不上 —— 小文件必然死锁。
    data = b"tiny config patch\n"
    stream, K, _ = make_stream("tiny.txt", data, 4096)
    assert K == 1, "本用例要求 K=1，实得 %d" % K
    s = StreamSession()
    events = []
    for _ in range(10):
        events.append(s.feed(next(stream)))
        if s.is_complete:
            break
    assert s.is_complete, "K=1 必须能完成，实得事件序列 %r" % (events,)
    # 抗残留强度：仍然需要 3 个包才 lock，不是首包即锁
    assert events[:2] == ['accumulating', 'accumulating'], \
        "K=1 也必须收满 3 个包才 lock（残留画面通常是 1 帧），实得 %r" % (events[:2],)
    assert s.assemble() is not None
    print("  ✅ PASSED")


def test_k2_does_not_deadlock():
    print("[TEST] K=2 不死锁（P1 回归）...")
    stream, K, data = make_stream("small.txt", b"a small patch\n" * 60, 200)
    assert K == 2, "本用例要求 K=2，实得 %d" % K
    s = StreamSession()
    for _ in range(20):
        s.feed(next(stream))
        if s.is_complete:
            break
    assert s.is_complete, "K=2 必须能完成"
    print("  ✅ PASSED")


def test_lock_needs_distinct_seeds_when_k_large():
    print("[TEST] K>=3 时 3 个不同 seed 是等价条件...")
    stream, K, _ = make_stream("a.txt", incompressible(5000), 800)
    assert K >= 3
    payload_packets = [next(stream) for _ in range(1)]
    s = StreamSession()
    # 连喂同一个包 5 次：包数够了，但不同 seed 只有 1 个 → 不能 lock
    for _ in range(5):
        ev = s.feed(payload_packets[0])
        assert ev in ('accumulating', 'duplicate'), \
            "重复同一个包不该 lock，实得 %r" % ev
    assert s.locked_key is None, "只有 1 个不同 seed 时 K>=3 不得 lock"
    print("  ✅ PASSED")


def test_end_to_end_clean_session():
    print("[TEST] 干净会话端到端解出...")
    data = incompressible(40000)
    stream, K, _ = make_stream("blob.bin", data, 800)
    s = StreamSession()
    fed = 0
    while not s.is_complete and fed < 10 * K + 50:
        s.feed(next(stream))
        fed += 1
    assert s.is_complete, "只解出 %d/%d 块" % (s.solved_count, K)
    from stream_packet import parse_payload
    meta, restored = parse_payload(s.assemble(), s.K, s.blocklen)
    assert restored == data, "端到端必须逐字节还原"
    assert meta['filename'] == "blob.bin"
    print("  ✅ PASSED")


def test_foreign_and_corrupt_are_distinguished():
    print("[TEST] 非本协议字节与损坏包分开归类...")
    s = StreamSession()
    assert s.feed(b'hello world') == 'foreign', "非 QF 字节算 foreign，不算损坏"
    assert s.feed(b'QR' + b'\x00' * 20) == 'foreign', "v1/v2 的包也算 foreign"
    stream, _, _ = make_stream("a.txt", b"x" * 5000, 800)
    bad = bytearray(next(stream))
    bad[-1] ^= 0xFF
    assert s.feed(bytes(bad)) == 'corrupt', "checksum 失败算 corrupt"
    print("  ✅ PASSED")


def test_progress_and_stall_reporting():
    print("[TEST] progress 反映真实解出块数，不假涨...")
    stream, K, _ = make_stream("a.txt", incompressible(60000), 800)
    s = StreamSession()
    assert s.progress == 0.0
    for _ in range(LOCK_MIN_PACKETS):
        s.feed(next(stream))
    assert s.locked_key is not None, "3 个包后应已 lock"
    # lock 前的包（含触发 lock 的那一个）只记 (seed, checksum) 不留 data，
    # 都不喂解码器，所以刚 lock 完解码器是空的——progress 恰为 0.0，
    # 这是确定值而不是"还没来得及"。若将来有人把触发包改成也喂进去，
    # 这条断言会立刻报警，逼他重新审视 'locked' 事件名的语义。
    assert s.progress == 0.0, \
        "刚 lock 时解码器还是空的，progress 必须恰为 0.0，实得 %r" % s.progress
    # K=76 >= LT_MIN_K，前 K 个包都是系统性包，所以 lock 后第一个包（seed=3）
    # 必然直接给出一个源块，进度必然严格大于 0——不依赖度分布抽样的运气。
    s.feed(next(stream))
    assert 0.0 < s.progress < 1.0, "喂进第一个包后应有进度，实得 %r" % s.progress
    prev = s.progress
    while not s.is_complete:
        s.feed(next(stream))
        assert s.progress >= prev, "进度不得回退"
        assert s.progress <= 1.0
        prev = s.progress
    assert s.progress == 1.0
    print("  ✅ PASSED")


def test_seed_conflict_forces_reset():
    print("[TEST] 同 seed 不同 data 立即重置（优先级最高）...")
    # 两个不同文件人为撞到同一个 (nonce, K, blocklen)——模拟 16 位 nonce 碰撞。
    # 用两个不同种子的高熵数据（而不是 b"A"*5000 这类重复串）：重复串会被
    # lzma 压到 K=1，K=1 时 a、b 会话共用同一个 seed=0，两次断言 K==K2 虽然
    # 仍然成立，但 test_end_to_end 之外的很多假设会跟着塌——这里固定用
    # incompressible() 保证 K 落在两位数，两个会话各自有多个不同 seed。
    a, K, _ = make_stream("a.txt", incompressible(5000, seed=1), 800, nonce_override=0x1234)
    b, K2, _ = make_stream("b.txt", incompressible(5000, seed=2), 800, nonce_override=0x1234)
    assert K == K2, "本用例要求两个会话的 K 相同（这正是碰撞的定义）"

    s = StreamSession()
    for _ in range(LOCK_MIN_PACKETS):
        s.feed(next(a))
    assert s.locked_key is not None
    # lock 后再喂几个，这几个才真正进了新建的去重表
    victim = None
    for _ in range(3):
        raw = next(a)
        if victim is None:
            victim = unpack_packet(raw)
        s.feed(raw)
    solved_before = s.solved_count
    assert solved_before > 0

    # 不能靠两个独立流"凑巧撞上同一个 seed"：K<10 时 seed 由
    # round_permutation(轮次, K) 决定，K 相同则两流逐项相同；K>=10 时 seed
    # 从 0 严格递增——两种情况下 b 的头几个包用的都是 a 在 lock 时已丢弃
    # 的那批 seed，撞不到 lock 后新建的去重表。改为从 b 流里筛出与 victim
    # 同 seed 的那个包：它 nonce/K/blocklen 与 a 全同（nonce_override 模拟碰撞）、
    # seed 相同而 data 不同，就是定义上的冲突包。
    conflict_raw = None
    for _ in range(2 * K):
        raw = next(b)
        if unpack_packet(raw).seed == victim.seed:
            conflict_raw = raw
            break
    assert conflict_raw is not None, "2K 个包内必然出现该 seed"

    ev = s.feed(conflict_raw)
    assert ev == 'conflict', "同 seed 不同 checksum 必须报 conflict，实得 %r" % ev
    assert s.solved_count == 0, "冲突必须清空解码状态，不等门槛"
    assert s.locked_key is None, "冲突后应解锁，从零重新累积"
    assert s.stats.conflicts == 1

    # 冲突后能正常锁到 b 会话并解出
    fed = 0
    while not s.is_complete and fed < 10 * K + 50:
        s.feed(next(b))
        fed += 1
    assert s.is_complete, "冲突后必须能重新锁定并解出新会话"
    print("  ✅ PASSED")


def test_session_switch_when_locked_goes_quiet():
    print("[TEST] locked 会话长时间无新包 + 另一桶达门槛 → 切换...")
    a, _, _ = make_stream("a.txt", incompressible(5000), 800)
    b, Kb, data_b = make_stream("b.txt", incompressible(9000), 900)   # 不同 blocklen → 不同桶

    s = StreamSession(switch_idle_packets=5)
    for _ in range(LOCK_MIN_PACKETS):
        s.feed(next(a))
    locked_a = s.locked_key
    assert locked_a is not None

    # a 停播，b 开始播。前几个 b 包只能进别的桶
    events = []
    for _ in range(20):
        events.append(s.feed(next(b)))
        if s.locked_key != locked_a:
            break
    assert 'switched' in events, "另一桶达门槛且当前会话静默后应切换，实得 %r" % (events,)
    assert s.locked_key != locked_a and s.K == Kb
    assert s.solved_count <= 1, "切换必须清空解码状态"
    assert s.stats.switches == 1

    fed = 0
    while not s.is_complete and fed < 10 * Kb + 50:
        s.feed(next(b))
        fed += 1
    assert s.is_complete, "切换后必须能解出新会话"
    print("  ✅ PASSED")


def test_no_switch_while_locked_session_is_active():
    print("[TEST] locked 会话仍在活跃时不切换...")
    # _idle 清零的条件是 admit() 返回 'new'（_decode 里 self._idle = 0 在
    # add_packet 之前），不是"解出了新块"——'redundant' 同样清零。
    # 所以 K>=LT_MIN_K 时 seed 严格递增、checksum 各异，每个包都是 'new'，
    # _idle 永远清零，哪怕会话早解完了也不会被切走。取 K≈63（> 60 轮预算）
    # 不是机理必需，只是让下面"a 一直有新包"这句注释在字面上也成立。
    a, _, _ = make_stream("a.txt", incompressible(50000, seed=1), 800)   # K≈63
    b, _, _ = make_stream("b.txt", b"B" * 9000, 900)                      # K=1，见下方说明
    s = StreamSession(switch_idle_packets=5)
    for _ in range(LOCK_MIN_PACKETS):
        s.feed(next(a))
    locked_a = s.locked_key
    # a 和 b 交替出现：a 一直有新包，不该切走。b 故意用 b"B"*9000/900 压成
    # K=1：同时用到合取式门槛（min(3,1)=1，3 个包即可 lock）和 §8.6"切换门槛
    # 取候选桶自己的 K"这条细节——两桶 K 分别是 63 和 1。
    for _ in range(60):
        s.feed(next(a))
        s.feed(next(b))
    assert s.locked_key == locked_a, "当前会话持续有新包时不得切换"
    assert s.stats.switches == 0
    print("  ✅ PASSED")


def test_completed_session_can_be_switched_away():
    print("[TEST] 已完整解出的会话不再霸占 lock...")
    # K<=9 解完后发送端继续循环重放，对接收端全是 duplicate，按 §8.6
    # "持续无新包"的口径就该让位——否则连传两个文件时第二个永远收不到。
    a, K, _ = make_stream("a.txt", incompressible(5000, seed=1), 800)
    assert K < LT_MIN_K, "本用例依赖纯系统性循环：解完后的包必然是逐字节重放"
    b, _, _ = make_stream("b.txt", b"B" * 9000, 900)
    s = StreamSession(switch_idle_packets=5)
    for _ in range(LOCK_MIN_PACKETS):
        s.feed(next(a))
    locked_a = s.locked_key
    while not s.is_complete:
        s.feed(next(a))
    for _ in range(20):
        s.feed(next(a))
        s.feed(next(b))
        if s.locked_key != locked_a:
            break
    assert s.locked_key != locked_a, "解完的会话应让位给新会话"
    assert s.stats.switches == 1
    print("  ✅ PASSED")


def test_reset_decoding_clears_state():
    print("[TEST] reset_decoding 清空解码状态但保留 lock...")
    a, K, _ = make_stream("a.txt", incompressible(5000), 800)
    s = StreamSession()
    for _ in range(LOCK_MIN_PACKETS + 5):
        s.feed(next(a))
    assert s.solved_count > 0
    s.reset_decoding('sha256')
    assert s.solved_count == 0, "SHA256 不匹配必须丢弃全部解码状态"
    assert s.stats.resets == 1
    # 重置后必须还能从零累积到完成（坏包的 LT seed 几乎不会再出现）
    fed = 0
    while not s.is_complete and fed < 10 * K + 50:
        s.feed(next(a))
        fed += 1
    assert s.is_complete, "重置后必须能重新累积到完成"
    print("  ✅ PASSED")


def test_stats_accounting():
    print("[TEST] 累计诊断口径...")
    a, _, _ = make_stream("a.txt", incompressible(5000), 800)
    s = StreamSession()
    s.note_frame()
    s.feed(b'not a queqiao packet')
    bad = bytearray(next(a))
    bad[-1] ^= 0xFF
    s.feed(bytes(bad))
    first = next(a)
    s.feed(first)
    s.feed(first)                                # 重复包
    assert s.stats.frames == 1
    assert s.stats.codes == 4, "每次 feed 都算一个码，实得 %d" % s.stats.codes
    assert s.stats.foreign == 1
    assert s.stats.corrupt == 1
    assert s.stats.valid == 2, "重复包也是通过校验的有效包"
    assert s.stats.duplicate == 1
    text = s.stats.summary()
    for token in ('帧', '码', '有效', '损坏'):
        assert token in text, "诊断摘要应含 %r，实得 %r" % (token, text)
    assert '\ufffd' not in text, "诊断摘要不得含替换字符"
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_lock_requires_three_packets()
    test_k1_does_not_deadlock()
    test_k2_does_not_deadlock()
    test_lock_needs_distinct_seeds_when_k_large()
    test_end_to_end_clean_session()
    test_foreign_and_corrupt_are_distinguished()
    test_progress_and_stall_reporting()
    test_seed_conflict_forces_reset()
    test_session_switch_when_locked_goes_quiet()
    test_no_switch_while_locked_session_is_active()
    test_completed_session_can_be_switched_away()
    test_reset_decoding_clears_state()
    test_stats_accounting()
    print("\n✅ All stream session tests passed!")
