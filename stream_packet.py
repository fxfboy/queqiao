#!/usr/bin/env python3
"""QueQiao v3 流式包格式：16 字节头 + 载荷布局，发送端与接收端共用。

v1/v2 的 12 字节头被手工复制在六处（encoder.py / decoder.py /
decode_pyzbar.py 和三个测试），改一次要同步六个文件。v3 只有这一份。
"""

import hashlib
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
    if not (0 <= nonce <= 0xFFFF):
        raise ValueError("nonce 必须落在 [0, 65535]，实得 %d" % nonce)
    if not (0 <= seed <= 0xFFFFFFFF):
        raise ValueError("seed 必须落在 [0, 2^32-1]，实得 %d" % seed)
    if not (1 <= K <= 0xFFFF):
        raise ValueError("K 必须落在 [1, 65535]，实得 %d" % K)
    if not (1 <= blocklen <= 0xFFFF):
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
