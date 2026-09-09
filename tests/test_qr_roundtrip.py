#!/usr/bin/env python3
"""Full roundtrip: encode bytes → render real QR PNGs → decode with pyzbar → compare."""
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

SAMPLE = b"QR roundtrip sample\n" + bytes(range(256)) * 4


def test_full_roundtrip_with_qr():
    print("[1/4] Encoding sample bytes...")
    chunks, original_size, compressed_size = encode_chunks(SAMPLE, chunk_size=400, filename="roundtrip.bin")
    print(f"  {len(chunks)} chunks, {compressed_size} compressed bytes")

    print("[2/4] Rendering QR PNGs...")
    paths = []
    for payload in chunks:
        img = chunk_to_qr_image(payload, box_size=10, border=4)
        p = tempfile.mktemp(suffix='.png')
        img.save(p)
        paths.append(p)

    decoded = {}
    total = None
    try:
        print("[3/4] Decoding QR PNGs with pyzbar...")
        for p in paths:
            results = zbar_decode(Image.open(p), symbols=[ZBarSymbol.QRCODE])
            assert results, f"failed to decode {p}"
            raw = base64.b85decode(results[0].data)
            assert raw[:2] == b'QR'
            idx, t, datalen = struct.unpack('>HHH', raw[2:8])
            checksum = raw[8:12]
            data = raw[12:12 + datalen]
            assert hashlib.sha256(data).digest()[:4] == checksum
            if total is None:
                total = t
            decoded[idx] = data

        print("[4/4] Merging and verifying bytes...")
        assert len(decoded) == total, f"missing chunks: {len(decoded)}/{total}"
        merged = b''.join(decoded[i] for i in range(1, total))
        restored = lzma.decompress(merged)
        assert restored == SAMPLE, "restored bytes must match input exactly"
        print("  ✅ Full roundtrip PASSED")
        return True
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


if __name__ == '__main__':
    sys.exit(0 if test_full_roundtrip_with_qr() else 1)
