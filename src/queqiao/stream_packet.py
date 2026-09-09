#!/usr/bin/env python3
"""QueQiao v3 流式包格式：16 字节头 + 载荷布局，发送端与接收端共用。

v1/v2 的 12 字节头被手工复制在六处（encoder.py / decoder.py /
decode_pyzbar.py 和三个测试），改一次要同步六个文件。v3 只有这一份。
"""

import hashlib
import json
import lzma
import os
import re
import struct

MAGIC = b'QF'
HEADER_SIZE = 16
_HEAD_FMT = '>2sHIHH'          # MAGIC(2) nonce(2) seed(4) K(2) blocklen(2) = 12 字节
_HEAD_SIZE = struct.calcsize(_HEAD_FMT)
assert _HEAD_SIZE == 12

# 接收端的内存防护上界，与发送端 SymbolEncoder.max_payload_bytes 是两回事：
# 接收端面对的是任意来源的字节，不能假设发送端用了哪种符号学。
# 取值只需 >= 任何可能的发送端上限：QR 实测 2362、JAB 约 4233。
# 注意 4096 < 4233——将来实现 JAB 流式必须上调此常量，否则会拒绝合法的 JAB 包。
MAX_BLOCKLEN = 4096
MAX_TOTAL_PAYLOAD = 256 * 1024 * 1024        # K x blocklen 的显式预算


class PacketError(Exception):
    """本协议的包，但格式或校验有问题——应计入"损坏丢弃"诊断。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class StreamPacket:
    __slots__ = ('nonce', 'seed', 'K', 'blocklen', 'data', 'checksum')

    def __init__(self, nonce, seed, K, blocklen, data, checksum):
        self.nonce = nonce
        self.seed = seed
        self.K = K
        self.blocklen = blocklen
        self.data = data
        self.checksum = checksum

    @property
    def bucket_key(self):
        """会话候选桶的键（§8.6）。"""
        return (self.nonce, self.K, self.blocklen)

    def __repr__(self):
        return ('StreamPacket(nonce=0x%04X, seed=%d, K=%d, blocklen=%d)'
                % (self.nonce, self.seed, self.K, self.blocklen))


def _checksum(head, data):
    """checksum 覆盖头+data，不只是 data。

    第一版的漏洞：K 和 blocklen 一旦被误读，系统性包判定就全乱，
    而它们完全不在校验范围内。改覆盖范围不增加一个字节。
    """
    return hashlib.sha256(head + data).digest()[:4]


def pack_packet(nonce, seed, K, blocklen, data):
    """产出一个完整的 16 + blocklen 字节包。"""
    if not 0 <= nonce <= 0xFFFF:
        raise ValueError("nonce 必须落在 [0, 65535]，实得 %d" % nonce)
    if not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed 必须落在 [0, 2^32-1]，实得 %d" % seed)
    if not 1 <= K <= 0xFFFF:
        raise ValueError("K 必须落在 [1, 65535]，实得 %d" % K)
    if not 1 <= blocklen <= 0xFFFF:
        raise ValueError("blocklen 必须落在 [1, 65535]，实得 %d" % blocklen)
    if len(data) != blocklen:
        raise ValueError("data 长度必须恒等于 blocklen=%d，实得 %d"
                         % (blocklen, len(data)))
    head = struct.pack(_HEAD_FMT, MAGIC, nonce, seed, K, blocklen)
    return head + _checksum(head, data) + bytes(data)


def peek_magic(raw):
    """返回 'QF'（v3 流式）、'QR'（v1/v2 分块）或 None。

    §4.2：靠 MAGIC 自动分辨版本，不靠用户记住用哪个子命令。
    """
    if len(raw) < 2:
        return None
    head = raw[:2]
    if head == MAGIC:
        return 'QF'
    if head == b'QR':
        return 'QR'
    return None


def unpack_packet(raw):
    """解析一个包。

    返回 None  —— 不是本协议的字节（画面里的其他码），**不算损坏**。
    抛 PacketError —— 是本协议的包但坏了，调用方按 e.reason 计入诊断。
    """
    if peek_magic(raw) != 'QF':
        return None
    if len(raw) < HEADER_SIZE:
        raise PacketError('length', "QF 包不足 %d 字节头，实得 %d"
                          % (HEADER_SIZE, len(raw)))

    head = raw[:_HEAD_SIZE]
    _, nonce, seed, K, blocklen = struct.unpack(_HEAD_FMT, head)

    # 在分配任何内存之前先查硬上限。头字段理论上可表达 65535 x 65535 = 4.00 GiB，
    # 损坏包完全可能带着极端 K/blocklen——绝不照单分配。
    if K < 1:
        raise PacketError('K', "K 必须 >= 1，实得 %d" % K)
    if blocklen < 1:
        raise PacketError('blocklen', "blocklen 必须 >= 1，实得 %d" % blocklen)
    if blocklen > MAX_BLOCKLEN:
        raise PacketError('blocklen', "blocklen %d 超过 MAX_BLOCKLEN=%d"
                          % (blocklen, MAX_BLOCKLEN))
    if K * blocklen > MAX_TOTAL_PAYLOAD:
        # 冗余防线：当前 MAX_BLOCKLEN=2^12 / K≤2^16-1 下乘积上限恰好差 4096 不可达，
        # 调大 MAX_BLOCKLEN 后会活过来。test_stream_packet.py 里有它的覆盖用例。
        raise PacketError('total_payload',
                          "K x blocklen = %d 超过 MAX_TOTAL_PAYLOAD=%d"
                          % (K * blocklen, MAX_TOTAL_PAYLOAD))

    # 严格相等。只取 raw[16:] 或只检查"至少有 blocklen 字节"都会让尾随字节
    # 被静默忽略或混进 XOR，协议就不再有唯一解析。
    if len(raw) != HEADER_SIZE + blocklen:
        raise PacketError('length', "整包长度必须恒等于 %d，实得 %d"
                          % (HEADER_SIZE + blocklen, len(raw)))

    checksum = raw[_HEAD_SIZE:HEADER_SIZE]
    data = raw[HEADER_SIZE:]
    if _checksum(head, data) != checksum:
        raise PacketError('checksum', "checksum 不匹配（seed=%d）" % seed)

    return StreamPacket(nonce, seed, K, blocklen, data, checksum)


# ──────────────────────────────────────────────────────────────
# 载荷布局：payload = meta_len(4,>I) | meta_json | lzma_data
# ──────────────────────────────────────────────────────────────

PAYLOAD_VERSION = 3
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_DEFAULT_OUTPUT_NAME = 'received.out'


class PayloadError(Exception):
    """载荷校验清单（§6.6）中的任一条不过。任一条不过都拒绝写文件。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def serialize_meta(meta):
    """字节确定的 meta_json。

    nonce 和续传语义都建立在它的字节确定性上。sort_keys=True 把
    "依赖 dict 插入顺序"这个隐性依赖彻底消除——这是防演化，不是修当前 bug。
    紧凑 separators 相对默认值省 9 字节（5 字段 = 4 逗号 + 5 冒号）。
    """
    return json.dumps(meta, ensure_ascii=True, separators=(',', ':'),
                      sort_keys=True).encode('utf-8')


