#!/usr/bin/env python3
"""RDP 退化的离线基线：真实渲染各档 QR，施加下采样 + 模糊 + JPEG，量单帧解出率。

这不是"跑一次就有结论"的验收门——真实 RDP 的退化比这复杂。它的作用是：
参数改动（ECC、blocklen、box_size、quiet zone）之后，有一条可复现的曲线
能看出是变好还是变坏，而不用每次都去连 RDP。
"""
import io


from PIL import Image, ImageFilter

from queqiao.fountain import FountainEncoder
from queqiao.qr_backends import get_backend
from queqiao.stream_decoder import decode_frame
from queqiao.stream_packet import build_payload, pack_packet, split_blocks
from queqiao.stream_profile import CALIBRATION_MATRIX, synthetic_payload
from queqiao.symbol_encoder import QRSymbolEncoder

PACKETS_PER_STAGE = 12          # 够看出趋势，又不至于跑几分钟


def degrade(image, scale=1.0, blur=0.0, jpeg_quality=None):
    out = image
    if scale != 1.0:
        w, h = out.size
        out = out.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.Resampling.BILINEAR)
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    if jpeg_quality is not None:
        buf = io.BytesIO()
        out.convert('RGB').save(buf, format='JPEG', quality=jpeg_quality)
        buf.seek(0)
        out = Image.open(buf)
        out.load()
    return out


def stage_packets(stage, n):
    payload, _mj, nonce = build_payload('calibration.bin', synthetic_payload())
    blocks = split_blocks(payload, stage['blocklen'])
    enc = FountainEncoder(blocks, M=None)
    out = []
    for _ in range(n):
        seed, data = enc.next_packet()
        out.append(pack_packet(nonce, seed, len(blocks), stage['blocklen'], data))
    return out


def measure(stage, backend, **degradation):
    symbol = QRSymbolEncoder(ecc=stage['ecc'], box_size=stage['box_size'], border=4)
    ok = 0
    packets = stage_packets(stage, PACKETS_PER_STAGE)
    for raw in packets:
        image = symbol.encode(raw)
        try:
            shot = degrade(image, **degradation)
            try:
                got = decode_frame(shot, backend)
            finally:
                if shot is not image:
                    shot.close()
        finally:
            image.close()
        if raw in got:
            ok += 1
    return ok / len(packets)


def test_pristine_is_perfect():
    print("[TEST] 无退化：每一档都必须 100%%（否则渲染或后端本身就坏了）...")
    backend = get_backend('zxing')
    for stage in CALIBRATION_MATRIX:
        rate = measure(stage, backend)
        print("   blocklen=%-5d ecc=%s box=%d → %6.1f%%"
              % (stage['blocklen'], stage['ecc'], stage['box_size'], rate * 100))
        assert rate == 1.0, (
            "无退化的渲染→解码必须 100%%，blocklen=%d 只有 %.1f%%。"
            "先查 QRSymbolEncoder 或 zxing 后端，别急着调标定参数。"
            % (stage['blocklen'], rate * 100))
    print("  ✅ PASSED")


def test_mild_degradation_keeps_low_density_stages():
    print("[TEST] 轻度退化（80%% 缩放 + JPEG 85）：低密度档应仍 ≥ 90%%...")
    backend = get_backend('zxing')
    for stage in CALIBRATION_MATRIX[:2]:      # 400 / 800 两档
        rate = measure(stage, backend, scale=0.8, jpeg_quality=85)
        print("   blocklen=%-5d → %6.1f%%" % (stage['blocklen'], rate * 100))
        assert rate >= 0.90, (
            "blocklen=%d 在轻度退化下只有 %.1f%%，低于 90%% 基线。"
            % (stage['blocklen'], rate * 100))
    print("  ✅ PASSED")


def test_heavy_degradation_reports_without_asserting():
    print("[TEST] 重度退化曲线（只报告，不设门）...")
    backend = get_backend('zxing')
    print("   %-10s %-8s %-8s %-8s" % ('blocklen', '60%+blur', '40%+blur', '30%+blur'))
    for stage in CALIBRATION_MATRIX:
        rates = [measure(stage, backend, scale=s, blur=1.0, jpeg_quality=75)
                 for s in (0.6, 0.4, 0.3)]
        print("   %-10d %-8.1f %-8.1f %-8.1f"
              % (stage['blocklen'], rates[0] * 100, rates[1] * 100, rates[2] * 100))
    print("  ✅ PASSED (基线已记录)")


if __name__ == '__main__':
    test_pristine_is_perfect()
    test_mild_degradation_keeps_low_density_stages()
    test_heavy_degradation_reports_without_asserting()
    print("\n✅ All degradation baseline tests passed!")
