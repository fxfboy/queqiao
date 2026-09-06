#!/usr/bin/env python3
"""stream_packet.py：16 字节头与载荷布局的单元测试。

规格 §6.6 的校验清单每一条都必须有一个负例。任何一条漏了，
接收端就会在那条上被畸形包穿透。
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stream_packet
from stream_packet import (
    MAGIC, HEADER_SIZE, MAX_BLOCKLEN, MAX_TOTAL_PAYLOAD,
    PacketError, StreamPacket, pack_packet, unpack_packet, peek_magic,
)


def test_pack_roundtrip():
    print("[TEST] 包头 pack/unpack 往返...")
    data = bytes(range(64))
    raw = pack_packet(nonce=0xBEEF, seed=123456, K=1000, blocklen=64, data=data)
    assert len(raw) == HEADER_SIZE + 64, "整包长度必须是 16 + blocklen"
    assert raw[:2] == MAGIC == b'QF'
    pkt = unpack_packet(raw)
    assert pkt.nonce == 0xBEEF and pkt.seed == 123456
    assert pkt.K == 1000 and pkt.blocklen == 64
    assert pkt.data == data
    assert len(pkt.checksum) == 4
    print("  ✅ PASSED")


def test_header_field_layout():
    print("[TEST] 头字段布局与字节序写死...")
    raw = pack_packet(nonce=0x0102, seed=0x03040506, K=0x0708, blocklen=8,
                      data=b'\x00' * 8)
    magic, nonce, seed, K, blocklen = struct.unpack('>2sHIHH', raw[:12])
    assert magic == b'QF'
    assert (nonce, seed, K, blocklen) == (0x0102, 0x03040506, 0x0708, 8)
    # 逐字节钉死，防止有人把 '>' 改成 '<' 或 '='
    assert raw[:12] == b'QF\x01\x02\x03\x04\x05\x06\x07\x08\x00\x08', \
        "头前 12 字节必须是大端无填充，实得 %r" % (raw[:12],)
    print("  ✅ PASSED")


def test_checksum_covers_header_not_just_data():
    print("[TEST] checksum 覆盖头+data，不只是 data...")
    data = b'x' * 16
    raw = pack_packet(nonce=1, seed=2, K=3, blocklen=16, data=data)
    want = hashlib.sha256(raw[:12] + data).digest()[:4]
    assert raw[12:16] == want, "checksum 必须是 sha256(头前12字节 | data)[:4]"
    # 改动头里任一字段（不改 data）都必须导致解析失败，但失败原因分两类：
    # ① nonce/seed/K（pos 2..9）没有独立校验，只能靠 checksum 兜住——这正是
    #   本用例要验证的：若 checksum 错写成 sha256(data) ，这八个位置全会漏网。
    # ② blocklen（pos 10、11）有更早且更强的保护：改了它，len(raw) 与头声明的
    #   16+blocklen 必然不再自洽，先撞 'length'，永远走不到 checksum 比对。
    #   这不是缺陷，正是「分配前先查上限」的顺序要求。
    # 故逐位置给确定期望，不用 reason in (...) 这种弱断言：若将来长度自洽
    # 检查被删，pos 10/11 会改而落到 checksum，必须被当场抓出来。
    #
    # 注意：'length' 这个期望值依赖上面 blocklen=16 这个具体取值。
    # 若改大到接近 MAX_BLOCKLEN（实测 blocklen=4096 时），翻转 pos 10 会超出
    # MAX_BLOCKLEN 而先报 'blocklen'——改动 blocklen 取值时需重新核对这两个期望。
    BLOCKLEN_POS = (10, 11)
    for pos in range(2, 12):
        bad = bytearray(raw)
        bad[pos] ^= 0x01
        expect = 'length' if pos in BLOCKLEN_POS else 'checksum'
        try:
            unpack_packet(bytes(bad))
        except PacketError as e:
            assert e.reason == expect, \
                "改第 %d 字节应报 %s，实得 %s" % (pos, expect, e.reason)
            continue
        raise AssertionError("改头第 %d 字节后校验竟然通过了" % pos)
    print("  ✅ PASSED")


def test_checksum_detects_data_corruption():
    print("[TEST] data 被改必须校验失败...")
    raw = bytearray(pack_packet(nonce=1, seed=2, K=3, blocklen=16, data=b'y' * 16))
    raw[HEADER_SIZE] ^= 0x80
    try:
        unpack_packet(bytes(raw))
    except PacketError as e:
        assert e.reason == 'checksum'
        print("  ✅ PASSED")
        return
    raise AssertionError("data 被改后校验竟然通过了")


def test_non_protocol_bytes_return_none():
    print("[TEST] 非本协议的字节返回 None，不算损坏...")
    # 画面里可能同时有 v1 的 QR 码或别的东西，这些不该被计入"损坏包"
    for raw in (b'', b'Q', b'QR', b'QR' + b'\x00' * 20, b'hello world', b'\x00' * 32):
        assert unpack_packet(raw) is None, "非 QF 开头应返回 None：%r" % (raw[:8],)
    print("  ✅ PASSED")


def test_length_must_be_exactly_header_plus_blocklen():
    print("[TEST] len(raw) 必须严格等于 16 + blocklen...")
    good = pack_packet(nonce=1, seed=2, K=3, blocklen=16, data=b'z' * 16)
    # 尾随一个字节：不能被静默忽略
    try:
        unpack_packet(good + b'\x00')
    except PacketError as e:
        assert e.reason == 'length'
    else:
        raise AssertionError("尾随字节必须被拒绝，不能只取 raw[16:16+blocklen]")
    # 截短一个字节
    try:
        unpack_packet(good[:-1])
    except PacketError as e:
        assert e.reason == 'length'
    else:
        raise AssertionError("截短的包必须被拒绝")
    # 连头都不够长
    try:
        unpack_packet(MAGIC + b'\x00' * 5)
    except PacketError as e:
        assert e.reason == 'length'
    else:
        raise AssertionError("不足 16 字节的 QF 包必须被拒绝")
    print("  ✅ PASSED")


def test_rejects_out_of_range_header_before_allocating():
    print("[TEST] 极端 K/blocklen 在分配前就被拒...")
    # 手工构造一个 checksum 正确、但 blocklen 超过 MAX_BLOCKLEN 的包，
    # 且 data 只有 1 字节——接收端绝不能照着头字段去分配。
    head = struct.pack('>2sHIHH', MAGIC, 0, 0, 1, MAX_BLOCKLEN + 1)
    raw = head + hashlib.sha256(head + b'q').digest()[:4] + b'q'
    try:
        unpack_packet(raw)
    except PacketError as e:
        assert e.reason == 'blocklen', "应报 blocklen 超限，实得 %s" % e.reason
    else:
        raise AssertionError("blocklen > MAX_BLOCKLEN 必须被拒绝")

    # K x blocklen 超过总预算。
    # 当前常量下这条分支不可达，且是代数上必然的：
    #   K 字段上限 2^16-1，blocklen 已被更早的 MAX_BLOCKLEN=2^12 检查压住，
    #   乘积上限 (2^16-1)·2^12 = 2^28 - 2^12，比 MAX_TOTAL_PAYLOAD=2^28 小恰好一个
    #   MAX_BLOCKLEN。这不是缺陷：MAX_BLOCKLEN 是更紧的那道约束，内存防护目标
    #   已由它达成；total_payload 是它之上的冗余防线，只在将来调大
    #   MAX_BLOCKLEN（比如为了更大的 JAB 块）时才会活过来。
    # 先把这个不可达关系断言下来当护栏：
    assert 0xFFFF * MAX_BLOCKLEN <= MAX_TOTAL_PAYLOAD, \
        ("MAX_BLOCKLEN 已调大到让 total_payload 可达（%d x %d > %d），"
         "请把下面的临时放宽用例换成直接构造的真实包"
         % (0xFFFF, MAX_BLOCKLEN, MAX_TOTAL_PAYLOAD))

    # 但不能因为「当前不可达」就不验证——未跑过的防御代码等于没有。
    # 临时放宽 MAX_BLOCKLEN，让 total_payload 成为第一道挡住它的检查。
    saved_max_blocklen = stream_packet.MAX_BLOCKLEN
    stream_packet.MAX_BLOCKLEN = 0xFFFF
    try:
        # 0xFFFF x 0xFFFF = 4,294,836,225 远超 2^28
        head = struct.pack('>2sHIHH', MAGIC, 0, 0, 0xFFFF, 0xFFFF)
        try:
            unpack_packet(head + b'\x00' * 4 + b'\x00' * 8)
        except PacketError as e:
            assert e.reason == 'total_payload', \
                "应报 total_payload 超限，实得 %s" % e.reason
        else:
            raise AssertionError("K x blocklen > MAX_TOTAL_PAYLOAD 必须被拒绝")
    finally:
        stream_packet.MAX_BLOCKLEN = saved_max_blocklen

    # K = 0 / blocklen = 0
    for K, blocklen in ((0, 8), (8, 0)):
        head = struct.pack('>2sHIHH', MAGIC, 0, 0, K, blocklen)
        raw = head + hashlib.sha256(head + b'').digest()[:4]
        try:
            unpack_packet(raw)
        except PacketError as e:
            assert e.reason in ('K', 'blocklen'), \
                "K=%d blocklen=%d 应被拒，实得 %s" % (K, blocklen, e.reason)
        else:
            raise AssertionError("K=%d blocklen=%d 必须被拒绝" % (K, blocklen))
    print("  ✅ PASSED")


def test_pack_validates_inputs():
    print("[TEST] pack_packet 自己也校验入参...")
    bad_cases = [
        dict(nonce=0x10000, seed=0, K=1, blocklen=4, data=b'aaaa'),   # nonce 溢出
        dict(nonce=0, seed=1 << 32, K=1, blocklen=4, data=b'aaaa'),   # seed 溢出
        dict(nonce=0, seed=0, K=65536, blocklen=4, data=b'aaaa'),     # K 溢出
        dict(nonce=0, seed=0, K=1, blocklen=4, data=b'aaa'),          # data 长度不符
        dict(nonce=0, seed=0, K=0, blocklen=4, data=b'aaaa'),         # K=0
    ]
    for kw in bad_cases:
        try:
            pack_packet(**kw)
        except (ValueError, struct.error):
            continue
        raise AssertionError("pack_packet 应拒绝 %r" % (kw,))
    print("  ✅ PASSED")


def test_peek_magic():
    print("[TEST] peek_magic 区分 v3 / v1v2 / 其他...")
    assert peek_magic(pack_packet(1, 2, 3, 8, b'a' * 8)) == 'QF'
    assert peek_magic(b'QR' + b'\x00' * 10) == 'QR'   # v1/v2 的分块包
    assert peek_magic(b'hello') is None
    assert peek_magic(b'') is None
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_pack_roundtrip()
    test_header_field_layout()
    test_checksum_covers_header_not_just_data()
    test_checksum_detects_data_corruption()
    test_non_protocol_bytes_return_none()
    test_length_must_be_exactly_header_plus_blocklen()
    test_rejects_out_of_range_header_before_allocating()
    test_pack_validates_inputs()
    test_peek_magic()
    print("\n✅ All stream packet tests passed!")
