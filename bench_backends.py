#!/usr/bin/env python3
"""pyzbar vs zxing 对比测试：chunk-size 800/1500/1800 三档

测试两类场景：
  A. pixel-perfect: 直接渲染的清晰 PNG (反映纯算法速度 + 干净图解码率)
  B. 真实拍照退化: 缩放 + 高斯模糊 + JPEG 有损压缩，按严酷度分四档
                    (mild / medium / harsh / brutal — 见 DEGRADE_PROFILES)

测试文件: decoder.py 自身
运行: DYLD_LIBRARY_PATH=/opt/homebrew/lib uv run python bench_backends.py
"""
import os
import sys
import time
import lzma
import hashlib
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageFilter
from encoder import encode_chunks, chunk_to_qr_image
from decoder import decode_and_merge_chunks
from qr_backends import PyzbarQRDecoder, ZxingQRDecoder


BACKENDS = [
    (PyzbarQRDecoder, 'pyzbar'),
    (ZxingQRDecoder,  'zxing'),
]


TEST_FILE = 'decoder.py'
CHUNK_SIZES = [800, 1500, 1800]
BOX_SIZE = 10
# 真实手机拍照退化：(缩放比, 高斯模糊半径, JPEG 质量) — 越靠后越接近"远拍+抖动+低质 JPEG"
DEGRADE_PROFILES = [
    ('mild',   0.50, 1.0, 90),
    ('medium', 0.30, 2.0, 75),
    ('harsh',  0.20, 3.0, 60),
    ('brutal', 0.15, 4.0, 50),
]


def render_pngs(sample_bytes, chunk_size, filename):
    chunks, _, comp = encode_chunks(sample_bytes, chunk_size=chunk_size, filename=filename)
    paths = []
    for payload in chunks:
        img = chunk_to_qr_image(payload, box_size=BOX_SIZE, border=4)
        p = tempfile.mktemp(suffix='.png')
        img.save(p)
        paths.append(p)
    return chunks, paths, comp


def degrade_pngs(paths, ratio, blur_radius, jpeg_quality):
    """模拟真实拍照: 缩放 + 高斯模糊 + JPEG 有损压缩"""
    out_paths = []
    for p in paths:
        img = Image.open(p).convert('RGB')
        w, h = img.size
        # 1. 下采样 (拍远了 / 屏幕分辨率不够)
        small = img.resize((int(w * ratio), int(h * ratio)), Image.BILINEAR)
        # 2. 高斯模糊 (对焦不准 / 手抖)
        blurred = small.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        # 3. 放回原尺寸
        restored = blurred.resize((w, h), Image.BILINEAR)
        # 4. JPEG 编解码 (手机相册常见格式)
        op = tempfile.mktemp(suffix='.jpg')
        restored.save(op, format='JPEG', quality=jpeg_quality)
        out_paths.append(op)
        img.close()
    return out_paths


def run_backend(adapter, png_paths):
    t0 = time.perf_counter()
    all_b85 = []
    for p in png_paths:
        results = adapter.decode_image(p)
        for r in results:
            all_b85.append(r.data.decode('ascii'))
    elapsed = time.perf_counter() - t0
    return elapsed, all_b85


def verify(all_b85, original_bytes):
    try:
        compressed, stats, meta = decode_and_merge_chunks(all_b85)
        restored = lzma.decompress(compressed)
        byte_ok = restored == original_bytes
        sha_ok = meta is not None and hashlib.sha256(restored).hexdigest() == meta.get('sha256')
        return byte_ok and sha_ok, stats
    except Exception as e:
        return False, {'error': str(e)}


