#!/usr/bin/env python3
"""Tests for the JAB Code CLI bridge without requiring native JAB tools."""

import os
import stat
import tempfile
from pathlib import Path

from PIL import Image

from queqiao.decoder import decode_and_merge_chunks, decode_single_chunk
from queqiao.encoder import encode_chunks, generate_html
from queqiao.jabcode_cli import run_reader, run_writer
from queqiao.qr_backends.jab_backend import JabCodeDecoder


def make_executable(path, source):
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_writer_bridge(temp_dir):
    writer = temp_dir / "jabcodeWriter"
    fixture = temp_dir / "fixture.png"
    Image.new("RGB", (20, 20), "red").save(fixture)
    make_executable(writer, f"""#!/usr/bin/env python3
import pathlib, shutil, sys
args = sys.argv[1:]
source = pathlib.Path(args[args.index('--input-file') + 1]).read_bytes()
output = pathlib.Path(args[args.index('--output') + 1])
assert b'\\x00\\xff' in source
shutil.copyfile({str(fixture)!r}, output)
""")
    output = temp_dir / "result.png"
    run_writer(b"binary\x00\xff", output, executable=str(writer))
    assert output.read_bytes() == fixture.read_bytes()

    old_value = os.environ.get("QUEQIAO_JAB_WRITER")
    os.environ["QUEQIAO_JAB_WRITER"] = str(writer)
    html = temp_dir / "jab.html"
    try:
        generate_html([b"binary\x00\xff"], html, backend="jab")
    finally:
        if old_value is None:
            os.environ.pop("QUEQIAO_JAB_WRITER", None)
        else:
            os.environ["QUEQIAO_JAB_WRITER"] = old_value
    assert "data:image/png;base64," in html.read_text(encoding="utf-8")
    assert "JAB codes" in html.read_text(encoding="utf-8")


def test_reader_and_backend(temp_dir):
    chunks, _, _ = encode_chunks(b"JAB raw payload", chunk_size=100)
    expected = chunks[0]
    payload_file = temp_dir / "expected.bin"
    payload_file.write_bytes(expected)
    reader = temp_dir / "jabcodeReader"
    make_executable(reader, f"""#!/usr/bin/env python3
import pathlib, sys
args = sys.argv[1:]
pathlib.Path(args[args.index('--output') + 1]).write_bytes(pathlib.Path({str(payload_file)!r}).read_bytes())
""")
    image_path = temp_dir / "photo.jpg"
    Image.new("RGB", (32, 24), "white").save(image_path)

    assert run_reader(image_path, executable=str(reader)) == expected
    old_value = os.environ.get("QUEQIAO_JAB_READER")
    os.environ["QUEQIAO_JAB_READER"] = str(reader)
    try:
        backend = JabCodeDecoder()
        results = backend.decode_image(image_path)
    finally:
        if old_value is None:
            os.environ.pop("QUEQIAO_JAB_READER", None)
        else:
            os.environ["QUEQIAO_JAB_READER"] = old_value
    assert backend.payload_encoding == "raw"
    assert len(results) == 1 and results[0].data == expected
    assert decode_single_chunk(results[0].data, payload_encoding="raw") is not None


def test_raw_chunk_roundtrip():
    source = b"raw JAB roundtrip\x00\xff" * 100
    chunks, _, _ = encode_chunks(source, chunk_size=150)
    compressed, stats, metadata = decode_and_merge_chunks(
        chunks, payload_encoding="raw",
    )
    import lzma
    assert lzma.decompress(compressed) == source
    assert stats["decoded"] == len(chunks)
    assert metadata["sha256"]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="queqiao-jab-test-") as directory:
        temp_dir = Path(directory)
        test_writer_bridge(temp_dir)
        test_reader_and_backend(temp_dir)
        test_raw_chunk_roundtrip()
    print("test_jab_backend passed")
