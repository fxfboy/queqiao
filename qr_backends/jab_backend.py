"""JAB Code backend backed by the official reference reader CLI."""

import tempfile
from pathlib import Path

from PIL import Image

from jabcode_cli import find_executable, run_reader
from .base import QRDecoderAdapter, QRDecodeResult


class JabCodeDecoder(QRDecoderAdapter):
    name = "jab"
    payload_encoding = "raw"

    def __init__(self):
        self.reader = find_executable("reader")

    def decode_image(self, image_path):
        # The reference reader accepts PNG/TIFF. Normalize camera formats to PNG.
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                with tempfile.TemporaryDirectory(prefix="queqiao-jab-image-") as temp_dir:
                    normalized = Path(temp_dir) / "input.png"
                    image.convert("RGB").save(normalized, format="PNG")
                    data = run_reader(normalized, executable=self.reader)
        except ValueError:
            return []
        except Exception as e:
            raise ValueError(f"Cannot read JAB Code image: {image_path}") from e

        return [QRDecodeResult(data, 0, 0, width, height)]
