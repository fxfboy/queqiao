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
        "必须是 PIL.Image.Image（player 要 .save(format='PNG')，backend 要它），实得 %r" % type(img)
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


def test_all_zero_payload_survives_qrcode():
    print("[TEST] 全零载荷不触发 qrcode 的 glog(0)...")
    import zxingcpp
    # qrcode 8.2 的 numeric 模式把 '000' 编成 10 bit 全零，长 '0' 串会让某个 RS
    # 块的数据码字整块为零，库内部 glog(0) 崩溃。而 base85 把 4 个 \x00 编成
    # '00000'——流式分块用 \x00 补齐末块，"文件远小于 blocklen"这一最常见场景
    # 必然产生长 '0' 串。encoder 显式指定 byte 模式绕开整条 numeric 路径。
    # 没有这个测试，该缺陷只有在真机播放一个小文件时才会炸。
    enc = QRSymbolEncoder()
    for n in (200, 509, 800, 1848):
        payload = b'\x00' * n
        img = enc.encode(payload)              # 修复前这里抛 ValueError: glog(0)
        found = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
        assert len(found) == 1, "全零载荷 %d 字节应读出 1 个 QR，实得 %d" % (n, len(found))
        b85 = bytes(found[0].bytes) if found[0].bytes else found[0].text.encode('ascii')
        assert base64.b85decode(b85) == payload, "全零载荷 %d 字节未能原样还原" % n
    print("  ✅ PASSED")


def test_all_zero_payload_at_capacity_ceiling():
    print("[TEST] 满包全零仍在 v40 容量内（byte 模式最坏情况）...")
    # max_raw_for_base85(2331) == 1864，即 v40-M 的极限。全零是 base85 输出最长的
    # 情形之一，1864 字节恰好编成 2330 个字符，只剩 1 字符余量——容量公式是按
    # byte 模式算的，强制 byte 模式后模型与实际编码行为才真正自洽。
    enc = QRSymbolEncoder()                     # max_payload_bytes = 1864
    payload = b'\x00' * enc.max_payload_bytes
    assert len(base64.b85encode(payload)) <= QR_V40_BYTE_CAPACITY['M'], \
        "全零满包的 base85 输出超出 v40-M 容量，容量公式与实际编码模式不一致"
    enc.encode(payload)                         # 装不下会抛 DataOverflowError
    print("  ✅ PASSED")


def test_forcing_byte_mode_leaves_v1v2_images_identical():
    print("[TEST] 强制 byte 模式不改变 v1/v2 的正常载荷图像...")
    import os
    import qrcode as _qrcode
    import encoder as v12_encoder
    # base85 字符集含大小写字母和符号，随机载荷里出现 20 个连续数字的概率约 1e-18，
    # 所以正常载荷本来就走 byte 模式，显式指定后位流应当逐位相同。这条断言是
    # "修复不破坏 v1/v2 已生成 HTML"的依据；若哪天它红了，说明 v1/v2 的产物变了。
    for n in (400, 800, 1200):
        payload = os.urandom(n)
        b85 = base64.b85encode(payload).decode('ascii')
        auto = _qrcode.QRCode(version=None, box_size=5, border=2,
                              error_correction=_qrcode.constants.ERROR_CORRECT_M)
        auto.add_data(b85)                      # 库自动选模式（修复前的行为）
        auto.make(fit=True)
        auto_img = auto.make_image(fill_color="black", back_color="white")
        forced = v12_encoder.chunk_to_qr_image(payload)
        ia = (auto_img.get_image() if hasattr(auto_img, 'get_image') else auto_img).convert('L')
        ib = (forced.get_image() if hasattr(forced, 'get_image') else forced).convert('L')
        assert ia.tobytes() == ib.tobytes(), \
            "%d 字节随机载荷：显式 byte 模式改变了图像，v1/v2 的 HTML 会变" % n
    print("  ✅ PASSED")


def test_backend_accepts_path_and_pil_image():
    print("[TEST] backend 的 decode_image 同时接受路径和 PIL Image...")
    import tempfile
    from qr_backends import get_backend

    payload = b'polymorphic source test' * 20
    img = QRSymbolEncoder().encode(payload)
    backend = get_backend('zxing')

    from_image = backend.decode_image(img)
    assert len(from_image) == 1, "传 PIL Image 应读出 1 个码"
    assert base64.b85decode(from_image[0].data) == payload

    with tempfile.TemporaryDirectory(prefix='queqiao-poly-') as d:
        path = os.path.join(d, 'q.png')
        img.save(path, format='PNG')
        from_path = backend.decode_image(path)
        assert len(from_path) == 1, "传路径应读出 1 个码"
        assert from_path[0].data == from_image[0].data, "两种入参必须得到相同结果"
    print("  ✅ PASSED")


def test_backend_does_not_close_borrowed_image():
    print("[TEST] 借用的 PIL Image 不能被 backend 关掉...")
    from qr_backends import get_backend

    img = QRSymbolEncoder().encode(b'do not close me' * 20)
    backend = get_backend('zxing')
    for _ in range(3):
        backend.decode_image(img)
        # 若 backend 关掉了借用的对象，下一次访问会抛
        # "Attempt to use a closed image" / ValueError
        assert img.size[0] > 0, "backend 关掉了调用方的 Image —— 流式下一帧就废了"
        img.convert('L')
    print("  ✅ PASSED")


def test_backend_closes_its_own_file_handles():
    print("[TEST] 自己 open 的图像在异常路径上也要关...")
    import gc
    import tempfile
    import warnings
    from qr_backends import get_backend

    backend = get_backend('zxing')
    with tempfile.TemporaryDirectory(prefix='queqiao-close-') as d:
        path = os.path.join(d, 'q.png')
        QRSymbolEncoder().encode(b'close me' * 20).save(path, format='PNG')
        with warnings.catch_warnings():
            warnings.simplefilter('error', ResourceWarning)
            for _ in range(20):
                backend.decode_image(path)
            gc.collect()          # 未关闭的文件句柄会在此处触发 ResourceWarning
    print("  ✅ PASSED")


def test_backend_bad_source_still_raises():
    print("[TEST] 坏输入仍抛 ValueError（v1/v2 行为不变）...")
    from qr_backends import get_backend
    backend = get_backend('zxing')
    for bad in ('/nonexistent/path/nope.png', 12345, None):
        try:
            backend.decode_image(bad)
        except ValueError:
            continue
        raise AssertionError("坏输入 %r 应抛 ValueError" % (bad,))
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_max_raw_for_base85_matches_real_encoder()
    test_qr_symbol_encoder_contract()
    test_check_capacity_fails_early()
    test_encode_returns_real_pil_image()
    test_encode_does_not_double_base85()
    test_ecc_levels_change_output()
    test_encoder_chunk_to_qr_image_default_unchanged()
    test_all_zero_payload_survives_qrcode()
    test_all_zero_payload_at_capacity_ceiling()
    test_forcing_byte_mode_leaves_v1v2_images_identical()
    test_backend_accepts_path_and_pil_image()
    test_backend_does_not_close_borrowed_image()
    test_backend_closes_its_own_file_handles()
    test_backend_bad_source_still_raises()
    print("\n✅ All symbol encoder tests passed!")
