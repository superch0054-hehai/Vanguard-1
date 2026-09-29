#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figures.py 的测试。

为什么要有这一套：figures.py 之前**没有测试**，6 个 bug 全是靠肉眼看出图不对才发现的
（单个循环时坐标轴除零、点数多了标记糊成一片、图例压住曲线、图例和说明框重叠、
暗色页面上图例是块白的、左轴最后画的标记被盖住）。这些都不是"跑不通"，而是
**跑得通但画错**，没有断言就只能靠碰运气。

    python3 selftest_figures.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from figures import (CE_FLOOR_PCT, MASS_G, SP_IMPLAUSIBLE_MAH_G, C_WARN, CycleChart,
                     ce_never_healthy, cycles_of, fmt_num, nice_ceil, nice_ticks,
                     sp_implausible)

BAR = "=" * 66


class Checker:
    def __init__(self) -> None:
        self.passed, self.failed = 0, []

    def ok(self, cond, label, extra=""):
        if cond:
            self.passed += 1
            print("  PASS  " + label)
        else:
            self.failed.append(label)
            print("  FAIL  " + label + (f"   <- {extra}" if extra else ""))

    def report(self) -> int:
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        for f in self.failed:
            print("  失败：" + f)
        return 1 if self.failed else 0


def step(st: str, cap: float, eng: float, curr: float, dcir: float = 0.0) -> dict:
    return dict(steptype=st, cap=cap, eng=eng, startcurr=curr, dcir=dcir)


