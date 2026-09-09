# QueQiao (鹊桥)

QueQiao（鹊桥）是在两个物理隔离、没有网络路径的世界之间，靠屏幕显示二维码 + 相机拍摄来摆渡**任意文件**的工具。它只负责"把文件变成二维码、再从照片还原文件"，**不关心文件怎么来的** —— 你可以传一个代码文件、一个压缩包，或一个仓库 diff（用可选的 `make_diff.py` 生成）。

内网/外网被 air gap 隔开；QueQiao 只借一束光，把字节完整送到对岸。解码结果按字节还原，目标是和输入文件一模一样。

## 原理

```
任意输入文件 (文本或二进制)
        │
        ▼
    lzma(xz) 压缩 (preset 9 + EXTREME)
        │
        ▼
    元数据片 (index 0: 文件名/大小/SHA256) + 数据分片 (1..N, 每片默认 800 bytes)
        │
        ▼
    12 字节头部 (magic + index + total + datalen + SHA256[:4])
        │
        ▼
    QR: base85 编码 → QR 码；JAB: 原始二进制 → 彩色 JAB Code
        → HTML 页面 (自动打开浏览器)
        │
        ▼
    📸 全屏显示 → 拍照/截图 → 传到对端
        │
        ▼
    从照片解码 QR → 校验 + 合并 + 解压 + 整文件 SHA256 验证
        │
        ▼
    逐字节还原出原始文件
```

## 安装

**方式一：独立安装（推荐，无需本仓库）**

```bash
pipx install queqiao[pyzbar]      # 或 pip install queqiao[pyzbar]
queqiao --help                    # 统一入口，五个子命令
# pyzbar 需要本地 zbar 库：
#   macOS:  brew install zbar     （并 export DYLD_LIBRARY_PATH=/opt/homebrew/lib）
#   Ubuntu: sudo apt-get install libzbar0
#   Windows: 安装 Visual C++ Redistributable
```

**方式二：克隆仓库开发运行**

