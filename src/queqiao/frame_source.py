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
    _, hi = gray.getextrema()
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


def enumerate_monitors():
    """枚举所有显示器，返回 mss 全局坐标矩形列表（0 号"虚拟全桌面"已跳过）。

    任何异常都降级为空列表——圈选是纯 UI，枚举失败不该把接收端撂倒，
    由调用方回退到 Tk 自报的主屏尺寸。
    """
    try:
        import mss
        with mss.mss() as sct:
            return [dict(m) for m in sct.monitors[1:]]
    except Exception:
        return []


def tk_monitor_rects(mons, tk_w, tk_h):
    """mss 显示器矩形 → tkinter 全局逻辑矩形。返回 (rects, factor)。

    Tk 的 +x+y 和 mss 的 monitors 各用一套全局坐标，两套空间的比率就是
    factor（screen 空间 → mss 空间）。分两种情形：

    - Tk 看到的屏幕 == 所有显示器的 union → 两套空间一比一（factor=1）。
      Linux X11 多屏 Tk 报整个虚拟桌面；macOS 上 mss 用 CGDisplayBounds
      枚举、Tk 用逻辑点，实测两者恒等（探针：4K@2x 屏两边都是 1920×1080）。
    - 否则按**主屏**比率统一换算（Windows：mss 报物理像素，Tk 报 DPI
      虚拟化逻辑坐标，比率=缩放百分比）。混合 DPI 多屏（150%+100% 混插）
      会有几像素偏差——遮罩差一两个像素不影响抓码，不做 per-monitor DPI。

    注意比率必须拿主屏（mons[0]）算：union 在多屏下是两倍宽，拿来算比率
    会得到 2.0，把所有矩形再对半砍一遍。
    """
    left = min(m['left'] for m in mons)
    top = min(m['top'] for m in mons)
    right = max(m['left'] + m['width'] for m in mons)
    bottom = max(m['top'] + m['height'] for m in mons)
    if (right - left, bottom - top) == (tk_w, tk_h):
        factor = 1.0
    else:
        factor = detect_scale_factor(mons[0]['width'], tk_w)
    rects = [(int(round(m['left'] / factor)), int(round(m['top'] / factor)),
              int(round(m['width'] / factor)), int(round(m['height'] / factor)))
             for m in mons]
    return rects, factor


def _geometry_str(w, h, x, y):
    """Tk geometry 字符串。负坐标必须写成 +-N——单独的 -N 是
    "距右/下边缘 N"，语义完全不同。"""
    def coord(v):
        return '+%d' % v if v >= 0 else '+-%d' % abs(v)
    return '%dx%d%s%s' % (w, h, coord(x), coord(y))


