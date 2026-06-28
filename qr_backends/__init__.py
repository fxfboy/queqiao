"""QR 解码后端注册表。

主程序通过 `get_backend(name)` / `available_backends()` / `DEFAULT_BACKEND`
访问，不直接 import 具体后端模块。

新增后端步骤:
    1. 在本目录新建 `foo_backend.py`，继承 base.QRDecoderAdapter 实现 decode_image
    2. 在下面 import 并把它加入 _REGISTRY
"""
from .base import QRDecoderAdapter, QRDecodeResult
from .pyzbar_backend import PyzbarQRDecoder
from .zxing_backend import ZxingQRDecoder


_REGISTRY = {
    ZxingQRDecoder.name: ZxingQRDecoder,
    PyzbarQRDecoder.name: PyzbarQRDecoder,
}

DEFAULT_BACKEND = ZxingQRDecoder.name


def available_backends():
    """返回所有已注册的 backend 名字（按注册顺序）。"""
    return list(_REGISTRY.keys())


def get_backend(name):
    """实例化指定 backend。未注册的名字抛 ValueError；依赖缺失由 backend 自己抛 RuntimeError。"""
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown backend: {name!r}. Available: {available_backends()}"
        )
    return _REGISTRY[name]()


__all__ = [
    'QRDecoderAdapter',
    'QRDecodeResult',
    'PyzbarQRDecoder',
    'ZxingQRDecoder',
    'DEFAULT_BACKEND',
    'available_backends',
    'get_backend',
]
