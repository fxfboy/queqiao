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
    - 方法 `decode_image(source)`: 返回 list[QRDecodeResult]

    子类还可以覆盖 `payload_encoding`：'base85'（默认）或 'raw'。
    返回原始 chunk 字节的 backend **必须**覆盖它，否则头解析会在垃圾上失败。

    构造函数可在 __init__ 里 lazy-import 自己的第三方依赖，依赖缺失时
    抛出 RuntimeError，主程序据此提示用户安装。
    """

    name: str = None
    payload_encoding: str = 'base85'

    def _as_image(self, source):
        """接受路径或 PIL Image，返回 (image, should_close)。

        - source 是路径 → 自己 open，返回 (image, True)
        - source 是 PIL Image → **借用**，返回 (source, False)

        调用方必须把整个 decode 流程包进 try/finally，**仅当 should_close 为
        True 时**才 close。传进来的 PIL Image 是借用的：关掉它会让调用方的
        下一帧作废（流式路径每帧复用同一个抓屏对象）。
        """
        from PIL import Image

        if isinstance(source, Image.Image):
            return source, False
        try:
            return Image.open(source), True
        except Exception as e:
            raise ValueError(f"Cannot read image: {source!r}") from e

    @abstractmethod
    def decode_image(self, source):
        """从单张图片中检测并解码所有二维码，返回 [QRDecodeResult, ...]。

        source 是文件路径或 PIL Image（见 _as_image）。
        """