def derive_nonce(meta_json):
    """nonce = sha256(meta_json)[:2]，内容派生而非随机。

    同一文件重启发送端 → nonce/K/blocklen 全相同、lzma 字节一致 →
    两次的包可混用 = 天然支持中断续传。随机 nonce 反而把续传判成新会话。
    """
    return int.from_bytes(hashlib.sha256(meta_json).digest()[:2], 'big')


def build_payload(filename, file_bytes):
    """返回 (payload, meta_json, nonce)。payload 尚未补零填充。"""
    compressed = lzma.compress(file_bytes, format=lzma.FORMAT_XZ,
                               preset=9 | lzma.PRESET_EXTREME)
    meta = {
        'version': PAYLOAD_VERSION,
        'filename': filename,
        'size': len(file_bytes),
        'sha256': hashlib.sha256(file_bytes).hexdigest(),
        'compressed_size': len(compressed),
    }
    meta_json = serialize_meta(meta)
    payload = struct.pack('>I', len(meta_json)) + meta_json + compressed
    return payload, meta_json, derive_nonce(meta_json)


def split_blocks(payload, blocklen):
    """补零填充到 K x blocklen 再切成 K 个等长源块（XOR 要求各块等长）。"""
    if blocklen < 1:
        raise ValueError("blocklen 必须 >= 1")
    K = max(1, -(-len(payload) // blocklen))       # ceil，空 payload 也至少 1 块
    padded = payload.ljust(K * blocklen, b'\x00')
    return [padded[i * blocklen:(i + 1) * blocklen] for i in range(K)]


def _reject_constant(value):
    raise PayloadError('meta_fields', "meta_json 含非法常量 %s" % value)


def _validate_meta(meta):
    if not isinstance(meta, dict):
        raise PayloadError('meta_json', "meta_json 顶层必须是对象")
    if meta.get('version') != PAYLOAD_VERSION:
        raise PayloadError('version', "version 必须是 %d，实得 %r"
                           % (PAYLOAD_VERSION, meta.get('version')))
    if not isinstance(meta.get('filename'), str):
        raise PayloadError('meta_fields', "filename 必须是字符串")
    for field in ('size', 'compressed_size'):
        v = meta.get(field)
        # bool 是 int 的子类，必须显式排除
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise PayloadError('meta_fields', "%s 必须是非负整数，实得 %r" % (field, v))
    sha = meta.get('sha256')
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        raise PayloadError('meta_fields', "sha256 必须匹配 ^[0-9a-f]{64}$，实得 %r" % (sha,))


def parse_payload(assembled, K, blocklen):
    """走完整份 §6.6 校验清单。返回 (meta, file_bytes)。任一条不过抛 PayloadError。"""
    total = K * blocklen
    if len(assembled) != total:
        raise PayloadError('length', "拼装长度必须等于 K x blocklen = %d，实得 %d"
                           % (total, len(assembled)))
    if total < 4:
        raise PayloadError('meta_len', "载荷不足 4 字节的 meta_len 前缀")

    meta_len = struct.unpack('>I', assembled[:4])[0]
    if meta_len > total - 4:
        raise PayloadError('meta_len', "meta_len %d 超出可用空间 %d"
                           % (meta_len, total - 4))

    meta_blob = assembled[4:4 + meta_len]
    try:
        meta_text = meta_blob.decode('utf-8')
    except UnicodeDecodeError as e:
        raise PayloadError('meta_utf8', "meta_json 不是合法 UTF-8") from e
    try:
        # json.loads 默认接受 NaN/Infinity/-Infinity，必须用钩子拒绝
        meta = json.loads(meta_text, parse_constant=_reject_constant)
    except PayloadError:  # pylint: disable=try-except-raise  # PayloadError 是 ValueError 子类，必须先挡住再让下面兜底
        raise
    except ValueError as e:
        raise PayloadError('meta_json', "meta_json 不是合法 JSON: %s" % e) from e

    _validate_meta(meta)

    compressed_size = meta['compressed_size']
    if compressed_size > total - 4 - meta_len:
        raise PayloadError('compressed_size',
                           "compressed_size %d 超出可用空间 %d"
                           % (compressed_size, total - 4 - meta_len))

    end = 4 + meta_len + compressed_size
    compressed = assembled[4 + meta_len:end]

    # 尾部填充全零。它不补正确性缺口（填充区污染会被精确截掉，SHA256 照样过），
    # 真实收益是把一次静默的疑似误接受转化为可计数、可重置的诊断事件。
    tail = assembled[end:]
    if tail != b'\x00' * len(tail):
        raise PayloadError('padding', "尾部填充区 %d 字节非全零（疑似误接受）" % len(tail))

    try:
        file_bytes = lzma.decompress(compressed, format=lzma.FORMAT_XZ)
    except lzma.LZMAError as e:
        # 绝不复用 decoder.py:362 的"当明文写出去"策略——那与 v3 要求正好相反
        raise PayloadError('lzma', "lzma 解压失败，拒绝写文件: %s" % e) from e

    if len(file_bytes) != meta['size']:
        raise PayloadError('size', "解压后长度 %d != meta['size'] %d"
                           % (len(file_bytes), meta['size']))
    actual = hashlib.sha256(file_bytes).hexdigest()
    if actual != meta['sha256']:
        raise PayloadError('sha256', "解压后 SHA256 不匹配（期望 %s，实得 %s）"
                           % (meta['sha256'], actual))
    return meta, file_bytes


def safe_output_name(filename):
    """只取 basename，挡住路径穿越；空/危险名回落到安全默认名。"""
    name = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    name = os.path.basename(name).strip()
    if not name or name in ('.', '..'):
        return _DEFAULT_OUTPUT_NAME
    return name
