#!/usr/bin/env python3
"""端到端往返，跳过图像层：字节 → 包 → StreamSession → 还原文件。

图像渲染由 test_stream_qr_roundtrip.py 覆盖；这里专测信道退化下的协议行为，
所以能跑几千个包而不花几分钟。
"""
import hashlib
import os
import random
import tempfile
from pathlib import Path


from PIL import Image

from queqiao.fountain import FountainEncoder
from queqiao.stream_decoder import ReceiveResult, decode_frame, receive_stream, write_output
from queqiao.stream_packet import build_payload, pack_packet, split_blocks
from queqiao.stream_session import StreamSession

BLOCKLEN = 200


def make_packets(name, data, count, blocklen=BLOCKLEN, M=None):
    payload, _meta_json, nonce = build_payload(name, data)
    blocks = split_blocks(payload, blocklen)
    enc = FountainEncoder(blocks, M=M)
    out = []
    for _ in range(count):
        seed, chunk = enc.next_packet()
        out.append(pack_packet(nonce, seed, len(blocks), blocklen, chunk))
    return out, len(blocks)


class ScriptedBackend:
    """把预排好的包序列按帧吐出来。payload_encoding='raw' 直通。"""

    name = 'scripted'
    payload_encoding = 'raw'

    def __init__(self, frames):
        self.frames = list(frames)      # list[list[bytes]]
        self.calls = 0

    def decode_image(self, source):
        idx = self.calls
        self.calls += 1
        if idx >= len(self.frames):
            return []
        return [_R(p) for p in self.frames[idx]]


class Base85Backend(ScriptedBackend):
    payload_encoding = 'base85'

    def decode_image(self, source):
        import base64
        return [_R(base64.b85encode(r.data)) for r in
                ScriptedBackend.decode_image(self, source)]


class _R:
    def __init__(self, data):
        self.data = data


class ScriptedSource:
    describe = 'scripted'

    def __init__(self, n):
        self.n = n

    def __iter__(self):
        for _ in range(self.n):
            yield Image.new('RGB', (4, 4), (255, 255, 255))


def test_clean_roundtrip():
    print("[TEST] 无丢包：K 个系统性包就该收齐...")
    data = os.urandom(3000)
    packets, K = make_packets('demo.bin', data, 400)
    session = StreamSession()
    src = ScriptedSource(len(packets))
    backend = ScriptedBackend([[p] for p in packets])
    result = receive_stream(src, session, backend)
    assert isinstance(result, ReceiveResult)
    assert result.file_bytes == data, "还原字节必须与原文完全一致"
    assert result.meta['filename'] == 'demo.bin'
    assert session.stats.valid >= K
    print("  ✅ PASSED (K=%d, 用了 %d 帧)" % (K, session.stats.frames))


def test_base85_backend_path():
    print("[TEST] payload_encoding='base85' 的后端要先 b85decode...")
    data = os.urandom(1500)
    packets, _K = make_packets('b85.bin', data, 300)
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(packets)), session,
                            Base85Backend([[p] for p in packets]))
    assert result.file_bytes == data
    print("  ✅ PASSED")


def test_30_percent_loss_and_shuffle():
    print("[TEST] 30% 丢帧 + 乱序：喷泉码应仍能收齐...")
    rng = random.Random(20260905)
    data = os.urandom(6000)
    packets, K = make_packets('lossy.bin', data, K_BUDGET := 400)
    kept = [p for p in packets if rng.random() > 0.30]
    rng.shuffle(kept)
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(kept)), session,
                            ScriptedBackend([[p] for p in kept]))
    assert result.file_bytes == data
    print("  ✅ PASSED (K=%d, 收到 %d/%d 包)" % (K, len(kept), K_BUDGET))


def test_frozen_frame_does_not_stall_forever():
    print("[TEST] 静止残留画面：同一包重复 200 帧后，新包仍能推进...")
    data = os.urandom(4000)
    packets, _K = make_packets('frozen.bin', data, 400)
    frames = [[packets[0]]] * 200 + [[p] for p in packets]
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(frames)), session,
                            ScriptedBackend(frames))
    assert result.file_bytes == data
    assert session.stats.duplicate >= 199, "重复包必须被计数而不是当新包吃掉"
    print("  ✅ PASSED (duplicate=%d)" % session.stats.duplicate)


