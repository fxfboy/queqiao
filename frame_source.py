#!/usr/bin/env python3
"""接收端帧源：屏幕区域捕捉、tkinter 圈选、区域持久化。

§8.1 所有权契约：__iter__ 每次 yield 一个**新的** Image，所有权转移给调用方；
调用方在 try/finally 里 close。解码后端只是借用，绝不关（见 qr_backends/base.py 的 _as_image）。
"""

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

REGION_SCHEMA = 1

# 比这更小的区域几乎肯定是误操作（手一抖点了一下）
MIN_REGION_PX = 40

# 连续这么多帧全黑就提示权限问题。接收端跑 10–15 fps，30 帧约 2–3 秒。
BLACK_FRAME_ALERT = 30

# 连续这么多帧非黑但解不出任何码，提示"窗口被遮挡/区域选错/发送端没开"。
# is_black_frame 抓不到这种情况——被遮挡时画面五颜六色，不黑，但一个码都读不出。
# 与 BLACK_FRAME_ALERT 同量级，由 stream_decoder.py 的帧循环消费（Task 17）。
NO_CODE_ALERT = 30


def region_path():
    return Path.home() / '.queqiao' / 'last_region.json'


# ── 纯函数 ────────────────────────────────────────────────────

def detect_scale_factor(mss_width, tk_width):
    """物理像素宽 / 逻辑坐标宽。macOS Retina 返回 2.0，普通屏 1.0。

    §8.2：tkinter 报逻辑坐标，mss 报物理像素。混用会让抓取区域只覆盖
    目标的左上四分之一——画面看着正常，就是永远解不出码。
    """
    if tk_width <= 0:
        return 1.0
    ratio = mss_width / float(tk_width)
    for f in (1, 2, 3):
        if abs(ratio - f) < 0.05:
            return float(f)
    return ratio                 # 非整数倍（Windows 150% 缩放）原样返回


def scale_bbox(bbox, factor):
    left, top, width, height = bbox
    return (int(round(left * factor)), int(round(top * factor)),
            int(round(width * factor)), int(round(height * factor)))


def validate_bbox(bbox, screen_size):
    if len(bbox) != 4:
        raise ValueError("bbox 必须是 (left, top, width, height)，实得 %r" % (bbox,))
    left, top, width, height = (int(v) for v in bbox)
    sw, sh = screen_size
    if width < MIN_REGION_PX or height < MIN_REGION_PX:
        raise ValueError(
            "区域 %d×%d 太小（最小 %d×%d）。是不是只点了一下没拖动？"
            % (width, height, MIN_REGION_PX, MIN_REGION_PX))
    if left < 0 or top < 0:
        raise ValueError("区域左上角 (%d,%d) 越出屏幕。" % (left, top))
    if left + width > sw or top + height > sh:
        raise ValueError(
            "区域右下角 (%d,%d) 越出屏幕 %d×%d。"
            % (left + width, top + height, sw, sh))
    return (left, top, width, height)


def is_black_frame(image, threshold=8):
    """整帧最亮像素都低于阈值 → 判为全黑。

    macOS 未授"屏幕录制"权限时，截图 API 不报错，只是返回纯黑图像——
    这是最容易被误判成"码解不出来"的失败模式。
    """
    gray = image.convert('L')
    lo, hi = gray.getextrema()
    return hi < threshold


def save_region(bbox):
    p = region_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema': REGION_SCHEMA,
        'bbox': list(int(v) for v in bbox),
        'saved_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    p.write_text(json.dumps(payload, ensure_ascii=True, indent=2),
                 encoding='utf-8')


