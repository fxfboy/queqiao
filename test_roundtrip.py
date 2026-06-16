#!/usr/bin/env python3
"""Byte-roundtrip tests for the QR transfer pipeline (no diff dependency)."""
import os
import sys
import json
import lzma
import struct
import hashlib
import base64
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from encoder import encode_chunks, generate_html

# text + every byte value → proves the pipeline is byte-agnostic
SAMPLE = b"Hello, QR transfer!\n" + bytes(range(256)) * 8


def test_encode_chunks():
    print("[TEST] encode_chunks...")
    chunks, original_size, compressed_size = encode_chunks(SAMPLE, chunk_size=400, filename="test_sample.bin")
    assert original_size == len(SAMPLE), "original_size must equal input length"
    assert len(chunks) > 1, "expected metadata + at least one data chunk"
    for i, c in enumerate(chunks):
        assert c[:2] == b'QR', f"chunk {i} bad magic"

    raw0 = chunks[0]
    idx0, total0, datalen0 = struct.unpack('>HHH', raw0[2:8])
    assert idx0 == 0, "first chunk must be metadata (index 0)"
    meta = json.loads(raw0[12:12+datalen0].decode('utf-8'))
    assert meta['version'] == 1
    assert meta['filename'] == "test_sample.bin"
    assert meta['size'] == len(SAMPLE)
    assert meta['sha256'] == hashlib.sha256(SAMPLE).hexdigest()

    print("  ✅ PASSED")
    return chunks


def test_chunk_roundtrip(chunks):
    print("[TEST] chunk decode + merge + lzma decompress...")
    decoded = {}
    total = None
    for chunk in chunks:
        b85 = base64.b85encode(chunk).decode('ascii')   # simulate QR text payload
        raw = base64.b85decode(b85)
        assert raw[:2] == b'QR'
        idx, t, datalen = struct.unpack('>HHH', raw[2:8])
        checksum = raw[8:12]
        data = raw[12:12 + datalen]
        assert hashlib.sha256(data).digest()[:4] == checksum, f"checksum mismatch chunk {idx}"
        if total is None:
            total = t
        assert t == total
        decoded[idx] = data
    merged = b''.join(decoded[i] for i in range(1, total))
    restored = lzma.decompress(merged)
    assert restored == SAMPLE, "roundtrip bytes must match exactly"
    print("  ✅ PASSED")


def test_html_generation(chunks):
    print("[TEST] generate_html...")
    out = tempfile.mktemp(suffix='.html')
    try:
        generate_html(chunks, out, cols=4, qr_size=150)
        with open(out) as f:
            content = f.read()
        assert 'data:image/png;base64,' in content, "should embed base64 PNGs"
        assert f'/{len(chunks)}' in content, "should show the total count"
        print("  ✅ PASSED")
    finally:
        if os.path.exists(out):
            os.remove(out)


if __name__ == '__main__':
    chunks = test_encode_chunks()
    test_chunk_roundtrip(chunks)
    test_html_generation(chunks)
    print("\n✅ All roundtrip tests passed!")
