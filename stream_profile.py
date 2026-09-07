#!/usr/bin/env python3
"""~/.queqiao/profile.json 的读写，以及标定矩阵与合成载荷的定义。

§7.4：profile 损坏或 schema 不符时**回退到保守默认并提示，绝不崩**——
它是用户主目录里的文件，可能被手改、被旧版本写过、被磁盘写坏，
但这些都不该成为 receive 起不来的理由。
"""

import json
import time
from pathlib import Path

PROFILE_SCHEMA = 1

# 保守默认：v40-M 远未触顶，6 fps 有充裕余量。宁可慢，不要标定前就传不动。
CONSERVATIVE_DEFAULTS = {
    'blocklen': 800,
    'ecc': 'M',
    'fps': 6,
    'box_size': 6,
}

# 标定矩阵。blocklen 必须**两两不同**：接收端靠 (nonce, K, blocklen) 三元组
# 区分档位，同一 blocklen 的两档会落进同一个桶、统计被合并。
# 上界依据：v40-L 的 base85 上限是 2362 raw 字节，减 16 字节头 → 2346。
CALIBRATION_MATRIX = [
    {'blocklen': 400,  'ecc': 'Q', 'box_size': 8},
    {'blocklen': 800,  'ecc': 'M', 'box_size': 6},
    {'blocklen': 1200, 'ecc': 'M', 'box_size': 5},
    {'blocklen': 1800, 'ecc': 'M', 'box_size': 5},
    {'blocklen': 2300, 'ecc': 'L', 'box_size': 4},
]

_ECC_CHOICES = ('L', 'M', 'Q', 'H')
_MAX_BLOCKLEN_HINT = 2346          # v40-L 减 16 字节头

# 合成载荷：两端各自用同一个 splitmix64 种子生成同一份字节，不需要传文件。
# 160 KiB 且不可压缩，保证矩阵最大档（blocklen=2300）下仍有 K ≥ 64——
# K 太小时系统性包占比过高，"seed 序列缺号 = 丢帧"的标定口径不成立。
SYNTHETIC_SEED = 0x5175655169616F00      # "QueQiao\0"
SYNTHETIC_SIZE = 160 * 1024


def profile_path():
    return Path.home() / '.queqiao' / 'profile.json'


def synthetic_payload():
    from fountain import splitmix64_next
    out = bytearray()
    state = SYNTHETIC_SEED
    while len(out) < SYNTHETIC_SIZE:
        state, z = splitmix64_next(state)
        out += z.to_bytes(8, 'big')
    return bytes(out[:SYNTHETIC_SIZE])


def validate_settings(raw):
    """合法则返回规范化后的新 dict，否则返回 None。任何输入都不抛。"""
    if not isinstance(raw, dict):
        return None
    try:
        blocklen = raw['blocklen']
        ecc = raw['ecc']
        fps = raw['fps']
        box_size = raw['box_size']
    except (KeyError, TypeError):
        return None
    if type(blocklen) is not int or not (1 <= blocklen <= _MAX_BLOCKLEN_HINT):
        return None
    if ecc not in _ECC_CHOICES:
        return None
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
        return None
    if type(box_size) is not int or not (1 <= box_size <= 64):
        return None
    return {'blocklen': blocklen, 'ecc': ecc, 'fps': fps, 'box_size': box_size}


def load_profile():
    """返回 (settings, source)。source 是 'profile' 或 'default'。"""
    p = profile_path()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return dict(CONSERVATIVE_DEFAULTS), 'default'
    except Exception as e:
        print("  ⚠️  %s 读取失败（%s），改用保守默认。跑一次 --calibrate 可重建。"
              % (p, e.__class__.__name__))
        return dict(CONSERVATIVE_DEFAULTS), 'default'

    if not isinstance(raw, dict):
        print("  ⚠️  %s 格式不对，改用保守默认。" % p)
        return dict(CONSERVATIVE_DEFAULTS), 'default'
    if raw.get('schema') != PROFILE_SCHEMA:
        print("  ⚠️  %s 的 schema 是 %r，本版本要求 %d。改用保守默认，"
              "跑一次 --calibrate 可重建。" % (p, raw.get('schema'), PROFILE_SCHEMA))
        return dict(CONSERVATIVE_DEFAULTS), 'default'

    settings = validate_settings(raw.get('settings'))
    if settings is None:
        print("  ⚠️  %s 里的 settings 字段非法，改用保守默认。" % p)
        return dict(CONSERVATIVE_DEFAULTS), 'default'
    return settings, 'profile'


def save_profile(settings, measurements=None):
    normalized = validate_settings(settings)
    if normalized is None:
        raise ValueError("拒绝写入非法配置: %r" % (settings,))
    p = profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema': PROFILE_SCHEMA,
        'calibrated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'settings': normalized,
        'measurements': measurements or {},
    }
    p.write_text(json.dumps(payload, ensure_ascii=True, indent=2),
                 encoding='utf-8')
    return p
