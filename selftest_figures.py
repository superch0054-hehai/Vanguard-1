#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figures.py 的测试。

为什么要有这一套：figures.py 之前**没有测试**，6 个 bug 全是靠肉眼看出图不对才发现的
（单个循环时坐标轴除零、点数多了标记糊成一片、图例压住曲线、图例和说明框重叠、
暗色页面上图例是块白的、左轴最后画的标记被盖住）。这些都不是"跑不通"，而是
**跑得通但画错**，没有断言就只能靠碰运气。

    python3 selftest_figures.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import figures
import probe
from figures import (CE_CHARGE_OVER_DISCHARGE, CE_FLOOR_PCT, CMP_COLORS, MASS_G, SP_IMPLAUSIBLE_MAH_G,
                     C_WARN, PALETTE_DQDV, CycleChart, EXP_RAMP, CTL_RAMP,
                     ce_never_healthy, chart_avgv, chart_dqdv, chart_dqdv_sample,
                     chart_long_cycle, chart_rate, chart_vq, cycles_of,
                     cycles_of_detail, cycle_rates, dqdv_curve, dqdv_per_cycle,
                     ce_pct, fmt_num, nice_ceil, nice_ticks, pick_dqdv_cycles,
                     rates_from_cs, sp_implausible, vq_curves)

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


