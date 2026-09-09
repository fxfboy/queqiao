#!/usr/bin/env python3
"""Full repository-scale roundtrip on an arbitrary byte blob (no external fixtures)."""
import os
import sys
import lzma
import struct
import hashlib
import base64
import tempfile


from queqiao.encoder import encode_chunks, chunk_to_qr_image
from pyzbar.pyzbar import decode as zbar_decode, ZBarSymbol
from PIL import Image


def build_sample():
    parts = []
    for i in range(800):
        parts.append(b"line %d: the quick brown fox jumps over the lazy dog\n" % i)
    parts.append(bytes(range(256)) * 16)   # raw binary section
    return b''.join(parts)


def verify_full_roundtrip():
    sample = build_sample()
    print(f"[1/4] Encoding {len(sample):,} bytes...")
    chunks, _, compressed_size = encode_chunks(sample, chunk_size=400, filename="verify_sample.bin")
    print(f"  {len(chunks)} chunks, {compressed_size:,} compressed bytes")

    print("[2/4] Rendering QR PNGs...")
    paths = []
    for payload in chunks:
        img = chunk_to_qr_image(payload, box_size=10, border=4)
        p = tempfile.mktemp(suffix='.png')
        img.save(p)
        paths.append(p)

    decoded = {}
    total = None
    failed = 0
    try:
        print("[3/4] Decoding QR PNGs...")
        for p in paths:
            results = zbar_decode(Image.open(p), symbols=[ZBarSymbol.QRCODE])
            if not results:
                failed += 1
                continue
            raw = base64.b85decode(results[0].data)
            if raw[:2] != b'QR':
                failed += 1
                continue
            idx, t, datalen = struct.unpack('>HHH', raw[2:8])
            checksum = raw[8:12]
            data = raw[12:12 + datalen]
            if hashlib.sha256(data).digest()[:4] != checksum:
                failed += 1
                continue
            if total is None:
                total = t
            decoded[idx] = data

        print(f"[4/4] Merging ({len(decoded)}/{total}, failed={failed})...")
        missing = [i for i in range(1, total) if i not in decoded]
        if missing:
            print(f"  ❌ Missing {len(missing)} chunks: {missing[:10]}")
            return False
        merged = b''.join(decoded[i] for i in range(1, total))
        restored = lzma.decompress(merged)
        if restored == sample:
            print("  ✅ VERIFICATION PASSED (byte-identical)")
            return True
        print("  ❌ Content mismatch")
        return False
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


if __name__ == '__main__':
    sys.exit(0 if verify_full_roundtrip() else 1)
