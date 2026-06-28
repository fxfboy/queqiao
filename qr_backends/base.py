"""QR backend 抽象接口。

要新增一个 backend，新建一个模块（例如 qr_backends/foo_backend.py），
继承 QRDecoderAdapter，实现 `name` 和 `decode_image()`，再到 qr_backends/__init__.py
里 import + 加进 _REGISTRY 即可。
"""
from abc import ABC, abstractmethod


class QRDecodeResult:
    """单个二维码的解码结果：payload 字节 + 边界框（仅用于排序展示）。"""

    __slots__ = ('data', 'x', 'y', 'w', 'h')

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
    """所有 backend 的抽象基类。

    子类必须：
    - 类属性 `name`: CLI 上 --backend 用的标识符 (例如 'zxing', 'pyzbar')
    - 方法 `decode_image(path)`: 返回 list[QRDecodeResult]

    构造函数可在 __init__ 里 lazy-import 自己的第三方依赖，依赖缺失时
    抛出 RuntimeError，主程序据此提示用户安装。
    """

    name: str = None

    @abstractmethod
    def decode_image(self, image_path):
        """从单张图片中检测并解码所有二维码，返回 [QRDecodeResult, ...]。"""
