#!/usr/bin/env python3
"""profile 读写。重点是"任何脏数据都退化成保守默认，绝不抛"。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stream_profile
from stream_profile import (
    CALIBRATION_MATRIX, CONSERVATIVE_DEFAULTS, PROFILE_SCHEMA, SYNTHETIC_SIZE,
    load_profile, save_profile, synthetic_payload, validate_settings,
)


def _isolate(td):
    stream_profile.profile_path = lambda: Path(td) / 'profile.json'


def test_defaults_when_missing():
    print("[TEST] 没有 profile 时返回保守默认...")
    with tempfile.TemporaryDirectory() as td:
        orig = stream_profile.profile_path
        _isolate(td)
        try:
            settings, source = load_profile()
            assert source == 'default'
            assert settings == CONSERVATIVE_DEFAULTS
            assert settings is not CONSERVATIVE_DEFAULTS, "必须返回副本，别让调用方改到全局常量"
        finally:
            stream_profile.profile_path = orig
    print("  ✅ PASSED")


def test_save_load_roundtrip():
    print("[TEST] 写入后读回一致，且带 schema 与时间戳...")
    with tempfile.TemporaryDirectory() as td:
        orig = stream_profile.profile_path
        _isolate(td)
        try:
            p = save_profile({'blocklen': 1800, 'ecc': 'L', 'fps': 8, 'box_size': 5},
                             measurements={'decode_rate': 0.97, 'max_gap': 4})
            raw = json.loads(p.read_text())
            assert raw['schema'] == PROFILE_SCHEMA
            assert 'calibrated_at' in raw
            assert raw['measurements']['decode_rate'] == 0.97
            settings, source = load_profile()
            assert source == 'profile'
            assert settings['blocklen'] == 1800 and settings['ecc'] == 'L'
        finally:
            stream_profile.profile_path = orig
    print("  ✅ PASSED")


def test_corrupt_profile_falls_back_without_crashing():
    print("[TEST] §7.4：损坏 / 版本不符 / 字段非法 → 保守默认 + 提示，绝不崩...")
    bad = [
        '{ not json at all',
        '',
        '[]',
        '{"schema": 999, "settings": {"blocklen": 800, "ecc": "M", "fps": 6, "box_size": 6}}',
        '{"schema": 1}',
        '{"schema": 1, "settings": {"blocklen": 0, "ecc": "M", "fps": 6, "box_size": 6}}',
        '{"schema": 1, "settings": {"blocklen": 800, "ecc": "Z", "fps": 6, "box_size": 6}}',
        '{"schema": 1, "settings": {"blocklen": 800, "ecc": "M", "fps": -1, "box_size": 6}}',
        '{"schema": 1, "settings": {"blocklen": "800", "ecc": "M", "fps": 6, "box_size": 6}}',
        '{"schema": 1, "settings": {"blocklen": 999999, "ecc": "M", "fps": 6, "box_size": 6}}',
    ]
    with tempfile.TemporaryDirectory() as td:
        orig = stream_profile.profile_path
        p = Path(td) / 'profile.json'
        _isolate(td)
        try:
            for content in bad:
                p.write_text(content)
                settings, source = load_profile()
                assert source == 'default', "内容 %r 应退化成默认" % content[:40]
                assert settings == CONSERVATIVE_DEFAULTS
        finally:
            stream_profile.profile_path = orig
    print("  ✅ PASSED")


def test_validate_settings_accepts_good():
    print("[TEST] validate_settings 放行合法配置...")
    ok = validate_settings({'blocklen': 1200, 'ecc': 'Q', 'fps': 10, 'box_size': 4})
    assert ok == {'blocklen': 1200, 'ecc': 'Q', 'fps': 10, 'box_size': 4}
    assert validate_settings({'blocklen': 800}) is None, "缺字段应判非法"
    print("  ✅ PASSED")


def test_calibration_matrix_blocklens_are_distinct():
    print("[TEST] 标定矩阵的 blocklen 必须两两不同...")
    lens = [s['blocklen'] for s in CALIBRATION_MATRIX]
    assert len(lens) == len(set(lens)), (
        "接收端靠 (nonce, K, blocklen) 三元组区分档位，同一 blocklen 的两档会落进"
        "同一个桶、统计被合并。当前矩阵: %r" % lens)
    assert lens == sorted(lens), "矩阵按 blocklen 升序，报告才好读"
    for s in CALIBRATION_MATRIX:
        # CALIBRATION_MATRIX 按接口声明只有 blocklen/ecc/box_size 三个字段，
        # 不含 fps（标定时 fps 由别处统一控制，不是每档各异）。validate_settings
        # 要求四个字段齐全，所以叠加到 CONSERVATIVE_DEFAULTS 上再校验——这也正是
        # 播放时实际会用到的合并方式：矩阵条目覆盖默认值里的对应字段。
        merged = dict(CONSERVATIVE_DEFAULTS, **s)
        assert validate_settings(merged) is not None, "矩阵自身必须是合法配置: %r" % s
    print("  ✅ PASSED")


def test_synthetic_payload_is_deterministic_and_incompressible():
    print("[TEST] 合成载荷两端可各自生成同一份，且压不动...")
    import lzma
    a = synthetic_payload()
    b = synthetic_payload()
    assert a == b, "同一进程两次生成必须一致"
    assert len(a) == SYNTHETIC_SIZE
    assert a[:8].hex() != '0' * 16, "别退化成全零"
    ratio = len(lzma.compress(a, preset=9 | lzma.PRESET_EXTREME)) / len(a)
    assert ratio > 0.98, ("合成载荷必须压不动，否则各档 K 会缩水到 64 以下，"
                          "破坏 §7.4 的 K≥64 前提。实测压缩比 %.3f" % ratio)
    print("  ✅ PASSED (压缩比 %.3f)" % ratio)


def test_synthetic_payload_yields_k_at_least_64_on_every_stage():
    print("[TEST] §7.4 硬前提：每一档的 K 都 ≥ 64...")
    from stream_packet import build_payload, split_blocks
    payload, _mj, _n = build_payload('calibration.bin', synthetic_payload())
    for stage in CALIBRATION_MATRIX:
        K = len(split_blocks(payload, stage['blocklen']))
        assert K >= 64, (
            "blocklen=%d 只得 K=%d。K<64 时系统性阶段占比过高，"
            "'seed 序列缺号 = 丢帧' 的标定口径不成立。" % (stage['blocklen'], K))
    print("  ✅ PASSED")


if __name__ == '__main__':
    test_defaults_when_missing()
    test_save_load_roundtrip()
    test_corrupt_profile_falls_back_without_crashing()
    test_validate_settings_accepts_good()
    test_calibration_matrix_blocklens_are_distinct()
    test_synthetic_payload_is_deterministic_and_incompressible()
    test_synthetic_payload_yields_k_at_least_64_on_every_stage()
    print("\n✅ All profile tests passed!")
