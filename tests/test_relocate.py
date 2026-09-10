#!/usr/bin/env python3
"""自动重定位的单元测试（纯离线，无需真实屏幕/摄像头）。

场景：接收端圈选区域与喷泉码实际位置错位（多屏窗口挪动后必现），
帧循环应在连续 NO_CODE_ALERT 帧无码后触发 probe_full_monitors 扫描，
自动把抓取区域重定位到码的包围盒（含边距）并持久化，随后正常收齐文件。

mss 抓屏与显示器枚举被测试替身替换；数据包用真实协议构造
（build_payload → split_blocks → pack_packet），走完整的
StreamSession/FountainDecoder/parse_payload 校验链路。

运行: uv run python tests/test_relocate.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from queqiao import stream_decoder
from queqiao.frame_source import RELOCATE_MARGIN, ScreenSource
from queqiao.qr_backends.base import QRDecodeResult
from queqiao.stream_packet import build_payload, pack_packet, split_blocks
from queqiao.stream_session import StreamSession


class VisibleFake:
    """is_black_frame 眼里的"非黑帧"：灰度极值 (0,255)，永远不触发黑帧告警。"""

    covers_point = None

    def convert(self, mode):
        return self

    def getextrema(self):
        return (0, 255)

    def close(self):
        pass


class FakeRegionImage(VisibleFake):
    """（错位前的）圈选区域帧：码不在里面，decode_image 返回空。"""


class FakeFullImage(VisibleFake):
    """probe 扫描到的全屏帧：码在其中，位置 (3000,400)。"""

    covers_point = (3000, 400)


class FakeQRBackend:
    """码心落在 frame.covers_point 才吐包——模拟"区域对上才有码"。"""

    name = 'fake'
    payload_encoding = 'raw'          # 跳过 base85，直接喂协议包字节

    def __init__(self, packet, cx, cy, size=200):
        self.packet = packet
        self.cx, self.cy, self.size = cx, cy, size

    def decode_image(self, image):
        if image.covers_point != (self.cx, self.cy):
            return []
        return [QRDecodeResult(self.packet, self.cx, self.cy,
                               self.size, self.size)]


class FakeSource:
    """模拟 ScreenSource：retarget() 之后抓到的帧才看得见码。"""

    def __init__(self, bbox):
        self.bbox = bbox
        self.retargets = []

    @property
    def describe(self):
        return 'fake %r' % (self.bbox,)

    def retarget(self, bbox):
        self.retargets.append(tuple(bbox))
        self.bbox = bbox

    def __iter__(self):
        while True:
            yield FakeFullImage() if self.retargets else FakeRegionImage()


def fake_probe_full_monitors(backend, margin=RELOCATE_MARGIN):
    """替身：假装在第 3 屏 (1920,0) 的 (3000,400) 处找到 200×200 的码。"""
    assert backend.name == 'fake'
    return ((3000 - margin, 400 - margin, 200 + 2 * margin, 200 + 2 * margin), 1)


def test_autolocate_region():
    print("[TEST] autolocate_region: 扫描命中 → 保存并返回；未命中 → None 且不写 region...")
    from queqiao import frame_source
    real_probe = frame_source.probe_full_monitors
    real_save = frame_source.save_region
    saved = []
    frame_source.probe_full_monitors = \
        lambda backend, margin=RELOCATE_MARGIN: ((10, 20, 300, 300), 2)
    frame_source.save_region = lambda bbox: saved.append(tuple(bbox))
    try:
        bbox = frame_source.autolocate_region(FakeQRBackend(b'', 0, 0))
        assert bbox == (10, 20, 300, 300), bbox
        assert saved == [(10, 20, 300, 300)], saved

        frame_source.probe_full_monitors = \
            lambda backend, margin=RELOCATE_MARGIN: None
        assert frame_source.autolocate_region(FakeQRBackend(b'', 0, 0)) is None
        assert len(saved) == 1, "未命中时绝不能覆盖已存的区域"
    finally:
        frame_source.probe_full_monitors = real_probe
        frame_source.save_region = real_save
    print("  ✅ PASSED")


def main():
    test_autolocate_region()

    # 用真实协议构造 K=1 的会话：小 payload + blocklen 恰好一 blocks
    file_bytes = b'queqiao relocate test payload' * 10
    payload, _meta_json, nonce = build_payload('relocate-test.txt', file_bytes)
    blocklen = len(payload)
    assert blocklen <= 0xFFFF, '测试数据必须塞进一个 block'
    K = 1
    (block,) = split_blocks(payload, blocklen)
    packet = pack_packet(nonce, 0, K, blocklen, block)

    with tempfile.TemporaryDirectory():
        # 把 stream_decoder 里的全屏扫描与区域持久化换成测试替身
        real_probe = stream_decoder.probe_full_monitors
        real_save = stream_decoder.save_region
        saved = []
        stream_decoder.probe_full_monitors = fake_probe_full_monitors
        stream_decoder.save_region = lambda bbox: saved.append(tuple(bbox))

        try:
            # 圈选区域 (100,100,300,300) —— 码实际在 (3000,400)，完全错位
            source = FakeSource((100, 100, 300, 300))
            session = StreamSession()
            backend = FakeQRBackend(packet, cx=3000, cy=400)
            result = stream_decoder.receive_stream(
                source, session, backend, on_event=lambda *_: None)
        finally:
            stream_decoder.probe_full_monitors = real_probe
            stream_decoder.save_region = real_save

        # 1) 触发过且仅触发过一次重定位（成功后帧帧有码，冷却期无关紧要）
        assert len(source.retargets) == 1, source.retargets
        # 2) 重定位 bbox = 码盒外扩 margin
        expect = (3000 - RELOCATE_MARGIN, 400 - RELOCATE_MARGIN,
                  200 + 2 * RELOCATE_MARGIN, 200 + 2 * RELOCATE_MARGIN)
        assert source.retargets[0] == expect, source.retargets[0]
        # 3) 新区域已持久化（下次 receive 直接复用正确区域）
        assert saved and saved[0] == expect, saved
        # 4) 重定位后码可读 → 会话收齐，文件逐字节一致
        assert result.file_bytes == file_bytes
        assert result.meta.get('filename') == 'relocate-test.txt'

        # 5) ScreenSource.retarget 的契约：bbox/describe 立即生效
        ss = ScreenSource((1, 2, 3, 4))
        assert '3×4 @ (1,2)' in ss.describe
        ss.retarget((5, 6, 7, 8))
        assert ss.bbox == (5, 6, 7, 8)
        assert '7×8 @ (5,6)' in ss.describe
        assert ss.actual_size is None and ss.frames_grabbed == 0

    print('✅ test_relocate: 全部断言通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