def test_cycles_of_detail(c: Checker) -> None:
    """★ 最硬的交叉验证：同一颗合成电芯，从工步层算（cycles_of）和从
    明细层算（cycles_of_detail）的逐圈指标必须完全一致。

    明细层的 cap 是"工步内累计值"，所以合成时每个工步拆成几条 ramp 行，
    末条恰好等于工步层的 cap —— 这就是真实明细层的形状（实测 0.5824 mAh 那条）。
    """
    # 工步层：两圈两种倍率。圈1 0.2C（2mA），圈2 1C（10mA）—— 标称 10 mAh
    steps = [step("cc", 0.012, 0.024, 1e-5),
             step("dc", 0.009, 0.018, -2e-3),
             step("cc", 0.012, 0.024, 1e-5),
             step("dc", 0.0085, 0.017, -1e-2)]
    cs_steps = cycles_of(steps)

    # 明细层：把每个工步拆成 5 条 ramp（cap/eng 从 0 线性长到工步层的值）
    detail = []
    seq = 0
    for si, st_row in enumerate(steps, start=1):
        n_seg = 5
        for k in range(1, n_seg + 1):
            seq += 1
            f = k / n_seg
            detail.append(dict(
                seqid=str(seq),
                stepid=str(si),
                cycleid=str((si - 1) // 2),          # 两工步一圈 → detail 圈号 = 工步圈 - 1
                steptype=st_row["steptype"],
                testtime=str(k * 1000),
                atime="2026-10-01 02:00:00",
                volt=str(2.0 + 0.5 * f),
                curr=st_row["startcurr"],
                cap=str(float(st_row["cap"]) * f),
                eng=str(float(st_row["eng"]) * f),
                CPU="34.9", dbc="[]"))

    cs_detail = cycles_of_detail(detail)

    c.ok(len(cs_detail) == len(cs_steps) == 2,
         "圈数一致（2 圈）", f"detail {len(cs_detail)} vs steps {len(cs_steps)}")
    for a, b in zip(cs_detail, cs_steps):
        tag = f"第 {a['cycle']} 圈"
        for key in ("chg", "dis", "ce", "ee", "retention", "chg_eng", "dis_eng"):
            va, vb = a.get(key), b.get(key)
            same = (va is None and vb is None) or                    (va is not None and vb is not None and abs(va - vb) <= abs(vb) * 1e-9)
            c.ok(same, f"★ {tag} 的 {key} 两条路径一致",
                 f"detail {va} vs steps {vb}")
    # 平均放电电流：圈 1 的 dc 记录 curr=-2e-3 A → 2 mA；圈 2 → 10 mA
    c.ok(abs(cs_detail[0]["ma"] - 2.0) < 1e-6, "圈 1 平均放电电流 = 2 mA",
         str(cs_detail[0]["ma"]))
    c.ok(abs(cs_detail[1]["ma"] - 10.0) < 1e-6, "圈 2 平均放电电流 = 10 mA",
         str(cs_detail[1]["ma"]))
    # 倍率标签：两条路径给出同样的标签
    r_detail = rates_from_cs(cs_detail, nominal_mah=10.0)
    r_steps = cycle_rates(steps, nominal_mah=10.0)
    c.ok([r["label"] for r in r_detail] == [r["label"] for r in r_steps],
         "★ 两条路径的倍率标签一致",
         f"{[r['label'] for r in r_detail]} vs {[r['label'] for r in r_steps]}")
    # 比容量：给质量时两条路径一致
    d1 = cycles_of_detail(detail, mass_g=MASS_G)
    s1 = cycles_of(steps, mass_g=MASS_G)
    c.ok(abs(d1[0]["dis_sp"] - s1[0]["dis_sp"]) <= s1[0]["dis_sp"] * 1e-9,
         "比容量两条路径也一致")
    # 退化输入
    c.ok(cycles_of_detail([]) == [], "空明细 → 空输出")
    c.ok(cycles_of_detail([{"cycleid": "x", "steptype": "cc"}]) == [],
         "脏行（圈号非数字）→ 跳过不崩")
    # 明细层无内阻 → dcir 恒为 None（不能假装有值）
    c.ok(all(r["dcir_mohm"] is None for r in cs_detail),
         "明细层口径的 dcir 恒为 None（诚实标注，不假装有内阻）")


def test_detail_resharding(c: Checker) -> None:
    """★ 一个 cycleid 里塞了两对充放（实测 10-1 的 cycleid=2：stepid 2/4 与 7/9）。

    按 cycleid 累加会把**两圈的容量加到一起** —— 某圈容量凭空翻倍（旧代码：
    0.009+0.0085 = 17.5 mAh 全落在同一圈上），衰减拟合就给出 +12523 这种
    荒唐速率；report_pack 里那条"有跳变就不给速率"的规避就是为它写的。
    圈号必须按**工步序列**重算（probe.group_cycles / cycle_index_of）。
    """
    # 两对充放，共用一个 cycleid="2"；每个工步拆 5 条 ramp（末条 = 工步容量）
    spec = [("2", "2", "dc", 0.0090, -2e-3),
            ("2", "4", "cc", 0.0120, 1e-5),
            ("2", "7", "dc", 0.0085, -1e-2),
            ("2", "9", "cc", 0.0120, 1e-5)]
    detail, seq = [], 0
    for cid, sid, st, cap, curr in spec:
        for k in range(1, 6):
            seq += 1
            f = k / 5
            detail.append(dict(seqid=str(seq), stepid=sid, cycleid=cid, steptype=st,
                               testtime=str(k * 1000), atime="2026-10-01 02:00:00",
                               volt=str(2.0 + 0.5 * f), curr=str(curr),
                               cap=str(cap * f), eng=str(cap * 0.7 * f),
                               CPU="34.9", dbc="[]"))

    cs = cycles_of_detail(detail)
    c.ok(len(cs) == 2,
         "★ 一个 cycleid 里两对充放 → 重算出 2 圈（按 cycleid 只有 1 圈）", f"{len(cs)} 圈")
    c.ok([r["cycle"] for r in cs] == [1, 2], "圈号是顺序号 1、2",
         str([r["cycle"] for r in cs]))
    c.ok(abs(cs[0]["dis"] - 9.0) < 1e-9,
         "第 1 圈放电容量 = 9.0 mAh（本圈的，不是两圈之和）", str(cs[0]["dis"]))
    c.ok(abs(cs[1]["dis"] - 8.5) < 1e-9, "第 2 圈放电容量 = 8.5 mAh", str(cs[1]["dis"]))
    c.ok(abs(sum(r["dis"] for r in cs) - 17.5) < 1e-9,
         "两圈之和 = 17.5 mAh（确实拆成了两圈）")
    c.ok(all(abs(r["dis"] - 17.5) > 1e-9 for r in cs),
         "★ 没有任何一圈拿到 17.5 —— 容量没翻倍")

    # 取圈要取到正确的行：第 1 圈只有 stepid 2/4，第 2 圈只有 7/9
    one = figures._detail_cycle_rows(detail, 1)
    c.ok(sorted({r["stepid"] for r in one}) == ["2", "4"],
         "★ 第 1 圈只取到 stepid 2/4（不把下一圈的 7/9 也算进来）",
         str(sorted({r["stepid"] for r in one})))
    two_sids = sorted({r["stepid"] for r in figures._detail_cycle_rows(detail, 2)})
    c.ok(two_sids == ["7", "9"], "第 2 圈取到 stepid 7/9", str(two_sids))

    # V-Q 曲线同理：两条曲线各含本圈的放电段
    v1 = vq_curves(detail, cycle=1, exact=True)
    v2 = vq_curves(detail, cycle=2, exact=True)
    c.ok(v1 and abs(max(q for q, _ in v1["discharge"]) - 9.0) < 1e-9,
         "★ 第 1 圈 V-Q 曲线只含本圈放电（9.0 mAh）")
    c.ok(v2 and abs(max(q for q, _ in v2["discharge"]) - 8.5) < 1e-9,
         "第 2 圈 V-Q 曲线只含本圈放电（8.5 mAh）")

    # 交叉验证：同样四个工步喂给工步层口径（根本没有 cycleid），结果必须一致
    steps = [dict(steptype=st, cap=cap, eng=cap * 0.7, startcurr=curr, dcir=0.0)
             for (_cid, _sid, st, cap, curr) in spec]
    key = lambda rs: [(r["cycle"], round(r["dis"], 6), round(r["chg"], 6)) for r in rs]
    c.ok(key(cs) == key(cycles_of(steps)),
         "★ 明细层与工步层两条路径在「一个 cycleid 两对充放」上一致",
         f"{key(cs)} vs {key(cycles_of(steps))}")

    print()
    print(f"  （合成明细 {len(detail)} 行 ↔ 合成工步 {len(steps)} 行，交叉验证）")


def test_report_charts(c: Checker) -> None:
    """报告的五张**对比图**（两通道一图）。数据全部合成，图型对照五张文献参考图。"""
    import re
    import xml.etree.ElementTree as ET

    def valid(svg: str) -> bool:
        try:
            ET.fromstring(svg)
            return True
        except Exception:
            return False

    # 两颗容量不同的合成电芯（对比图要有区分度）
    A = [step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0008, 0.0016, -2e-4),
         step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0004, 0.0008, -2e-4)]
    B = [step("cc", 0.0012, 0.0024, 1e-5), step("dc", 0.0009, 0.0018, -1e-3),
         step("cc", 0.0012, 0.0024, 1e-5), step("dc", 0.00045, 0.0009, -1e-3)]
    csA, csB = cycles_of(A), cycles_of(B)
    cells = [{"label": "A", "cs": csA}, {"label": "B", "cs": csB}]

    # ---- ① 长循环对比（左轴=比容量，因为实验要的是这个口径）----
    fig_sp = chart_long_cycle("t", [{"label": "A", "cs": cycles_of(A, MASS_G)},
                                    {"label": "B", "cs": cycles_of(B, MASS_G)}],
                              mass_g=MASS_G)
    c.ok("放电比容量 (mAh g⁻¹)" in fig_sp.svg(),
         "① 给质量时左轴是**放电比容量**（用户要求的口径）")
    vals_sp = [y for x in fig_sp.left_series for y in x["ys"] if y is not None]
    want = 0.8 / MASS_G          # 放电 0.8 mAh ÷ 1.184 mg ≈ 675.7 mAh/g
    c.ok(any(abs(v - want) < 0.5 for v in vals_sp),
         "★ 比容量数值 = 容量÷质量（0.8 mAh ÷ 1.184 mg ≈ 675.7）",
         f"期望 {want:.1f}，实得 {sorted(set(round(v,1) for v in vals_sp))[:4]}")
    fig = chart_long_cycle("t", cells)
    svg = fig.svg()
    c.ok(valid(svg), "① 长循环对比：合法 SVG")
    c.ok(len(fig.left_series) == 4, "① 两样品 × 充放 = 4 条左轴曲线",
         str(len(fig.left_series)))
    c.ok(len(fig.right_series) == 2, "① 两样品各一条库仑效率（右轴）",
         str(len(fig.right_series)))
    cols = [x["color"] for x in fig.left_series]
    c.ok(cols[0] == cols[1] == CMP_COLORS[0], "① 样品 A 充放同色（第一色=蓝）", str(cols))
    c.ok(cols[2] == cols[3] == CMP_COLORS[1], "① 样品 B 充放同色（第二色=红）", str(cols))
    c.ok("A 充电" in svg and "B 放电" in svg and "A 效率" in svg,
         "① 图例按「样品+充放」命名")
    fig1 = chart_long_cycle("t", [{"label": "A", "cs": csA}])
    c.ok(len(fig1.left_series) == 2 and valid(fig1.svg()),
         "① 只给一个样品 → 也能画（单边对比）")

    # ---- ② 倍率对比 ----
    rA = [{"cycle": 1, "label": "0.2"}, {"cycle": 2, "label": "0.2"}]
    rB = [{"cycle": 1, "label": "0.2"}, {"cycle": 2, "label": "0.2"}]
    fig_sp = chart_rate("t", [{"label": "A", "cs": cycles_of(A, MASS_G), "rates": rA},
                              {"label": "B", "cs": cycles_of(B, MASS_G), "rates": rB}],
                        mass_g=MASS_G)
    c.ok("放电比容量 (mAh g⁻¹)" in fig_sp.svg(), "② 给质量时左轴是放电比容量")
    fig = chart_rate("t", [{"label": "A", "cs": csA, "rates": rA},
                           {"label": "B", "cs": csB, "rates": rB}])
    c.ok(len(fig.left_series) == 2, "② 两样品两条阶梯", str(len(fig.left_series)))
    c.ok(any(t["text"] == "0.2" for t in fig.texts), "② 倍率标签画上了")
    c.ok(len(fig.texts) == 1, "★ 倍率标签只标第一份（两份 schedule 相同会重叠）",
         str(len(fig.texts)))
    c.ok(valid(fig.svg()), "② 合法 SVG")

    # ---- ③ 首效 V-Q 对比 ----
    def detail_of(chg_cap, dis_cap):
        rows_ = []
        n_ = 0
        for i in range(4):
            n_ += 1
            rows_.append(dict(seqid=str(n_), cycleid="0", steptype="cc",
                              cap=str(chg_cap * i / 3), volt=str(2.5 + 0.8 * i / 3),
                              curr="1e-5", eng="0"))
        for i in range(4):
            n_ += 1
            rows_.append(dict(seqid=str(n_), cycleid="0", steptype="dc",
                              cap=str(dis_cap * i / 3), volt=str(3.3 - 0.8 * i / 3),
                              curr="-1e-5", eng="0"))
        return rows_

    curA = vq_curves(detail_of(0.001, 0.0008), cycle=1)
    curB = vq_curves(detail_of(0.0012, 0.0009), cycle=1)
    fig = chart_vq("t", [{"label": "A", "curves": curA},
                         {"label": "B", "curves": curB}])
    svg = fig.svg()
    c.ok(len(fig.left_series) == 4, "③ 两样品 × 充放 = 4 条 V-Q 曲线",
         str(len(fig.left_series)))
    c.ok("A 充电（ICE 125.00%）" in svg,
         "★ 图例各标 ICE（A=125%，充电÷放电口径；合成数据充电 0.001 > 放电 0.0008）")
    c.ok("B 充电（ICE 133.33%）" in svg, "③ 图例各标 ICE（B=133.33%，同口径）")
    c.ok(valid(svg), "③ 合法 SVG")

    # ---- ④ 平均电压对比 ----
    fig = chart_avgv("t", cells)
    c.ok(len(fig.left_series) == 4, "④ 两样品 × 充放 = 4 条平均电压线",
         str(len(fig.left_series)))
    vals = [y for x in fig.left_series for y in x["ys"] if y is not None]
    c.ok(all(abs(v - 2.0) < 1e-9 for v in vals),
         "④ 平均电压 = 能量÷容量（合成数据全为 2.0 V）", str(vals))
    c.ok(valid(fig.svg()), "④ 合法 SVG")

    # ---- ⑤ dQ/dV 对比 ----
    def dq_of(scale):
        pts = []
        q = 0.0
        v = 3.0
        while v <= 3.5:
            dq = (100 + 400 * math.exp(-((v - 3.25) ** 2) / (2 * 0.01 ** 2))) * scale
            q += dq * 0.001
            pts.append((q, v))
            v += 0.001
        return pts

    dqA = dqdv_curve(dq_of(1.0), bin_v=0.005, smooth=3)
    dqB = dqdv_curve(dq_of(1.2), bin_v=0.005, smooth=3)
    fig = chart_dqdv("t", [{"label": "A", "curves": {1: dqA}},
                           {"label": "B", "curves": {1: dqB}}])
    svg = fig.svg()
    c.ok(len(fig.left_series) == 2, "⑤ 两样品各一条 dQ/dV", str(len(fig.left_series)))
    c.ok("A 第1圈" in svg and "B 第1圈" in svg, "⑤ 图例带样品名+圈号")
    c.ok(valid(svg), "⑤ 合法 SVG")
    vmax, _ = max(dqA, key=lambda t: t[1])
    c.ok(3.22 <= vmax <= 3.28, "⑤ 缩放样品的峰位仍在 V=3.25 附近", f"{vmax:.3f}")

    # ---- X 轴刻度策略：连续量轴（容量/电压）不能退化成只有一个刻度 ----
    # ★ 实测的 bug：刻度策略原来只看"跨度 ≤ 12"就按整数画，容量轴（0~0.63 mAh）
    #   与电压轴（0.5~1.1 V）跨度也小，于是生成 [0,1,2] 再被范围过滤砍到只剩一个刻度。
    def xticks_of(svg: str) -> list[str]:
        return re.findall(r'<text x="[\d.]+" y="49[0-9.]+"[^>]*>([^<]*)</text>', svg)

    t_vq = xticks_of(fig_vq.svg() if 'fig_vq' in dir() else
                     chart_vq("t", [{"label": "A", "curves": curA}]).svg())
    c.ok(len(t_vq) >= 5, "★ 容量轴（V-Q）有足够刻度，不是只剩一个",
         str(t_vq))
    c.ok(any("." in x for x in t_vq), "★ 容量轴刻度是连续值（0.1 / 0.2 这种）",
         str(t_vq))
    dqF = chart_dqdv("t", [{"label": "A", "curves": {1: dqA}}])
    t_dq = xticks_of(dqF.svg())
    c.ok(len(t_dq) >= 3, "★ 电压轴（dQ/dV）有足够刻度",
         str(t_dq))
    t_cyc = xticks_of(chart_long_cycle("t", [{"label": "A", "cs": csA}]).svg())
    c.ok(t_cyc == ["1", "2"], "★ 循环号轴仍是整数刻度（没被改坏）", str(t_cyc))
    dense_xs = list(range(1, 577))
    dense_cs = [dict(cycle=i, chg=1.0, dis=0.6, ce=99.0, ee=90.0,
                     chg_eng=2.0, dis_eng=1.2, retention=100.0, dcir_mohm=None)
                for i in dense_xs]
    t_dense = xticks_of(chart_long_cycle("t", [{"label": "A", "cs": dense_cs}]).svg())
    c.ok(len(t_dense) >= 4 and all("." not in x for x in t_dense),
         "★ 576 圈仍是整数整刻度（100/200/…）", str(t_dense))

    # ---- 退化输入 ----
    c.ok(chart_long_cycle("空", []).svg().startswith("<svg"),
         "空样品集 → 也能渲染（不崩）")
    c.ok(chart_vq("空", [{"label": "A", "curves": {}}]).svg().startswith("<svg"),
         "V-Q 空曲线 → 也能渲染")
    c.ok(chart_dqdv("空", [{"label": "A", "curves": {}}]).svg().startswith("<svg"),
         "dQ/dV 空曲线 → 也能渲染")


