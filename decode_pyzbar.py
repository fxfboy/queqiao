#!/usr/bin/env python3
"""QueQiao (鹊桥) pyzbar decoder for better QR detection from screenshots."""
import os, sys, hashlib, struct, lzma, base64
from pathlib import Path
from pyzbar.pyzbar import decode as pyzbar_decode
from PIL import Image

def decode_qr_pyzbar(image_path):
    """Use pyzbar to decode all QR codes from an image."""
    img = Image.open(image_path)
    # Try multiple image modes for better detection
    results = []
    for mode in [None, 'L', '1']:
        try:
            test_img = img if mode is None else img.convert(mode)
            r = pyzbar_decode(test_img, symbols=[0])
            results.extend(r)
        except:
            pass
    
    # Deduplicate
    seen = set()
    unique = []
    for r in results:
        h = hashlib.md5(r.data).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(r)
    return [r.data for r in unique]

def decode_single_chunk(b85_data):
    MAGIC = b'QR'
    HEADER_SIZE = 12
    try:
        if isinstance(b85_data, str):
            b85_data = b85_data.encode('ascii')
        raw = base64.b85decode(b85_data)
        if len(raw) < HEADER_SIZE:
            return None
        magic = raw[:2]
        if magic != MAGIC:
            return None
        idx, total, datalen = struct.unpack('>HHH', raw[2:8])
        checksum = raw[8:12]
        data = raw[12:12 + datalen]
        if len(data) != datalen:
            return None
        expected = hashlib.sha256(data).digest()[:4]
        if checksum != expected:
            return None
        return (idx, total, data)
    except Exception:
        return None

image_dir = sys.argv[1] if len(sys.argv) > 1 else "bid"
output_file = sys.argv[2] if len(sys.argv) > 2 else "restored.out"
image_files = sorted(Path(image_dir).glob("*.png"))

if not image_files:
    print(f"❌ No PNG files found in {image_dir}")
    sys.exit(1)

all_qr_data = []
total_found = 0

for img_path in image_files:
    print(f"[📷] Processing: {img_path}")
    qr_list = decode_qr_pyzbar(str(img_path))
    print(f"  Found {len(qr_list)} QR codes")
    for data in qr_list:
        try:
            all_qr_data.append(data.decode('utf-8', errors='replace'))
        except:
            pass
    total_found += len(qr_list)

print(f"\nTotal QR codes found: {total_found}")

# Decode and merge chunks
chunks = {}
total = None
invalid_count = 0
for b85_str in all_qr_data:
    result = decode_single_chunk(b85_str)
    if result is None:
        invalid_count += 1
        continue
    idx, t, data = result
    if total is None:
        total = t
    elif t != total:
        continue
    if idx not in chunks:
        chunks[idx] = data

if total is None:
    print("❌ No valid chunks found")
    sys.exit(1)

missing = [i for i in range(total) if i not in chunks]
print(f"\n✅ Decoded: {len(chunks)}/{total} chunks")
print(f"   Invalid: {invalid_count}")
if missing:
    print(f"❌ Missing {len(missing)} chunks: {missing[:20]}{'...' if len(missing)>20 else ''}")
    sys.exit(1)

# Merge and decompress
result = b''
for i in range(total):
    result += chunks[i]

try:
    output_data = lzma.decompress(result)
except lzma.LZMAError:
    output_data = result

with open(output_file, 'wb') as f:
    f.write(output_data)

print(f"\n✅ Done!")
print(f"  Output: {output_file}")
print(f"  Size: {len(output_data):,} bytes")
