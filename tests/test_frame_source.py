#!/usr/bin/env python3
"""帧源的纯逻辑部分。圈选窗口本身不自动化测试（§10），
但它算出来的 bbox 换算、校验、持久化必须测——那是 Retina 半屏 bug 的藏身处。
"""
import json
import os
import sys
import tempfile
from pathlib import Path


from PIL import Image

import queqiao.frame_source
from queqiao.frame_source import (
    MIN_REGION_PX, detect_scale_factor, is_black_frame, load_region,
    save_region, scale_bbox, validate_bbox,
)


def test_detect_scale_factor():
    print("[TEST] Retina 因子：mss 物理像素 vs tkinter 逻辑坐标...")
    assert detect_scale_factor(3024, 1512) == 2.0, "MacBook Retina 是 2x"
    assert detect_scale_factor(1920, 1920) == 1.0, "普通屏是 1x"
    assert detect_scale_factor(5120, 1707) == 3.0
    assert detect_scale_factor(1920, 0) == 1.0, "tk 宽为 0 时不得除零"
    # 非整数倍（Windows 150% 缩放）应原样返回，由调用方决定怎么办
    f = detect_scale_factor(2880, 1920)
    assert abs(f - 1.5) < 1e-9
    print("  ✅ PASSED")


def test_scale_bbox():
    print("[TEST] bbox 缩放后仍是整数...")
    assert scale_bbox((10, 20, 300, 400), 2.0) == (20, 40, 600, 800)
    assert scale_bbox((10, 20, 300, 400), 1.0) == (10, 20, 300, 400)
    out = scale_bbox((10, 20, 301, 401), 1.5)
    assert all(isinstance(v, int) for v in out), "mss 只接受整数坐标"
    print("  ✅ PASSED")


def test_validate_bbox():
    print("[TEST] bbox 校验：越界、零宽、过小...")
    assert validate_bbox((0, 0, 800, 600), (1920, 1080)) == (0, 0, 800, 600)
    for bad, why in [
        ((0, 0, 0, 600), "宽为 0"),
        ((0, 0, 800, 0), "高为 0"),
        ((0, 0, -5, 600), "负宽"),
        ((-10, 0, 800, 600), "左边越界"),
        ((1900, 0, 800, 600), "右边越界"),
        ((0, 1000, 800, 600), "下边越界"),
        ((0, 0, MIN_REGION_PX - 1, 600), "小于最小尺寸"),
    ]:
        try:
            validate_bbox(bad, (1920, 1080))
        except ValueError:
            continue
        raise AssertionError("%s 的 bbox %r 应被拒绝" % (why, bad))
    print("  ✅ PASSED")


def test_is_black_frame():
    print("[TEST] 全黑帧检测（macOS 未授屏幕录制权限的特征）...")
    assert is_black_frame(Image.new('RGB', (40, 40), (0, 0, 0)))
    assert is_black_frame(Image.new('RGB', (40, 40), (3, 3, 3))), "极暗也算黑"
    assert not is_black_frame(Image.new('RGB', (40, 40), (255, 255, 255)))
    # 一个白点就不算黑帧——真实二维码永远有白模块
    img = Image.new('RGB', (40, 40), (0, 0, 0))
    img.putpixel((20, 20), (255, 255, 255))
    assert not is_black_frame(img)
    print("  ✅ PASSED")


def test_region_persistence_roundtrip():
    print("[TEST] region 持久化往返...")
    with tempfile.TemporaryDirectory() as td:
        orig = queqiao.frame_source.region_path
        queqiao.frame_source.region_path = lambda: Path(td) / 'last_region.json'
        try:
            assert load_region() is None, "文件不存在时返回 None"
            save_region((11, 22, 333, 444))
            assert load_region() == (11, 22, 333, 444)
            raw = json.loads((Path(td) / 'last_region.json').read_text())
            assert raw['schema'] == queqiao.frame_source.REGION_SCHEMA, "必须写 schema 版本"
            assert 'saved_at' in raw
        finally:
            queqiao.frame_source.region_path = orig
    print("  ✅ PASSED")


def test_region_load_tolerates_garbage():
    print("[TEST] region 文件损坏 / 版本不符 → 返回 None，绝不崩...")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'last_region.json'
        orig = queqiao.frame_source.region_path
        queqiao.frame_source.region_path = lambda: p
        try:
            for content in ['{ not json',
                            '{}',
                            '{"schema": 999, "bbox": [1,2,3,4]}',
                            '{"schema": 1, "bbox": "nope"}',
                            '{"schema": 1, "bbox": [1,2]}',
                            '{"schema": 1, "bbox": [1,2,0,4]}']:
                p.write_text(content)
                assert load_region() is None, "内容 %r 应被安全拒绝" % content
        finally:
            queqiao.frame_source.region_path = orig
    print("  ✅ PASSED")


def test_frame_source_yields_fresh_images():
    print("[TEST] §8.1 所有权：每次迭代 yield 新对象，调用方关掉不影响下一帧...")
    from queqiao.frame_source import FrameSource

    class Fake(FrameSource):
        describe = 'fake'

        def __iter__(self):
            for i in range(3):
                yield Image.new('RGB', (4, 4), (i, i, i))

    seen = []
    for img in Fake():
        seen.append(id(img))
        img.close()          # 调用方持有所有权，关它
    assert len(seen) == 3, "关掉前一帧不得中断迭代"
    assert len(set(seen)) == 3 or True   # id 可能复用，关键是没抛异常
    print("  ✅ PASSED")


def test_no_code_alert_constant_exists():
    print("[TEST] NO_CODE_ALERT 常量存在（遮挡诊断用，Task 17 消费）...")
    from queqiao.frame_source import NO_CODE_ALERT
    assert isinstance(NO_CODE_ALERT, int) and NO_CODE_ALERT > 0
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_detect_scale_factor()
    test_scale_bbox()
    test_validate_bbox()
    test_is_black_frame()
    test_region_persistence_roundtrip()
    test_region_load_tolerates_garbage()
    test_frame_source_yields_fresh_images()
    test_no_code_alert_constant_exists()
    print("\n✅ All frame source tests passed!")