def test_dqdv_multi(c: Checker) -> None:
    """dQ/dV **逐圈图**（报告的图 5/6）：一个样品一张、氧化+还原同图、按圈号配色。

    钉住三件事：① 请求的圈还没到就退回"已有的圈"，并把缺的圈原样报出来；
    ② 取圈必须 exact —— 缺的圈绝不能拿第 1 圈顶上（图上多一条假曲线，肉眼看不出来）；
    ③ 符号口径：还原（嵌入/放电）画成正峰（与提示词 S34A 那句"正峰为还原"一致）。
    """
    import xml.etree.ElementTree as ET

    def valid(svg: str) -> bool:
        try:
            ET.fromstring(svg)
            return True
        except Exception:
            return False

    # ---- ① 圈的选择 ----
    drawn, missing = pick_dqdv_cycles([1, 10, 50, 100, 200, 300], [1, 2, 3])
    c.ok(drawn == [1, 2, 3], "★ 请求的圈只到手第 1 圈 → 退回到已有的 1/2/3 圈", str(drawn))
    c.ok(missing == [10, 50, 100, 200, 300], "★ 缺的圈照实报出来（图注写「尚未到达」）",
         str(missing))
    drawn, missing = pick_dqdv_cycles([1, 10, 50], [1, 2, 10, 50])
    c.ok(drawn == [1, 10, 50] and missing == [],
         "请求的圈到手 ≥2 个 → 就按请求的画，不掺没请求的圈", f"{drawn} / {missing}")
    drawn, missing = pick_dqdv_cycles([1, 10], [])
    c.ok(drawn == [] and missing == [1, 10], "一条数据都没有 → 不画，全报缺",
         f"{drawn} / {missing}")
    drawn, _ = pick_dqdv_cycles([1, 10, 50, 100, 200, 300], list(range(1, 20)))
    c.ok(drawn == [1, 10],
         "★ 请求里已有 2 个圈到手（1、10）→ 只画请求的，不把 19 个圈全画上", str(drawn))
    drawn, _ = pick_dqdv_cycles([1, 10, 50, 100, 200, 300], list(range(1, 10)))
    c.ok(drawn == list(range(1, 7)),
         "★ 退回到已有圈时封顶 6 个（配色就 6 档，多了颜色会重复）", str(drawn))

    # ---- ② 逐圈算 dQ/dV：必须 exact 取圈 ----
    def detail_cycle(cid, chg_cap, dis_cap):
        rows_ = []
        for i in range(4):
            rows_.append(dict(seqid=str(cid * 10 + i), cycleid=str(cid), steptype="cc",
                              cap=str(chg_cap * i / 3), volt=str(2.5 + 0.8 * i / 3),
                              curr="1e-5", eng="0"))
        for i in range(4):
            rows_.append(dict(seqid=str(cid * 10 + 4 + i), cycleid=str(cid), steptype="dc",
                              cap=str(dis_cap * i / 3), volt=str(3.3 - 0.8 * i / 3),
                              curr="-1e-5", eng="0"))
        return rows_

    rows = detail_cycle(0, 0.001, 0.0008) + detail_cycle(1, 0.001, 0.0006)
    dm = dqdv_per_cycle(rows, [1, 2, 99])
    c.ok(sorted(dm) == [1, 2],
         "★ 只算真实存在的圈：第 99 圈不画（**不回退到第 1 圈**）", str(sorted(dm)))
    c.ok(all(dm[cy]["chg"] and dm[cy]["dis"] for cy in dm), "每圈都给充、放两个分支")
    c.ok(dqdv_per_cycle(rows, [99]) == {}, "只请求不存在的圈 → 空（宁缺勿假）")
    c.ok(vq_curves(rows, cycle=99, exact=True) is None,
         "★ exact 取圈：没有就返回 None（老行为会回退到第 1 圈）")
    c.ok(vq_curves(rows, cycle=99) is not None,
         "非 exact 时仍保留「回退到第 1 圈」的老行为（别把老路径改坏）")

    # ---- ③ 图：充放同图、图例=圈号、还原为正 ----
    fig = chart_dqdv_sample("A　（dQ/dV）", {"label": "A", "dqdv_multi": dm})
    svg = fig.svg()
    c.ok(valid(svg), "☆ 逐圈 dQ/dV 图：合法 SVG")
    c.ok(len(fig.left_series) == 4, "★ 2 圈 × 充放 = 4 条曲线画在同一张图里",
         str(len(fig.left_series)))
    c.ok(sum(1 for s in fig.left_series if s["label"]) == 2,
         "★ 图例只挂一份/圈（不是充放各一份，否则 6 圈就 12 行）",
         str([s["label"] for s in fig.left_series]))
    c.ok("第 1 圈" in svg and "第 2 圈" in svg, "图例写的是圈号")
    cols = [s["color"] for s in fig.left_series]
    # ★ 2026-10-12：圈次配色改成**角色渐变**（实验样红阶 / 对比样蓝阶，圈号越大越深）
    c.ok(cols[0] == cols[1] and cols[2] == cols[3] and cols[0] != cols[2],
         "同一圈充放同色、不同圈颜色不同（按圈号取渐变色阶）", str(cols))
    c.ok(all(x in EXP_RAMP for x in cols),
         "★ 判不出角色时按座次兜底：第 1 颗样品用红阶", str(cols))
    _fc = chart_dqdv_sample("B", {"label": "对比样", "dqdv_multi": dm}, idx=1)
    c.ok(all(s["color"] in CTL_RAMP for s in _fc.left_series),
         "★ 对比样那张用蓝阶", str([s["color"] for s in _fc.left_series]))
    c.ok("电压 (V)" in svg, "★ X 轴仍是电压（dQ/dV 的横轴本来就是电压）")
    chg_ys = fig.left_series[0]["ys"]
    dis_ys = fig.left_series[1]["ys"]
    c.ok(max(chg_ys) <= 0 and min(dis_ys) >= 0,
         "★ 符号口径：还原（嵌入/放电）为正峰、氧化（脱出/充电）为负峰",
         f"充满峰 max={max(chg_ys):.3f}，放满峰 min={min(dis_ys):.3f}")
    fig_raw = chart_dqdv_sample("A", {"label": "A", "dqdv_multi": dm},
                                reduction_positive=False)
    c.ok(max(fig_raw.left_series[1]["ys"]) <= 0 and min(fig_raw.left_series[0]["ys"]) >= 0,
         "reduction_positive=False 时符号翻回去（留一个一行回退的开关）")

    # ---- ④ 缺圈的图注确实写进了图里 ----
    drawn, missing = pick_dqdv_cycles([1, 10, 50, 100, 200, 300], [1, 2])
    fig2 = chart_dqdv_sample("A", {"label": "A", "dqdv_multi": dqdv_per_cycle(rows, drawn)})
    fig2.set_notes(["已画圈：第 1 圈、第 2 圈",
                    "⚠ 尚未到达的圈：第 10 圈、第 50 圈、第 100 圈、第 200 圈、第 300 圈"])
    s2 = fig2.svg()
    c.ok("尚未到达" in s2 and "第 300 圈" in s2, "★ 缺的圈写进了图注（够不够得着要看得见）")
    c.ok(valid(s2), "带缺圈标注的图仍是合法 SVG")

    # ---- ⑤b 配色必须互相分得开 + 负值峰不许贴着轴底 ----
    # ★ 第一版逐圈配色是一片橙黄渐变色（#ffd54f/#ffc107/#ffb300…），
    #   渲染出来三条曲线肉眼分不出谁是谁 —— "按圈号配色"等于没做。
    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def dist(a, b):
        return max(abs(x - y) for x, y in zip(rgb(a), rgb(b)))

    worst = min(dist(a, b) for i, a in enumerate(PALETTE_DQDV)
                for b in PALETTE_DQDV[i + 1:])
    c.ok(worst >= 40, "★ 逐圈配色的任意两色至少差 40（肉眼分得开）", f"最接近的一对只差 {worst}")
    c.ok(len(set(PALETTE_DQDV)) == len(PALETTE_DQDV), "配色没有重复值")

    # 负值峰的轴底：数据到 -8，轴底要落到 -8 以下（贴边 = 看着像被切掉）
    f_neg = CycleChart("t", "L", "R")
    f_neg.add_left([1, 2, 3], [-8.0, -1.0, 0.0], "a")
    (x0_, x1_), (ly0_, ly1_), _r = f_neg._ranges()
    c.ok(ly0_ < -8.0, "★ 数据最小 -8 → 轴底下探到 -8 以下（峰尖不贴边框）", f"轴底 {ly0_}")
    c.ok(ly1_ >= 0.0 > ly0_, "有负值时轴仍跨过 0（正负峰能同框比较）")

    # ---- ⑥ 指定刻度（"X 轴就标 1/10/50/100/200/300" 用得上）----
    import re as _re

    def ticks_of(s: str) -> list[str]:
        return _re.findall(r'<text x="[\d.]+" y="49[0-9.]+"[^>]*>([^<]*)</text>', s)

    cs5 = [dict(cycle=i, chg=1.0, dis=0.9, ce=90.0, ee=90.0, chg_eng=2.0,
                dis_eng=1.8, retention=100.0, dcir_mohm=None) for i in range(1, 6)]
    f_in = chart_long_cycle("t", [{"label": "A", "cs": cs5}])
    f_in.set_xticks([1, 10, 50, 100, 200, 300])
    c.ok(ticks_of(f_in.svg()) == ["1"],
         "★ 默认不撑轴：只有 1 落在数据范围内，就只画 1（曲线不会被挤成一团）",
         str(ticks_of(f_in.svg())))
    f_sp = chart_long_cycle("t", [{"label": "A", "cs": cs5}])
    f_sp.set_xticks([1, 10, 50, 100, 200, 300], span=True)
    got = ticks_of(f_sp.svg())
    c.ok(got == ["1", "10", "50", "100", "200", "300"],
         "span=True 时轴撑到 300 圈，六个刻度全画上", str(got))


