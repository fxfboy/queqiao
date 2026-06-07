#!/usr/bin/env python3
"""
QueQiao (鹊桥) - Encoder
读取单个文件，lzma(xz) 压缩并分片，生成带二维码的 HTML 页面（用于打印/显示/拍照传输）。

用法:
    python encoder.py input.txt -o qr.html
"""

import sys
import hashlib
import struct
import argparse
import lzma
import base64
import math
from pathlib import Path
from io import BytesIO

import qrcode


# ──────────────────────────────────────────────────────────────
# 2. 数据编码：压缩 + 分片 + 校验
# ──────────────────────────────────────────────────────────────

def encode_chunks(data, chunk_size=400):
    """
    将数据编码为多个分片
    
    分片格式:
    - magic:    2 bytes  (0x51 0x52 = "QR")
    - index:    2 bytes  (分片序号，从 0 开始)
    - total:    2 bytes  (总分片数)
    - datalen:  2 bytes  (本片数据长度)
    - checksum: 4 bytes  (SHA256 的前 4 字节)
    - data:     N bytes  (实际数据)
    
    头部总长: 12 bytes
    """
    MAGIC = b'QR'
    HEADER_SIZE = 12  # 2+2+2+2+4
    
    # 压缩（lzma/xz，比 gzip 对文本压缩率更高，约多省 24%）
    compressed = lzma.compress(data, preset=9 | lzma.PRESET_EXTREME)
    
    # 分片
    raw_chunks = []
    for i in range(0, len(compressed), chunk_size):
        raw_chunks.append(compressed[i:i+chunk_size])
    
    total = len(raw_chunks)
    encoded = []
    
    for idx, chunk in enumerate(raw_chunks):
        checksum = hashlib.sha256(chunk).digest()[:4]
        header = MAGIC + struct.pack('>HHH', idx, total, len(chunk)) + checksum
        payload = header + chunk
        encoded.append(payload)
    
    return encoded, len(data), len(compressed)


# ──────────────────────────────────────────────────────────────
# 3. 生成 HTML 页面
# ──────────────────────────────────────────────────────────────

def chunk_to_qr_image(payload, box_size=5, border=2):
    """将分片数据编码为二维码图片"""
    # 用 base85 编码（比 base64 更紧凑）
    b85 = base64.b85encode(payload).decode('ascii')
    
    qr = qrcode.QRCode(
        version=None,  # 自动选择最小版本
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(b85)
    qr.make(fit=True)
    
    return qr.make_image(fill_color="black", back_color="white")


def generate_html(chunks, output_path, cols=6, qr_size=180):
    """生成 HTML 页面，网格显示所有二维码"""
    
    total = len(chunks)
    rows = math.ceil(total / cols)
    
    # 预生成所有二维码的 base64
    qr_images = []
    for idx, payload in enumerate(chunks):
        img = chunk_to_qr_image(payload, box_size=4, border=2)
        buf = BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        qr_images.append(b64)
        
        # 进度
        if (idx + 1) % 10 == 0 or idx == total - 1:
            print(f"  Generating QR codes: {idx+1}/{total}", end='\r')
    
    print()
    
    # 生成 HTML
    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>QueQiao (鹊桥) - {total} codes</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #ffffff;
    font-family: -apple-system, monospace;
    padding: 10px;
}}
.header {{
    text-align: center;
    padding: 8px;
    background: #f0f0f0;
    border-radius: 8px;
    margin-bottom: 10px;
}}
.header h1 {{
    font-size: 18px;
    color: #333;
}}
.header p {{
    font-size: 14px;
    color: #666;
    margin-top: 4px;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat({cols}, 1fr);
    gap: 8px;
    max-width: 1920px;
    margin: 0 auto;
}}
.qr-item {{
    text-align: center;
    background: #fafafa;
    border: 2px solid #ddd;
    border-radius: 6px;
    padding: 6px;
    position: relative;
}}
.qr-item img {{
    width: {qr_size}px;
    height: {qr_size}px;
    image-rendering: pixelated;
}}
.qr-label {{
    font-size: 13px;
    font-weight: bold;
    color: #333;
    margin-top: 3px;
}}
.qr-item .num {{
    position: absolute;
    top: 2px;
    left: 6px;
    font-size: 11px;
    color: #999;
}}
.page-nav {{
    text-align: center;
    padding: 10px;
    font-size: 14px;
    color: #666;
}}
@media print {{
    .grid {{ page-break-inside: auto; }}
    .qr-item {{ page-break-inside: avoid; }}
}}
</style>
</head>
<body>
<div class="header">
    <h1>QueQiao (鹊桥)</h1>
    <p>Total: <strong>{total}</strong> QR codes | 
       Grid: {cols} cols × {rows} rows | 
       Print this page for backup</p>
