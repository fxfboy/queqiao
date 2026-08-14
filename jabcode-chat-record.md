# QueQiao JAB Code 讨论记录

日期：2026-08-06

## 用户

写一个使用 JAB code 提升二维码容量的方案，编码和解码都使用，作为另一个 backend。

## 助手

已增加完整 JAB Code backend：

- 编码：`--backend jab`，原始二进制直接写入 JAB Code，避免 base85 膨胀。
- 解码：`--backend jab`，调用官方 `jabcodeReader`。
- JAB 默认参数：`chunk-size=3000`、8 色、纠错级别 3、单列 900px。
- 支持 `QUEQIAO_JAB_WRITER`、`QUEQIAO_JAB_READER` 指定官方工具路径。
- 保持原有 QR wire format 和默认行为兼容。
- README、Shell/Windows 帮助及模拟原生工具测试均已更新。

使用方式：

```bash
./run.sh encode input.bin -o jab.html --backend jab
./run.sh decode jab-photo.png -o restored.bin --backend jab
```

需要先构建官方参考实现：https://github.com/jabcode/jabcode 。官方 reader 每次只能读取一个完整 JAB Code，因此整页截图需要先按码裁剪。

## 用户

jabcode 对于单码的大小是怎么规定的？

## 助手

JAB Code 的“单码大小”分三层：模块矩阵大小、输出图片像素大小、实际数据容量。

官方实现用 `side-version` 决定矩阵边长：

```text
边长模块数 = 4 × version + 17
version 范围 = 1..32
```

所以单个基础符号从 Version 1 的 `21 × 21` modules 到 Version 32 的 `145 × 145` modules。图片像素尺寸由 `module-size` 决定：

```text
图片宽度 ≈ 横向模块数 × module-size
图片高度 ≈ 纵向模块数 × module-size
```

容量还取决于颜色数、纠错级别、定位和校准图案、调色板、metadata，以及内容经过 JAB 高层编码后的 bit 数。QueQiao 没有固定 `symbol-version`，官方 writer 会自动选择装得下数据的最小正方形版本。

## 用户

如果说我当前屏幕分辨率是 1920×1080，那么是不是我可以生成码的时候，一次生成一个大图，在不缩放的情况下，这一个码最大能放下多少字节？

## 助手

可以。以官方 JAB Code 实现、单个基础符号、8 色、ECC level 3、任意二进制数据计算：

- 最大版本：Version 32
- 矩阵尺寸：`145 × 145 modules`
- 1080 像素高度下最大整数模块尺寸：`floor(1080 / 145) = 7 px`
- 原始 PNG 尺寸：`145 × 7 = 1015 px`
- 单码能够保证容纳约 `4245 bytes` 原始 JAB payload
- 扣除 QueQiao 的 12 字节分片头，对应最大 `chunk-size` 约为 `4233 bytes`

接近上限时，像素级截图建议使用 `chunk-size=4000`、`module-size=7`；相机拍屏则保守一些。

## 用户

也就是说 chunk-size 4000 大约能放 4k 的文件，我要传输个 1m 大小的文件需要几个码？

## 助手

码数按 LZMA 压缩后的大小计算，并额外增加一个 metadata 码：

```text
码数 = ceil(压缩后字节数 / 4000) + 1
```

如果文件是不可压缩的 1 MiB，即 1,048,576 字节，需要 263 个数据码加 1 个 metadata 码，共 264 个。文本、源码和日志通常会因为压缩而少很多；压缩包、图片、视频等通常接近不可压缩估算。

## 用户

你来测试一下？把当前窗口的聊天记录保存成文件，然后用 4000 的 chunksize 编个码，我看看是什么样的。