def load_region():
    """读上次的区域。任何异常（缺失/损坏/版本不符/字段非法）一律返回 None。"""
    p = region_path()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get('schema') != REGION_SCHEMA:
        return None
    bbox = raw.get('bbox')
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        vals = tuple(int(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if vals[2] <= 0 or vals[3] <= 0:
        return None
    return vals


# ── 帧源 ──────────────────────────────────────────────────────

class FrameSource(ABC):
    """§8.1：每次迭代 yield 一个新 Image，所有权转移给调用方。"""

    describe = 'frame source'

    @abstractmethod
    def __iter__(self):
        raise NotImplementedError


class ScreenSource(FrameSource):
    """用 mss 反复抓同一块**物理像素** bbox。bbox 必须已换算过 Retina 因子。"""

    def __init__(self, bbox, interval=0.0):
        self.bbox = tuple(int(v) for v in bbox)
        self.interval = interval
        self.frames_grabbed = 0
        self.actual_size = None
        self.describe = "屏幕区域 %d×%d @ (%d,%d)" % (
            self.bbox[2], self.bbox[3], self.bbox[0], self.bbox[1])

    def __iter__(self):
        import mss
        from PIL import Image

        region = {'left': self.bbox[0], 'top': self.bbox[1],
                  'width': self.bbox[2], 'height': self.bbox[3]}
        with mss.mss() as sct:
            while True:
                shot = sct.grab(region)
                image = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
                if self.frames_grabbed == 0:
                    self.actual_size = shot.size
                    # §8.2：首帧把实际抓取尺寸打出来。它与请求的 bbox 不一致
                    # 就是 Retina 换算出问题的第一手证据。
                    print("  首帧实际抓取尺寸: %d×%d（请求 %d×%d）"
                          % (shot.size[0], shot.size[1], self.bbox[2], self.bbox[3]))
                self.frames_grabbed += 1
                yield image
                if self.interval:
                    time.sleep(self.interval)


# ── 圈选（GUI，不做自动化测试） ────────────────────────────────

def ensure_tcl_env():
    """uv 装的 python-build-standalone 把 tcl8.6 打包在 sys.base_prefix/lib 下，
    venv 的 sys.prefix 下没有，而 Tk 按 sys.prefix 找 init.tcl——于是
    `import tkinter` 后第一次建窗口就抛 `Can't find a usable init.tcl`。
    player.py 里有一份带完整推导注释的同款修复；接收端的圈选窗同样需要。
    """
    import os
    import sys

    if os.environ.get('TCL_LIBRARY'):
        return
    base = getattr(sys, 'base_prefix', sys.prefix)
    if base == sys.prefix:
        return
    for ver in ('8.6', '8.5'):
        tcl = os.path.join(base, 'lib', 'tcl' + ver)
        if os.path.exists(os.path.join(tcl, 'init.tcl')):
            os.environ['TCL_LIBRARY'] = tcl
            tk_dir = os.path.join(base, 'lib', 'tk' + ver)
            if os.path.isdir(tk_dir):
                os.environ['TK_LIBRARY'] = tk_dir
            return


def select_region(prompt=None):
    """全屏半透明覆盖层上拖框选区。返回**物理像素** bbox，取消返回 None。"""
    ensure_tcl_env()
    import tkinter as tk

    root = tk.Tk()
    # 不用 attributes('-fullscreen')：macOS 上原生全屏会开一个独立 Space，
    # 遮罩自己独占一屏，要圈选的播放窗反而看不见。改成铺满屏幕尺寸的
    # 普通置顶窗口，效果一样但不进 Space。
    tk_w = root.winfo_screenwidth()
    tk_h = root.winfo_screenheight()
    root.geometry("%dx%d+0+0" % (tk_w, tk_h))
    try:
        root.attributes('-alpha', 0.28)
    except tk.TclError:
        pass
    root.configure(bg='black')
    root.attributes('-topmost', True)
    root.config(cursor='crosshair')

    canvas = tk.Canvas(root, bg='black', highlightthickness=0)
    canvas.pack(fill='both', expand=True)
    canvas.create_text(
        tk_w // 2, 40, fill='white', font=('TkDefaultFont', 20),
        text=prompt or "拖动框选发送端播放窗的区域　·　Esc 取消")

    state = {'x0': 0, 'y0': 0, 'rect': None, 'result': None}

    def on_press(e):
        state['x0'], state['y0'] = e.x, e.y
        if state['rect'] is not None:
            canvas.delete(state['rect'])
        state['rect'] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline='#00ff88', width=3)

    def on_drag(e):
        if state['rect'] is not None:
            canvas.coords(state['rect'], state['x0'], state['y0'], e.x, e.y)

    def on_release(e):
        left, top = min(state['x0'], e.x), min(state['y0'], e.y)
        state['result'] = (left, top, abs(e.x - state['x0']), abs(e.y - state['y0']))
        root.destroy()

    canvas.bind('<ButtonPress-1>', on_press)
    canvas.bind('<B1-Motion>', on_drag)
    canvas.bind('<ButtonRelease-1>', on_release)
    root.bind('<Escape>', lambda _e: root.destroy())
    root.mainloop()

    if state['result'] is None:
        return None

    logical = validate_bbox(state['result'], (tk_w, tk_h))

    # 逻辑坐标 → 物理像素
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[1]
        factor = detect_scale_factor(mon['width'], tk_w)
    if abs(factor - round(factor)) > 1e-6:
        print("  ⚠️  屏幕缩放因子 %.3f 不是整数倍，抓取区域可能有 1–2 像素偏差。" % factor)
    if factor != 1.0:
        print("  检测到 %gx 显示缩放，bbox 换算为物理像素。" % factor)
    return scale_bbox(logical, factor)


def resolve_region(reselect=False):
    """拿到本次要用的物理 bbox：优先复用上次的，除非 --reselect 或没存过。"""
    if not reselect:
        saved = load_region()
        if saved is not None:
            print("  复用上次的区域: %d×%d @ (%d,%d)（要改用 --reselect）"
                  % (saved[2], saved[3], saved[0], saved[1]))
            return saved
    bbox = select_region()
    if bbox is None:
        raise KeyboardInterrupt("已取消区域选择")
    save_region(bbox)
    return bbox
