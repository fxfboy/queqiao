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
import json
import webbrowser
import base64
import math
import time
import tempfile
from pathlib import Path
from io import BytesIO

import qrcode
from qrcode.util import QRData, MODE_8BIT_BYTE

from queqiao.jabcode_cli import find_executable, run_writer


# ──────────────────────────────────────────────────────────────
# 2. 数据编码：压缩 + 分片 + 校验
# ──────────────────────────────────────────────────────────────

def encode_chunks(data, chunk_size=400, filename=None):
    """
    将数据编码为多个分片

    分片格式 (每片共用):
    - magic:    2 bytes  (0x51 0x52 = "QR")
    - index:    2 bytes  (分片序号; 0 = 元数据, 1..N = 数据)
    - total:    2 bytes  (总分片数, 含元数据)
    - datalen:  2 bytes  (本片数据长度)
    - checksum: 4 bytes  (SHA256 的前 4 字节)
    - data:     N bytes  (实际数据)

    头部总长: 12 bytes

    Index 0 (元数据片) 的 data 是 JSON:
    {"version":1,"filename":"...","size":N,"sha256":"...","compressed_size":N}
    """
    MAGIC = b'QR'

    file_sha256 = hashlib.sha256(data).hexdigest()

    compressed = lzma.compress(data, preset=9 | lzma.PRESET_EXTREME)

    raw_chunks = []
    for i in range(0, len(compressed), chunk_size):
        raw_chunks.append(compressed[i:i+chunk_size])

    total = len(raw_chunks) + 1  # +1 for metadata chunk

    meta = {
        "version": 1,
        "filename": filename or "",
        "size": len(data),
        "sha256": file_sha256,
        "compressed_size": len(compressed),
    }
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    meta_checksum = hashlib.sha256(meta_bytes).digest()[:4]
    meta_header = MAGIC + struct.pack('>HHH', 0, total, len(meta_bytes)) + meta_checksum
    encoded = [meta_header + meta_bytes]

    for idx, chunk in enumerate(raw_chunks):
        checksum = hashlib.sha256(chunk).digest()[:4]
        header = MAGIC + struct.pack('>HHH', idx + 1, total, len(chunk)) + checksum
        encoded.append(header + chunk)

    return encoded, len(data), len(compressed)


# ──────────────────────────────────────────────────────────────
# 3. 生成 HTML 页面
# ──────────────────────────────────────────────────────────────

_ECC_LEVELS = {
    'L': qrcode.constants.ERROR_CORRECT_L,
    'M': qrcode.constants.ERROR_CORRECT_M,
    'Q': qrcode.constants.ERROR_CORRECT_Q,
    'H': qrcode.constants.ERROR_CORRECT_H,
}


def chunk_to_qr_image(payload, box_size=5, border=2, ecc=None):
    """将分片数据编码为二维码图片。

    ecc=None 时用 ERROR_CORRECT_M，与本函数历史行为逐字节相同；
    v3 流式路径的标定会显式传 'L'/'M'/'Q'/'H'。
    """
    # 用 base85 编码（比 base64 更紧凑）
    b85 = base64.b85encode(payload).decode('ascii')

    if ecc is None:
        error_correction = qrcode.constants.ERROR_CORRECT_M
    else:
        try:
            error_correction = _ECC_LEVELS[ecc]
        except (KeyError, TypeError) as exc:
            raise ValueError("ecc 必须是 'L'/'M'/'Q'/'H' 之一，实得 %r" % (ecc,)) from exc

    qr = qrcode.QRCode(
        version=None,  # 自动选择最小版本
        error_correction=error_correction,
        box_size=box_size,
        border=border,
    )
    # 必须显式指定 byte 模式，不能让 qrcode 自己挑（默认 optimize=20 会做混合
    # 模式优化，即使 optimize=0，QRData 的 check_data 也会自动识别）。
    #
    # qrcode 8.2 有一个真实缺陷：numeric 模式把每 3 个数字编成 10 bit，'000'
    # 编出来是 10 bit 全零。载荷里出现足够长的 '0' 串时，某个 RS 块的数据码字
    # 整块为零，库内部 Polynomial.__init__ 剥掉前导零后退化成零多项式，
    # RS 除法取 glog(self[0]) 就撞上 glog(0)，抛 ValueError。实测 '0'*150 尚可
    # （尾部还跟着 0xEC/0x11 填充字节），'0'*200 起必崩，换 version 和 ECC 都躲不掉。
    # 注意触发条件是字符 '0' 而不是"纯数字"——'1'*300 和 '0123456789'*30 都正常。
    #
    # 这对本项目不是边角情况：流式分块用 \x00 补齐末块，而 base85 把 4 个 \x00
    # 编成 '00000'，所以"文件远小于 blocklen"这一最常见场景必然产生长 '0' 串。
    #
    # 强制 byte 模式绕开整条 numeric 路径：base85 的字符全是可打印 ASCII（>= 0x21），
    # 数据码字不可能整块为零。代价为零——base85 字符集含大小写字母和符号，正常
    # 载荷本来就走 byte 模式，实测 30 个随机载荷修复前后图像逐位相同，v1/v2 的
    # HTML 不受影响。反而顺带修正了一处隐患：symbol_encoder 的容量模型
    # （QR_V40_BYTE_CAPACITY + max_raw_for_base85）一直是按 byte 模式算的，
    # 之前库若偷偷改用别的模式，算出来的容量就和实际编码行为脱节了。
    qr.add_data(QRData(b85.encode('ascii'), mode=MODE_8BIT_BYTE, check_data=False))
    qr.make(fit=True)

    return qr.make_image(fill_color="black", back_color="white")


