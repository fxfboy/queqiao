"""zxing-cpp 后端（C++ port of ZXing 的 Python wheel）。

纯 wheel，无系统库依赖。pixel-perfect QR 上比 pyzbar 快约 10×；
返回的 barcode.bytes 是原始 payload bytes，无需 utf-8 中转。
"""
import hashlib

from PIL import Image

from .base import QRDecoderAdapter, QRDecodeResult


class ZxingQRDecoder(QRDecoderAdapter):
    name = 'zxing'

    def __init__(self):
        try:
            import zxingcpp
        except ImportError as e:
            raise RuntimeError(
                "zxing backend is unavailable. Install Python package zxing-cpp: "
                "pip install zxing-cpp (pure wheel, no system library required)."
            ) from e

        self.zxingcpp = zxingcpp
        self.qrcode_format = zxingcpp.BarcodeFormat.QRCode

    def decode_image(self, image_path):
        try:
            img = Image.open(image_path)
        except Exception as e:
            raise ValueError(f"Cannot read image: {image_path}") from e

        results = []
        for mode in [None, 'L']:
            try:
                test_img = img if mode is None else img.convert(mode)
                results.extend(
                    self.zxingcpp.read_barcodes(test_img, formats=self.qrcode_format)
                )
            except Exception:
                pass

        seen = set()
        unique = []
        for item in results:
            data = bytes(item.bytes) if item.bytes else item.text.encode('utf-8')
            data_hash = hashlib.md5(data).hexdigest()
            if data_hash in seen:
                continue
            seen.add(data_hash)

            x, y, w, h = 0, 0, 0, 0
            pos = getattr(item, 'position', None)
            if pos is not None:
                xs = [pos.top_left.x, pos.top_right.x,
                      pos.bottom_right.x, pos.bottom_left.x]
                ys = [pos.top_left.y, pos.top_right.y,
                      pos.bottom_right.y, pos.bottom_left.y]
                x = int(min(xs))
                y = int(min(ys))
                w = int(max(xs) - x)
                h = int(max(ys) - y)

            unique.append(QRDecodeResult(data, x, y, w, h))

        return unique
