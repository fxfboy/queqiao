"""pyzbar (zbar 系统库的 Python 绑定) 后端。

需要本地安装 zbar (macOS: brew install zbar; Linux: libzbar0; Windows: zbar/VC runtime)。
macOS 上 run.sh 会自动 export DYLD_LIBRARY_PATH=/opt/homebrew/lib。
"""
import hashlib

from .base import QRDecoderAdapter, QRDecodeResult


class PyzbarQRDecoder(QRDecoderAdapter):
    name = 'pyzbar'

    def __init__(self):
        try:
            from pyzbar.pyzbar import decode as pyzbar_decode, ZBarSymbol
        except ImportError as e:
            raise RuntimeError(
                "pyzbar backend is unavailable. Install Python package pyzbar "
                "and the native zbar library (macOS: brew install zbar; "
                "Linux: install libzbar0/zbar; Windows: install zbar/VC runtime)."
            ) from e

        self.pyzbar_decode = pyzbar_decode
        self.qrcode_symbol = ZBarSymbol.QRCODE

    def decode_image(self, source):
        img, should_close = self._as_image(source)
        try:
            results = []
            for mode in [None, 'L', '1']:
                try:
                    test_img = img if mode is None else img.convert(mode)
                    results.extend(
                        self.pyzbar_decode(test_img, symbols=[self.qrcode_symbol])
                    )
                except Exception:
                    pass
        finally:
            if should_close:
                img.close()

        seen = set()
        unique = []
        for item in results:
            data_hash = hashlib.md5(item.data).hexdigest()
            if data_hash in seen:
                continue
            seen.add(data_hash)

            x, y, w, h = 0, 0, 0, 0
            rect = getattr(item, 'rect', None)
            if rect is not None:
                x = getattr(rect, 'left', 0)
                y = getattr(rect, 'top', 0)
                w = getattr(rect, 'width', 0)
                h = getattr(rect, 'height', 0)

            unique.append(QRDecodeResult(item.data, x, y, w, h))

        return unique
