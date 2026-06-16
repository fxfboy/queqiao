#!/usr/bin/env python3
"""
QueQiao (鹊桥) - Decoder
从拍照图片中解码二维码，逐字节还原出原始文件。

用法:
    python decoder.py photo1.jpg photo2.jpg -o restored.out
    python decoder.py photos_dir -o restored.out --backend pyzbar
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
from abc import ABC, abstractmethod

from PIL import Image


# ──────────────────────────────────────────────────────────────
# 1. 从图片中检测和解码二维码 (adapter implementations)
# ──────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {
    '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp'
}


class QRDecodeResult:
    """A decoded QR payload with optional positional metadata."""

    def __init__(self, data, x=0, y=0, w=0, h=0):
        self.data = data
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def as_position_dict(self):
        return {
            'data': self.data,
            'x': self.x,
            'y': self.y,
            'w': self.w,
            'h': self.h,
        }


class QRDecoderAdapter(ABC):
    """Adapter interface for QR detection backends."""

    name = None

    @abstractmethod
    def decode_image(self, image_path):
        """Return a list of QRDecodeResult objects decoded from image_path."""


class PyzbarQRDecoder(QRDecoderAdapter):
    name = 'pyzbar'

    def __init__(self):
        try:
            from pyzbar.pyzbar import decode as pyzbar_decode, ZBarSymbol
        except ImportError as e:
            raise RuntimeError(
                "pyzbar backend is unavailable. Install Python package pyzbar "
                "and the native zbar library (macOS: brew install zbar; "
                "Linux: install libzbar0/zbar; Windows: install zbar/VC runtime)."
            ) from e

        self.pyzbar_decode = pyzbar_decode
        self.qrcode_symbol = ZBarSymbol.QRCODE

    def decode_image(self, image_path):
        try:
            img = Image.open(image_path)
        except Exception as e:
            raise ValueError(f"Cannot read image: {image_path}") from e

        results = []
        for mode in [None, 'L', '1']:
            try:
                test_img = img if mode is None else img.convert(mode)
                results.extend(
                    self.pyzbar_decode(test_img, symbols=[self.qrcode_symbol])
                )
            except Exception:
                pass

        seen = set()
        unique = []
        for item in results:
            data_hash = hashlib.md5(item.data).hexdigest()
            if data_hash in seen:
                continue
            seen.add(data_hash)

            x, y, w, h = 0, 0, 0, 0
            rect = getattr(item, 'rect', None)
            if rect is not None:
                x = getattr(rect, 'left', 0)
                y = getattr(rect, 'top', 0)
                w = getattr(rect, 'width', 0)
                h = getattr(rect, 'height', 0)

            unique.append(QRDecodeResult(item.data, x, y, w, h))

        return unique


class OpenCVQRDecoder(QRDecoderAdapter):
    name = 'opencv'

    def __init__(self):
        try:
            import cv2
        except ImportError as e:
            raise RuntimeError(
                "opencv backend is unavailable. Install opencv-python-headless: "
                "pip install opencv-python-headless"
            ) from e

    def decode_image(self, image_path):
        return [QRDecodeResult(**qr) for qr in decode_qr_from_image_opencv(image_path)]

def preprocess_image(img):
    """预处理图片，提高二维码检测率"""
    import cv2
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()
    
    results = [gray]
    
    # 自适应阈值
    try:
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2
        )
        results.append(adaptive)
    except:
        pass
    
    # Otsu 阈值
    try:
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        results.append(otsu)
    except:
        pass
    
    # 增强对比度
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        results.append(enhanced)
    except:
        pass
    
    # 反色
    try:
        inverted = cv2.bitwise_not(gray)
        results.append(inverted)
    except:
        pass
    
    return results


def decode_qr_from_image_opencv(image_path):
    """
    从图片中检测并解码所有二维码
    返回: [{'data': bytes, 'x': int, 'y': int, 'w': int, 'h': int}, ...]
    """
    import cv2
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Cannot read image: {image_path}")
    
    detector = cv2.QRCodeDetector()
    
    all_results = []
    seen_data = set()
    
    # 方法1: 使用 detectAndDecodeMulti
    preprocessed = preprocess_image(img)
    
    for processed_img in preprocessed:
        try:
            retval, decoded_info, points, straight_qrcode = detector.detectAndDecodeMulti(processed_img)
            
            if retval:
                for i, info in enumerate(decoded_info):
                    if info:
                        data_bytes = info.encode('utf-8')
                        data_hash = hashlib.md5(data_bytes).hexdigest()
                        
                        if data_hash not in seen_data:
                            seen_data.add(data_hash)
                            
                            # 获取边界框
                            if points is not None and i < len(points):
                                pts = points[i]
                                x = int(pts[:, 0].min())
                                y = int(pts[:, 1].min())
                                w = int(pts[:, 0].max() - x)
                                h = int(pts[:, 1].max() - y)
                            else:
                                x, y, w, h = 0, 0, 0, 0
                            
                            all_results.append({
                                'data': data_bytes,
                                'x': x,
                                'y': y,
                                'w': w,
                                'h': h,
                            })
        except Exception:
            pass
    
    # 方法2: 尝试 detectAndDecode (单个二维码)
    for processed_img in preprocessed:
        try:
            data, points, straight = detector.detectAndDecode(processed_img)
            if data:
                data_bytes = data.encode('utf-8')
                data_hash = hashlib.md5(data_bytes).hexdigest()
                
                if data_hash not in seen_data:
                    seen_data.add(data_hash)
                    
                    if points is not None and len(points) > 0:
                        pts = points
                        if len(pts.shape) == 3:
                            pts = pts[0]
                        x = int(pts[:, 0].min())
                        y = int(pts[:, 1].min())
                        w = int(pts[:, 0].max() - x)
                        h = int(pts[:, 1].max() - y)
                    else:
                        x, y, w, h = 0, 0, 0, 0
                    
                    all_results.append({
                        'data': data_bytes,
                        'x': x,
                        'y': y,
                        'w': w,
                        'h': h,
                    })
        except Exception:
            pass
    
    return all_results


def get_decoder_adapter(name):
    adapters = {
        PyzbarQRDecoder.name: PyzbarQRDecoder,
        OpenCVQRDecoder.name: OpenCVQRDecoder,
    }
    return adapters[name]()


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

def decode_single_chunk(b85_data):
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
        if isinstance(b85_data, str):
            b85_data = b85_data.encode('ascii')
        
        # base85 解码
        raw = base64.b85decode(b85_data)
        
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


def decode_and_merge_chunks(qr_data_list):
    """
    解码所有分片，合并数据

    Index 0 = 元数据 (JSON), Index 1..N = 数据分片
    返回: (merged_data, stats, metadata)
    """
    chunks = {}
    total = None
    invalid_count = 0

    for b85_data in qr_data_list:
        result = decode_single_chunk(b85_data)

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
  # 从单张照片解码
  python decoder.py photo.jpg -o restored.out

  # 从多张照片解码（二维码分布在多页时）
  python decoder.py page1.jpg page2.jpg page3.jpg -o restored.out

  # 从目录中的图片解码（默认使用 pyzbar）
  python decoder.py photos_dir -o restored.out

  # 强制使用 OpenCV 后端
  python decoder.py photos_dir -o restored.out --backend opencv

  # 如果还原出来的是一个 patch，可应用:
  patch -p1 < restored.out
"""
    )
    parser.add_argument('images', nargs='+', help='包含二维码的照片文件或目录')
    parser.add_argument('-o', '--output', default='restored.out',
                        help='输出文件 (默认: restored.out)')
    parser.add_argument('--backend', choices=['pyzbar', 'opencv'], default='pyzbar',
                        help='二维码识别后端 (默认: pyzbar)')
    parser.add_argument('--debug', action='store_true', help='显示调试信息')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("  QueQiao (鹊桥) - Decoder")
    print("=" * 60)
    print(f"  Backend: {args.backend}")
    print()

    try:
        adapter = get_decoder_adapter(args.backend)
    except RuntimeError as e:
        print(f"❌ {e}")
        print()
        print("可选方案:")
        print("  - 安装 zbar 后重试 pyzbar")
        print("  - 或临时使用: --backend opencv")
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
                data_str = qr['data'].decode('utf-8', errors='replace')
                all_qr_data.append(data_str)
                
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
        compressed_data, stats, metadata = decode_and_merge_chunks(all_qr_data)
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

    # Step 4: 确定输出文件名
    if args.output == 'restored.out' and metadata and metadata.get('filename'):
        name = metadata['filename'].replace('/', '').replace('\\', '').strip()
        if name and name not in ('.', '..'):
            args.output = name
            print(f"  📁 Using filename from metadata: {args.output}")

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
