#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖 SVG 出图 —— 给边缘盒子上用。

**为什么不用 matplotlib**：
盒子（Debian 12）不通外网、也没有离线 wheel 包，装不了 matplotlib 和它十来个依赖。
而需求书要求「全部数据处理与分析均在本地边缘端完成」—— 图**必须在盒子上生成**。
所以只能零依赖：直接拼 SVG 文本。

SVG 是纯文本格式，浏览器 / Word / PPT 都能用，矢量缩放不失真。
中文靠 `font-family` 交给渲染端（盒子只管生成，不负责渲染）。

用法：
    from figures import cycle_chart
    svg = cycle_chart(cycles, title="...", notes=["..."])

命令行：
    python3 figures.py --data-dir ./data --out ./figs
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_esc

from probe import (CAP_TO_MAH, CE_CHARGE_OVER_DISCHARGE, CHARGING_STEPTYPES,
                   DISCHARGING_STEPTYPES, _steptype, ce_pct, group_cycles)

VERSION = "1.0"

# 样式常量（和参考图/之前的 matplotlib 版保持一致）
C_LEFT = "#d62728"      # 左轴系列颜色
C_RIGHT = "#1f77b4"     # 右轴系列颜色
C_AXIS = "#222222"
C_GRID = "#e8e8e8"
C_NOTE_BG = "#f5f5f5"
C_NOTE_ED = "#cccccc"
# 以 ⚠ 开头的说明行用这个颜色画（见 set_notes）。黑白色打印时它仍是深灰，
# 但至少和普通说明区分得开 —— 图会脱离网页被贴进报告，警告必须跟着图走。
C_WARN = "#b42318"

MASS_G = 1.184e-3
"""算「比容量 (mAh/g)」时用的活性物质质量，单位 g。

⚠️ **这是某一个样品的值，不是所有数据集都一样。**
它来自测试方案的反推：120 mAh/g × 1.184 mg = 0.142 mAh，而测试方案里
「老化段 1C」声明的是 0.1421 mA —— 两头对得上，所以对这个样品它是可信的。

问题在于**采集到的数据里没有质量字段**（连 meta.json 都没有），所以代码只能
用这个常数去算所有通道。实测 27-188-10-1 的电流是 0.284 mA（是这个样品的 2 倍），
算出来的比容量达到 799 mAh/g —— 超过任何常见锂电正极的量级，说明质量假设不适用。

**质量属于「每个测试一份」的信息，必须从测试记录里取**，不能靠常数。
这里保留常数只是为了让没有质量信息时也能出一个"看得见假设"的数，
同时用 `SP_IMPLAUSIBLE_MAH_G` 把明显不对的情况标出来。
"""

SP_IMPLAUSIBLE_MAH_G = 500.0
"""比容量超过这个值就认为「质量假设用错了」。

为什么取 500：常见锂电正极的实测比容量都在 300 mAh/g 以内
（LFP ~160、NCM ~180、富锂锰基 ~280），500 已经远超它们。
这是个**警告阈值，不是材料学结论** —— 它只用来抓"数量级明显不对"的情况。"""


def sp_implausible(cs: list[dict]) -> float | None:
    """返回最大的那个超出合理量级的比容量；没有就返回 None。

    有这个提示，总比把 799 mAh/g 当成实验结果写进报告强 ——
    这种错从数字上完全看不出来（799 看着像个正常读数）。
    """
    vals = [c[k] for c in cs for k in ("dis_sp", "chg_sp")
            if isinstance(c.get(k), (int, float)) and c[k] > SP_IMPLAUSIBLE_MAH_G]
    return max(vals) if vals else None


CE_FLOOR_PCT = 50.0
"""整段测试的最大库仑效率低于这个值，就认为**这颗电芯没有在放电**。

为什么需要单独一条：实测 27-188-10-3 的放电容量 602 圈全是 0（红线贴着 0），
库仑效率从 2% 爬到 25%。它同时也会被「首圈是化成循环」那条命中 ——
但两条说的**不是一回事**：那条只说"基准选错了"，读者可能反过来理解成
"那用第 2 圈当基准就有 100%，电池是好的"。实际情况是**这颗电芯没在工作**。

正常的锂电充放电，库仑效率应很快接近 100%（通常 >99%）。整段测试连一半都
上不去，说明放电相对于充电一直少得离谱 —— 电芯失效、接线/夹具有问题、
或者测试条件不对。这是**要让实验室确认**的，不是我们能在数据端解决的。
"""


# 效率口径（充电 ÷ 放电，负极半电池）—— 定义在 probe.py，**只那一个开关**
def ce_never_healthy(cs: list[dict]) -> float | None:
    """整段测试的**放电/充电容量比**最大值（低于 CE_FLOOR_PCT 才返回，否则 None）。

    ★ 为什么这里用"放电 ÷ 充电"而不是 `ce` 字段：本项目的库仑效率口径是
    **充电 ÷ 放电**（负极半电池，见 `ce_pct`），电芯"根本没在放电"时该值会趋于
    无穷大或直接是 None（放电为 0），用它**判不出来**。判"有没有在放电"这件事，
    放电/充电比是更直接的量 —— 口径相反不影响这条启发式的含义。
    """
    vals = []
    for c in cs:
        chg, dis = c.get("chg"), c.get("dis")
        if not isinstance(chg, (int, float)) or not isinstance(dis, (int, float)):
            continue
        if chg > 0:
            vals.append(dis / chg * 100.0)
    if not vals:
        return None
    m = max(vals)
    return m if m < CE_FLOOR_PCT else None
FONT = ("-apple-system, 'Segoe UI', 'Microsoft YaHei', SimHei, "
        "'Noto Sans CJK SC', sans-serif")

# 尺寸（集中放这里）。第一次画出来字太小 —— 因为画布是 1200px 宽、
# 而之前 matplotlib 版是 1900px 宽，同样 13px 的字相对就小了一圈。
# 这里按"相对画布的比例"定，和 matplotlib 版视觉一致。
FS_TITLE, FS_LABEL, FS_TICK = 24, 18, 16
FS_NOTE, FS_LEGEND, FS_ANNO = 16, 17, 16
LW_MAIN, LW_THIN = 2.6, 2.0
R_MARK, SQ = 5.6, 10


def esc(v) -> str:
    return _xml_esc(str(v), {'"': "&quot;", "'": "&apos;"})


def nice_ticks(lo: float, hi: float, count: int = 6) -> list[float]:
    """给 [lo, hi] 挑一组"好看"的刻度值（1/2/5 × 10^n 的整数倍）。"""
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [lo, hi]
    raw = (hi - lo) / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    out: list[float] = []
    v = start
    while v <= hi + step * 1e-9 and len(out) < 40:
        if v >= lo - step * 1e-9:
            out.append(round(v, 12))
        v += step
    return out or [lo, hi]


def nice_ceil(v: float, lo: float, count: int = 6) -> float:
    """把坐标轴上界抬到「刚好落在刻度上」的位置。

    为什么需要：刻度只落在 1/2/5×10ⁿ 这种整齐的值上，于是**轴顶会留白**。
    实测 nice_ticks(0, 59.9) 只给出 0/20/40 —— 数据到 59.9，标签却停在 40，
    轴的上三分之一一个数都没有，报告里的图会像没画完。

    做法是让轴**顶到**最上面那条刻度，而不是让刻度去够轴顶。
    """
    if not math.isfinite(v) or not math.isfinite(lo):
        return v
    t = nice_ticks(lo, v, count)
    if len(t) < 2:
        return v
    step = t[1] - t[0]
    if step <= 0:
        return v
    if t[-1] >= v - step * 1e-9:
        return t[-1]
    return t[-1] + step


def fmt_num(v: float) -> str:
    """刻度数字：不用科学计数法看着舒服；**只有极小值**才用科学计数法。

    为什么大数也要展开：DCIR 那根轴的量级是 10⁶（mΩ），轴上一排 "2e+06"
    在实验报告里没人愿意读。注意 `%g` 自己一过 1e6 就转成科学计数法了，
    所以这里必须显式格式化 —— 光靠 `%g` 实现不了这个承诺。
    """
    if v == 0 or not math.isfinite(v):
        return "0" if v == 0 else str(v)
    a = abs(v)
    if a < 1e-6:
        return f"{v:.0e}"
    if a >= 100000:
        # 到 1e5 以上就按整数印。刻度值来自 nice_ticks（1/2/2.5/5 × 10ⁿ），
        # 这个量级上一定是整数，所以不会丢小数。
        return f"{v:.0f}"
    # %g 自己会取舍 —— 0.0004 就印成 0.0004，不会变成 4e-04
    return f"{v:g}"


