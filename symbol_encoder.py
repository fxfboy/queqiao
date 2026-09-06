#!/usr/bin/env python3
"""SymbolEncoder：原始包字节 → PIL Image。本轮只实现 QR。

抽象的收益就一条：将来加 JAB 流式是"新增一个实现类 + 验证风险"，
stream_encoder.py / player.py / fountain.py 一行不改。它不会让 JAB 流式
变得容易——难点在 native subprocess 和 RDP 色深，抽象碰不到那两个。

本轮不做：编码器注册表、标定轴抽象、拆包。见设计文档 §5.1。
"""

import base64
from abc import ABC, abstractmethod

from encoder import chunk_to_qr_image

# QR Version 40 在 byte 模式下各 ECC 等级的字符容量（QR 标准固定值）
QR_V40_BYTE_CAPACITY = {'L': 2953, 'M': 2331, 'Q': 1663, 'H': 1273}


def max_raw_for_base85(capacity):
    """base85 输出不超过 capacity 个字符时，能载的最大 raw 字节数。

    不能手算 ceil(n/4)*5：Python 的 base64.b85encode 对尾部不足 4 字节的
    r 字节只输出 r+1 个字符，不是补齐成 5 个。正确公式是
    5*(n//4) + (r+1 if r else 0)。v40-L 由此得 2362（早期文档写的
    2346 和 2360 都是用错公式算出来的）。
    """
    q = capacity // 5
    best = 4 * q
    for r in (1, 2, 3):
        if 5 * q + (r + 1) <= capacity:
            best = 4 * q + r
    return best


class SymbolEncoder(ABC):
    """符号学抽象。四个成员各自解决一个具体问题，没有为未来预留的空位。"""

    # CLI 标识与诊断输出用
    name = None

    # 'base85' | 'raw'，必须与接收端 adapter 的同名属性一致。
    # 发送端和接收端是两台机器、没有回程通道，无法运行时协商；错配的表现是
    # "一个包也解不出"，看起来像信道问题而不是配置问题。所以它是接口的一等成员。
    payload_encoding = None

    # 单个符号可载的 raw 字节上限（含 16 字节头）
    max_payload_bytes = None

    @abstractmethod
    def encode(self, payload):
        """payload 是完整的原始包字节（16 字节头 + data），未经任何 transport 编码。

        transport 编码（base85 与否）是实现内部的事——这从结构上消除了
        "调用方自己先 base85、实现里又编一次"的双重编码。
        """

    def check_capacity(self, blocklen):
        """启动时校验，早失败早报错（§9）。"""
        from stream_packet import HEADER_SIZE
        need = blocklen + HEADER_SIZE
        if need > self.max_payload_bytes:
            raise ValueError(
                "blocklen=%d 加 %d 字节头共 %d 字节，超出 %s 符号容量 %d。"
                "请调小 blocklen 或降低 ECC 等级。"
                % (blocklen, HEADER_SIZE, need, self.name, self.max_payload_bytes)
            )


class QRSymbolEncoder(SymbolEncoder):
    name = 'qr'
    payload_encoding = 'base85'

    def __init__(self, ecc='M', box_size=5, border=2):
        if ecc not in QR_V40_BYTE_CAPACITY:
            raise ValueError("ecc 必须是 'L'/'M'/'Q'/'H' 之一，实得 %r" % (ecc,))
        self.ecc = ecc
        self.box_size = box_size
        self.border = border
        self.max_payload_bytes = max_raw_for_base85(QR_V40_BYTE_CAPACITY[ecc])

    def encode(self, payload):
        # 这里是全项目唯一调用 chunk_to_qr_image 的流式入口。该函数内部第一件事
        # 就是 base64.b85encode(payload)，所以传进去的必须是原始包字节。
        img = chunk_to_qr_image(payload, box_size=self.box_size,
                               border=self.border, ecc=self.ecc)
        # qrcode 返回的是 PilImage wrapper，取出真 PIL Image。
        # 转 RGB 是因为 tkinter 的 ImageTk 对 '1' 模式在部分平台上表现不稳。
        pil = img.get_image() if hasattr(img, 'get_image') else img
        return pil.convert('RGB')