def test_two_sessions_interleaved_switches():
    print("[TEST] 两个会话交错：应锁到其中之一并完成，不混料...")
    a_data = os.urandom(2500)
    b_data = os.urandom(2500)
    a_packets, _ = make_packets('a.bin', a_data, 400)
    b_packets, _ = make_packets('b.bin', b_data, 400)
    # 前 40 帧是 B 的残留，之后全是 A
    frames = [[p] for p in b_packets[:40]] + [[p] for p in a_packets]
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(frames)), session,
                            ScriptedBackend(frames))
    assert result.file_bytes in (a_data, b_data), "必须完整还原其中一个会话"
    if result.meta['filename'] == 'a.bin':
        assert result.file_bytes == a_data
    else:
        assert result.file_bytes == b_data
    print("  ✅ PASSED (锁到 %s, switches=%d)"
          % (result.meta['filename'], session.stats.switches))


def test_foreign_codes_are_ignored():
    print("[TEST] 画面里混进 v1 的 QR 码：计入 foreign，不干扰解码...")
    data = os.urandom(2000)
    packets, _K = make_packets('mixed.bin', data, 400)
    junk = b'QR' + b'\x00' * 20
    frames = [[junk, p] for p in packets]
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(frames)), session,
                            ScriptedBackend(frames))
    assert result.file_bytes == data
    # receive_stream 一旦 is_complete 就提前 return，不会耗尽全部 400 帧——
    # K=12（2000 字节 payload / blocklen=200）通常 17-20 帧就收齐，远够不到
    # 固定阈值 100。每帧恰好携带 1 个 junk 码，所以 foreign 必然等于处理过的帧数。
    assert session.stats.foreign > 0 and session.stats.foreign == session.stats.frames, \
        "v1 的 QR magic 必须被认作 foreign，且每帧恰好 1 个"
    print("  ✅ PASSED (foreign=%d)" % session.stats.foreign)


def test_small_file_k_equals_one():
    print("[TEST] K=1 小文件（P1 死锁回归）：3 个同 seed 包即 lock...")
    data = b'tiny config patch\n' * 3
    packets, K = make_packets('tiny.txt', data, 20, blocklen=4096)
    assert K == 1, "本用例需要 K=1，实得 %d" % K
    session = StreamSession()
    result = receive_stream(ScriptedSource(len(packets)), session,
                            ScriptedBackend([[p] for p in packets]))
    assert result.file_bytes == data
    print("  ✅ PASSED")


def test_write_output_uses_basename_only():
    print("[TEST] §9：文件名只取 basename，路径成分一律丢弃...")
    with tempfile.TemporaryDirectory() as td:
        meta = {'filename': '../../../etc/passwd', 'size': 3,
                'sha256': hashlib.sha256(b'abc').hexdigest()}
        p = write_output(meta, b'abc', out_dir=td)
        assert p.parent == Path(td), "写出位置必须落在 out_dir 内，实得 %s" % p
        assert '..' not in p.name
        assert p.read_bytes() == b'abc'
    print("  ✅ PASSED")


def test_write_output_respects_explicit_path():
    print("[TEST] 显式 -o 优先于 meta 里的文件名...")
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / 'chosen.bin'
        meta = {'filename': 'ignored.bin', 'size': 3,
                'sha256': hashlib.sha256(b'xyz').hexdigest()}
        p = write_output(meta, b'xyz', out_path=target)
        assert p == target and p.read_bytes() == b'xyz'
    print("  ✅ PASSED")


def test_decode_frame_tolerates_backend_errors():
    print("[TEST] 后端对某帧抛异常时跳过该帧，不中断整个接收...")
    class Angry:
        name = 'angry'
        payload_encoding = 'raw'

        def decode_image(self, source):
            raise ValueError("corrupt frame")

    out = decode_frame(Image.new('RGB', (4, 4)), Angry())
    assert not out, "解码异常应吞掉并返回空列表"
    print("  ✅ PASSED")


def test_decode_frame_rejects_bad_base85():
    print("[TEST] base85 后端扫到非 base85 文本 → 丢弃，不抛...")
    class Junk:
        name = 'junk'
        payload_encoding = 'base85'

        def decode_image(self, source):
            return [_R(b'not~~~valid~~~b85!!!\x00\xff')]

    out = decode_frame(Image.new('RGB', (4, 4)), Junk())
    assert not out, "b85decode 失败的负载应被静默丢弃（它多半是别人的码）"
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_clean_roundtrip()
    test_base85_backend_path()
    test_30_percent_loss_and_shuffle()
    test_frozen_frame_does_not_stall_forever()
    test_two_sessions_interleaved_switches()
    test_foreign_codes_are_ignored()
    test_small_file_k_equals_one()
    test_write_output_uses_basename_only()
    test_write_output_respects_explicit_path()
    test_decode_frame_tolerates_backend_errors()
    test_decode_frame_rejects_bad_base85()
    print("\n✅ All stream roundtrip tests passed!")
