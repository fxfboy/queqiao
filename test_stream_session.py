#!/usr/bin/env python3
"""stream_session.py：接收端会话状态机。

K=1 / K=2 的死锁回归是本文件最重要的用例——这两档对应原文件约 5-17 KB
（配置补丁、单个 patch），是主路径而不是边角。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fountain import FountainEncoder
from stream_packet import build_payload, pack_packet, split_blocks
from stream_session import LOCK_MIN_PACKETS, StreamSession


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


if __name__ == '__main__':
    test_lock_requires_three_packets()
    test_k1_does_not_deadlock()
    test_k2_does_not_deadlock()
    test_lock_needs_distinct_seeds_when_k_large()
    test_end_to_end_clean_session()
    test_foreign_and_corrupt_are_distinguished()
    test_progress_and_stall_reporting()
    print("\n✅ All stream session tests passed!")