def cleanup(paths):
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def main():
    with open(TEST_FILE, 'rb') as f:
        sample = f.read()
    print(f"测试文件: {TEST_FILE}  ({len(sample):,} bytes)")
    print(f"PNG box_size = {BOX_SIZE} (单 module 像素)")
    print()

    perf_rows = []   # pixel-perfect
    degrade_rows = []

    for cs in CHUNK_SIZES:
        chunks, paths, comp = render_pngs(sample, cs, TEST_FILE)
        n = len(chunks)
        png_w = Image.open(paths[0]).size[0]
        print(f"━━━ chunk_size={cs} ━━━")
        print(f"  生成 {n} 个 QR (压缩 {comp:,} bytes), 单 PNG {png_w}×{png_w}")

        # A. pixel-perfect
        print(f"  [A] pixel-perfect:")
        for cls, label in BACKENDS:
            adapter = cls()
            elapsed, b85 = run_backend(adapter, paths)
            ok, stats = verify(b85, sample)
            valid = stats.get('decoded', 0) if isinstance(stats, dict) else 0
            print(f"        {label:6s} {elapsed:6.3f}s "
                  f"({elapsed/n*1000:5.1f} ms/QR) "
                  f"valid {valid:>2}/{n} "
                  f"byte: {'✅' if ok else '❌'}")
            perf_rows.append((cs, n, label, elapsed, valid, ok))

        # B. 真实拍照退化模拟
        print(f"  [B] 降质模拟 (缩放+模糊+JPEG):")
        for profile_name, ratio, blur, jpeg_q in DEGRADE_PROFILES:
            deg_paths = degrade_pngs(paths, ratio, blur, jpeg_q)
            tag = f"{profile_name}({int(ratio*100)}%/blur{blur}/jpeg{jpeg_q})"
            for cls, label in BACKENDS:
                adapter = cls()
                elapsed, b85 = run_backend(adapter, deg_paths)
                ok, stats = verify(b85, sample)
                valid = stats.get('decoded', 0) if isinstance(stats, dict) else 0
                missing = stats.get('missing', []) if isinstance(stats, dict) else []
                missing_count = len(missing) if isinstance(missing, list) else 0
                print(f"        {tag:32s} {label:6s} {elapsed:6.3f}s "
                      f"valid {valid:>2}/{n} miss {missing_count:>2} "
                      f"byte: {'✅' if ok else '❌'}")
                degrade_rows.append((cs, n, profile_name, label, elapsed, valid, missing_count, ok))
            cleanup(deg_paths)
        print()

        cleanup(paths)

    # 汇总
    print("═" * 80)
    print("  A. Pixel-perfect 汇总")
    print("═" * 80)
    print(f"{'chunk':>6} {'n_qr':>5} {'backend':<8} {'time(s)':>8} {'detect':>10} {'ok':>4}")
    for cs, n, label, elapsed, valid, ok in perf_rows:
        print(f"{cs:>6} {n:>5} {label:<8} {elapsed:>8.3f} "
              f"{valid:>4}/{n:<5} {'✅' if ok else '❌':>4}")

    print()
    print("═" * 80)
    print("  B. 降质场景汇总 (缩放+模糊+JPEG)")
    print("═" * 80)
    print(f"{'chunk':>6} {'n_qr':>5} {'profile':>8} {'backend':<8} {'time(s)':>8} {'valid':>8} {'miss':>5} {'ok':>4}")
    for cs, n, prof, label, elapsed, valid, miss, ok in degrade_rows:
        print(f"{cs:>6} {n:>5} {prof:>8} {label:<8} "
              f"{elapsed:>8.3f} {valid:>4}/{n:<3} {miss:>5} {'✅' if ok else '❌':>4}")

    # 速度比 (pixel-perfect, 各 backend 相对 zxing)
    print()
    print("  速度比 (pixel-perfect, 相对 zxing):")
    for cs in CHUNK_SIZES:
        zx = next(r for r in perf_rows if r[0] == cs and r[2] == 'zxing')
        for _, label in BACKENDS:
            if label == 'zxing':
                continue
            row = next(r for r in perf_rows if r[0] == cs and r[2] == label)
            ratio = row[3] / zx[3] if zx[3] else float('inf')
            tag = f"{label} 慢 {ratio:.1f}x" if ratio > 1 else f"{label} 快 {1/ratio:.1f}x"
            print(f"    chunk_size={cs}: {tag}")

    # 鲁棒性: 按 profile 严酷顺序找各 backend 还能 100% 解出的最严档
    print()
    print("  鲁棒性 (能 100% 解出的最严 profile):")
    profile_order = [p[0] for p in DEGRADE_PROFILES]
    for cs in CHUNK_SIZES:
        for _, label in BACKENDS:
            ok_profs = [r[2] for r in degrade_rows
                        if r[0] == cs and r[3] == label and r[7]]
            if ok_profs:
                worst = max(ok_profs, key=lambda p: profile_order.index(p))
            else:
                worst = '全部失败'
            print(f"    chunk_size={cs}  {label:6s}: 最严能扛 {worst}")


if __name__ == '__main__':
    main()
