#!/usr/bin/env python3
"""
QueQiao (鹊桥) - Decoder
从拍照图片中解码二维码，逐字节还原出原始文件。

用法:
    python decoder.py photo1.jpg photo2.jpg          # 默认用元数据中的原始文件名
    python decoder.py photos_dir -o my_output.bin    # 强制指定输出名
    python decoder.py photos_dir --backend zxing     # 显式指定后端
"""

import os
import sys
import hashlib
import struct
import lzma
import json
import argparse
import base64
from pathlib import Path

from qr_backends import DEFAULT_BACKEND, available_backends, get_backend


IMAGE_EXTENSIONS = {
    '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp'
}


def expand_image_inputs(inputs):
    """Expand file/dir CLI inputs into a sorted list of image files."""
    image_files = []

    for item in inputs:
        path = Path(item)
        if path.is_dir():
            matches = [
                child for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS
            ]
            image_files.extend(sorted(matches))
        else:
            image_files.append(path)

    return [str(path) for path in image_files]


# ──────────────────────────────────────────────────────────────
# 2. 排序和合并
# ──────────────────────────────────────────────────────────────

def sort_qr_by_position(qr_list):
    """
    按位置排序二维码（从上到下，从左到右）
    """
    if not qr_list:
        return qr_list
    
    # 计算中心点
    for qr in qr_list:
        qr['cx'] = qr['x'] + qr['w'] / 2
        qr['cy'] = qr['y'] + qr['h'] / 2
    
    # 估算行高
    heights = [qr['h'] for qr in qr_list if qr['h'] > 0]
    if not heights:
        return qr_list
    median_height = sorted(heights)[len(heights) // 2]
    row_threshold = median_height * 0.6
    
    # 按 y 坐标排序
    qr_list.sort(key=lambda q: q['cy'])
    
    # 分行
    rows = []
    current_row = [qr_list[0]]
    current_y = qr_list[0]['cy']
    
    for qr in qr_list[1:]:
        if abs(qr['cy'] - current_y) < row_threshold:
            current_row.append(qr)
        else:
            rows.append(current_row)
            current_row = [qr]
            current_y = qr['cy']
    
    if current_row:
        rows.append(current_row)
    
    # 每行内按 x 排序
    result = []
    for row in rows:
        row.sort(key=lambda q: q['cx'])
        result.extend(row)
    
    return result


# ──────────────────────────────────────────────────────────────
# 3. 解码分片数据
# ──────────────────────────────────────────────────────────────

def decode_single_chunk(payload, payload_encoding='base85'):
    """
    解码单个分片
    
    分片格式:
    - magic:    2 bytes  (0x51 0x52 = "QR")
    - index:    2 bytes
    - total:    2 bytes
    - datalen:  2 bytes
    - checksum: 4 bytes
    - data:     N bytes
    
    返回: (index, total, data) 或 None
    """
    MAGIC = b'QR'
    HEADER_SIZE = 12
    
    try:
        # 如果是字符串，编码为 bytes
        if payload_encoding == 'base85':
            if isinstance(payload, str):
                payload = payload.encode('ascii')
            raw = base64.b85decode(payload)
        elif payload_encoding == 'raw':
            raw = payload.encode('latin1') if isinstance(payload, str) else payload
        else:
            raise ValueError(f"Unknown payload encoding: {payload_encoding}")
        
        # 检查最小长度
        if len(raw) < HEADER_SIZE:
            return None
        
        # 解析头部
        magic = raw[:2]
        if magic != MAGIC:
            return None
        
        idx, total, datalen = struct.unpack('>HHH', raw[2:8])
        checksum = raw[8:12]
        data = raw[12:12 + datalen]
        
        # 验证数据长度
        if len(data) != datalen:
            return None
        
        # 验证校验和
        expected = hashlib.sha256(data).digest()[:4]
        if checksum != expected:
            print(f"  ⚠️  Checksum mismatch for chunk {idx} (expected {expected.hex()}, got {checksum.hex()})")
            return None
        
        return (idx, total, data)
        
    except Exception as e:
        return None


def decode_and_merge_chunks(qr_data_list, payload_encoding='base85'):
    """
    解码所有分片，合并数据

    Index 0 = 元数据 (JSON), Index 1..N = 数据分片
    返回: (merged_data, stats, metadata)
    """
    chunks = {}
    total = None
    invalid_count = 0

    for b85_data in qr_data_list:
        result = decode_single_chunk(b85_data, payload_encoding=payload_encoding)

        if result is None:
            invalid_count += 1
            continue

        idx, t, data = result

        if total is None:
            total = t
        elif t != total:
            print(f"  ⚠️  Inconsistent total count: expected {total}, got {t}")
            continue

        if idx in chunks:
            if chunks[idx] == data:
                continue
            else:
                print(f"  ⚠️  Conflicting data for chunk {idx}, using first occurrence")
                continue

        chunks[idx] = data

    if total is None:
        raise ValueError("No valid chunks found in any image")

    metadata = None
    if 0 in chunks:
        try:
            metadata = json.loads(chunks[0].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("  ⚠️  Metadata chunk (index 0) is malformed, ignoring")
    else:
        print("  ⚠️  Metadata chunk (index 0) missing; no file integrity check available")

    missing = []
    for i in range(1, total):
        if i not in chunks:
            missing.append(i)

    stats = {
        'total': total,
        'decoded': len(chunks),
        'missing': missing,
        'invalid': invalid_count,
        'duplicate': len(qr_data_list) - len(chunks) - invalid_count,
    }

    if missing:
        print(f"\n  ❌ Missing {len(missing)} data chunks: {missing[:20]}{'...' if len(missing) > 20 else ''}")
        raise ValueError(f"Cannot restore: {len(missing)} data chunks missing")

    result = b''
    for i in range(1, total):
        result += chunks[i]

    return result, stats, metadata


# ──────────────────────────────────────────────────────────────
# 4. 主程序
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='QueQiao (鹊桥): decode QR codes from photos to restore the original file',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从单张照片解码（不传 -o, 用元数据中的原始文件名）
  python decoder.py photo.jpg

  # 从多张照片解码（二维码分布在多页时）
  python decoder.py page1.jpg page2.jpg page3.jpg

  # 从目录中的图片解码（默认使用 zxing-cpp, 纯 wheel 无系统库依赖）
  python decoder.py photos_dir

  # 强制输出文件名 (不再读取元数据 filename)
  python decoder.py photos_dir -o my_output.bin

  # 强制使用 pyzbar 后端 (需 zbar 系统库)
  python decoder.py photos_dir --backend pyzbar

  # 如果还原出来的是一个 patch，可应用:
  patch -p1 < <output>
"""
    )
    parser.add_argument('images', nargs='+', help='包含二维码的照片文件或目录')
    parser.add_argument('-o', '--output', default=None,
                        help='输出文件 (默认: 元数据中的原始文件名, 找不到时回退为 restored.out)')
    parser.add_argument('--backend', choices=available_backends(), default=DEFAULT_BACKEND,
                        help=f'二维码识别后端 (默认: {DEFAULT_BACKEND}); 详见 qr_backends/')
    parser.add_argument('--debug', action='store_true', help='显示调试信息')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("  QueQiao (鹊桥) - Decoder")
    print("=" * 60)
    print(f"  Backend: {args.backend}")
    print()

    try:
        adapter = get_backend(args.backend)
    except RuntimeError as e:
        print(f"❌ {e}")
        print()
        print("可选方案:")
        print("  - 安装 zxing-cpp 后重试: pip install zxing-cpp (默认后端)")
        print("  - 或使用 --backend pyzbar (需系统 zbar 库)")
        sys.exit(1)

    image_paths = expand_image_inputs(args.images)
    if not image_paths:
        print("❌ No image files found in the provided inputs")
        sys.exit(1)
    
    # Step 1: 从所有图片中提取二维码
    all_qr_data = []
    total_found = 0
    
    for img_path in image_paths:
        print(f"[📷] Processing: {img_path}")
        
        if not os.path.exists(img_path):
            print(f"  ❌ File not found, skipping")
            continue
        
        try:
            qr_results = adapter.decode_image(img_path)
            qr_list = [result.as_position_dict() for result in qr_results]
            print(f"  Found {len(qr_list)} QR codes")
            
            sorted_qr = sort_qr_by_position(qr_list)
            
            for i, qr in enumerate(sorted_qr):
                all_qr_data.append(qr['data'])
                
                if args.debug:
                    print(f"    #{i+1}: pos=({qr['x']},{qr['y']}) size={qr['w']}x{qr['h']}")
            
            total_found += len(qr_list)
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
            if args.debug:
                import traceback
                traceback.print_exc()
            continue
        
        print()
    
    print(f"Total QR codes found: {total_found}")
    print()
    
    if not all_qr_data:
        print("❌ No QR codes found in any image!")
        print("   Tips:")
        print("   - Ensure the image is clear and well-lit")
        print("   - Try taking the photo closer to the screen")
        print("   - Make sure the entire QR grid is visible")
        sys.exit(1)
    
    # Step 2: 解码合并
    print("[🔧] Decoding and verifying chunks...")

    try:
        compressed_data, stats, metadata = decode_and_merge_chunks(
            all_qr_data, payload_encoding=adapter.payload_encoding,
        )
    except ValueError as e:
        print(f"\n❌ Failed: {e}")
        sys.exit(1)

    print(f"  ✅ All {stats['total']} chunks verified")
    print(f"     Decoded:   {stats['decoded']}")
    print(f"     Invalid:   {stats['invalid']}")
    print(f"     Duplicate: {stats['duplicate']}")
    if metadata:
        print(f"     Metadata:  version={metadata.get('version')}, "
              f"filename={metadata.get('filename', '?')}, "
              f"size={metadata.get('size', '?')}")
    print()

    # Step 3: 解压
    print("[📦] Decompressing...")

    try:
        output_data = lzma.decompress(compressed_data)
    except lzma.LZMAError:
        print("  ⚠️  Data is not valid xz, writing raw bytes...")
        output_data = compressed_data
    except Exception as e:
        print(f"  ❌ Decompression failed: {e}")
        sys.exit(1)

    # Step 3.5: 验证整文件 SHA256
    if metadata and 'sha256' in metadata:
        actual_sha256 = hashlib.sha256(output_data).hexdigest()
        expected_sha256 = metadata['sha256']
        if actual_sha256 != expected_sha256:
            print(f"\n  ❌ SHA256 MISMATCH")
            print(f"  Expected: {expected_sha256}")
            print(f"  Actual:   {actual_sha256}")
            print(f"  The restored file may be corrupted!")
            sys.exit(1)
        else:
            print(f"  ✅ SHA256 verified: {actual_sha256[:16]}...")

    # Step 4: 确定输出文件名 —— 用户未传 -o 时才从元数据回退
    if args.output is None:
        name = None
        if metadata and metadata.get('filename'):
            candidate = metadata['filename'].replace('/', '').replace('\\', '').strip()
            if candidate and candidate not in ('.', '..'):
                name = candidate
        if name:
            args.output = name
            print(f"  📁 Using filename from metadata: {args.output}")
        else:
            args.output = 'restored.out'

    # Step 5: 保存（按字节写出，与输入逐字节一致）
    with open(args.output, 'wb') as f:
        f.write(output_data)
    
    print()
    print("=" * 60)
    print("✅ Done!")
    print(f"  Output: {args.output}")
    print(f"  Size:   {len(output_data):,} bytes")
    print()
    print("  输出已按字节还原。如果它是一个 patch，可用:")
    print(f"    patch -p1 < {args.output}")
    print("=" * 60)


if __name__ == '__main__':
    main()