def _seg_hits_rect(x1: float, y1: float, x2: float, y2: float,
                   bx: float, by: float, bw: float, bh: float) -> bool:
    """线段 (x1,y1)-(x2,y2) 是否与矩形 [bx,bx+bw]×[by,by+bh] 相交。

    用 Liang–Barsky 参数化裁剪：线段写成 P = P1 + t·d（t∈[0,1]），
    对四条边求出 t 的允许区间，区间非空即相交。

    为什么需要它：图例落位要判断"这个框会不会压住线"。只数数据**点**是不够的 ——
    只有两三圈时顶点全在画面左右边缘，而直线横扫整幅图，图例就会压上去（实测过）。
    """
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - bx), (dx, bx + bw - x1),
                 (-dy, y1 - by), (dy, by + bh - y1)):
        if p == 0:
            if q < 0:
                return False        # 与这条边平行且落在框外
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def _path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    d = [f"M {points[0][0]:.2f},{points[0][1]:.2f}"]
    d += [f"L {x:.2f},{y:.2f}" for x, y in points[1:]]
    return " ".join(d)


class CycleChart:
    """双 Y 轴折线图（左轴容量、右轴效率），空心圆点 + 细连线。"""

    W, H = 1200, 560
    ML, MR, MT, MB = 142, 112, 58, 90

    def __init__(self, title: str, left_label: str, right_label: str,
                 x_label: str = "循环号"):
        self.title, self.left_label, self.right_label, self.x_label = \
            title, left_label, right_label, x_label
        self.left_series: list[dict] = []
        self.right_series: list[dict] = []
        self.notes: list[str] = []
        self.caption: list[str] = []
        self.texts: list[dict] = []      # 图内任意位置的文字（数据坐标，y 用左轴）
        self.anno: dict | None = None
        self.xlim: tuple[float, float] | None = None
        self.left_ylim: tuple[float, float] | None = None
        self.right_ylim: tuple[float, float] | None = None
        # 指定刻度（X 轴本来就是圈号的图用，比如"只标 1/10/50/100/200/300"）。
        # None = 按数据自动挑刻度。
        self.xticks_fixed: list[float] | None = None
        self.xticks_span = False

    def set_xticks(self, ticks, span: bool = False):
        """固定 X 轴刻度。span=True 时把坐标范围也撑到 [最小刻度, 最大刻度]。

        默认 span=False：只画"落在数据范围内"的刻度 —— 数据只有 5 圈、
        刻度却要标到 300 的话，轴一撑开，曲线就缩成左下角一小团。
        """
        self.xticks_fixed = [float(t) for t in ticks]
        self.xticks_span = span

    # ---------------- 数据 ----------------
    def add_left(self, xs, ys, label, color=C_LEFT, marker="circle"):
        self.left_series.append(dict(xs=list(xs), ys=list(ys), label=label,
                                     color=color, marker=marker))

    def add_right(self, xs, ys, label=None, color=C_RIGHT, marker="square"):
        self.right_series.append(dict(xs=list(xs), ys=list(ys), label=label,
                                      color=color, marker=marker))

    def set_notes(self, lines: list[str]):
        """图内的说明框（压在图上方）。**能不用就不用** —— 见 set_caption。"""
        self.notes = list(lines)

    def set_caption(self, lines: list[str]):
        """图**下方**的图注（报告里的实验条件就写这儿）。

        为什么要有这个：说明框画在图里，一旦行数多了就会**盖住曲线**。
        实测 7 行说明把前几十圈的红线全挡了 —— 而那正是「首圈是化成循环」
        这件事最该被看见的地方。放到图外就没有遮挡，还能用满整幅宽度。
        （图内说明框仍保留：短标注压在图里更紧凑。）
        """
        self.caption = list(lines)

    def set_annotation(self, text_lines: list[str], from_xy: tuple[float, float],
                       to_xy: tuple[float, float]):
        """from_xy/to_xy 都是数据坐标 (x, 左轴 y) —— 箭头从文字指向数据点。"""
        self.anno = dict(text=text_lines, src=from_xy, dst=to_xy)

    def add_text(self, x: float, y: float, text: str, color: str | None = None,
                 size: int = 16, anchor: str = "middle") -> None:
        """图内任意位置放一段文字（数据坐标，y 用**左轴**）。

        倍率图的段标签（0.2 / 0.5 / 1 / 2 …）用它标在每段数据上方。
        """
        self.texts.append(dict(x=x, y=y, text=text, color=color,
                               size=size, anchor=anchor))

    # ---------------- 渲染 ----------------
    def _ranges(self):
        xs = [x for s in self.left_series + self.right_series for x in s["xs"]]
        ly = [y for s in self.left_series for y in s["ys"] if y is not None]
        ry = [y for s in self.right_series for y in s["ys"] if y is not None]
        # 没有任何数据也要能渲染（空架子总比当机好）—— 回退到 0~1 的空坐标架
        x0, x1 = self.xlim or ((min(xs), max(xs)) if xs else (0.0, 1.0))
        # 固定刻度 + span：坐标范围撑到刻度两端（"圈号 1→300"这种轴要它）
        if self.xticks_fixed and self.xticks_span:
            x0 = min(x0, min(self.xticks_fixed))
            x1 = max(x1, max(self.xticks_fixed))
        # 上界顶到刻度上（nice_ceil），否则最上面那条网格线上没有标签。
        # 下界保持 0 起（电池数据看绝对量级，不从中间截断）。
        ly_lo = min([0.0] + ly)
        ry_lo = min([0.0] + ry)
        # 数据有负值时（dQ/dV 的还原峰在 0 以下），轴底不能**正好**压在最小值上 ——
        # 峰尖贴着边框画，看着就是"被切掉了"。往上抬 8% 再取整到刻度上。
        if ly and ly_lo < 0:
            ly_lo = -nice_ceil(abs(ly_lo) * 1.08, 0.0)
        if ry and ry_lo < 0:
            ry_lo = -nice_ceil(abs(ry_lo) * 1.08, 0.0)
        ly0, ly1 = self.left_ylim or (
            ly_lo, nice_ceil(max(ly), ly_lo) if ly else 1)
        ry0, ry1 = self.right_ylim or (
            ry_lo, nice_ceil(max(ry), ry_lo) if ry else 1)

        # 退化区间必须撑开，否则 sx()/sly() 会除零。
        # 最典型的就是「只有 1 圈」的数据集（min == max）—— 实测真的崩过。
        if x1 <= x0:
            x0, x1 = x0 - 0.5, x0 + 0.5
        if ly1 <= ly0:
            ly1 = ly0 + 1.0
        if ry1 <= ry0:
            ry1 = ry0 + 1.0
        return (x0, x1), (ly0, ly1), (ry0, ry1)

    def svg(self) -> str:
        (x0, x1), (ly0, ly1), (ry0, ry1) = self._ranges()
        px0, px1 = self.ML, self.W - self.MR
        py0, py1 = self.H - self.MB, self.MT          # py0=下(大) py1=上(小)

        # 图注加在**画布下方**：只把画布长高，绘图区尺寸和位置一点不变，
        # 所以加不加图注都不会改变曲线的形状和比例。
        cap_step = FS_NOTE + 9
        cap_h = (len(self.caption) * cap_step + 22) if self.caption else 0
        H_total = self.H + cap_h

        def sx(v): return px0 + (v - x0) / (x1 - x0) * (px1 - px0)
        def sly(v): return py0 - (v - ly0) / (ly1 - ly0) * (py0 - py1)
        def sry(v): return py0 - (v - ry0) / (ry1 - ry0) * (py0 - py1)

        o: list[str] = []
        o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W}" '
                 f'height="{H_total}" viewBox="0 0 {self.W} {H_total}">')
        o.append(f'<rect width="{self.W}" height="{H_total}" fill="#ffffff"/>')
        o.append(f'<g font-family="{FONT}" fill="{C_AXIS}">')

        # ---- 网格 + 左轴刻度 ----
        lt = nice_ticks(ly0, ly1)
        for v in lt:
            y = sly(v)
            o.append(f'<line x1="{px0}" y1="{y:.1f}" x2="{px1}" y2="{y:.1f}" '
                     f'stroke="{C_GRID}" stroke-width="1"/>')
            o.append(f'<text x="{px0-11}" y="{y+5:.1f}" font-size="{FS_TICK}" '
                     f'text-anchor="end">{esc(fmt_num(v))}</text>')
            o.append(f'<line x1="{px0-5}" y1="{y:.1f}" x2="{px0}" y2="{y:.1f}" '
                     f'stroke="{C_AXIS}" stroke-width="1"/>')
        # ---- 右轴刻度（没有右轴系列就整根不画 —— 单轴图留个 0/1 的空轴很难看）----
        if self.right_series:
            rt = nice_ticks(ry0, ry1)
            for v in rt:
                y = sry(v)
                o.append(f'<text x="{px1+13}" y="{y+5:.1f}" font-size="{FS_TICK}" '
                         f'text-anchor="start">{esc(fmt_num(v))}</text>')
                o.append(f'<line x1="{px1}" y1="{y:.1f}" x2="{px1+5}" y2="{y:.1f}" '
                         f'stroke="{C_AXIS}" stroke-width="1"/>')
        # ---- X 轴刻度 ----
        # 策略按**数据本身是不是整数**选，而不是按跨度：
        #   · 循环号（整数）→ 每圈一个刻度，圈多了改用整刻度（100/200/…）
        #   · 容量 / 电压（连续量）→ 用"好看刻度"（0.1/0.2/…）
        # ★ 原来只看"跨度 ≤ 12"，容量/电压轴跨度也小（实测 0.63 mAh、0.65 V），
        #   于是走了整数分支、生成 [0,1,2] 这种刻度，再被范围过滤砍掉大半 ——
        #   首效VQ 的横轴只剩一个 "0"、dQ/dV 只剩一个 "1"（用户看出来的）。
        xs_all = [x for s in self.left_series + self.right_series for x in s["xs"]]
        xs_int_like = (all(float(v).is_integer() for v in xs_all)
                       if xs_all else True)
        span = x1 - x0
        if self.xticks_fixed:
            # 调用方点名要的刻度（如圈号 1/10/50/100/200/300）；范围外的仍要滤掉
            xticks = list(self.xticks_fixed)
        elif span <= 12 and xs_int_like:
            xticks = list(range(int(math.floor(x0)), int(math.ceil(x1)) + 1))
        else:
            xticks = list(nice_ticks(x0, x1, 8))
        # ★ 必须滤掉轴范围外的刻度：只有 1 圈时轴被撑成 0.5~1.5，
        #   而整数刻度是 0/1/2 —— 0 和 2 会被画到 x=-331 / x=1561（画布外）。
        #   肉眼看图看不出来（在画布外嘛），但 SVG 里留着越界坐标，
        #   换个画布尺寸或缩放就冒出来了。实测踩过。
        xticks = [t for t in xticks if x0 <= t <= x1]
        for v in xticks:
            x = sx(v)
            o.append(f'<line x1="{x:.1f}" y1="{py0}" x2="{x:.1f}" y2="{py0+5}" '
                     f'stroke="{C_AXIS}" stroke-width="1"/>')
            o.append(f'<text x="{x:.1f}" y="{py0+26}" font-size="{FS_TICK}" '
                     f'text-anchor="middle">{esc(fmt_num(v))}</text>')
        # ---- 边框 ----
        o.append(f'<rect x="{px0}" y="{py1}" width="{px1-px0}" height="{py0-py1}" '
                 f'fill="none" stroke="{C_AXIS}" stroke-width="1.3"/>')

        # ---- 曲线（先画右轴，再画左轴，左轴压在上面）----
        for s in self.right_series:
            pts = [(sx(x), sry(y)) for x, y in zip(s["xs"], s["ys"]) if y is not None]
            o.append(f'<path d="{_path(pts)}" fill="none" stroke="{s["color"]}" '
                     f'stroke-width="{LW_THIN}" opacity="0.8"/>')
        # 点多的时候把标记缩小（576 个方块挤在一起会糊成一片）
        npts = max((len(s["xs"]) for s in self.left_series + self.right_series),
                   default=0)
        mscale = 1.0 if npts <= 60 else (0.62 if npts <= 200 else 0.0)
        for s in self.left_series:
            pts = [(sx(x), sly(y)) for x, y in zip(s["xs"], s["ys"]) if y is not None]
            o.append(f'<path d="{_path(pts)}" fill="none" stroke="{s["color"]}" '
                     f'stroke-width="{LW_MAIN}"/>')
        for s in self.right_series:
            for x, y in zip(s["xs"], s["ys"]):
                if y is not None and mscale:
                    o.append(self._marker(sx(x), sry(y), s["color"], s["marker"], mscale))
        # 左轴的标记最后画 —— 它是主角，别被右轴的点盖住
        for s in self.left_series:
            for x, y in [(sx(a), sly(b)) for a, b in zip(s["xs"], s["ys"]) if b is not None]:
                if mscale:
                    o.append(self._marker(x, y, s["color"], s["marker"], mscale))

        # ---- 图内文字（数据坐标，倍率段标签等）----
        for t in self.texts:
            fill = f' fill="{t["color"]}"' if t["color"] else ""
            o.append(f'<text x="{sx(t["x"]):.1f}" y="{sly(t["y"]):.1f}" '
                     f'font-size="{t["size"]}" text-anchor="{t["anchor"]}"'
                     f'{fill}>{esc(t["text"])}</text>')

        # ---- 标题 ----
        o.append(f'<text x="{(px0+px1)/2:.0f}" y="{self.MT-20}" font-size="{FS_TITLE}" '
                 f'text-anchor="middle">{esc(self.title)}</text>')
        # ---- 轴标签 ----
        o.append(f'<text x="{(px0+px1)/2:.0f}" y="{self.H-28}" font-size="{FS_LABEL}" '
                 f'text-anchor="middle">{esc(self.x_label)}</text>')
        o.append(f'<text x="30" y="{(py0+py1)/2:.0f}" font-size="{FS_LABEL}" '
                 f'text-anchor="middle" transform="rotate(-90 30 {(py0+py1)/2:.0f})">'
                 f'{esc(self.left_label)}</text>')
        if self.right_series:
            o.append(f'<text x="{self.W-24}" y="{(py0+py1)/2:.0f}" font-size="{FS_LABEL}" '
                     f'text-anchor="middle" transform="rotate(90 {self.W-24} {(py0+py1)/2:.0f})">'
                     f'{esc(self.right_label)}</text>')

        # ---- 测试条件文字框（左上，压在数据之上但不遮曲线）----
        if self.notes:
            bw = max(self._w(n) for n in self.notes) * (FS_NOTE * 0.46) + 28
            bh = len(self.notes) * (FS_NOTE + 13) + 20
            bx, by = px0 + 16, py1 + 14
            o.append(f'<rect x="{bx}" y="{by}" width="{bw:.0f}" height="{bh:.0f}" '
                     f'rx="7" fill="{C_NOTE_BG}" stroke="{C_NOTE_ED}" stroke-width="1"/>')
            for i, n in enumerate(self.notes):
                fill = f' fill="{C_WARN}"' if n.startswith("⚠") else ""
                o.append(f'<text x="{bx+15}" y="{by+FS_NOTE+13+i*(FS_NOTE+13)}" '
                         f'font-size="{FS_NOTE}"{fill}>{esc(n)}</text>')
        # ---- 图例（右上）----
        lg = [(s["label"], s["color"], s["marker"])
              for s in self.left_series + self.right_series if s.get("label")]
        # 图例宽度**按最长条目标签算**，不再固定 275 —— 卡片越小，越有机会
        # 塞进曲线之间的空档。实测 6 条曲线、只有 4 圈数据的图上，275 宽的固定卡片
        # 六个候选位全被线穿过（最小命中数 2）；收窄后能找到完全不压线的位置。
        lg_text = max((self._w(lab) for lab, _, _ in lg), default=8)
        LG_W = min(max(76 + lg_text * (FS_LEGEND * 0.52) + 14, 180), 320)
        LG_H = 34 * max(len(lg), 1) + 8
        # 候选位置除了四个角，**还要有"上下两条平线之间的中段"**。
        # 为什么：容量和效率都是"开头一变、之后很平"的形状，于是上下各压着一条线，
        # 四个角**全都**有线穿过 —— 只挑角的话必然压住点什么。实测就是图例被蓝线穿了。
        mid_y = py1 + (py0 - py1 - LG_H) // 2
        corners = [(px1 - LG_W - 8, py1 + 8), (px0 + 8, py1 + 8),
                   (px1 - LG_W - 8, mid_y), (px0 + 8, mid_y),
                   (px1 - LG_W - 8, py0 - LG_H - 8), (px0 + 8, py0 - LG_H - 8)]
        allpts = [(sx(x), sly(y)) for s in self.left_series
                  for x, y in zip(s["xs"], s["ys"]) if y is not None]
        allpts += [(sx(x), sry(y)) for s in self.right_series
                   for x, y in zip(s["xs"], s["ys"]) if y is not None]
        # ★ 命中计分必须把**线段**也算上，不能只数顶点。
        #   实测的 bug：只有 2 圈时顶点全在画面左右边缘、候选框内缩 8px 一个都框不住，
        #   六个候选全得 0 分，min() 按顺序取了右上角 —— 而两点连成的直线横扫整幅图，
        #   正好从那个框里穿过，图例就压在线上。一句话：点少的时候，点代表不了线。
        segs: list[tuple[float, float, float, float]] = []
        for s in self.left_series:
            pts = [(sx(x), sly(y)) for x, y in zip(s["xs"], s["ys"]) if y is not None]
            segs += [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
        for s in self.right_series:
            pts = [(sx(x), sry(y)) for x, y in zip(s["xs"], s["ys"]) if y is not None]
            segs += [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
        note_rect = None
        if self.notes:
            nw = max(self._w(n) for n in self.notes) * (FS_NOTE * 0.46) + 28
            nh = len(self.notes) * (FS_NOTE + 13) + 20
            note_rect = (px0 + 16, py1 + 14, nw, nh)
        def _hits(bx, by):
            n = sum(1 for x, y in allpts
                    if bx <= x <= bx + LG_W and by <= y <= by + LG_H)
            n += sum(1 for sg in segs
                     if _seg_hits_rect(sg[0], sg[1], sg[2], sg[3],
                                       bx, by, LG_W, LG_H))
            # 和"测试条件文字框"有交叠就直接排除（给个巨大的惩罚值）
            if note_rect:
                nx, ny, nw, nh = note_rect
                if bx < nx + nw and nx < bx + LG_W and by < ny + nh and ny < by + LG_H:
                    n += 10 ** 6
            return n
        lx0, ly0 = min(corners, key=lambda c: _hits(*c))
        # 六个候选各自的命中数（诊断与测试用）：挑出来的必须是最小的那个，
        # 而且"最小也压线"这件事要看得见 —— 不然只能靠肉眼盯图。
        self.legend_scores = sorted((_hits(*c), c) for c in corners)
        self.legend_hits = self.legend_scores[0][0]
        # 图例先铺一层半透明白底：万一还是落在线上，线从文字中间穿过去会很难读。
        # 半透明而不是纯白 —— 压住的是曲线，透一点能看出下面有东西，不会以为那里没数据。
        o.append(f'<rect x="{lx0}" y="{ly0}" width="{LG_W}" height="{LG_H}" rx="7" '
                 f'fill="#ffffff" fill-opacity="0.85"/>')
        for i, (lab, col, mk) in enumerate(lg):
            lx, ly = lx0 + 12, ly0 + 22 + i * 32
            o.append(f'<path d="M {lx},{ly} L {lx+30},{ly}" stroke="{col}" '
                     f'stroke-width="2"/>')
            o.append(self._marker(lx + 15, ly, col, mk))
            o.append(f'<text x="{lx+44}" y="{ly+6}" font-size="{FS_LEGEND}">{esc(lab)}</text>')

        # ---- 箭头注释 ----
        if self.anno:
            ax, ay = sx(self.anno["src"][0]), sly(self.anno["src"][1])
            tx, ty = sx(self.anno["dst"][0]), sly(self.anno["dst"][1])
            x1a, y1a = tx, ty
            x2a, y2a = ax, ay
            o.append(f'<line x1="{x2a:.1f}" y1="{y2a:.1f}" x2="{x1a:.1f}" y2="{y1a:.1f}" '
                     f'stroke="#777" stroke-width="1.4"/>')
            ang = math.atan2(y1a - y2a, x1a - x2a)
            for d in (2.7, -2.7):
                o.append(f'<line x1="{x1a:.1f}" y1="{y1a:.1f}" '
                         f'x2="{x1a-13*math.cos(ang+d):.1f}" '
                         f'y2="{y1a-13*math.sin(ang+d):.1f}" '
                         f'stroke="#777" stroke-width="1.4"/>')
            n = len(self.anno["text"])
            for i, t in enumerate(self.anno["text"]):
                # 第一行要在最上面：所以倒着排（i=0 给最高那行）
                o.append(f'<text x="{x2a:.1f}" y="{y2a-10-(n-1-i)*(FS_ANNO+7):.1f}" font-size="{FS_ANNO}" '
                         f'text-anchor="middle">{esc(t)}</text>')

        # ---- 图注（画布下方，绘图区之外）----
        # ⚠ 开头的行走警告色 —— 图会脱离网页被贴进报告，警告必须跟着图走。
        for i, line in enumerate(self.caption):
            fill = f' fill="{C_WARN}"' if line.startswith("⚠") else ""
            o.append(f'<text x="{self.ML}" y="{self.H + 24 + i * cap_step}" '
                     f'font-size="{FS_NOTE}"{fill}>{esc(line)}</text>')

        o.append("</g></svg>")
        return "\n".join(o)

    @staticmethod
    def _w(s: str) -> int:
        """粗略算中文/英文混排的显示宽度（中文按 2 个字符算）。"""
        return sum(2 if ord(c) > 0x2E80 else 1 for c in s)

    @staticmethod
    def _marker(x: float, y: float, color: str, kind: str, scale: float = 1.0) -> str:
        if scale <= 0:
            return ""
        if kind == "square":
            q = SQ * scale
            return (f'<rect x="{x-q/2:.1f}" y="{y-q/2:.1f}" width="{q:.1f}" '
                    f'height="{q:.1f}" fill="#ffffff" stroke="{color}" '
                    f'stroke-width="{1.9*scale:.2f}"/>')
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{R_MARK*scale:.2f}" '
                f'fill="#ffffff" stroke="{color}" stroke-width="{1.9*scale:.2f}"/>')


# ---------------------------------------------------------------------------

def cycles_of(steps: list[dict], mass_g: float | None = None) -> list[dict]:
    """按「一圈 = 一对充放电」切分并算指标。mass_g 给了就同时算比容量。"""
    blocks = group_cycles(steps) or []
    out: list[dict] = []
    first_dis = None
    for i, blk in enumerate(blocks, 1):
        c = d = eng_c = eng_d = 0.0
        dcir = None
        for r in blk:
            st = _steptype(r)
            cap = float(r.get("cap") or 0) * CAP_TO_MAH
            eng = float(r.get("eng") or 0) * CAP_TO_MAH
            if st in CHARGING_STEPTYPES:
                c += cap
                eng_c += eng
            elif st in DISCHARGING_STEPTYPES:
                d += cap
                eng_d += eng
                v = float(r.get("dcir") or 0)
                if v > 0:
                    dcir = v
        if first_dis is None:
            first_dis = d
        rec = dict(cycle=i, chg=c, dis=d,
                   ce=ce_pct(c, d),
                   ee=(eng_d / eng_c * 100) if eng_c else None,
                   retention=(d / first_dis * 100) if first_dis else None,
                   dcir_mohm=dcir,
                   # 平均电压图的原料（mWh ÷ mAh = V）；加了新键，旧消费方不受影响
                   chg_eng=eng_c, dis_eng=eng_d)
        if mass_g:
            rec["dis_sp"] = d / mass_g
            rec["chg_sp"] = c / mass_g
        out.append(rec)
    return out


def load_steps(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_detail(path: Path) -> list[dict]:
    """明细层加载。**行数大（几万）**——只给绘图/计算用，别整份塞给 AI 或打印。"""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 报告参考图（2026-10-01）：长循环 / 倍率 / 首效 V-Q / 平均电压 / dQ-dV
#
# 每个函数只接**数据**（不接路径）—— 路径的活归 CLI，测试直接喂合成数据。
# 全部基于同一套 CycleChart，保证五张图风格一致（字体、轴、图例、图注）。
# ---------------------------------------------------------------------------

# 多系列调色板（参考图里 Baseline/PBSF 那种双样品对比，以及 dQ/dV 的圈数渐变）
PALETTE = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]
# dQ/dV 参考图是"橙黄由浅到深 = 圈数增加"，照抄这个视觉语言
PALETTE_DQDV = ["#1f4e9c", "#12a08a", "#b8860b", "#d35400", "#c0392b", "#6c3483"]
# 逐圈 dQ/dV 图按圈号配色。**必须是互相分得开的颜色** —— 第一版用了
# 一片橙黄渐变（#ffd54f/#ffc107/#ffb300…），出图后三条曲线肉眼分不出谁是谁，
# 图例也成了三条一模一样的色块，"按圈号区分"这件事等于没做。
# 现在改成色相递进、明度都够深（白底上也看得清），相邻圈的 RGB 距离足够大。
# 对比图的样品配色：第一样品蓝、第二样品红 —— 五张参考图统一的视觉语言
# （参考图里就是 Baseline=蓝、PBSF=红 两个样品叠在一张图里）
CMP_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"]


def chart_long_cycle(title: str, cells: list[dict],
                     mass_g: float | None = None) -> CycleChart:
    """参考图一（长循环）**对比版**：多个样品的充/放容量（左轴）＋库仑效率（右轴）。

    cells: [{"label": 样品名, "cs": cycles_of / cycles_of_detail 的输出}, …]
    每样品一个颜色（第一蓝、第二红）；充电与放电用不同 marker 区分。

    mass_g 给了 → 左轴用**放电比容量 (mAh g⁻¹)**（参考图就是这个口径，不同载量的
    电池也才能比）；没给 → 退回绝对容量 (mAh)，轴标题如实标注。
    """
    use_sp = mass_g is not None
    kc, kd = ("chg_sp", "dis_sp") if use_sp else ("chg", "dis")
    ylab = "放电比容量 (mAh g⁻¹)" if use_sp else "容量 (mAh)"
    fig = CycleChart(title, ylab, "库仑效率 (%)")
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        lab = cell["label"]
        xs = [c["cycle"] for c in cell["cs"]]
        fig.add_left(xs, [c.get(kd) for c in cell["cs"]], f"{lab} 放电",
                     color=color, marker="circle")
        fig.add_left(xs, [c.get(kc) for c in cell["cs"]], f"{lab} 充电",
                     color=color, marker="square")
        fig.add_right(xs, [c.get("ce") for c in cell["cs"]], f"{lab} 效率",
                      color=color, marker="circle")
    return fig


def cycle_rates(steps: list[dict],
                nominal_mah: float | None = None) -> list[dict]:
    """每圈的倍率：该圈**放电工步**平均电流的绝对值。

    nominal_mah 给了 → 换算成 C 倍率（mA ÷ 标称容量数值），label 形如 "0.5"；
    不给 → label 直接用电流，形如 "1.2 mA"。
    ★ label 按 2 位小数取整后再分组：每圈电流有微小波动（实测 1.00008 /
    0.999936 / 1.000004 这种），按精确值分组会把一个倍率段打碎成十几个标签。
    返回与 cycles_of 的圈号一一对应：[{cycle, ma, rate, label}, …]。
    """
    out: list[dict] = []
    for i, blk in enumerate(group_cycles(steps) or [], 1):
        currs = []
        for r in blk:
            st = _steptype(r)
            raw = r.get("startcurr")
            if st in DISCHARGING_STEPTYPES and raw not in (None, ""):
                try:
                    currs.append(abs(float(raw)))
                except ValueError:
                    pass
        ma = (sum(currs) / len(currs) * 1000.0) if currs else 0.0
        if nominal_mah:
            rate = round(ma / nominal_mah, 2)
            out.append({"cycle": i, "ma": ma, "rate": rate, "label": f"{rate:g}"})
        else:
            out.append({"cycle": i, "ma": ma, "rate": ma, "label": f"{round(ma, 2):g} mA"})
    return out


def chart_rate(title: str, cells: list[dict],
               mass_g: float | None = None) -> CycleChart:
    """参考图二（倍率）**对比版**：各样品的阶梯容量曲线叠加。

    cells: [{"label", "cs", "rates"}] —— rates 是该样品的倍率标签列表
    （工步层用 cycle_rates，明细层用 rates_from_cs）。
    ★ 倍率段标签只画**第一个样品**的：两样品的倍率 schedule 相同，标两遍会重叠。

    mass_g 给了 → 左轴用**放电比容量 (mAh g⁻¹)**；没给 → 绝对容量 (mAh)。
    （倍率标签不受影响：C 倍率是按标称容量算的，与这里画什么无关。）
    """
    kd = "dis_sp" if mass_g is not None else "dis"
    ylab = "放电比容量 (mAh g⁻¹)" if mass_g is not None else "放电容量 (mAh)"
    fig = CycleChart(title, ylab, "", x_label="循环号")
    span_ys: list[float] = []
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        ys = [c.get(kd) for c in cell["cs"]]
        span_ys += [v for v in ys if v is not None]
        fig.add_left([c["cycle"] for c in cell["cs"]], ys,
                     cell["label"], color=color)
        if i == 0:
            rate_by = {r["cycle"]: r for r in cell.get("rates", [])}
            segs: list[dict] = []
            for c in cell["cs"]:
                lab = (rate_by.get(c["cycle"]) or {}).get("label")
                if lab is None:
                    continue
                if segs and segs[-1]["label"] == lab:
                    segs[-1]["cycles"].append(c["cycle"])
                    segs[-1]["ys"].append(c.get(kd))
                else:
                    segs.append({"label": lab, "cycles": [c["cycle"]],
                                 "ys": [c.get(kd)]})
            for seg in segs:
                if len(seg["cycles"]) < 2:
                    continue    # 单圈的标签没地方站（太挤），至少两个点才标
                mid = (seg["cycles"][0] + seg["cycles"][-1]) / 2
                top = max(v for v in seg["ys"] if v is not None)
                span = max(span_ys) - (min(span_ys) if span_ys else 0)
                fig.add_text(mid, top + span * 0.05, seg["label"],
                             color=C_AXIS, size=17)
    return fig


def _detail_cycle_rows(detail_rows: list[dict], cycle: int,
                       exact: bool = False) -> list[dict]:
    """取「第 cycle 圈」的明细行。cycle 用**人话口径**（第 1 圈起），
    对应明细层 cycleid = cycle - 1（AGENTS.md：detail.cycleid 比 steps 的小 1）。
    若该圈没有任何行，回退到明细里出现的最小 cycleid（保证首圈永远画得出来）。

    exact=True 时**不回退**：多圈叠加的图必须"没有就是没有"，
    否则请求的第 50 圈会拿第 1 圈的数据顶上 —— 图上多出一条看不出错的假曲线。
    """
    def cid_of(r):
        try:
            return int(r.get("cycleid"))
        except (TypeError, ValueError):
            return None
    ids = sorted({cid_of(r) for r in detail_rows if cid_of(r) is not None})
    target = cycle - 1
    if target not in ids:
        if exact or not ids:
            return []
        target = ids[0]
    return [r for r in detail_rows if cid_of(r) == target]


def vq_curves(detail_rows: list[dict],
              cycle: int = 1, exact: bool = False) -> dict[str, list[tuple[float, float]]] | None:
    """指定圈的充/放电 **电压-容量** 曲线（参考图三的数据）。

    明细层的 cap 是**工步内累计值**（每工步从 0 起），所以 V-Q 直接可用：
    充电工步的 (cap, volt) 就是充电曲线，放电工步的就是放电曲线。
    返回 {"charge": [(q_mah, v)…], "discharge": […], "ice": 首效%|None}，
    q 单位 mAh；找不到该圈返回 None。
    """
    rows = _detail_cycle_rows(detail_rows, cycle, exact=exact)
    if not rows:
        return None

    def rows_of(sts: set[str]) -> list[tuple[float, float]]:
        pts = []
        for r in rows:
            if _steptype(r) in sts:
                try:
                    q = float(r.get("cap") or 0) * CAP_TO_MAH
                    v = float(r.get("volt") or 0)
                except ValueError:
                    continue
                pts.append((q, v))
        pts.sort(key=lambda p: p[0])
        return pts

    charge = rows_of(CHARGING_STEPTYPES)
    discharge = rows_of(DISCHARGING_STEPTYPES)
    q_chg = max((q for q, _ in charge), default=0.0)
    q_dis = max((q for q, _ in discharge), default=0.0)
    ice = ce_pct(q_chg, q_dis)     # 首圈效率同口径（负极半电池：充/放）
    return {"charge": charge, "discharge": discharge, "ice": ice}


def chart_vq(title: str, cells: list[dict]) -> CycleChart:
    """参考图三（首效）**对比版**：各样品首圈充/放电 电压-容量曲线，图例各标 ICE。

    cells: [{"label", "curves"}] —— curves 是 vq_curves() 的返回
    （charge/discharge 的 (Q_mAh, V) 点列 + ice）。
    """
    fig = CycleChart(title, "电压 (V)", "", x_label="容量 (mAh)")
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        cur = cell.get("curves") or {}
        ice = cur.get("ice")
        ice_txt = f"（ICE {ice:.2f}%）" if ice else ""
        fig.add_left([q for q, _ in cur.get("charge", [])],
                     [v for _, v in cur.get("charge", [])],
                     f"{cell['label']} 充电{ice_txt}", color=color)
        fig.add_left([q for q, _ in cur.get("discharge", [])],
                     [v for _, v in cur.get("discharge", [])],
                     f"{cell['label']} 放电", color=color)
    return fig


def chart_avgv(title: str, cells: list[dict]) -> CycleChart:
    """参考图四（平均电压）**对比版**：各样品的充/放电平均电压 vs 圈号。

    平均电压 = 该圈能量 ÷ 该圈容量（mWh ÷ mAh = V）。
    cs 来自 cycles_of / cycles_of_detail。
    """
    fig = CycleChart(title, "平均电压 (V)", "", x_label="循环号")
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        lab = cell["label"]
        fig.add_left([c["cycle"] for c in cell["cs"]],
                     [(c["chg_eng"] / c["chg"]) if c.get("chg") else None
                      for c in cell["cs"]],
                     f"{lab} 充电", color=color)
        fig.add_left([c["cycle"] for c in cell["cs"]],
                     [(c["dis_eng"] / c["dis"]) if c.get("dis") else None
                      for c in cell["cs"]],
                     f"{lab} 放电", color=color)
    return fig


def dqdv_curve(points: list[tuple[float, float]],
               bin_v: float = 0.005, smooth: int = 3) -> list[tuple[float, float]]:
    """从时间有序的 (Q_mAh, V) 序列算 **dQ/dV – V** 曲线（参考图五的数据）。

    方法：按电压分箱（默认 5 mV 一箱），箱内累计 ΣdQ 和 ΣdV，
    dQ/dV = ΣdQ ÷ ΣdV —— **ΣdV 带符号**：充电时电压往上走、dQ/dV 为正；
    放电时电压往下走（Q 还在增），dQ/dV 为负 —— 参考图里充放峰一正一负就是这么来的。
    最后对相邻箱做滑动平均（smooth 个箱）。为什么这么算：直接差分全是毛刺，
    先平滑再差分又会把峰抹平 —— 分箱对噪声最不敏感，峰位也保得住。

    （第一版这里除的是正的箱宽，放电分支算出正斜率 —— 是测试抓出来的。）
    """
    if len(points) < 2:
        return []
    bins: dict[int, list] = {}
    last_q = points[0][0]
    last_v = points[0][1]
    for q, v in points[1:]:
        dv = v - last_v
        if abs(dv) < 1e-9:
            last_q, last_v = q, v
            continue
        b = int(math.floor(min(last_v, v) / bin_v))
        acc = bins.setdefault(b, [0.0, 0.0])
        acc[0] += (q - last_q)
        acc[1] += dv
        last_q, last_v = q, v
    if not bins:
        return []
    ks = sorted(bins)
    raw = []
    for k in ks:
        dq, dv = bins[k]
        mid = k * bin_v + bin_v / 2
        raw.append((mid, dq / dv if abs(dv) > 1e-12 else 0.0))
    if smooth <= 1:
        return raw
    out: list[tuple[float, float]] = []
    half = smooth // 2
    for i in range(len(raw)):
        lo, hi = max(0, i - half), min(len(raw), i + half + 1)
        out.append((raw[i][0], sum(v for _, v in raw[lo:hi]) / (hi - lo)))
    return out


def chart_dqdv(title: str, cells: list[dict],
               y_label: str = "dQ/dV (mAh V⁻¹)") -> CycleChart:
    """参考图五（dQ/dV）**对比版**：每样品一条代表圈的 dQ/dV 曲线，按样品配色。

    cells: [{"label", "curves": {圈号: dqdv_curve() 的返回}}] ——
    每样品取**圈号最小**的那条（调用方把想对比的圈放进 curves 即可）。
    """
    fig = CycleChart(title, y_label, "", x_label="电压 (V)")
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        cycs = sorted(cell.get("curves", {}))
        if not cycs:
            continue
        pts = cell["curves"][cycs[0]]
        # pts 是 (电压, dQ/dV) —— X=电压、Y=dQ/dV。（更早的版本画反了，
        # 图上"电压"跑到 Y 轴去 —— 是人眼比对参考图时抓出来的。）
        fig.add_left([v for v, _ in pts], [q for _, q in pts],
                     f"{cell['label']} 第{cycs[0]}圈", color=color)
    return fig


def chart_dqdv_branch(title: str, cells: list[dict], branch: str = "dis") -> CycleChart:
    """dQ/dV **单分支**对比图 —— 报告的图 4（放电分支）与图 5（充电分支）分开出。

    cells: [{"label", "dqdv": {"chg": dqdv_curve(充电点), "dis": dqdv_curve(放电点)}}]
    branch="dis" 画放电分支（离子嵌入，参考图里为负峰方向），"chg" 画充电分支。

    为什么要拆：渲染模板的 FIGURE:4 与 FIGURE:5 是两个独立图位，
    报告正文（S34A）也要求"分放电分支与充电分支分别写主峰位置"。
    """
    br_name = "放电分支" if branch == "dis" else "充电分支"
    fig = CycleChart(title, "dQ/dV (mAh V⁻¹)", "", x_label="电压 (V)")
    for i, cell in enumerate(cells):
        color = CMP_COLORS[i % len(CMP_COLORS)]
        dq = cell.get("dqdv", {}).get(branch, [])
        if not dq:
            continue
        cycs = sorted(cell.get("curves", {}))
        cyc_txt = f" 第{cycs[0]}圈" if cycs else ""
        fig.add_left([v for v, _ in dq], [q for _, q in dq],
                     f"{cell['label']}{cyc_txt}", color=color)
    return fig


def dqdv_per_cycle(detail_rows: list[dict],
                   cycles: list[int]) -> dict[int, dict[str, list[tuple[float, float]]]]:
    """多圈的 dQ/dV：{圈号: {"chg": [(V, dQ/dV)…], "dis": [...]}}。

    **exact 取圈**（不让缺的圈回退到第 1 圈）+ 只留充放都算得出来的圈 ——
    图上少一条曲线看得见，多一条假曲线看不见（后者才要命）。
    """
    out: dict[int, dict[str, list[tuple[float, float]]]] = {}
    for cyc in cycles:
        cur = vq_curves(detail_rows, cycle=cyc, exact=True)
        if not cur:
            continue
        chg = dqdv_curve(cur["charge"]) if cur["charge"] else []
        dis = dqdv_curve(cur["discharge"]) if cur["discharge"] else []
        if chg or dis:
            out[cyc] = {"chg": chg, "dis": dis}
    return out


def pick_dqdv_cycles(requested: list[int], available: list[int],
                     max_n: int = 6) -> tuple[list[int], list[int]]:
    """要画哪些圈 → (画出来的圈, 缺的圈)。

    规则：先取"请求 ∩ 已有"。若不足 2 圈（测试刚开始，请求的第 10/50/100…
    都还没到），就**退回到已有的圈**（≤ 请求里的最大圈号，最多 max_n 个），
    并把缺的圈原样报给调用方去写进图注 —— 少了要看得出来，不能补不能编。
    """
    have = [c for c in requested if c in set(available)]
    if len(have) >= 2:
        return have, [c for c in requested if c not in set(have)]
    cap = max(requested) if requested else (max(available) if available else 0)
    drawn = [c for c in available if c <= cap][:max_n]
    return drawn, [c for c in requested if c not in set(drawn)]


def chart_dqdv_sample(title: str, cell: dict,
                      y_label: str = "dQ/dV (mAh V⁻¹)",
                      reduction_positive: bool = True) -> CycleChart:
    """**一个样品一张**的 dQ/dV 图：同圈的氧化(充电)与还原(放电)画在同一幅里，
    不同圈用一族色（`PALETTE_DQDV` 由浅到深）区分 —— 就是参考图五的样子。

    cell: {"label", "dqdv_multi": dqdv_per_cycle() 的返回}

    reduction_positive=True 把**还原（嵌入/放电）画成正峰**。为什么默认翻符号：
    提示词体系 S34A 明确写"正峰为还原即离子嵌入、负峰为氧化即离子脱出"，
    而 dqdv_curve 按 ΣdV 符号算出来充电为正 —— 不翻的话正文和图会互相打脸。
    """
    fig = CycleChart(title, y_label, "", x_label="电压 (V)")
    dm = cell.get("dqdv_multi") or {}
    # 两个分支乘同一个符号：原本"充电为正、放电为负"，乘 -1 就变成
    # "放电(还原/嵌入)为正、充电(氧化/脱出)为负"。
    sign = -1.0 if reduction_positive else 1.0
    for i, cyc in enumerate(sorted(dm)):
        color = PALETTE_DQDV[i % len(PALETTE_DQDV)]
        brs = dm[cyc]
        # 图例只挂一份/圈（同一圈的两个分支同色，一上一下分居轴两侧）
        for key, lab in (("chg", f"第 {cyc} 圈"), ("dis", None)):
            pts = brs.get(key) or []
            if not pts:
                continue
            fig.add_left([v for v, _ in pts], [sign * q for _, q in pts],
                         lab, color=color)
    return fig


def cycles_of_detail(detail_rows: list[dict],
                     mass_g: float | None = None) -> list[dict]:
    """从**明细层**算逐圈指标 —— 输出键位与 cycles_of 完全一致。

    为什么需要：live 区只有明细层，而**跑着的测试工步层容量不回填（实测全 0）**，
    所以进行中数据的逐圈指标只能从明细层算。

    与 cycles_of 的算法差异只有一处，但很关键：明细层的 cap 是**工步内累计值**，
    工步容量 = 该工步**末条** cap（时间序最后一条）—— 对明细行求和会把同一份
    容量重复计几十遍（AGENTS.md 记过的坑）。energy 同理。

    圈号 = 明细 cycleid + 1（人话口径）。明细层没有内阻列 → dcir 恒为 None；
    另带每圈平均放电电流 "ma"（**mA**，倍率标签用）。
    """
    step_end: dict[tuple, dict] = {}
    curr_by_cycle: dict[int, list[float]] = {}
    for r in detail_rows:
        st = _steptype(r)
        try:
            cid = int(r.get("cycleid"))
        except (TypeError, ValueError):
            continue
        key = (cid, r.get("stepid"))
        try:
            cap = float(r.get("cap") or 0)
            eng = float(r.get("eng") or 0)
        except ValueError:
            cap = eng = 0.0
        # 文件本就按时间序排列，直接覆盖 = 留下末条
        step_end[key] = {"st": st, "cap": cap, "eng": eng}
        if st in DISCHARGING_STEPTYPES:
            try:
                curr_by_cycle.setdefault(cid, []).append(abs(float(r.get("curr") or 0)))
            except ValueError:
                pass

    per_cycle: dict[int, dict] = {}
    for (cid, _sid), e in step_end.items():
        chg = e["st"] in CHARGING_STEPTYPES
        dis = e["st"] in DISCHARGING_STEPTYPES
        if not chg and not dis:
            continue                    # rest 等工步不参与容量累计
        pc = per_cycle.setdefault(cid, {"chg": 0.0, "dis": 0.0,
                                        "chg_eng": 0.0, "dis_eng": 0.0})
        if chg:
            pc["chg"] += e["cap"]
            pc["chg_eng"] += e["eng"]
        if dis:
            pc["dis"] += e["cap"]
            pc["dis_eng"] += e["eng"]

    out: list[dict] = []
    first_dis = None
    for cid in sorted(per_cycle):
        pc = per_cycle[cid]
        c = pc["chg"] * CAP_TO_MAH
        d = pc["dis"] * CAP_TO_MAH
        if first_dis is None:
            first_dis = d
        ma = (sum(curr_by_cycle.get(cid, [])) / len(curr_by_cycle[cid]) * 1000.0
              if curr_by_cycle.get(cid) else 0.0)
        rec = dict(cycle=cid + 1, chg=c, dis=d,
                   ce=ce_pct(c, d),
                   ee=(pc["dis_eng"] / pc["chg_eng"] * 100) if pc["chg_eng"] else None,
                   retention=(d / first_dis * 100) if first_dis else None,
                   dcir_mohm=None,
                   chg_eng=pc["chg_eng"] * CAP_TO_MAH,
                   dis_eng=pc["dis_eng"] * CAP_TO_MAH,
                   ma=ma)
        if mass_g:
            rec["dis_sp"] = d / mass_g
            rec["chg_sp"] = c / mass_g
        out.append(rec)
    return out


def rates_from_cs(cs: list[dict],
                  nominal_mah: float | None = None) -> list[dict]:
    """从含 "ma"（每圈平均放电电流，mA）的逐圈数据构造倍率标签（chart_rate 用）。

    分组口径与 cycle_rates 相同：label 按 2 位小数取整，防止电流微波动把段打碎。
    """
    out: list[dict] = []
    for c in cs:
        ma = c.get("ma") or 0.0
        if nominal_mah:
            rate = round(ma / nominal_mah, 2)
            out.append({"cycle": c["cycle"], "ma": ma, "rate": rate,
                        "label": f"{rate:g}"})
        else:
            out.append({"cycle": c["cycle"], "ma": ma,
                        "label": f"{round(ma, 2):g} mA"})
    return out


def resolve_dataset(data: Path, entry: str,
                    source: str) -> tuple[Path | None, str]:
    """找某个通道的数据目录。entry = channel= 文件夹名（如 27-188-10-1）。

    source=auto：live/ 有就用 live（进行中的新实验），否则正式数据集。
    同一通道多个 testid 时取**数字最大**的（最新的测试）。
    返回 (目录, 来源标记 live/official)；找不到返回 (None, "")。
    """
    cands: list[tuple[Path, str]] = []
    for root, kind in ((data / "live", "live"), (data, "official")):
        d = root / f"channel={entry}"
        if not d.is_dir():
            continue
        tids = sorted(int(x.parts[-1].split("=")[1]) for x in d.glob("testid=*")
                      if x.parts[-1].split("=")[1].isdigit())
        if tids:
            cands.append((d / f"testid={tids[-1]}", kind))
    if not cands:
        return None, ""
    if source == "auto":
        for dd, kind in cands:          # live（进行中的新实验）优先
            if kind == "live":
                return dd, kind
        return cands[0]
    if source == "steps":               # 工步层只在正式数据集里
        for dd, kind in cands:
            if (dd / "steps.csv").exists():
                return dd, kind
        return None, ""
    for dd, kind in cands:              # detail：两边都可能有
        if (dd / "detail.csv").exists():
            return dd, kind
    return None, ""


def main() -> int:
    p = argparse.ArgumentParser(description="零依赖 SVG 出图 —— 报告对比图（多通道一图）")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--out", default="./figs")
    p.add_argument("--chart", choices=["long", "rate", "vq", "avgv", "dqdv"],
                   required=True,
                   help="报告参考图：长循环/倍率/首效VQ/平均电压/dQdV")
    p.add_argument("--compare", required=True,
                   help="要对比的通道，逗号分隔（至少 2 个），"
                        "如 27-188-10-1,27-188-10-2")
    p.add_argument("--labels", default=None,
                   help="图例样品名，逗号分隔（默认用通道号），如 改性,基线")
    p.add_argument("--source", choices=["auto", "steps", "detail"], default="auto",
                   help="auto=优先 live（进行中的新数据），否则正式数据集；"
                        "steps=只用正式数据集的工步层；detail=只用明细层")
    p.add_argument("--cycle", type=int, default=1,
                   help="vq / dqdv 用：第几圈（人话口径，第 1 圈起）")
    p.add_argument("--dqdv-cycles", default=None,
                   help="dQ/dV 要画哪些圈（逗号分隔，如 1,10,50,100,200,300）。"
                        "给了就按「每样品一张、多圈叠加、充放同图」出（报告用的形态）；"
                        "不给则用 --cycle 出单圈对比图")
    p.add_argument("--dqdv-sign", choices=["dis", "chg"], default="dis",
                   help="dQ/dV 谁画成正峰：dis=还原/嵌入(放电)为正（默认，"
                        "与提示词体系 S34A 的口径一致），chg=氧化/脱出(充电)为正")
    p.add_argument("--dqdv-branch", choices=["both", "dis", "chg"], default="both",
                   help="dQ/dV 画哪个分支：both=合在一张，dis=放电分支，chg=充电分支"
                        "（报告模板的 FIGURE:4/5 是分开的两张）")
    p.add_argument("--mass-mg", default=None,
                   help="活性物质质量（mg）—— 长循环/倍率图的左轴用它换算比容量；"
                        "两个值（逗号分隔）可分别对应两个通道，如 1.184,1.20。"
                        "**不给就报错而不是悄悄用错数**：比容量的分母错了，"
                        "整张图的数就是假的")
    p.add_argument("--nominal-mah", type=float, default=None,
                   help="标称容量（mAh）—— 倍率图把电流换算成 C 倍率用；不给则标 mA")
    p.add_argument("--max-cycles", type=int, default=0, help="只画前 N 圈，0=全部")
    args = p.parse_args()

    data = Path(args.data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    entries = [e.strip() for e in args.compare.split(",") if e.strip()]
    if len(entries) < 2:
        print("对比图至少要 2 个通道：--compare 27-188-10-1,27-188-10-2",
              file=sys.stderr)
        return 2
    labels = ([l.strip() for l in args.labels.split(",") if l.strip()]
              if args.labels else entries)
    if len(labels) != len(entries):
        print("--labels 的个数要和 --compare 一致", file=sys.stderr)
        return 2

    # 活性物质质量：长循环/倍率图的左轴是**比容量**，分母错了整张图就是假的 ——
    # 所以这两张图没有质量就直接拒绝，而不是拿默认值悄悄算。
    masses: list[float | None] = [None] * len(entries)
    need_mass = args.chart in ("long", "rate")
    if args.mass_mg:
        parts = [x.strip() for x in str(args.mass_mg).split(",") if x.strip()]
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            print("--mass-mg 只能是数字（可用逗号分隔给每个通道一个）", file=sys.stderr)
            return 2
        if len(vals) == 1:
            masses = [vals[0]] * len(entries)
        elif len(vals) == len(entries):
            masses = vals
        else:
            print("--mass-mg 要么 1 个值（所有通道共用），要么和 --compare 个数一致",
                  file=sys.stderr)
            return 2
    elif need_mass:
        print(f"❌ --chart {args.chart} 的左轴是比容量，必须给活性物质质量：")
        print("     --mass-mg 1.184              （两颗电池同载量）")
        print("     --mass-mg 1.184,1.20         （分别对应两个通道）")
        print("   质量来自测试记录，采集数据里没有这个字段；")
        print("   拿不到就先别出这两张图（数量级会整整差几倍）。")
        return 2

    # ---- 逐通道取数 ----
    cells: list[dict] = []
    any_live = False
    for entry, label, mass_mg in zip(entries, labels, masses):
        mass_g = (mass_mg / 1000.0) if mass_mg else None
        dd, kind = resolve_dataset(data, entry, args.source)
        if dd is None:
            print(f"  ⚠️  {entry}（{label}）：找不到数据目录，跳过")
            continue
        sp, dp = dd / "steps.csv", dd / "detail.csv"
        src = args.source
        if src == "auto":
            src = "steps" if sp.exists() else ("detail" if dp.exists() else None)
        if src is None:
            print(f"  ⚠️  {entry}：没有 steps.csv 也没有 detail.csv，跳过")
            continue
        if kind == "live":
            any_live = True
        print(f"  {label} ← {dd}  [数据源: "
              f"{'明细层' if src == 'detail' else '工步层'}｜{kind}]")

        cell: dict = {"label": label}
        need_cs = args.chart in ("long", "rate", "avgv")
        # ---- 取数：按数据源决定逐圈指标的算法（工步层 / 明细层）----
        # ★ 之前这里把"需要逐圈数据"和"需要工步层"混为一谈：live 只有明细层，
        #   却在 need_cs 时去要 steps.csv，把 live 数据整个跳过了。
        steps: list[dict] | None = None
        detail: list[dict] | None = None
        if src == "steps":
            steps = load_steps(sp)
            cs = cycles_of(steps, mass_g)
        else:
            detail = load_detail(dp)
            cs = cycles_of_detail(detail, mass_g)
        if args.max_cycles:
            cs = cs[:args.max_cycles]
        if need_cs:
            if not cs:
                print(f"  ⚠️  跳过 {entry}："
                      f"{'工步层' if src == 'steps' else '明细层'}里没有完整的充放电对")
                continue
            cell["cs"] = cs
        # vq / dqdv 无论哪个源都要明细层
        if args.chart in ("vq", "dqdv") and detail is None:
            if not dp.exists():
                print(f"  ⚠️  {entry}：需要明细层但没有 detail.csv，跳过")
                continue
            detail = load_detail(dp)
        if args.chart == "rate":
            cell["rates"] = (cycle_rates(steps, args.nominal_mah)
                             if src == "steps"
                             else rates_from_cs(cell["cs"], args.nominal_mah))
        if args.chart == "vq":
            cur = vq_curves(detail, cycle=args.cycle)
            if not cur or not (cur["charge"] or cur["discharge"]):
                print(f"  ⚠️  {entry}：第 {args.cycle} 圈没有明细数据，跳过")
                continue
            cell["curves"] = cur
        if args.chart == "dqdv":
            if args.dqdv_cycles:
                # 每样品一张、多圈叠加：圈列表 = 请求 ∩ 已完成；不足 2 圈退回到已有圈
                want = [int(x) for x in str(args.dqdv_cycles).split(",") if x.strip()]
                done = [c["cycle"] for c in cs if (c.get("dis") or 0) > 0] or \
                       sorted({int(r.get("cycleid") or 0) + 1 for r in detail})
                drawn, missing = pick_dqdv_cycles(want, done)
                cell["dqdv_multi"] = dqdv_per_cycle(detail, drawn)
                cell["dqdv_cycles_drawn"] = sorted(cell["dqdv_multi"])
                cell["dqdv_cycles_missing"] = missing
                if not cell["dqdv_multi"]:
                    print(f"  ⚠️  {entry}：没有任何一圈算得出 dQ/dV，跳过")
                    continue
            else:
                cur = vq_curves(detail, cycle=args.cycle)
                if not cur:
                    print(f"  ⚠️  {entry}：第 {args.cycle} 圈没有明细数据，跳过")
                    continue
                # 合图（both）与分分支（dis/chg）两种结构都备好，按 --dqdv-branch 取用
                cell["curves"] = {args.cycle: dqdv_curve(cur["charge"])
                                  + dqdv_curve(cur["discharge"])}
                cell["dqdv"] = {"chg": dqdv_curve(cur["charge"]),
                                "dis": dqdv_curve(cur["discharge"])}
        cells.append(cell)

    if len(cells) < 1:
        print("没有任何可画的数据", file=sys.stderr)
        return 1

    # ---- 画对比图 ----
    suffix = {"long": "长循环", "rate": "倍率", "vq": "首效VQ",
              "avgv": "平均电压", "dqdv": "dQdV"}[args.chart]
    if args.chart == "dqdv" and args.dqdv_branch != "both":
        # 分支图是两个独立文件（报告模板的 FIGURE:4/5），名字必须区分开，
        # 否则放电/充电两次运行互相覆盖（实测就是这样丢了一张）。
        suffix += "放电分支" if args.dqdv_branch == "dis" else "充电分支"
    title = f"{' vs '.join(labels)}　（{suffix}）"

    if args.chart == "dqdv" and args.dqdv_cycles:
        # 每样品一张（报告用）：一个文件一张图，文件名按样品名区分
        for cell in cells:
            drawn = cell.get("dqdv_cycles_drawn") or []
            missing = cell.get("dqdv_cycles_missing") or []
            fig = chart_dqdv_sample(
                f"{cell['label']}　（dQ/dV）", cell,
                reduction_positive=(args.dqdv_sign == "dis"))
            lines = [f"已画圈：{'、'.join('第 %d 圈' % c for c in drawn)}"]
            if missing:
                lines.append("⚠ 尚未到达的圈：" + "、".join("第 %d 圈" % c for c in missing))
            if args.dqdv_sign == "dis":
                lines.append("符号口径：还原（嵌入/放电）为正峰")
            # 放图注而不是图内说明框 —— 说明框固定左上角，会压住 dQ/dV 的峰
            fig.set_caption(lines)
            dst = out / ("dQdV_" + str(cell["label"]) + ".svg")
            dst.write_text(fig.svg(), encoding="utf-8")
            print(f"  ✅ {dst}")
        return 0

    if args.chart == "long":
        fig = chart_long_cycle(title, cells, masses[0] / 1000.0 if args.mass_mg else None)
    elif args.chart == "rate":
        fig = chart_rate(title, cells, masses[0] / 1000.0 if args.mass_mg else None)
    elif args.chart == "vq":
        fig = chart_vq(title, cells)
    elif args.chart == "avgv":
        fig = chart_avgv(title, cells)
    elif args.dqdv_branch == "dis":
        fig = chart_dqdv_branch(title, cells, branch="dis")
    elif args.dqdv_branch == "chg":
        fig = chart_dqdv_branch(title, cells, branch="chg")
    else:
        fig = chart_dqdv(title, cells)
    caps: list[str] = []
    if args.mass_mg:
        used = "、".join(f"{l} {m:g} mg" for l, m in zip(labels, masses) if m)
        caps.append(f"活性物质质量：{used}（来自测试记录）")
    if any_live:
        caps.append("含进行中测试的数据（每日增量）—— "
                    "趋势观察用，结论以测试结束后的正式数据集为准")
    if caps:
        fig.set_caption(caps)
    dst = out / ("对比_" + suffix + "_" + "_vs_".join(labels) + ".svg")
    dst.write_text(fig.svg(), encoding="utf-8")
    print(f"  ✅ {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
