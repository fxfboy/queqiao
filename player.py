#!/usr/bin/env python3
"""QueQiao v3 发送端：tkinter 播放窗。

用法:
    python player.py <file> [--blocklen N] [--ecc L|M|Q|H] [--fps N]

GUI 层薄到只剩"把图贴上去"——帧率节拍、尺寸计算、状态文本全在下面的纯函数里，
由 test_stream_lifecycle.py 覆盖。tkinter 部分不做自动化测试（§10）。
"""

import argparse
import base64
import io
import os
import signal
import sys
import time
from pathlib import Path


def ensure_tcl_env():
    """在 import tkinter 之前把 TCL_LIBRARY/TK_LIBRARY 补上。返回补的路径或 None。

    uv 装的 python-build-standalone **打包了** tcl8.6/tk8.6，但 Tcl 是按
    `sys.prefix/lib/tcl8.6` 自动搜索 init.tcl 的；在 .venv 里 sys.prefix 指向
    venv 目录，那底下没有 lib/tcl8.6，于是 `import tkinter` 直接抛
    `_tkinter.TclError: Can't find a usable init.tcl`。库就在 sys.base_prefix 下。

    三条边界：
    - 已有 TCL_LIBRARY（用户或 run.sh 显式设过）就原样不动，不覆盖。
    - 不在 venv 里（base_prefix == prefix）说明是系统解释器，它自己知道路径。
    - 探测不到就静默返回 None——系统 python 和多数 Linux 发行版本来就是对的，
      在这里报错只会把一个不存在的问题喊出来。
    """
    if os.environ.get('TCL_LIBRARY'):
        return None
    base = getattr(sys, 'base_prefix', sys.prefix)
    if base == sys.prefix:
        return None
    for ver in ('8.6', '8.5'):
        tcl = os.path.join(base, 'lib', 'tcl' + ver)
        tk_dir = os.path.join(base, 'lib', 'tk' + ver)
        if os.path.exists(os.path.join(tcl, 'init.tcl')):
            os.environ['TCL_LIBRARY'] = tcl
            if os.path.isdir(tk_dir):
                os.environ['TK_LIBRARY'] = tk_dir
            return tcl
    return None


ensure_tcl_env()

import tkinter as tk          # noqa: E402 - 必须在 ensure_tcl_env() 之后

from stream_encoder import DEFAULT_BLOCKLEN, GeneratorError, StreamEncoder  # noqa: E402
from symbol_encoder import QRSymbolEncoder  # noqa: E402

DEFAULT_FPS = 6

# QR 标准要求 quiet zone 4 个模块。chunk_to_qr_image 的默认 border=2 是给
# HTML 网格用的；RDP 压缩下留足边距更稳，所以流式路径显式用 4。
STREAM_BORDER = 4

DEFAULT_BOX_SIZE = 6

END_BG = '#8b0000'
END_FG = '#ffffff'


def image_to_tk_data(img):
    """PIL Image → base64 PNG 字节，喂给 `tk.PhotoImage(data=...)`。

    **不用 `PIL.ImageTk`**：Pillow 的官方 macOS wheel 不带 `_imagingtk` C 扩展
    （构建时没有 Tk 头文件），`ImageTk.PhotoImage()` 抛
    `invalid command name "PyImagingPhoto"`。Tk 8.6 原生认 PNG，这条路零 C 扩展
    依赖，实测 625x625 的图 Tk 侧解码 2.3 ms。

    PPM 不行——Tk 不认 PIL 写出的 P6；GIF 可行但是 256 色调色板，语义上不该用来
    承载"精确的黑白模块"。
    """
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue())


def interval_ms_for_fps(fps):
    """帧率 → after 的毫秒节拍。6 fps → 167 ms。"""
    if fps <= 0:
        raise ValueError("fps 必须为正，实得 %r" % (fps,))
    return max(1, int(round(1000.0 / fps)))


def fit_box_size(modules, screen_px, border=STREAM_BORDER):
    """给定模块数和可用像素，返回最大的**整数**每模块像素数。

    §7.2：窗口尺寸取模块数的整数倍，不让任何一层做非整数缩放——
    非整数缩放会把模块边界糊掉，RDP 压缩之后更糊。
    """
    total_modules = modules + 2 * border
    box = screen_px // total_modules
    if box < 1:
        raise ValueError(
            "屏幕只有 %d 像素，装不下 %d 个模块（含 quiet zone）。"
            "请调小 --blocklen 或换更大的屏幕。" % (screen_px, total_modules)
        )
    return box


def format_status(packets_sent, elapsed, K):
    fps = packets_sent / elapsed if elapsed > 0 else 0.0
    return "已发 %d 包 │ K=%d │ %.1f fps │ Esc 停止" % (packets_sent, K, fps)


def enable_dpi_awareness():
    """§7.2：不声明 DPI 感知，Windows 会按位图拉伸窗口，模块边界直接糊掉。"""
    if sys.platform != 'win32':
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # Win 8.1+
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()       # Vista+ 回退
        except Exception:
            pass


