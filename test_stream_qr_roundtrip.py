#!/usr/bin/env python3
"""SymbolEncoder 契约 + 真实 QR 渲染的端到端往返。

与 test_fountain.py（纯字节）分开：这个文件要真的渲染 PNG 再读回来。
用 zxing backend（纯 wheel，无原生库），因此不需要 DYLD_LIBRARY_PATH。
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image

from symbol_encoder import (
    QR_V40_BYTE_CAPACITY, QRSymbolEncoder, SymbolEncoder, max_raw_for_base85,
)


def test_max_raw_for_base85_matches_real_encoder():
    print("[TEST] base85 容量公式与真实 b85encode 一致...")
    # 这个数只能按 Python 的实际行为算，不能手算 ceil(n/4)*5：
    # b85encode 对尾部不足 4 字节的 r 字节只输出 r+1 个字符，不是补齐成 5 个。
    for capacity in QR_V40_BYTE_CAPACITY.values():
        n = max_raw_for_base85(capacity)
        assert len(base64.b85encode(b'\x00' * n)) <= capacity, \
            "capacity=%d 时 n=%d 竟然超了" % (capacity, n)
        assert len(base64.b85encode(b'\x00' * (n + 1))) > capacity, \
            "capacity=%d 时 n=%d 不是最大值" % (capacity, n)
    # 规格里的两个参考值
    assert max_raw_for_base85(2953) == 2362, "v40-L 应为 2362（不是 2346 也不是 2360）"
    assert max_raw_for_base85(2331) == 1864, "v40-M 应为 1864（对应 chunk <= 1848）"
    print("  ✅ PASSED")


def test_qr_symbol_encoder_contract():
    print("[TEST] QRSymbolEncoder 满足 SymbolEncoder 契约...")
    enc = QRSymbolEncoder()
    assert isinstance(enc, SymbolEncoder)
    assert enc.name == 'qr'
    assert enc.payload_encoding == 'base85', \
        "两端必须一致：接收端 QRDecoderAdapter.payload_encoding 也是 base85"
    assert enc.ecc == 'M', "默认 ECC 必须是 M，与 v1/v2 行为一致"
    assert enc.max_payload_bytes == 1864
    assert QRSymbolEncoder(ecc='L').max_payload_bytes == 2362
    assert QRSymbolEncoder(ecc='H').max_payload_bytes == max_raw_for_base85(1273)
    for bad in ('m', 'X', '', None, 1):
        try:
            QRSymbolEncoder(ecc=bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError("应拒绝 ecc=%r" % (bad,))
    print("  ✅ PASSED")


def test_check_capacity_fails_early():
    print("[TEST] check_capacity 在启动时就拒绝过大的 blocklen...")
    enc = QRSymbolEncoder()                     # max_payload_bytes = 1864
    enc.check_capacity(1848)                    # 1848 + 16 = 1864，恰好触顶
    for bad in (1849, 2000, 4096):
        try:
            enc.check_capacity(bad)
        except ValueError as e:
            assert 'blocklen' in str(e) or str(bad) in str(e)
            continue
        raise AssertionError("blocklen=%d 应被拒绝（16 字节头装不下）" % bad)
    print("  ✅ PASSED")


def test_encode_returns_real_pil_image():
    print("[TEST] encode() 返回真 PIL.Image，不是 qrcode 的 wrapper...")
    enc = QRSymbolEncoder()
    img = enc.encode(b'hello' * 20)
    assert isinstance(img, Image.Image), \
        "必须是 PIL.Image.Image（tkinter 的 ImageTk 和 backend 都要它），实得 %r" % type(img)
    assert img.size[0] > 0 and img.size[1] > 0
    # 能被 PIL 正常保存和重开
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    assert Image.open(buf).size == img.size
    print("  ✅ PASSED")


def test_encode_does_not_double_base85():
    print("[TEST] encode() 收原始字节，内部只做一次 base85...")
    import zxingcpp
    enc = QRSymbolEncoder()
    payload = bytes(range(256)) * 2                 # 512 字节，含不可打印字节
    img = enc.encode(payload)
    found = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    assert len(found) == 1, "应恰好读出 1 个 QR，实得 %d" % len(found)
    b85 = bytes(found[0].bytes) if found[0].bytes else found[0].text.encode('ascii')
    assert base64.b85decode(b85) == payload, \
        "一次 base85 解码就应还原原始字节；若是双重 base85，这里会得到另一串 ASCII"
    print("  ✅ PASSED")


def test_ecc_levels_change_output():
    print("[TEST] ECC 等级真的传到了 qrcode...")
    payload = b'z' * 900
    sizes = {}
    for ecc in ('L', 'M', 'Q', 'H'):
        img = QRSymbolEncoder(ecc=ecc).encode(payload)
        sizes[ecc] = img.size[0]
    assert sizes['L'] <= sizes['M'] <= sizes['Q'] <= sizes['H'], \
        "同样载荷下 ECC 越高码越大，实得 %r" % (sizes,)
    assert len(set(sizes.values())) > 1, "四档 ECC 产出完全相同，说明参数没传下去"
    print("  ✅ PASSED")


def test_encoder_chunk_to_qr_image_default_unchanged():
    print("[TEST] chunk_to_qr_image 加了 ecc 参数但默认行为不变...")
    import encoder as v12_encoder
    payload = b'compat check' * 30
    a = v12_encoder.chunk_to_qr_image(payload)
    b = v12_encoder.chunk_to_qr_image(payload, ecc='M')
    ia = a.get_image() if hasattr(a, 'get_image') else a
    ib = b.get_image() if hasattr(b, 'get_image') else b
    assert ia.convert('L').tobytes() == ib.convert('L').tobytes(), \
        "ecc 默认值必须等价于 ERROR_CORRECT_M，否则 v1/v2 的 HTML 会变"
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_max_raw_for_base85_matches_real_encoder()
    test_qr_symbol_encoder_contract()
    test_check_capacity_fails_early()
    test_encode_returns_real_pil_image()
    test_encode_does_not_double_base85()
    test_ecc_levels_change_output()
    test_encoder_chunk_to_qr_image_default_unchanged()
    print("\n✅ All symbol encoder tests passed!")