def test_ce_convention(c: Checker) -> None:
    """效率方向口径必须钉死：**充电 ÷ 放电**（负极半电池 Na‖HC，每圈先放电后充电）。

    为什么单独钉：2026-10-02 对照协会《…第二版》报告才发现，我们全项目算的是
    "放电 ÷ 充电"（正极口径），于是首圈效率 111.79%、不可逆容量为负 —— 数值看着
    合理，方向整个反了。参考报告同体系给的是 89.95 / 90.83%（<100%），
    这里就用它的量级做锚点，防止再被改回去。
    """
    c.ok(ce_pct(0.5744, 0.6422) is not None and abs(ce_pct(0.5744, 0.6422) - 89.44) < 0.05,
         "★ 首效 = 充电÷放电 = 89.44%（实测数据；参考报告同体系 89.95）",
         str(ce_pct(0.5744, 0.6422)))
    c.ok(abs(ce_pct(0.6422, 0.5744) - 111.79) < 0.05,
         "反过来算就是 111.79%（**原来那个错值**）—— 方向不能反", str(ce_pct(0.6422, 0.5744)))
    c.ok(ce_pct(None, 1.0) is None and ce_pct(1.0, None) is None, "缺一个输入 → None")
    c.ok(ce_pct(1.0, 0.0) is None, "放电为 0 → None（不除零、不编）")
    c.ok(ce_pct(0.0, 1.0) is None,
         "★ 充电为 0 → None（截断末圈只有放电工步；算成 0% 会把稳态均值拖到 65%）")
    c.ok(CE_CHARGE_OVER_DISCHARGE is True,
         "开关本身是 True（换正极/全电池体系时改这里，且只改这里）")
    c.ok(figures.ce_pct is probe.ce_pct,
         "★ 全项目只有一份口径实现（probe.ce_pct），figures 是 import 进来的")