依赖由 `pyproject.toml` 声明，用 [uv](https://docs.astral.sh/uv/) 管理。首次运行 `./run.sh`（或 `run.bat`）会自动创建 `.venv` 并 `uv sync --extra pyzbar` 同步依赖（仓库源码以 editable 方式安装）。手动安装：

```bash
# 先装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh   （或 brew install uv）
uv sync --extra pyzbar            # 创建 .venv 并安装依赖
```

JAB Code 是可选 backend，使用其官方 C 参考实现。分别构建
`jabcodeWriter` 和 `jabcodeReader` 后放入 `PATH`；也可以通过
`QUEQIAO_JAB_WRITER` / `QUEQIAO_JAB_READER` 指向可执行文件。源码与构建说明：
<https://github.com/jabcode/jabcode>。这些原生工具不属于 Python 包，`uv sync`
不会安装它们。

`./run.sh` 会在首次创建虚拟环境时尽力自动安装 zbar（macOS 使用 Homebrew，Linux 使用 apt/yum）。如果系统包管理器不可用或安装失败，可以按上面的命令手动安装；不想装 zbar 也行——默认后端 `zxing` 是纯 wheel，开箱即用。

## 使用方法

### 使用入口说明

下面所有示例用 `./run.sh <子命令>`（仓库内开发运行，Windows 用 `run.bat`）。独立安装的用户把 `./run.sh` 换成 `queqiao` 即可，例如 `queqiao encode input.txt -o qr.html`。

### 编码端（内网）—— 文件 → 二维码 HTML

```bash
./run.sh encode input.txt -o qr.html
# 不指定 -o 时自动生成: output/qr-{chunk_size}-{时间戳}.html
# 生成后自动在浏览器中打开；加 --no-open 可跳过
```

浏览器打开后按 **F11** 全屏，保持屏幕水平、亮度充足，用手机/相机拍下所有二维码（或直接截图）。

### JAB Code 高容量模式

JAB Code 用 8 色模块承载原始二进制分片，省掉 QR 路径约 25% 的 base85
文本膨胀，并利用彩色模块提高单码容量。编码和解码必须成对选择 JAB backend：

```bash
./run.sh encode input.bin -o jab.html --backend jab
./run.sh decode jab-photo.png -o restored.bin --backend jab
```

JAB 默认使用 `chunk-size=3000`、单列 900px 展示、8 色和纠错级别 3；可用
`--jab-colors`、`--jab-module-size`、`--jab-ecc-level` 调整。官方 reader 一次
读取一幅完整 JAB Code，不提供一张照片中的多码检测，因此每个输入图片应只
包含一个码。对 HTML 整页截图时，需要先按码裁剪，再把所有裁剪图交给 decoder。
拍照场景对色准、白平衡和显示器色彩表现比黑白 QR 更敏感，建议先做小文件实测。

### 解码端（外网）—— 照片 → 文件

```bash
./run.sh decode photo.jpg
# 多张照片:
./run.sh decode photo1.jpg photo2.jpg
# 或直接扫描目录中的图片:
./run.sh decode photos_dir
# 不传 -o 时，解码器会用元数据里的原始文件名；找不到时回退为 restored.out
# 想强制指定输出名:
./run.sh decode photos_dir -o my_output.bin
```

`queqiao decode` 默认使用 zxing-cpp 检测二维码（纯 wheel、无系统库依赖，pixel-perfect 场景比 pyzbar 快约 10×）。如果想用 pyzbar 后端：

```bash
./run.sh decode photos_dir --backend pyzbar
```

JAB Code 图片使用 `--backend jab`，并要求安装上述官方 reader。

### 流式传输（喷泉码）—— 屏幕实时摆渡

除了一次性拍照/截图往返，还内置喷泉码（fountain code）流式模式：发送端循环播放 QR 帧流，接收端持续抓屏直到收齐。丢帧靠喷泉码冗余自动补齐，不需要人工清点二维码数量。

```bash
# 发送端（内网）: 打开置顶播放窗，循环发包，收端收齐后按 Esc 停止
./run.sh stream file.bin            # 可调 --blocklen / --ecc L|M|Q|H / --fps / --box-size

# 接收端（外网）: 圈选播放窗所在屏幕区域，收齐自动写入
./run.sh receive                    # -o 指定输出路径；默认用发送端文件名写到当前目录
```

接收端说明：

- **多显示器**：首次圈选时每块屏幕各铺一层半透明遮罩，在任意一块屏上拖框即可（副屏、负坐标排布都支持），多屏时每层遮罩会标注"屏幕 N/M"。
- 圈选结果存 `~/.queqiao/last_region.json`，下次自动复用；`--reselect` 强制重新圈选。
- macOS 需要在「系统设置 → 隐私与安全性 → 屏幕录制」给当前终端授权，否则抓到的帧是纯黑。
- 收齐并通过整文件 SHA256 校验后才写文件；Ctrl-C 随时中止。
- 标定模式（可选）：`./run.sh stream --calibrate` 配合 `./run.sh receive --calibrate`，实测各档参数（blocklen/ecc/box-size）的单帧解出率，自动选出最优档并保存，后续 stream/receive 直接继承。

### 传输方式与 chunk-size 选择

二维码数量 = ceil(压缩后大小 / chunk-size)，只跟压缩后体积和分片大小有关。选多大的 `--chunk-size`，取决于你怎么把二维码传到对端：

| 场景 | 推荐参数 | 特点 |
|------|----------|------|
| **拍照传输** | `--chunk-size 800`（默认） | 二维码密度适中、容错好，扛得住镜头畸变/对焦/反光 |
| **截图传输（推荐）** | `--chunk-size 1000~1500 --qr-size 340` | 截图像素级无损，可用更大分片，二维码数量大幅减少 |
| **截图传输（极限）** | `--chunk-size 1800 --qr-size 380` | 单码顶到 QR 最大规格 version 40，数量最少 |

- `--chunk-size` 上限约为 **1800**：再大单个二维码会超出 QR version 40 的容量，编码器报错。
- 截图场景务必把 `--qr-size` 调到 ≥ 单码原图尺寸（高密度码原图更大），否则浏览器/截图下采样会让密集模块糊掉、解不出。
- 实测一份 ~487KB 文本：默认档 (`chunk-size 800`) 约 118 个二维码，截图极限档 (`chunk-size 1800`) 仅 53 个。

### 可选：用 `make_diff.py` 生成仓库 diff

如果你要传输的是"两个仓库之间的差异"，先生成一个 diff 文件，再把它当普通文件编码：

```bash
./run.sh diff /path/to/external-repo /path/to/internal-repo -o changes.patch
./run.sh encode changes.patch -o qr.html
# 解码后应用:
cd /path/to/repo && patch -p1 < restored.out
```

`make_diff.py` 支持文件过滤：

```bash
uv run python -m queqiao.make_diff /ext /int --ext .py .ts     # 只对比指定扩展名
uv run python -m queqiao.make_diff /ext /int --no-gitignore    # 不读取 .gitignore
uv run python -m queqiao.make_diff /ext /int --ignore "*.log" "tmp"  # 额外忽略模式
```

## 参数

### queqiao encode
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `input` | - | 要传输的输入文件（任意文件） |
| `-o` | `output/qr-{chunk_size}-{时间戳}.html` | 输出 HTML 文件 |
| `--cols` | QR `6` / JAB `1` | 每行码数量 |
| `--qr-size` | QR `180` / JAB `900` | HTML 中码图尺寸（像素） |
| `--chunk-size` | QR `800` / JAB `3000` | 每片数据字节数 |
| `--backend` | `qr` | 码制：`qr` 或 `jab` |
| `--jab-colors` | `8` | JAB Code 颜色数：4 或 8 |
| `--jab-module-size` | `12` | JAB Code 模块像素数 |
| `--jab-ecc-level` | `3` | JAB Code 纠错级别 1-10 |
| `--no-open` | - | 生成后不自动打开浏览器 |

### queqiao decode
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `images` | - | 照片文件或目录（可多个） |
| `-o` | 元数据中的 filename，找不到时为 `restored.out` | 显式传任意值都按字面写入，不再读元数据 |
| `--backend` | `zxing` | `zxing` / `pyzbar`（QR）或 `jab`（JAB Code） |
| `--debug` | - | 显示调试信息 |

### queqiao diff（可选）
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `dir1` | - | 基准目录（外网仓库） |
| `dir2` | - | 目标目录（内网仓库） |
| `-o` | `diff.patch` | 输出 diff 文件 |
| `--ext` | - | 只包含指定扩展名 |
| `--no-gitignore` | - | 不使用 .gitignore 规则 |
| `--ignore` | - | 额外的忽略模式 |

## 容错机制

1. **QR 纠错**：M 级别（可纠 ~15% 错误）
2. **分片校验**：每片 12 字节头部含 SHA256 前 4 字节校验，不通过则丢弃
3. **元数据片 (index 0)**：携带文件名、原始大小和整文件 SHA256；解码端据此验证还原结果按字节一致
4. **顺序无关**：靠分片头部的 index 重组，与拍摄顺序/位置无关
5. **重复/缺失检测**：自动去重，缺片会明确报告
6. **多图片支持**：可分多次拍摄，所有图片中的二维码合并到同一个池

## 测试

```bash
./run.sh test        # tests/test_roundtrip.py —— 纯逻辑字节往返
./run.sh verify      # tests/verify_full.py —— 真·渲染+pyzbar 解码往返
uv run python tests/test_jab_backend.py # JAB CLI 适配层测试（用模拟原生工具）
uv run python tests/run_all.py        # 顺序跑完全部测试套件（推荐）
uv run python tests/test_cli.py       # CLI 子进程冒烟测试；含 pyzbar 用例，需可用 zbar
uv run python tests/test_make_diff.py # make_diff 目录对比
```

## 扩展：新增二维码解码后端

所有解码后端都在 `src/queqiao/qr_backends/` 目录下，每个后端是一个独立模块，注册在 `qr_backends/__init__.py` 的 `_REGISTRY` 里。`queqiao decode` 只通过 `get_backend(name)` / `available_backends()` / `DEFAULT_BACKEND` 这三个 API 访问注册表，不感知具体后端。

加一个新后端两步：

**1. 新建 `src/queqiao/qr_backends/<name>_backend.py`**，继承 `QRDecoderAdapter`：

```python
# src/queqiao/qr_backends/foo_backend.py
from PIL import Image
from .base import QRDecoderAdapter, QRDecodeResult

class FooQRDecoder(QRDecoderAdapter):
    name = 'foo'    # CLI 上 --backend 用的标识符

    def __init__(self):
        try:
            import foolib                          # 第三方依赖 lazy-import
        except ImportError as e:
            raise RuntimeError("foo backend 不可用，请 pip install foolib") from e
        self.foolib = foolib

    def decode_image(self, image_path):
        # 返回 list[QRDecodeResult]；每个 result 含 data (bytes) + 可选边界框 (x,y,w,h)
        img = Image.open(image_path)
        codes = self.foolib.scan(img)
        return [QRDecodeResult(c.payload, c.x, c.y, c.w, c.h) for c in codes]
```

**2. 在 `src/queqiao/qr_backends/__init__.py` 里 import 并加入 `_REGISTRY`**：

```python
from .foo_backend import FooQRDecoder

_REGISTRY = {
    ZxingQRDecoder.name: ZxingQRDecoder,
    PyzbarQRDecoder.name: PyzbarQRDecoder,
    FooQRDecoder.name: FooQRDecoder,   # 新增
}
```

完成。`queqiao decode --backend foo` 自动可用，`--help` 里也会列出。如要把它设为默认，把 `DEFAULT_BACKEND = FooQRDecoder.name`。

性能/准确率对比可以丢给 `tests/bench_backends.py`：在 `BACKENDS` 列表里加上 `(FooQRDecoder, 'foo')`，跑一次就能对比 pixel-perfect 速度和退化场景下的鲁棒性。

## License

MIT