</div>
<div class="grid">
"""
    
    for idx, b64_img in enumerate(qr_images):
        html += f"""
<div class="qr-item">
    <span class="num">#{idx+1}</span>
    <img src="data:image/png;base64,{b64_img}" alt="QR {idx+1}">
    <div class="qr-label">{idx+1}/{total}</div>
</div>
"""
    
    html += """
</div>
<div class="page-nav">
    <p>📸 拍照提示：保持屏幕水平，确保所有二维码清晰可见</p>
</div>
</body>
</html>
"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    
    return output_path


# ──────────────────────────────────────────────────────────────
# 4. 主程序
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='QueQiao (鹊桥): encode a file into a QR-code HTML page for air-gap transfer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 把任意文件编码成二维码 HTML
  python encoder.py input.txt -o qr.html

  # 自定义网格列数和二维码大小
  python encoder.py input.bin -o qr.html --cols 8 --qr-size 160

  # 配合 make_diff.py（可选）传输仓库 diff
  python make_diff.py ~/ext-repo ~/int-repo -o changes.patch
  python encoder.py changes.patch -o qr.html
"""
    )
    parser.add_argument('input', help='要传输的输入文件 (任意文件)')
    parser.add_argument('-o', '--output', default='qr_diff.html',
                        help='输出 HTML 文件 (默认: qr_diff.html)')
    parser.add_argument('--cols', type=int, default=6,
                        help='每行显示的二维码数量 (默认: 6)')
    parser.add_argument('--qr-size', type=int, default=180,
                        help='二维码图片尺寸/像素 (默认: 180)')
    parser.add_argument('--chunk-size', type=int, default=400,
                        help='每个分片的数据字节数 (默认: 400)')

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"❌ Input file not found: {args.input}")
        sys.exit(1)

    data = input_path.read_bytes()

    print("=" * 60)
    print("  QueQiao (鹊桥) - Encoder")
    print("=" * 60)
    print()
    print(f"[1/2] Reading input: {args.input}")
    print(f"  Size: {len(data):,} bytes")
    print()

    if not data:
        print("❌ Input file is empty, nothing to encode.")
        sys.exit(1)

    print(f"[2/2] Encoding (chunk_size={args.chunk_size})...")
    chunks, original_size, compressed_size = encode_chunks(data, chunk_size=args.chunk_size)
    compression_ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
    print(f"  Original:   {original_size:,} bytes")
    print(f"  Compressed: {compressed_size:,} bytes ({compression_ratio:.1f}% saved)")
    print(f"  QR codes:   {len(chunks)}")
    print()

    output_path = generate_html(chunks, args.output, cols=args.cols, qr_size=args.qr_size)

    print()
    print("=" * 60)
    print("✅ Done!")
    print(f"  HTML: {output_path}")
    print(f"  QR codes: {len(chunks)}")
    print()
    print("  拍照提示:")
    print("    1. 在浏览器中打开 HTML 文件并全屏 (F11)")
    print("    2. 确保屏幕亮度足够")
    print("    3. 用手机/相机水平拍摄整个屏幕")
    print("    4. 确保所有二维码清晰可见")
    print("=" * 60)


if __name__ == '__main__':
    main()
