#!/usr/bin/env python3
"""Subprocess smoke tests for the encoder/decoder CLIs (via `python -m queqiao.*`)."""
import os
import sys
import subprocess
import tempfile
import shutil

SAMPLE = b"Hello, CLI!\n" + bytes(range(256)) * 8   # includes non-utf8 bytes


def test_encoder_cli():
    print("[TEST] queqiao encoder CLI (file in → html out)...")
    src = tempfile.mktemp(suffix='.bin')
    out = tempfile.mktemp(suffix='.html')
    with open(src, 'wb') as f:
        f.write(SAMPLE)
    try:
        r = subprocess.run([sys.executable, '-m', 'queqiao.encoder', src, '-o', out, '--no-open'],
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
    print("[TEST] queqiao decoder CLI (QR images → raw bytes out, default backend)...")
    from queqiao.encoder import encode_chunks, chunk_to_qr_image
    sample = b'\x00\x01\x02\xff\xfe binary+text\n' * 8   # non-utf8 on purpose
    chunks, _, _ = encode_chunks(sample, chunk_size=400)
    d = tempfile.mkdtemp()
    out = tempfile.mktemp(suffix='.out')
    try:
        for i, payload in enumerate(chunks):
            chunk_to_qr_image(payload, box_size=10, border=4).save(os.path.join(d, f'{i:03d}.png'))
        r = subprocess.run([sys.executable, '-m', 'queqiao.decoder', d, '-o', out],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"decoder failed: {r.stderr}"
        with open(out, 'rb') as f:
            assert f.read() == sample, "decoded bytes must match input exactly"
        print("  ✅ PASSED")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if os.path.exists(out):
            os.remove(out)


def test_decoder_cli_directory_pyzbar():
    print("[TEST] queqiao decoder CLI (dir of PNGs → raw bytes out, pyzbar backend)...")
    from queqiao.encoder import encode_chunks, chunk_to_qr_image
    sample = b'\x00\xff decoder dir bytes\n' * 20
    chunks, _, _ = encode_chunks(sample, chunk_size=400)
    d = tempfile.mkdtemp()
    out = tempfile.mktemp(suffix='.out')
    try:
        for i, payload in enumerate(chunks):
            chunk_to_qr_image(payload, box_size=10, border=4).save(os.path.join(d, f'{i:03d}.png'))
        r = subprocess.run([
            sys.executable, '-m', 'queqiao.decoder', d,
            '-o', out, '--backend', 'pyzbar',
        ], capture_output=True, text=True)
        assert r.returncode == 0, f"decoder dir failed: {r.stderr}"
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
    test_decoder_cli_directory_pyzbar()
    print("\n✅ test_cli passed!")