class PlayerWindow:

    def __init__(self, encoder, fps=DEFAULT_FPS):
        self.encoder = encoder
        self.interval = interval_ms_for_fps(fps)
        self.started_at = None
        self._after_id = None
        self._photo = None          # 必须挂在长生命周期对象上，否则被 Tk 回收成空白
        self._stopping = False
        self._error = None

        self.root = tk.Tk()
        self.root.title("QueQiao 鹊桥 — 流式发送")
        self.root.configure(bg='white')
        self.root.resizable(False, False)
        self.label = tk.Label(self.root, bg='white', bd=0, highlightthickness=0)
        self.label.pack()
        self.status = tk.Label(self.root, bg='white', fg='#333',
                               font=('TkDefaultFont', 11))
        self.status.pack(fill='x')

        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.bind('<Escape>', lambda _e: self.on_close())

    # ── 主循环 ────────────────────────────────────────────────

    def tick(self):
        """**无条件重新调度自己**（唯一例外：正在关窗）。

        after 链是 Tk 的 C 循环唯一周期性回到 Python 解释器的通路。链一断，
        §7.5 的 SIGINT handler 将永远不被执行——不是延迟变大，是彻底失效。
        所以队列空（正常的瞬态）绝不能提前 return。
        """
        self._after_id = None
        if self._stopping:
            return
        try:
            frame = self.encoder.get_frame()
        except GeneratorError as e:
            self._error = e
            self.show_end(error=e)
            return
        if frame is not None:
            try:
                # 引用必须挂在 self 上：Tk 只对 PhotoImage 存弱引用，
                # 局部变量一出作用域画面就变空白。
                self._photo = tk.PhotoImage(data=image_to_tk_data(frame.image))
            finally:
                frame.image.close()     # FrameSource/Frame 的所有权在调用方
            self.label.configure(image=self._photo)
            self.status.configure(text=format_status(
                self.encoder.packets_sent,
                time.monotonic() - self.started_at,
                self.encoder.K,
            ))
        self._after_id = self.root.after(self.interval, self.tick)

    def show_end(self, error=None):
        """END 画面：红底大字 + 已发包数。不再调度 tick。"""
        self._stopping = True
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.encoder.stop()
        text = "已停止\n已发 %d 包" % self.encoder.packets_sent
        if error is not None:
            text = "生成失败\n%s" % (error.__cause__ or error)
        self.label.configure(image='', text=text, bg=END_BG, fg=END_FG,
                             font=('TkDefaultFont', 40, 'bold'),
                             width=16, height=5)
        self.status.configure(text="按 Esc 或关闭窗口退出", bg=END_BG, fg=END_FG)
        self.root.configure(bg=END_BG)
        self.root.bind('<Escape>', lambda _e: self.root.destroy())
        self.root.protocol('WM_DELETE_WINDOW', self.root.destroy)

    def on_close(self):
        """① close 回调只做三件事：置 stop、取消 after、destroy。**不在这里 join。**

        这个回调跑在 Tk 事件循环线程上，join 会阻塞事件循环本身。
        """
        self._stopping = True
        self.encoder.stop()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()

    def run(self):
        # SIGINT 转成与关窗同一条停止路径，不让 KeyboardInterrupt 直接穿透
        # 留下未 join 的生成线程。handler 只在字节码间隙执行，而 mainloop 是
        # C 循环——它依赖上面 tick 的 after 节拍把控制权交回解释器。
        try:
            signal.signal(signal.SIGINT, lambda _s, _f: self.on_close())
        except ValueError:
            pass                # 不在主线程时（不该发生），忽略
        self.started_at = time.monotonic()
        self.encoder.start()
        self._after_id = self.root.after(0, self.tick)
        try:
            self.root.mainloop()
        finally:
            # join 放在 mainloop 返回之后，不在 close 回调里
            self.encoder.stop()
            if not self.encoder.join():
                print("⚠️  生成线程未在超时内结束，放弃等待并继续退出。")
        if self._error is not None:
            raise self._error


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='stream',
        description='QueQiao (鹊桥) v3: 流式喷泉码播放，配合 receive 使用')
    parser.add_argument('input', help='要传输的文件')
    parser.add_argument('--blocklen', type=int, default=DEFAULT_BLOCKLEN,
                        help='每个源块的字节数 (默认: %d)' % DEFAULT_BLOCKLEN)
    parser.add_argument('--ecc', choices=('L', 'M', 'Q', 'H'), default='M',
                        help='QR 纠错级别 (默认: M)')
    parser.add_argument('--fps', type=float, default=DEFAULT_FPS,
                        help='播放帧率 (默认: %d)' % DEFAULT_FPS)
    parser.add_argument('--box-size', type=int, default=DEFAULT_BOX_SIZE,
                        help='每个模块的像素数 (默认: %d)' % DEFAULT_BOX_SIZE)
    args = parser.parse_args(argv)

    path = Path(args.input)
    if not path.is_file():
        print("❌ 文件不存在: %s" % args.input)
        return 1
    data = path.read_bytes()
    if not data:
        print("❌ 文件为空，无内容可传。")
        return 1

    enable_dpi_awareness()

    symbol = QRSymbolEncoder(ecc=args.ecc, box_size=args.box_size,
                             border=STREAM_BORDER)
    try:
        encoder = StreamEncoder(path.name, data, symbol_encoder=symbol,
                                blocklen=args.blocklen)
    except ValueError as e:
        print("❌ %s" % e)
        return 1

    print("=" * 60)
    print("  QueQiao (鹊桥) - 流式发送")
    print("=" * 60)
    print("  文件:     %s (%d 字节)" % (path.name, len(data)))
    print("  源块:     K=%d × blocklen=%d" % (encoder.K, encoder.blocklen))
    print("  nonce:    0x%04x" % encoder.nonce)
    print("  帧率:     %.1f fps  ECC-%s  box=%d" % (args.fps, args.ecc, args.box_size))
    print()
    print("  发送端一直循环发包，不设预算。收齐后接收端会提示，届时按 Esc 停止。")
    print("  运维: RDP 窗口不能被遮挡或最小化；关掉屏保；画质与色深调到最高。")
    print("=" * 60)

    PlayerWindow(encoder, fps=args.fps).run()
    print("\n  已停止，共发出 %d 包。" % encoder.packets_sent)
    return 0


if __name__ == '__main__':
    sys.exit(main())