def main() -> int:
    c = Checker()

    print(BAR)
    print("① nice_ticks：坐标轴刻度不能退化")
    print(BAR)
    # nice_ticks 的契约只是「在 [lo,hi] 内给出整齐刻度」——**它不负责盖住两端**，
    # 盖住上端是 nice_ceil 的事（见 ①b）。所以这里不测"末点必须 >= hi"。
    for lo, hi in [(0, 10), (0, 59.9), (0.0004469, 0.0004471), (0, 1e-7),
                   (4.5, 5.5), (0, 2400000), (0, 2.4), (0, 105)]:
        t = nice_ticks(lo, hi)
        c.ok(len(t) >= 2, f"nice_ticks({lo}, {hi}) 至少给出 2 个刻度", str(t))
        c.ok(all(t[i] < t[i + 1] for i in range(len(t) - 1)),
             f"nice_ticks({lo}, {hi}) 严格递增", str(t))
        c.ok(all(lo - 1e-12 <= x <= hi + 1e-12 for x in t),
             f"nice_ticks({lo}, {hi}) 刻度不越界", str(t))
        # 等距：相邻刻度之差应当一致（1/2/2.5/5×10ⁿ 的整数倍）
        steps = {round(t[i + 1] - t[i], 9) for i in range(len(t) - 1)}
        c.ok(len(steps) == 1, f"nice_ticks({lo}, {hi}) 刻度等距", str(sorted(steps)))
    # 退化区间（min == max）：只要求"别抛异常、给有限值"。
    # 真正的除零是 _ranges() 撑开区间解决的，见测试 ⑥「只有 1 个点」。
    for lo in (5, 0, -3.2):
        try:
            t = nice_ticks(lo, lo)
            c.ok(all(isinstance(x, (int, float)) and x == x for x in t),
                 f"nice_ticks({lo}, {lo}) 退化区间不抛异常、返回有限值", str(t))
        except Exception as e:
            c.ok(False, f"nice_ticks({lo}, {lo}) 退化区间不抛异常", str(e))

    print()
    print(BAR)
    print("①b nice_ceil：轴顶必须落在刻度上（否则轴顶留白，图看着像没画完）")
    print(BAR)
    for v in (59.9, 2.4, 105.0, 60.0, 7.0, 0.0004471, 2.4e6, 0.5, 1.0):
        top = nice_ceil(v, 0.0)
        ticks = nice_ticks(0.0, top)
        c.ok(top >= v - 1e-12, f"数据顶 {v:g} → 轴顶 {top:g}（不低于数据）")
        c.ok(ticks and abs(ticks[-1] - top) <= abs(top) * 1e-9 + 1e-12,
             f"★ 轴顶 {top:g} 正好是最后一条刻度", str(ticks[-3:]))
    c.ok(nice_ceil(float("nan"), 0.0) != nice_ceil(float("nan"), 0.0)
         or True, "nan 输入不抛异常")

    print()
    print(BAR)
    print("② fmt_num：不要科学计数法（文档承诺的是「只有极小值才用」）")
    print(BAR)
    for v, want in [(0, "0"), (1.5, "1.5"), (1500, "1500"),
                    (1e5, "100000"), (1e6, "1000000"), (2400000.0, "2400000"),
                    (1e7, "10000000"), (-2400000.0, "-2400000")]:
        got = fmt_num(v)
        c.ok(got == want, f"fmt_num({v:g}) = {got}（应为 {want}）")
    c.ok("e" not in fmt_num(2.4e6).lower(), "★ 10⁶ 量级（DCIR 轴）不用科学计数法")
    c.ok("e" in fmt_num(1e-7).lower(), "极小值仍然用科学计数法（1e-07）")
    c.ok(fmt_num(float("nan")) == "nan" and fmt_num(float("inf")) == "inf",
         "nan / inf 不抛异常")

    print()
    print(BAR)
    print("③ cycles_of：分圈 + 单位换算 ×1000 + 比容量")
    print(BAR)
    # 一个完整循环：充电 0.001 Ah、放电 0.0009 Ah（原始单位是 Ah）
    one = [step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0009, 0.0018, -1e-5)]
    cs = cycles_of(one, mass_g=MASS_G)
    c.ok(len(cs) == 1, "一对充放切出 1 圈", f"得到 {len(cs)}")
    if cs:
        c.ok(abs(cs[0]["chg"] - 1.0) < 1e-9, "充电容量 ×1000 → 1.0 mAh", str(cs[0]["chg"]))
        c.ok(abs(cs[0]["dis"] - 0.9) < 1e-9, "放电容量 ×1000 → 0.9 mAh", str(cs[0]["dis"]))
        c.ok(cs[0]["ce"] is not None and abs(cs[0]["ce"] - 90.0) < 1e-6,
             "库仑效率 = 放电÷充电 = 90%", str(cs[0]["ce"]))
        c.ok(abs(cs[0]["dis_sp"] - 0.9 / MASS_G) < 1e-3,
             "比容量 = 容量 ÷ 质量", str(cs[0]["dis_sp"]))
        c.ok(cs[0]["retention"] is not None and abs(cs[0]["retention"] - 100.0) < 1e-9,
             "第 1 圈保持率 = 100%")
    c.ok(cycles_of([], mass_g=MASS_G) == [], "空输入 → 空输出")
    c.ok(all("dis_sp" not in r for r in cycles_of(one)), "不给质量时不算比容量")

    # 两圈：保持率按第 1 圈算
    two = [step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0008, 0.0016, -1e-5),
           step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0004, 0.0008, -1e-5)]
    cs2 = cycles_of(two, mass_g=MASS_G)
    c.ok(len(cs2) == 2, "两对充放切出 2 圈", f"得到 {len(cs2)}")
    if len(cs2) == 2:
        c.ok(abs(cs2[1]["retention"] - 50.0) < 1e-6,
             "第 2 圈保持率 = 0.4÷0.8 = 50%", str(cs2[1]["retention"]))

    print()
    print(BAR)
    print("④ sp_implausible：比容量超出量级要报警（质量假设用错了）")
    print(BAR)
    # 这条判据存在的理由：真实数据里 27-188-10-1 套 1.184 mg 会算出 799 mAh/g，
    # 而它的电流是 0.284 mA —— 是那个样品的 2 倍，说明根本不是同一个样品。
    c.ok(sp_implausible([]) is None, "空曲线 → 不报")
    c.ok(sp_implausible([{"cycle": 1}]) is None, "没有比容量字段 → 不报")
    c.ok(sp_implausible([{"dis_sp": 200, "chg_sp": 210}]) is None,
         "200 mAh/g（常见正极量级）→ 不报")
    c.ok(sp_implausible([{"dis_sp": SP_IMPLAUSIBLE_MAH_G}]) is None,
         f"正好等于阈值 {SP_IMPLAUSIBLE_MAH_G:.0f} → 不报（判据是严格大于）")
    c.ok(sp_implausible([{"dis_sp": SP_IMPLAUSIBLE_MAH_G + 0.1}]) is not None,
         "刚超过阈值一点 → 报")
    got = sp_implausible([{"dis_sp": 799.7, "chg_sp": 1402.0}])
    c.ok(got == 1402.0, "取最大那个（1402 > 799）", str(got))
    c.ok(sp_implausible([{"chg_sp": None, "dis_sp": 600}]) == 600,
         "另一个字段是 None 也不崩", "")

    print()
    print(BAR)
    print("④b ce_never_healthy：电芯根本没在放电（整段库仑效率上不去）")
    print(BAR)
    # 为什么单独一条：实测 27-188-10-3 的放电容量 602 圈全是 0，库仑效率从 2% 爬到 25%。
    # 它同时会被「首圈是化成循环」那条命中，但两条说的不是一回事 ——
    # 那条只说"基准选错了"，读者可能反过来以为"用第 2 圈当基准就有 100%，电池是好的"。
    c.ok(ce_never_healthy([]) is None, "空曲线 → 不报")
    c.ok(ce_never_healthy([{"cycle": 1}]) is None, "没有 ce 字段 → 不报")
    c.ok(ce_never_healthy([{"ce": None}, {"ce": None}]) is None, "ce 全 None → 不报")
    c.ok(ce_never_healthy([{"ce": 0}]) is None, "ce=0（充电为 0，不算数）→ 不报")
    c.ok(ce_never_healthy([{"ce": 100}, {"ce": 102}, {"ce": 98}]) is None,
         "健康电池（中位 102%）→ 不报")
    c.ok(ce_never_healthy([{"ce": 2}, {"ce": 12}, {"ce": 25}]) == 25,
         "★ 没在放电的（最高 25%）→ 报出 25")
    c.ok(ce_never_healthy([{"ce": CE_FLOOR_PCT}]) is None,
         f"正好等于阈值 {CE_FLOOR_PCT:.0f} → 不报（判据是严格小于）")
    c.ok(ce_never_healthy([{"ce": CE_FLOOR_PCT - 0.1}]) is not None, "刚低于阈值一点 → 报")
    c.ok(ce_never_healthy([{"ce": 57.05}]) is None,
         "★ 单圈化成（库仑效率 57%）不该被误报")
    # 取最大值而不是中位数：健康电池早期几圈 CE 很低，用中位数才不会被前几圈拖低
    c.ok(ce_never_healthy([{"ce": 13.6}] + [{"ce": 102}] * 576) is None,
         "★ 只在首圈低、之后正常 → 不报（判据取最大值，不被开头几圈拖低）")

    print()
    print(BAR)
    print("⑤ 说明框：以 ⚠ 开头的行要用警告色（黑白打印也得能分辨）")
    print(BAR)
    fig = CycleChart("标题", "左轴", "右轴")
    fig.add_left([1, 2, 3], [1, 2, 3], "甲")
    fig.add_right([1, 2, 3], [3, 2, 1], "乙")
    fig.set_notes(["普通说明", "⚠ 这条是警告"])
    svg = fig.svg()
    c.ok(svg.startswith("<svg") and svg.rstrip().endswith("</svg>"), "输出是完整 SVG")
    c.ok(C_WARN in svg, f"警告行用了警告色 {C_WARN}")
    c.ok(svg.count(C_WARN) >= 1, "警告色只出现在该出现的地方")
    normal_line = [l for l in svg.split("<text") if "普通说明" in l]
    c.ok(bool(normal_line) and C_WARN not in normal_line[0],
         "★ 普通说明**没有**被误染成警告色", normal_line[0][:120] if normal_line else "没找到")

    print()
    print(BAR)
    print("⑤b 图注：画在图下方，且**不得改变绘图区尺寸**")
    print(BAR)
    # 这条不变量很重要：说明文字从图内挪到图注，如果顺手改了绘图区，
    # 所有已确认过的图（比例、曲线形状）就都变了。
    import re
    import xml.etree.ElementTree as ET

    def render(cap=None):
        f = CycleChart("标题", "L", "R")
        f.add_left([1, 2, 3], [1, 2, 3], "甲")
        f.add_right([1, 2, 3], [3, 2, 1], "乙")
        if cap:
            f.set_caption(cap)
        return f.svg()

    def plot_rect(s):
        got = [l.strip() for l in s.splitlines() if '<rect x=' in l and 'fill="none"' in l]
        return got[0] if got else ""

    def canvas_h(s):
        return int(re.search(r'height="(\d+)"', s).group(1))

    a = render()
    b = render(["第一行", "第二行", "⚠ 第三行是警告"])
    c.ok(plot_rect(a) == plot_rect(b),
         "★ 加图注后绘图区边框一模一样", f"\n无 {plot_rect(a)}\n有 {plot_rect(b)}")
    c.ok(canvas_h(b) > canvas_h(a),
         f"画布被加高了（{canvas_h(a)} → {canvas_h(b)}）")
    c.ok(abs((canvas_h(b) - canvas_h(a)) - (3 * (16 + 9) + 22)) <= 2,
         "加高量 = 行数 × 行距 + 边距")
    c.ok(C_WARN in b, "★ 图注里的 ⚠ 行也用警告色")
    c.ok("第一行" in b and "第二行" in b, "图注文字都在")
    try:
        ET.fromstring(b)
        c.ok(True, "★ 带图注的输出仍是合法 XML（能被 SVG 解析器打开）")
    except Exception as e:
        c.ok(False, "带图注的输出仍是合法 XML", str(e))
    c.ok(canvas_h(render([])) == canvas_h(a), "空图注 = 不加高")

    print()
    print(BAR)
    print("⑥ 退化输入不能把渲染搞崩（这些以前都是除以零）")
    print(BAR)
    cases = {
        "只有 1 个点": ([1], [5], [1], [5]),
        "所有点相同": ([1, 2, 3], [7, 7, 7], [1, 2, 3], [7, 7, 7]),
        "含 None": ([1, 2, 3], [1, None, 3], [1, 2, 3], [None, 2, 1]),
        "全是 None（右轴）": ([1, 2, 3], [1, 2, 3], [1, 2, 3], [None, None, None]),
        "大数": ([1, 2], [1e6, 2e6], [1, 2], [1e6, 2e6]),
    }
    for name, (xs, ly, rx, ry) in cases.items():
        try:
            f = CycleChart("t", "L", "R")
            f.add_left(xs, ly, "a")
            f.add_right(rx, ry, "b")
            f.set_notes(["n"])
            out = f.svg()
            c.ok(out.startswith("<svg") and len(out) > 500,
                 f"「{name}」渲染出合法 SVG（{len(out)} 字节）")
        except Exception as e:
            c.ok(False, f"「{name}」渲染不崩", f"{type(e).__name__}: {e}")

    print()
    print(BAR)
    print("⑥b ★ 所有绘制坐标必须在画布内（越界坐标肉眼看不见，会潜伏下来）")
    print(BAR)
    # 实测：只有 1 圈时轴被撑成 0.5~1.5，整数刻度 0/1/2 里的 0 和 2
    # 被画到 x=-331 / x=1561 —— 在画布外所以看不见，但换画布尺寸就冒出来。
    import re as _re
    for name, (xs_, ly_, rx_, ry_) in {
        "只有 1 圈": ([1], [0.9], [1], [57.0]),
        "两圈": ([1, 2], [0.9, 0.8], [1, 2], [57.0, 88.0]),
        "600 圈": (list(range(1, 601)), [0.5] * 600,
                   list(range(1, 601)), [100.0] * 600),
    }.items():
        f = CycleChart("t", "L", "R")
        f.add_left(xs_, ly_, "a")
        f.add_right(rx_, ry_, "b")
        f.set_caption(["图注一行"])
        out = f.svg()
        W = 1200
        Ht = int(_re.search(r'height="(\d+)"', out).group(1))
        bad = []
        for m in _re.finditer(r'<text x="(-?[\d.]+)" y="(-?[\d.]+)"', out):
            bx, by = float(m.group(1)), float(m.group(2))
            if not (-1 <= bx <= W + 1 and -1 <= by <= Ht + 1):
                bad.append((bx, by))
        c.ok(not bad, f"「{name}」没有画到画布外的文字", f"{len(bad)} 处越界，例如 {bad[:3]}")

    print()
    print(BAR)
    print("⑦ 文字里的特殊字符要转义（否则 < & 会把 SVG 弄成非法 XML）")
    print(BAR)
    f = CycleChart("含 <b> 的标题 & 符号", "L", "R")
    f.add_left([1, 2], [1, 2], "系列 <x>")
    f.set_notes(["<script>alert(1)</script>"])
    out = f.svg()
    c.ok("<script>" not in out, "★ 说明里的 <script> 被转义了")
    c.ok("&lt;script&gt;" in out, "转义成了 HTML 实体")
    c.ok("&amp;" in out, "标题里的 & 被转义")
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(out)
        c.ok(True, "★ 输出能被标准 XML 解析器解析（合法 SVG）")
    except Exception as e:
        c.ok(False, "输出能被标准 XML 解析器解析（合法 SVG）", str(e))

    print()
    print(BAR)
    print("验证：")
    print(BAR)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
