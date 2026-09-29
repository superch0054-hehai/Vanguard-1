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
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_esc

from probe import (CAP_TO_MAH, CHARGING_STEPTYPES, DISCHARGING_STEPTYPES,
                   _steptype, group_cycles)

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


def ce_never_healthy(cs: list[dict]) -> float | None:
    """整段测试的最大库仑效率（低于 CE_FLOOR_PCT 才返回，否则 None）。"""
    vals = [c["ce"] for c in cs
            if isinstance(c.get("ce"), (int, float)) and c["ce"] > 0]
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
        self.anno: dict | None = None
        self.xlim: tuple[float, float] | None = None
        self.left_ylim: tuple[float, float] | None = None
        self.right_ylim: tuple[float, float] | None = None

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

    # ---------------- 渲染 ----------------
    def _ranges(self):
        xs = [x for s in self.left_series + self.right_series for x in s["xs"]]
        ly = [y for s in self.left_series for y in s["ys"] if y is not None]
        ry = [y for s in self.right_series for y in s["ys"] if y is not None]
        x0, x1 = self.xlim or (min(xs), max(xs))
        # 上界顶到刻度上（nice_ceil），否则最上面那条网格线上没有标签。
        # 下界保持 0 起（电池数据看绝对量级，不从中间截断）。
        ly_lo = min([0.0] + ly)
        ry_lo = min([0.0] + ry)
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
        # ---- 右轴刻度 ----
        rt = nice_ticks(ry0, ry1)
        for v in rt:
            y = sry(v)
            o.append(f'<text x="{px1+13}" y="{y+5:.1f}" font-size="{FS_TICK}" '
                     f'text-anchor="start">{esc(fmt_num(v))}</text>')
            o.append(f'<line x1="{px1}" y1="{y:.1f}" x2="{px1+5}" y2="{y:.1f}" '
                     f'stroke="{C_AXIS}" stroke-width="1"/>')
        # ---- X 轴刻度 ----
        # 圈数少（≤12）就每圈一个刻度；多了改用"整刻度"（100/200/…），
        # 否则 602 圈会画成 1/61/121/… 这种不整齐的刻度。
        span = x1 - x0
        if span <= 12:
            xticks = list(range(int(math.floor(x0)), int(math.ceil(x1)) + 1))
        else:
            xticks = [t for t in nice_ticks(x0, x1, 8) if x0 <= t <= x1]
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

        # ---- 标题 ----
        o.append(f'<text x="{(px0+px1)/2:.0f}" y="{self.MT-20}" font-size="{FS_TITLE}" '
                 f'text-anchor="middle">{esc(self.title)}</text>')
        # ---- 轴标签 ----
        o.append(f'<text x="{(px0+px1)/2:.0f}" y="{self.H-28}" font-size="{FS_LABEL}" '
                 f'text-anchor="middle">{esc(self.x_label)}</text>')
        o.append(f'<text x="30" y="{(py0+py1)/2:.0f}" font-size="{FS_LABEL}" '
                 f'text-anchor="middle" transform="rotate(-90 30 {(py0+py1)/2:.0f})">'
                 f'{esc(self.left_label)}</text>')
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
        LG_W, LG_H = 275, 34 * max(len(lg), 1) + 8
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
        note_rect = None
        if self.notes:
            nw = max(self._w(n) for n in self.notes) * (FS_NOTE * 0.46) + 28
            nh = len(self.notes) * (FS_NOTE + 13) + 20
            note_rect = (px0 + 16, py1 + 14, nw, nh)
        def _hits(bx, by):
            n = sum(1 for x, y in allpts
                    if bx <= x <= bx + LG_W and by <= y <= by + LG_H)
            # 和"测试条件文字框"有交叠就直接排除（给个巨大的惩罚值）
            if note_rect:
                nx, ny, nw, nh = note_rect
                if bx < nx + nw and nx < bx + LG_W and by < ny + nh and ny < by + LG_H:
                    n += 10 ** 6
            return n
        lx0, ly0 = min(corners, key=lambda c: _hits(*c))
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
                   ce=(d / c * 100) if c else None,
                   ee=(eng_d / eng_c * 100) if eng_c else None,
                   retention=(d / first_dis * 100) if first_dis else None,
                   dcir_mohm=dcir)
        if mass_g:
            rec["dis_sp"] = d / mass_g
            rec["chg_sp"] = c / mass_g
        out.append(rec)
    return out


def load_steps(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    p = argparse.ArgumentParser(description="零依赖 SVG 出图")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--out", default="./figs")
    p.add_argument("--mass-mg", type=float, default=1.184,
                   help="活性物质质量（mg），用于算比容量；0 = 不算")
    p.add_argument("--max-cycles", type=int, default=0, help="只画前 N 圈，0=全部")
    args = p.parse_args()

    data = Path(args.data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mass = (args.mass_mg / 1000.0) if args.mass_mg else None

    n = 0
    for sp in sorted(data.glob("channel=*/testid=*/steps.csv")):
        steps = load_steps(sp)
        if not steps:
            print(f"  ⚠️  跳过 {sp.parent.parent.name}/{sp.parent.name}：steps.csv 是空的")
            continue
        cs = cycles_of(steps, mass)
        if args.max_cycles:
            cs = cs[:args.max_cycles]
        if not cs:
            # ★ 别静默跳过 —— 运维时"什么都没生成"最难看。
            # 只有充电没放电、或工步序列切不出合格块，都会走到这里。
            from probe import group_cycles as _gc
            why = ("工步里没有完整的充放电对"
                   if _gc(steps)
                   else "工步序列切不出合格的圈（缺充电或缺放电）")
            print(f"  ⚠️  跳过 {sp.parent.parent.name}/{sp.parent.name}：{why}")
            continue
        ch = sp.parts[-3].split("=", 1)[1]
        tid = sp.parts[-2].split("=", 1)[1]
        xs = [r["cycle"] for r in cs]
        yk = "dis_sp" if mass else "dis"

        fig = CycleChart(
            f"通道 {ch} / 测试 {tid}" + (f"　前 {len(cs)} 圈" if args.max_cycles else ""),
            "放电比容量 (mAh g⁻¹)" if mass else "放电容量 (mAh)",
            "库仑效率 (%)")
        fig.add_right(xs, [r["ce"] for r in cs], "库仑效率")
        fig.add_left(xs, [r[yk] for r in cs], "放电" + ("比容量" if mass else "容量"))
        if mass:
            fig.set_notes([f"Neware BTS85  通道 {ch}",
                           f"活性物质 {args.mass_mg} mg   0.1C 化成 3 圈 → 1C 老化",
                           "扣式电池  截止 2.5 / 0.5 V  25 ℃"])
        dst = out / f"fig_{ch}_{tid}.svg"
        dst.write_text(fig.svg(), encoding="utf-8")
        print(f"  ✅ {dst}  ({len(cs)} 圈)")
        n += 1
    if not n:
        print("  没有找到 steps.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