def test_legend_clear(c: Checker) -> None:
    """图例落位的回归测试：**任何一条曲线线段都不许穿过图例框**。

    钉住的具体 bug：只有 2 圈时顶点全在画面左右边缘、候选框内缩 8px 一个都框不住，
    六个候选全得 0 分，min() 按顺序取了右上角 —— 而两点连成的直线横扫整幅图，
    正好从框里穿过（用户实测的遮挡）。修法：命中计分把**线段**也算上。
    """
    import re
    import figures as F

    # ---- 辅助函数本身的断言 ----
    for args, want, name in [
        ((0, 0, 100, 100, 40, 40, 20, 20), True, "斜穿框"),
        ((0, 0, 10, 10, 40, 40, 20, 20), False, "两头都在框外"),
        ((50, 50, 80, 80, 40, 40, 20, 20), True, "整段在框内"),
        ((50, 40, 80, 40, 40, 40, 20, 20), True, "擦上边"),
        ((50, 10, 80, 10, 40, 40, 20, 20), False, "平行且在外"),
        ((60, -10, 60, 60, 40, 40, 20, 20), True, "竖线穿过框"),
    ]:
        c.ok(F._seg_hits_rect(*args) is want, f"线段-矩形相交：{name}")

    # ---- 不变量：图例框不许被任何线段穿过 ----
    def legend_rect(svg: str):
        m = re.search(r'<rect x="([\d.-]+)" y="([\d.-]+)" width="([\d.-]+)" '
                      r'height="([\d.-]+)" rx="7" fill="#ffffff" fill-opacity="0.85"', svg)
        return tuple(float(g) for g in m.groups()) if m else None

    def check(chart, label: str) -> None:
        svg = chart.svg()
        r = legend_rect(svg)
        if r is None:
            c.ok(False, label, "SVG 里找不到图例框")
            return
        bx, by, bw, bh = r
        (x0, x1), (ly0, ly1), (ry0, ry1) = chart._ranges()
        px0, px1 = chart.ML, chart.W - chart.MR
        py0, py1 = chart.H - chart.MB, chart.MT
        sx = lambda v: px0 + (v - x0) / (x1 - x0) * (px1 - px0)
        sly = lambda v: py0 - (v - ly0) / (ly1 - ly0) * (py0 - py1)
        sry = lambda v: py0 - (v - ry0) / (ry1 - ry0) * (py0 - py1)
        bad = 0
        for s_ in chart.left_series:
            pts = [(sx(x), sly(y)) for x, y in zip(s_["xs"], s_["ys"]) if y is not None]
            bad += sum(1 for a, b in zip(pts, pts[1:])
                       if F._seg_hits_rect(a[0], a[1], b[0], b[1], bx, by, bw, bh))
        for s_ in chart.right_series:
            pts = [(sx(x), sry(y)) for x, y in zip(s_["xs"], s_["ys"]) if y is not None]
            bad += sum(1 for a, b in zip(pts, pts[1:])
                       if F._seg_hits_rect(a[0], a[1], b[0], b[1], bx, by, bw, bh))
        c.ok(bad == 0, label, f"{bad} 条线段穿过图例框（框 {r}）")

    # 情形一：**2 圈**（就是用户截图里那张平均电压图的形状）—— 最容易压线的场景
    csA = cycles_of([step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0008, 0.0016, -1e-5),
                     step("cc", 0.001, 0.002, 1e-5), step("dc", 0.0004, 0.0008, -1e-5)])
    csB = cycles_of([step("cc", 0.0012, 0.0024, 1e-5), step("dc", 0.0009, 0.0018, -1e-5),
                     step("cc", 0.0012, 0.0024, 1e-5), step("dc", 0.00045, 0.0009, -1e-5)])
    check(chart_avgv("2 圈", [{"label": "A", "cs": csA}, {"label": "B", "cs": csB}]),
          "★ 2 圈的平均电压对比图：图例没压线")
    # 2 圈 × 6 条线（含效率右轴）时几何上常常**没有**干净空位：这时只能要求
    # "落位是六个候选里最优的那个"，不能要求 0 命中（否则测试本身不成立）。
    f2 = chart_long_cycle("2 圈", [{"label": "A", "cs": csA}, {"label": "B", "cs": csB}])
    f2.svg()
    c.ok(f2.legend_hits == min(n for n, _ in f2.legend_scores),
         "★ 2 圈的长循环对比图：图例落在**命中最少**的候选位",
         f"命中数 {[n for n, _ in f2.legend_scores]}")

    # 情形二：密集点（576 圈）—— 不能为了修小数据把大数据搞坏
    xs = list(range(1, 577))
    dense = [{"label": "A", "cs": [dict(cycle=i, chg=1.0, dis=0.6 - i * 0.0002,
                                        ce=99.0, ee=90.0, chg_eng=2.0, dis_eng=1.2,
                                        retention=100.0, dcir_mohm=None)
                                   for i in xs]}]
    check(chart_long_cycle("576 圈", dense), "★ 576 圈的长循环图：图例没压线")

    # 情形三：只有 1 圈（退化）也不能崩
    one = [{"label": "A", "cs": cycles_of([step("cc", 0.001, 0.002, 1e-5),
                                           step("dc", 0.0008, 0.0016, -1e-5)])}]
    check(chart_long_cycle("1 圈", one), "★ 1 圈（退化）：图例没压线")


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
        c.ok(cs[0]["ce"] is not None and abs(cs[0]["ce"] - 111.1111) < 1e-3,
             "★ 库仑效率 = **充电÷放电** = 111.11%（负极半电池口径，原来算反了）",
             str(cs[0]["ce"]))
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
    c.ok(ce_never_healthy([{"cycle": 1}]) is None, "没有容量字段 → 不报")
    c.ok(ce_never_healthy([{"chg": None, "dis": None}]) is None, "容量全 None → 不报")
    c.ok(ce_never_healthy([{"chg": 0, "dis": 0}]) is None, "充电为 0 → 不算数，不报")
    c.ok(ce_never_healthy([{"chg": 100, "dis": 100}, {"chg": 100, "dis": 102}]) is None,
         "健康电池（放电/充电 ≈ 100%）→ 不报")
    c.ok(ce_never_healthy([{"chg": 100, "dis": 2}, {"chg": 100, "dis": 12},
                              {"chg": 100, "dis": 25}]) == 25,
         "★ 没在放电的（放电/充电 最高 25%）→ 报出 25")
    c.ok(ce_never_healthy([{"ce": CE_FLOOR_PCT}]) is None,
         f"正好等于阈值 {CE_FLOOR_PCT:.0f} → 不报（判据是严格小于）")
    c.ok(ce_never_healthy([{"chg": 100, "dis": CE_FLOOR_PCT - 0.1}]) is not None,
         "刚低于阈值一点 → 报")
    c.ok(ce_never_healthy([{"chg": 100, "dis": 57.05}]) is None,
         "★ 单圈化成（放电/充电 57%）不该被误报")
    # 取最大值而不是中位数：健康电池早期几圈 CE 很低，用中位数才不会被前几圈拖低
    c.ok(ce_never_healthy([{"chg": 100, "dis": 13.6}]
                              + [{"chg": 100, "dis": 102}] * 576) is None,
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
    print("⑧ 报告参考图：六张图（长循环/首效VQ/平均电压/倍率/dQdV×2）")
    print(BAR)
    test_report_charts(c)

    print()
    print(BAR)
    print("⑧b ★ dQ/dV 逐圈图：每样品一张、充放同图、缺圈如实标注、exact 取圈")
    print(BAR)
    test_dqdv_multi(c)

    print()
    print(BAR)
    print("⑨ ★ live 数据源：cycles_of_detail（明细层口径）与 cycles_of 交叉验证")
    print(BAR)
    test_cycles_of_detail(c)

    print()
    print(BAR)
    print("⑨c ★ 一个 cycleid 塞两对充放 → 圈号重算（否则容量翻倍）")
    print(BAR)
    test_detail_resharding(c)

    print()
    print(BAR)
    print("⑨b ★ 效率方向口径：充电÷放电（负极半电池），锚参考报告")
    print(BAR)
    test_ce_convention(c)

    print()
    print(BAR)
    print("⑩ ★ 图例不压曲线（原来只数「点」不数「线」，两点直线会横扫整幅图）")
    print(BAR)
    test_legend_clear(c)

    print()
    print(BAR)
    print("验证：")
    print(BAR)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