def chunk_to_jab_image(payload, colors=8, module_size=12, ecc_level=3,
                       executable=None):
    """Use the official reference writer to encode raw bytes as JAB Code."""
    with tempfile.TemporaryDirectory(prefix="queqiao-jab-png-") as temp_dir:
        output_path = Path(temp_dir) / "code.png"
        run_writer(
            payload, output_path, colors=colors, module_size=module_size,
            ecc_level=ecc_level, executable=executable,
        )
        from PIL import Image
        with Image.open(output_path) as image:
            return image.convert("RGB").copy()


def generate_html(chunks, output_path, cols=6, qr_size=180, backend='qr',
                  jab_colors=8, jab_module_size=12, jab_ecc_level=3):
    """生成 HTML 页面，网格显示所有 QR 或 JAB Code。"""
    
    total = len(chunks)
    rows = math.ceil(total / cols)
    
    jab_writer = find_executable("writer") if backend == 'jab' else None

    # 预生成所有码图的 base64
    qr_images = []
    for idx, payload in enumerate(chunks):
        if backend == 'jab':
            img = chunk_to_jab_image(
                payload, colors=jab_colors, module_size=jab_module_size,
                ecc_level=jab_ecc_level, executable=jab_writer,
            )
        else:
            img = chunk_to_qr_image(payload, box_size=4, border=2)
        buf = BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        qr_images.append(b64)
        
        # 进度
        if (idx + 1) % 10 == 0 or idx == total - 1:
            print(f"  Generating {backend.upper()} codes: {idx+1}/{total}", end='\r')
    
    print()
    
    # 生成 HTML
    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>QueQiao (鹊桥) - {total} {backend.upper()} codes</title>
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
    <p>Total: <strong>{total}</strong> {backend.upper()} codes |
       Grid: {cols} cols × {rows} rows | 
       Print this page for backup</p>
</div>
<div class="grid">
"""
    
    for idx, b64_img in enumerate(qr_images):
        html += f"""
<div class="qr-item">
    <span class="num">#{idx+1}</span>
    <img src="data:image/png;base64,{b64_img}" alt="{backend.upper()} {idx+1}">
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

def main(argv=None):
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
    parser.add_argument('-o', '--output', default=None,
                        help='输出 HTML 文件 (默认: output/qr-{chunk_size}-{timestamp}.html)')
    parser.add_argument('--cols', type=int, default=None,
                        help='每行显示的码数量 (默认: QR 6, JAB 1)')
    parser.add_argument('--qr-size', type=int, default=None,
                        help='HTML 中码图尺寸/像素 (默认: QR 180, JAB 900)')
    parser.add_argument('--chunk-size', type=int, default=None,
                        help='每个分片的数据字节数 (默认: QR 800, JAB 3000)')
    parser.add_argument('--backend', choices=('qr', 'jab'), default='qr',
                        help='码制后端: qr 或 jab (默认: qr)')
    parser.add_argument('--jab-colors', type=int, choices=(4, 8), default=8,
                        help='JAB Code 颜色数 (默认: 8)')
    parser.add_argument('--jab-module-size', type=int, default=12,
                        help='JAB Code 单模块像素数 (默认: 12)')
    parser.add_argument('--jab-ecc-level', type=int, choices=range(1, 11), default=3,
                        help='JAB Code 纠错级别 1-10 (默认: 3, 约 6%%)')
    parser.add_argument('--no-open', action='store_true',
                        help='生成后不自动打开浏览器 (默认: 自动打开)')

    args = parser.parse_args(argv)

    if args.chunk_size is None:
        args.chunk_size = 3000 if args.backend == 'jab' else 800
    if args.cols is None:
        args.cols = 1 if args.backend == 'jab' else 6
    if args.qr_size is None:
        args.qr_size = 900 if args.backend == 'jab' else 180

    if args.output is None:
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        output_dir = Path.cwd() / 'output'
        output_dir.mkdir(exist_ok=True)
        args.output = str(output_dir / f'qr-{args.chunk_size}-{timestamp}.html')

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

    print(f"[2/2] Encoding (backend={args.backend}, chunk_size={args.chunk_size})...")
    chunks, original_size, compressed_size = encode_chunks(data, chunk_size=args.chunk_size, filename=input_path.name)
    compression_ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
    print(f"  Original:   {original_size:,} bytes")
    print(f"  Compressed: {compressed_size:,} bytes ({compression_ratio:.1f}% saved)")
    print(f"  Codes:      {len(chunks)}")
    print()

    try:
        output_path = generate_html(
            chunks, args.output, cols=args.cols, qr_size=args.qr_size,
            backend=args.backend, jab_colors=args.jab_colors,
            jab_module_size=args.jab_module_size,
            jab_ecc_level=args.jab_ecc_level,
        )
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("✅ Done!")
    print(f"  HTML: {output_path}")
    print(f"  Backend: {args.backend}")
    print(f"  Codes:   {len(chunks)}")
    print()
    print("  拍照提示:")
    print("    1. 在浏览器中打开 HTML 文件并全屏 (F11)")
    print("    2. 确保屏幕亮度足够")
    print("    3. 用手机/相机水平拍摄整个屏幕")
    print("    4. 确保所有二维码清晰可见")
    print("=" * 60)

    if not args.no_open:
        file_url = Path(args.output).resolve().as_uri()
        print(f"\n  🌐 Opening in browser: {file_url}")
        webbrowser.open(file_url)


if __name__ == '__main__':
    main()
