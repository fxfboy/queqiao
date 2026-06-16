# QueQiao (鹊桥)

QueQiao（鹊桥）是在两个物理隔离、没有网络路径的世界之间，靠屏幕显示二维码 + 相机拍摄来摆渡**任意文件**的工具。它只负责"把文件变成二维码、再从照片还原文件"，**不关心文件怎么来的** —— 你可以传一个代码文件、一个压缩包，或一个仓库 diff（用可选的 `make_diff.py` 生成）。

内网/外网被 air gap 隔开；QueQiao 只借一束光，把字节完整送到对岸。解码结果按字节还原，目标是和输入文件一模一样。

## 原理

```
任意输入文件 (文本或二进制)
        │
        ▼
    lzma(xz) 压缩 (节省 ~75-85%)
        │
        ▼
    分片 + SHA256 校验 (每片 400 bytes)
        │
        ▼
    base85 编码 → 生成 QR 码 → HTML 页面
        │
        ▼
    📸 全屏显示 → 拍照 → 传到对端
        │
        ▼
    从照片解码 QR → 校验 + 合并 + 解压
        │
        ▼
    逐字节还原出原始文件
```

## 安装依赖

首次运行 `./run.sh`（或 `run.bat`）会自动创建 `.venv` 并安装依赖。手动安装：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install qrcode[pil] Pillow opencv-python-headless pyzbar
# pyzbar 需要本地 zbar 库：
#   macOS:  brew install zbar
#   Ubuntu: sudo apt-get install libzbar0
#   Windows: 安装 Visual C++ Redistributable
```

`./run.sh` 会在首次创建虚拟环境时尽力自动安装 zbar（macOS 使用 Homebrew，Linux 使用 apt/yum）。如果系统包管理器不可用或安装失败，请按上面的命令手动安装；也可以临时使用 `--backend opencv`。

## 使用方法

### 编码端（内网）—— 文件 → 二维码 HTML

```bash
./run.sh encode input.txt -o qr.html
# 或直接: python encoder.py input.txt -o qr.html
```

在浏览器打开 `qr.html`，按 **F11** 全屏，保持屏幕水平、亮度充足，用手机/相机拍下所有二维码。

### 解码端（外网）—— 照片 → 文件

```bash
./run.sh decode photo.jpg -o restored.out
# 多张照片:
./run.sh decode photo1.jpg photo2.jpg -o restored.out
# 或直接扫描目录中的图片:
./run.sh decode photos_dir -o restored.out
```

`decoder.py` 默认使用 pyzbar 检测二维码（识别更稳）。如果 zbar 系统库暂时不可用，可切到 OpenCV 后端：

```bash
./run.sh decode photos_dir -o restored.out --backend opencv
```

### 传输方式与 chunk-size 选择

二维码数量 = ceil(压缩后大小 / chunk-size)，只跟压缩后体积和分片大小有关。选多大的 `--chunk-size`，取决于你怎么把二维码传到对端：

| 场景 | 推荐参数 | 特点 |
|------|----------|------|
| **拍照传输** | `--chunk-size 400`（默认） | 二维码密度低、容错高，扛得住镜头畸变/对焦/反光 |
| **截图传输（推荐）** | `--chunk-size 1000~1500 --qr-size 340` | 截图像素级无损，可用更大分片，二维码数量大幅减少 |
| **截图传输（极限）** | `--chunk-size 1800 --qr-size 380` | 单码顶到 QR 最大规格 version 40，数量最少 |

- `--chunk-size` 上限约为 **1800**：再大单个二维码会超出 QR version 40 的容量，编码器报错。
- 截图场景务必把 `--qr-size` 调到 ≥ 单码原图尺寸（高密度码原图更大），否则浏览器/截图下采样会让密集模块糊掉、解不出。
- 实测一份 ~487KB 文本：拍照默认档 235 个二维码，截图极限档仅 53 个（详见 `output/RESULTS.md`）。

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
python make_diff.py /ext /int --ext .py .ts            # 只对比指定扩展名
python make_diff.py /ext /int --no-gitignore           # 不读取 .gitignore
python make_diff.py /ext /int --ignore "*.log" "tmp"   # 额外忽略模式
```

## 参数

### encoder.py
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `input` | - | 要传输的输入文件（任意文件） |
| `-o` | `qr_diff.html` | 输出 HTML 文件 |
| `--cols` | `6` | 每行二维码数量 |
| `--qr-size` | `180` | 二维码尺寸（像素） |
| `--chunk-size` | `400` | 每片数据字节数 |

### decoder.py
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `images` | - | 照片文件或目录（可多个） |
| `-o` | `restored.out` | 输出文件 |
| `--backend` | `pyzbar` | 识别后端：`pyzbar` 或 `opencv` |
| `--debug` | - | 显示调试信息 |

### make_diff.py（可选）
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
2. **数据校验**：每片 SHA256 校验
3. **顺序无关**：靠分片头部的 index 重组，与拍摄顺序/位置无关
4. **重复/缺失检测**：自动去重，缺片会明确报告
5. **多图片支持**：可分多次拍摄

## 测试

```bash
./run.sh test        # test_roundtrip.py —— 纯逻辑字节往返
./run.sh verify      # verify_full.py —— 真·渲染+pyzbar 解码往返
.venv/bin/python test_make_diff.py    # make_diff 目录对比
.venv/bin/python test_cli.py          # 三个 CLI 的子进程冒烟测试
```

## License

MIT
