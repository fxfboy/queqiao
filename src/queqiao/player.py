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

from queqiao.stream_encoder import DEFAULT_BLOCKLEN, GeneratorError, StreamEncoder  # noqa: E402
from queqiao.symbol_encoder import QRSymbolEncoder  # noqa: E402
from queqiao.stream_profile import CALIBRATION_MATRIX, load_profile, synthetic_payload  # noqa: E402

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


CALIBRATE_STAGE_SECONDS = 20


def make_stage_encoder(stage, file_bytes):
    """按一档标定配置造一个 StreamEncoder。

    **强制 M=None（即 M=∞，关闭系统性重发）**：标定口径是"seed 序列缺号 = 丢帧"，
    §6.4.1 的系统性重发会主动复用已发过的 seed，一开就让缺号统计失去意义。
    这条是硬约束，不继承用户 profile。
    """
    symbol = QRSymbolEncoder(ecc=stage['ecc'], box_size=stage['box_size'],
                             border=STREAM_BORDER)
    return StreamEncoder('calibration.bin', file_bytes, symbol_encoder=symbol,
                         blocklen=stage['blocklen'], M=None)


class PlayerWindow:

    def __init__(self, encoder, fps=DEFAULT_FPS, stages=None, stage_seconds=None,
                 stage_bytes=None):
        self.encoder = encoder
        self.interval = interval_ms_for_fps(fps)
        self.started_at = None
        self._after_id = None
        self._photo = None          # 必须挂在长生命周期对象上，否则被 Tk 回收成空白
        self._stopping = False
        self._error = None
        self.stages = list(stages or [])
        self.stage_seconds = stage_seconds
        self.stage_bytes = stage_bytes
        self.stage_index = 0
        self._stage_deadline = None
        self._positioned = False    # 首帧后把窗口钳回屏幕内，只做一次
        self._retiring = []          # 已 stop 但还没 join 的 encoder
        if self.stages:
            # 分档轮转要能造出下一档的 encoder，这两样缺一不可。
            # 让它在构造时就炸，而不是等第一次换档时炸在 Tk 回调里。
            assert self.stage_seconds and self.stage_bytes, \
                "传了 stages 就必须同时给 stage_seconds 和 stage_bytes"

        self.root = tk.Tk()
        self.root.title("QueQiao 鹊桥 — 流式发送")
        self.root.configure(bg='white')
        # 发送窗必须始终可见——接收端抓的就是这块屏幕区域，被别的窗口
        # 压住后收端只会"连续 N 帧非黑但解不出任何码"（同 frame_source
        # 圈选遮罩的 -topmost 用法）。
        self.root.attributes('-topmost', True)
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
        if self._stage_deadline is not None and \
                time.monotonic() >= self._stage_deadline:
            self.advance_stage()
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
            if not self._positioned:
                self._positioned = True
                self.clamp_onto_screen()
            self.status.configure(text=format_status(
                self.encoder.packets_sent,
                time.monotonic() - self.started_at,
                self.encoder.K,
            ))
        self._after_id = self.root.after(self.interval, self.tick)

    def clamp_onto_screen(self):
        """窗口必须完整落在屏幕内：屏幕合成器不渲染屏幕外像素，抓屏抓到
        的 QR 右侧会缺一条白边，接收端一帧都解不出（2026-09-08 实测：
        窗口悬出 51px，4.8 fps 播了上千帧无一解出）。tkinter 的默认摆放
        位置不保证不出屏，所以首帧显示后按真实窗口尺寸钳一次。
        """
        self.root.update_idletasks()
        ww, wh = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if ww > sw or wh > sh:
            print("⚠️  播放窗 %dx%d 比屏幕 %dx%d 还大，屏幕装不下这个码。"
                  "请调小 --box-size 或 --blocklen。" % (ww, wh, sw, sh))
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        nx = min(max(0, x), sw - ww)
        ny = min(max(0, y), sh - wh)
        if (nx, ny) != (x, y):
            self.root.geometry('+%d+%d' % (nx, ny))
            print("  窗口原在 (%d,%d) 会悬出屏幕，已移到 (%d,%d)。" % (x, y, nx, ny))

    def advance_stage(self):
        """切到标定矩阵的下一档。

        旧 encoder 只 stop 不 join——join 会阻塞 Tk 事件循环。它的生成线程
        会在 PUT_TIMEOUT 内自己退出，统一留到 run() 的 finally 里收。
        """
        self.encoder.stop()
        self._retiring.append(self.encoder)
        self.stage_index += 1
        if self.stage_index >= len(self.stages):
            self.show_end()
            return
        stage = self.stages[self.stage_index]
        self.encoder = make_stage_encoder(stage, self.stage_bytes)
        self.encoder.start()
        self._stage_deadline = time.monotonic() + self.stage_seconds
        print("  [%d/%d] blocklen=%d ecc=%s box=%d → K=%d"
              % (self.stage_index + 1, len(self.stages), stage['blocklen'],
                 stage['ecc'], stage['box_size'], self.encoder.K))

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
        if self.stages:
            self._stage_deadline = time.monotonic() + self.stage_seconds
        self.encoder.start()
        self._after_id = self.root.after(0, self.tick)
        try:
            self.root.mainloop()
        finally:
            self.encoder.stop()
            pending = self._retiring + [self.encoder]
            for enc in pending:
                enc.stop()
                if not enc.join():
                    print("⚠️  某个生成线程未在超时内结束，放弃等待并继续退出。")
        if self._error is not None:
            raise self._error


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='queqiao stream',
        description='QueQiao (鹊桥) v3: 流式喷泉码播放，配合 receive 使用')
    parser.add_argument('input', nargs='?', help='要传输的文件（--calibrate 时可省略）')
    parser.add_argument('--blocklen', type=int, default=None,
                        help='每个源块的字节数 (默认: %d)' % DEFAULT_BLOCKLEN)
    parser.add_argument('--ecc', choices=('L', 'M', 'Q', 'H'), default=None,
                        help='QR 纠错级别 (默认: M)')
    parser.add_argument('--fps', type=float, default=None,
                        help='播放帧率 (默认: %d)' % DEFAULT_FPS)
    parser.add_argument('--box-size', type=int, default=None,
                        help='每个模块的像素数 (默认: %d)' % DEFAULT_BOX_SIZE)
    parser.add_argument('--calibrate', action='store_true',
                        help='标定模式: 依次播放各档参数，配合 receive --calibrate 使用')
    parser.add_argument('--stage-seconds', type=int, default=CALIBRATE_STAGE_SECONDS,
                        help='标定时每档播放秒数 (默认: %d)' % CALIBRATE_STAGE_SECONDS)
    args = parser.parse_args(argv)

    if args.calibrate:
        enable_dpi_awareness()
        data = synthetic_payload()
        stages = CALIBRATION_MATRIX
        print("=" * 60)
        print("  QueQiao (鹊桥) - 标定模式")
        print("=" * 60)
        print("  合成载荷 %d 字节，共 %d 档，每档 %d 秒，总计约 %d 秒。"
              % (len(data), len(stages), args.stage_seconds,
                 len(stages) * args.stage_seconds))
        print("  接收端现在就运行: ./run.sh receive --calibrate")
        print("  系统性重发已强制关闭 (M=∞)，否则 seed 缺号统计不成立。")
        print("=" * 60)
        first = make_stage_encoder(stages[0], data)
        print("  [1/%d] blocklen=%d ecc=%s box=%d → K=%d"
              % (len(stages), stages[0]['blocklen'], stages[0]['ecc'],
                 stages[0]['box_size'], first.K))
        fps = args.fps if args.fps is not None else DEFAULT_FPS
        win = PlayerWindow(first, fps=fps, stages=stages,
                           stage_seconds=args.stage_seconds, stage_bytes=data)
        win.run()
        print("\n  标定播放结束。到接收端看报告。")
        return 0

    if args.input is None:
        parser.error("需要指定输入文件（或用 --calibrate 进入标定模式）")

    # 只有非标定路径读 profile。标定就是要**生成** profile，
    # 继承上一次的结果会让基准随着每次标定漂移。
    settings, source = load_profile()
    if source == 'profile':
        print("  使用标定结果: blocklen=%d ecc=%s fps=%g box=%d"
              % (settings['blocklen'], settings['ecc'], settings['fps'],
                 settings['box_size']))
    if args.blocklen is None:
        args.blocklen = settings['blocklen']
    if args.ecc is None:
        args.ecc = settings['ecc']
    if args.fps is None:
        args.fps = settings['fps']
    if args.box_size is None:
        args.box_size = settings['box_size']

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