def select_region(prompt=None):
    """多屏圈选：**每块显示器**各铺一层半透明遮罩，在任意屏上拖框。

    返回 bbox 是 **mss 全局坐标**（直接喂给 mss.grab），取消返回 None。
    """
    ensure_tcl_env()
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()             # root 只当 Toplevel 的载体，自己不露面
    tk_w = root.winfo_screenwidth()
    tk_h = root.winfo_screenheight()

    mons = enumerate_monitors()
    if not mons or mons[0]['width'] <= 0:
        mons = [{'left': 0, 'top': 0, 'width': tk_w, 'height': tk_h}]
    rects, factor = tk_monitor_rects(mons, tk_w, tk_h)
    if abs(factor - round(factor)) > 1e-6:
        print("  ⚠️  屏幕缩放因子 %.3f 不是整数倍，抓取区域可能有 1–2 像素偏差。" % factor)
    if factor != 1.0:
        print("  检测到 %gx 显示缩放，bbox 换算为 mss 坐标。" % factor)

    # 不用 attributes('-fullscreen')：macOS 上原生全屏会开一个独立 Space，
    # 遮罩自己独占一屏，要圈选的播放窗反而看不见。改成每块屏一个铺满该屏
    # 的普通置顶窗，效果一样、不进 Space，且任何一块屏都能圈。
    windows = []
    state = {'result': None}    # {'mon': mss矩形, 'local': 本屏逻辑矩形}

    for idx, ((lx, ly, lw, lh), mon) in enumerate(zip(rects, mons), 1):
        win = tk.Toplevel(root)
        win.geometry(_geometry_str(lw, lh, lx, ly))
        try:
            win.attributes('-alpha', 0.28)
        except tk.TclError:
            pass
        win.configure(bg='black')
        win.attributes('-topmost', True)
        win.config(cursor='crosshair')

        canvas = tk.Canvas(win, bg='black', highlightthickness=0,
                           cursor='crosshair')
        canvas.pack(fill='both', expand=True)
        canvas.create_text(
            lw // 2, 40, fill='white', font=('TkDefaultFont', 20),
            text=prompt or "拖动框选发送端播放窗的区域　·　Esc 取消")
        if len(rects) > 1:
            canvas.create_text(20, 90, anchor='w', fill='#999999',
                               font=('TkDefaultFont', 13),
                               text="屏幕 %d/%d" % (idx, len(rects)))

        drag = {'mon': mon, 'logical': (lx, ly, lw, lh),
                'x0': 0, 'y0': 0, 'rect': None, 'canvas': canvas}

        def on_press(e, drag=drag):
            drag['x0'], drag['y0'] = e.x, e.y
            if drag['rect'] is not None:
                drag['canvas'].delete(drag['rect'])
            drag['rect'] = drag['canvas'].create_rectangle(
                e.x, e.y, e.x, e.y, outline='#00ff88', width=3)

        def on_drag(e, drag=drag):
            if drag['rect'] is not None:
                drag['canvas'].coords(drag['rect'],
                                      drag['x0'], drag['y0'], e.x, e.y)

        def on_release(e, drag=drag):
            # 只记原始矩形，校验和换算留在 mainloop 之后——校验失败时
            # 窗口必须已经撤干净（和单屏版同一时序）。
            left, top = min(drag['x0'], e.x), min(drag['y0'], e.y)
            state['result'] = {
                'mon': drag['mon'],
                'logical': drag['logical'],
                'local': (left, top, abs(e.x - drag['x0']), abs(e.y - drag['y0'])),
            }
            finish()

        canvas.bind('<ButtonPress-1>', on_press)
        canvas.bind('<B1-Motion>', on_drag)
        canvas.bind('<ButtonRelease-1>', on_release)
        win.bind('<Escape>', lambda _e: finish())
        windows.append(win)

    def finish():
        """先逐窗 withdraw，再销毁。

        **为什么必须 withdraw 在前**：macOS Tk（8.6.14 实测）上 destroy() 的
        原生 NSWindow teardown 依赖事件循环冲刷。mainloop 一返回，调用方
        （stream_decoder 的帧循环）立刻扎进 mss+zxing 死循环、不再处理任何
        事件——一旦 destroy 的 teardown 在哪种真实鼠标时序下被跳过，28% 透明
        的遮罩就永远钉在屏幕上：拦住全部点击、进程被标成"未响应"（彩虹球），
        只能强退（2026-09-08 用户连续复现，取证见 CGWindowList：主线程已在
        帧循环、遮罩窗口仍在屏）。withdraw 是同步的 orderOut，不依赖后续
        事件循环，先撤屏再销毁，幽灵在构造上不可能出现。多屏版每一层都要
        撤——漏一层就是多一块幽灵。
        """
        for w in windows:
            try:
                w.wm_withdraw()
            except tk.TclError:
                pass
        for w in windows:
            try:
                w.destroy()
            except tk.TclError:
                pass
        root.destroy()

    root.mainloop()

    # 兜底：mainloop 退出后把 destroy 可能遗留的原生 teardown 冲完。
    # destroy 之后 Tk 对象已死，update() 抛 TclError 属正常，吞掉即可。
    try:
        root.update()
    except tk.TclError:
        pass

    if state['result'] is None:
        return None

    picked = state['result']
    logical = validate_bbox(picked['local'], picked['logical'][2:4])

    # 本屏逻辑矩形 → mss 全局坐标：屏幕原点 + 区域偏移×缩放。按所在屏的
    # 原点做偏移，副屏（left/top 为负或超出主屏）也能落对位置。
    pl, pt, pw, ph = scale_bbox(logical, factor)
    mon = picked['mon']
    return (mon['left'] + pl, mon['top'] + pt, pw, ph)


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
