#!/usr/bin/env python3
"""Subprocess smoke tests for the encoder/decoder/decode_pyzbar CLIs."""
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SAMPLE = b"Hello, CLI!\n" + bytes(range(256)) * 8   # includes non-utf8 bytes


def test_encoder_cli():
    print("[TEST] encoder.py CLI (file in → html out)...")
    src = tempfile.mktemp(suffix='.bin')
    out = tempfile.mktemp(suffix='.html')
    with open(src, 'wb') as f:
        f.write(SAMPLE)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'encoder.py'), src, '-o', out],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"encoder failed: {r.stderr}"
        assert os.path.exists(out), "html output should exist"
        with open(out) as f:
            assert 'data:image/png;base64,' in f.read(), "html should embed QR PNGs"
        print("  ✅ PASSED")
    finally:
        for p in (src, out):
            if os.path.exists(p):
                os.remove(p)


def test_decoder_cli():
    print("[TEST] decoder.py CLI (QR image → raw bytes out)...")
    from encoder import encode_chunks, chunk_to_qr_image
    sample = b'\x00\x01\x02\xff\xfe binary+text\n' * 8   # non-utf8 on purpose
    chunks, _, _ = encode_chunks(sample, chunk_size=400)
    assert len(chunks) == 1, "sample should fit in one chunk (single QR image)"
    png = tempfile.mktemp(suffix='.png')
    out = tempfile.mktemp(suffix='.out')
    chunk_to_qr_image(chunks[0], box_size=10, border=4).save(png)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'decoder.py'), png, '-o', out],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"decoder failed: {r.stderr}"
        with open(out, 'rb') as f:
            assert f.read() == sample, "decoded bytes must match input exactly"
        print("  ✅ PASSED")
    finally:
        for p in (png, out):
            if os.path.exists(p):
                os.remove(p)


def test_decode_pyzbar_cli():
    print("[TEST] decode_pyzbar.py CLI (dir of PNGs → raw bytes out)...")
    import shutil
    from encoder import encode_chunks, chunk_to_qr_image
    sample = b'\x00\xff pyzbar bytes\n' * 20   # non-utf8 on purpose
    chunks, _, _ = encode_chunks(sample, chunk_size=400)
    d = tempfile.mkdtemp()
    out = tempfile.mktemp(suffix='.out')
    try:
        for i, payload in enumerate(chunks):
            chunk_to_qr_image(payload, box_size=10, border=4).save(os.path.join(d, f'{i:03d}.png'))
        r = subprocess.run([sys.executable, os.path.join(HERE, 'decode_pyzbar.py'), d, out],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"decode_pyzbar failed: {r.stderr}"
        with open(out, 'rb') as f:
            assert f.read() == sample, "decoded bytes must match input exactly"
        print("  ✅ PASSED")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if os.path.exists(out):
            os.remove(out)


if __name__ == '__main__':
    test_encoder_cli()
    test_decoder_cli()
    test_decode_pyzbar_cli()
    print("\n✅ test_cli passed!")
