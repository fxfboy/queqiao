"""JAB Code backend backed by the official reference reader CLI."""

import tempfile
from pathlib import Path

from queqiao.jabcode_cli import find_executable, run_reader
from .base import QRDecoderAdapter, QRDecodeResult


class JabCodeDecoder(QRDecoderAdapter):
    name = "jab"
    payload_encoding = "raw"

    def __init__(self):
        self.reader = find_executable("reader")

    def decode_image(self, source):
        # The reference reader accepts PNG/TIFF. Normalize camera formats to PNG.
        image, should_close = self._as_image(source)
        try:
            width, height = image.size
            with tempfile.TemporaryDirectory(prefix="queqiao-jab-image-") as temp_dir:
                normalized = Path(temp_dir) / "input.png"
                image.convert("RGB").save(normalized, format="PNG")
                data = run_reader(normalized, executable=self.reader)
        except ValueError:
            return []
        except Exception as e:
            raise ValueError(f"Cannot read JAB Code image: {source!r}") from e
        finally:
            # 只关自己打开的。借用的 Image 关掉会让调用方的下一帧作废。
            if should_close:
                image.close()

        return [QRDecodeResult(data, 0, 0, width, height)]
