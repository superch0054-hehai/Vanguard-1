#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告数据包构建 —— 把采集数据换算成提示词体系的数据契约。

产出（写入 --out，默认 ./报告任务）：
  global_context.json        全局上下文（每次模型调用携带）
  block_data/S31A.json …     块级数据包（字段名沿用《提示词体系》第 2/3 章）

分工（见 提示词文件/先看这个.md）：程序算全部数字，模型只写段落。
本脚本算出的每个数字都会进报告，所以**缺的数据填 null，绝不填 0 或估算值**
（体系文档第 2.2 节的明文规定）。

用法：
  python3 report_pack.py --data-dir ./data/live --mass-mg 2 \
      --channels 27-188-10-1,27-188-10-2 --out ./报告任务
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import figures as F
from probe import CAP_TO_MAH

VERSION = "1.0"

# 判定"是否超出个体波动范围"的默认阈值（体系文档 §2.1：n=1 时的约定口径）
THRESHOLDS = {"retention_pp": 2.0, "capacity_relative_pct": 5.0, "voltage_mv": 10.0}

# 报告 dQ/dV 图（图 5/6）默认要画的圈：整卷测试的观察点。
# 数据还没跑到就先缺着 —— 图上写"尚未到达"，不补不编。
DQDV_CYCLES = [1, 10, 50, 100, 200, 300]


# ---------------------------------------------------------------------------
# 单颗电池的全部派生指标（从明细层）
# ---------------------------------------------------------------------------

def load_cell(data_dir: Path, channel: str, mass_mg: float,
              dqdv_cycles: list[int] | None = None) -> dict:
    """找该通道最新的数据目录（live 优先），算出报告需要的全部指标。"""
    mass_g = mass_mg / 1000.0
    cands = []
    for root, kind in ((data_dir / "live", "live"), (data_dir, "official")):
        d = root / f"channel={channel}"
        if not d.is_dir():
            continue
        tids = sorted(int(x.parts[-1].split("=")[1]) for x in d.glob("testid=*")
                      if x.parts[-1].split("=")[1].isdigit())
        if tids:
            cands.append((d / f"testid={tids[-1]}", kind, tids[-1]))
    if not cands:
        return {}
    # live（进行中的新数据）优先；否则正式
    dd = next((c[0] for c in cands if c[1] == "live"), cands[0][0])
    kind = next((c[1] for c in cands if c[0] == dd))
    detail = F.load_detail(dd / "detail.csv")
    cs = F.cycles_of_detail(detail, mass_g)
    cur1 = F.vq_curves(detail, cycle=1)

    # ---- 有效圈（有放电容量的圈）----
    done = [c for c in cs if (c.get("dis") or 0) > 0]
    # ★ 体系文档 §2.3 第 7 条：剔除末圈被截断的循环。live（进行中）的最后一圈
    #   必然是被截断的 —— 不剔除的话，"最新比容量骤降"的假象会进报告
    #   （实测 264 末圈 32 mAh/g，剔除后恢复正常水平）。
    trend = done[:-1] if kind == "live" and len(done) >= 2 else done
    effective = [c["cycle"] for c in trend]

    # ---- 衰减速率：放电比容量 vs 圈号的最小二乘斜率（每百圈）----
    # ★ 明细层一个 cycleid 里可能塞了两对充放（合并口径），某圈容量会突然翻倍 ——
    #   这时线性拟合会给出 +12523 这种荒唐速率（实测）。有跳变就**不给速率**。
    jump = None
    for p, q in zip(trend, trend[1:]):
        if p.get("dis") and q.get("dis") and q["dis"] > p["dis"] * 1.3:
            jump = (p["cycle"], q["cycle"])
            break
    slope = 0.0
    if jump is None and len(trend) >= 2:
        xs = [c["cycle"] for c in trend]
        ys = [c["dis_sp"] for c in trend]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom * 100.0) if denom else 0.0

    # ---- 库仑效率统计（剔除首圈；CE>100.5% 记为异常段——体系文档的剔除口径）----
    ces = [c["ce"] for c in cs if c.get("ce") is not None][1:] or []
    ce_anomaly = None
    if ces:
        bad = [i + 1 for i, v in enumerate(ces) if v > 100.5]
        if bad:
            ce_anomaly = {"from_cycle": bad[0], "to_cycle": bad[-1], "max": max(ces)}
    ce_steady = [v for v in ces if v <= 100.5]

    # ---- ΔV 极化（充电平均电压 − 放电平均电压，mV）与平台容量占比 ----
    dvs, plat, dis_v = [], [], []
    for c in cs:
        if not c.get("chg") or not c.get("dis"):
            dvs.append(None)
            plat.append(None)
            dis_v.append(None)
            continue
        dv_v = c["chg_eng"] / c["chg"] - c["dis_eng"] / c["dis"]
        dvs.append(dv_v * 1000.0)
        # 平均放电电压（能量平均电压口径 = 放电比能量 ÷ 放电比容量），协会报告 3.4 节口径
        dis_v.append(c["dis_eng"] / c["dis"])
        # 平台容量占比：该圈放电容量中电压低于 0.10 V 的份额（按体系文档口径）
        plat.append(platform_share_of_cycle(dd, c["cycle"], 0.10))
    plat_vals = [v for v in plat if v is not None]

    # ---- 平均放电电压的衰减速率（mV/百圈，只对有效的圈拟合）----
    avgv_slope = None
    pairs = [(c["cycle"], dis_v[i]) for i, c in enumerate(cs)
             if i < len(dis_v) and dis_v[i] is not None and c["cycle"] in set(effective)]
    if len(pairs) >= 2:
        mx = sum(p[0] for p in pairs) / len(pairs)
        my = sum(p[1] for p in pairs) / len(pairs)
        den = sum((p[0] - mx) ** 2 for p in pairs)
        if den:
            avgv_slope = round(
                sum((p[0] - mx) * (p[1] - my) for p in pairs) / den * 100.0 * 1000.0, 2)
    ee_vals = [(c["cycle"], c["ee"]) for c in cs
               if c.get("ee") is not None and c["cycle"] in set(effective)]

    # ---- 保持率序列（基线 = 首个完成放电的圈）与两电池交叉检测 ----
    # 保持率只看有效圈：把没走完的末圈排除掉，否则"最新保持率"会取到截断圈的假数
    ret = {c["cycle"]: c["retention"] for c in cs
           if c.get("retention") is not None and c["cycle"] in set(effective)}
    # ★ 内部序列是"相对第 1 圈"的；报告口径的基线是第 4 圈 → 必须**重新归一**，
    #   否则表 4 的"保持率-基线"会写成 84.94% 而不是 100%（看着像已经衰减了）。
    ret_rel = {}
    if BASELINE_CYCLE in ret and ret[BASELINE_CYCLE]:
        b = ret[BASELINE_CYCLE]
        ret_rel = {k: round(v / b * 100.0, 2) for k, v in ret.items()}

    # ---- dQ/dV 两分支（第 1 圈）与峰位 ----
    cur1 = F.vq_curves(detail, cycle=1)
    dq = {"chg": F.dqdv_curve(cur1["charge"]) if cur1 else [],
          "dis": F.dqdv_curve(cur1["discharge"]) if cur1 else []}

    # ---- 多圈 dQ/dV（报告图 5/6：每样品一张、氧化+还原同图、按圈号配色）----
    want = list(dqdv_cycles) if dqdv_cycles else list(DQDV_CYCLES)
    drawn, missing = F.pick_dqdv_cycles(want, effective)
    dqdv_multi = F.dqdv_per_cycle(detail, drawn)
    peaks_multi = {c: peaks(v) for c, v in sorted(dqdv_multi.items())}

    # ---- 数据截止时间（live 的 progress.json；正式数据集用采集时间）----
    prog = dd / "progress.json"
    data_cutoff = None
    if prog.exists():
        try:
            data_cutoff = json.loads(prog.read_text(encoding="utf-8-sig")).get("updated_at")
        except (json.JSONDecodeError, OSError):
            pass
    if not data_cutoff:
        data_cutoff = done[-1]["cycle"] if done else None
        data_cutoff = f"测试 {cands[0][2]} 的数据" if cands else None

    # ---- 实测电压窗口（从明细层现算）----
    # ★ 这里原来是写死的 "0.02~3.31 V"。那是另一批数据（正极 3.3 V 体系）的范围；
    #   现在这几颗是 2 V 体系（实测 0.01~2.14 V），写死就把别的实验的窗口
    #   抄进了报告的表 3 —— 数据口径错，从结果上看不出来。所以现算。
    vv = []
    for r in detail:
        try:
            vv.append(float(r.get("volt")))
        except (TypeError, ValueError):
            continue
    v_min = min(vv) if vv else None
    v_max = max(vv) if vv else None

    return {
        "dir": str(dd), "kind": kind, "testid": cands[0][2] if cands else None,
        "v_min": v_min, "v_max": v_max,
        "cs": cs, "effective_cycles": len(trend),
        # 画图用的逐圈序列：**与趋势/指标同一套圈**（进行中数据的末圈被截断，
        # 已经剔掉）。图和数据包用两套圈的话，图上那个掉到 0 的末点会被当成真数据。
        "cs_plot": [c for c in cs if c["cycle"] in set(effective)],
        "dropped_cycles": [c["cycle"] for c in cs if c["cycle"] not in set(effective)],
        "cycle_range": [effective[0], effective[-1]] if effective else None,
        "dis_sp_last": trend[-1]["dis_sp"] if trend else None,
        "fade_rate_linear_fit": (round(slope, 2)
                                 if len(trend) >= 2 and jump is None else None),
        "fade_rate_unreliable": ("逐圈容量在 第 %d→%d 圈 出现非单调跳变"
                                "（明细层圈号合并口径所致），衰减速率不给出"
                                % jump) if jump else None,
        "progress_updated": data_cutoff,
        "ce_steady_mean": round(sum(ce_steady) / len(ce_steady), 2) if ce_steady else None,
        "ce_fluctuation": round(max(ce_steady) - min(ce_steady), 2) if ce_steady else None,
        "ce_anomaly": ce_anomaly,
        "first": first_cycle(cur1, cs, mass_g),
        "dv_series": dvs, "dv_mean": round(sum(v for v in dvs if v is not None)
                                           / len([v for v in dvs if v is not None]), 1)
        if any(v is not None for v in dvs) else None,
        "dv_last": next((v for v in reversed(dvs) if v is not None), None),
        "dis_v_series": dis_v,
        "dis_v_first": next((v for v in dis_v if v is not None), None),
        "dis_v_last": next((v for v in reversed(dis_v) if v is not None), None),
        "avgv_slope_mv_100c": avgv_slope,
        "ee_first": ee_vals[0][1] if ee_vals else None,
        "ee_last": ee_vals[-1][1] if ee_vals else None,
        "platform_last": plat_vals[-1] if plat_vals else None,
        "retention": ret, "retention_rel_base": ret_rel,
        "dqdv": dq,
        "dqdv_multi": dqdv_multi,
        "dqdv_cycles_drawn": sorted(dqdv_multi),
        "dqdv_cycles_missing": missing,
        "peaks_multi": peaks_multi,
        "vq1": cur1,
    }


def platform_share_of_cycle(dd: Path, cycle_no: int, threshold_v: float) -> float | None:
    """某圈放电容量中、电压低于 threshold_v 的份额（0~1）。

    口径：按**逐条记录的容量增量**累计 —— 只有电压低于阈值的那些记录，
    其容量才算进"平台区"。**不能用工步末值**：末值只有一行，放电一路走到
    0.01 V，那一行必然低于阈值，份额会永远算出 100%（协会报告同体系给出的是
    68% 量级，所以这个口径明显不对）。
    """
    import csv
    detail_path = dd / "detail.csv"
    if not detail_path.exists():
        return None
    total = below = 0.0
    prev: dict[str, float] = {}
    with detail_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("cycleid") != str(cycle_no - 1) or r.get("steptype") not in ("dc", "dv"):
                continue
            try:
                cap = float(r.get("cap") or 0)
                volt = float(r.get("volt") or 0)
            except ValueError:
                continue
            sid = r.get("stepid") or ""
            inc = cap - prev.get(sid, 0.0)
            prev[sid] = cap
            if inc <= 0:
                continue
            total += inc
            if volt < threshold_v:
                below += inc
    return round(below / total, 4) if total > 0 else None


def first_cycle(cur1: dict | None, cs: list[dict],
                mass_g: float | None = None) -> dict:
    """首圈指标（3.3 节用）：充/放**比容量**、首效、不可逆容量。

    ★ 必须除以质量（g）：这里是比容量口径。原来的 `/1.0` 让首圈比容量直接输出成
    "0.6422 mAh·g⁻¹"，实际是 0.6422 mAh —— 差 1000 倍，而且看图看不出来
    （协会同体系报告给的是 284~296 mAh·g⁻¹，一对就露馅）。
    """
    if not cur1:
        return {"charge_sp": None, "discharge_sp": None, "ce": None,
                "irreversible_sp": None}
    q_chg = max((q for q, _ in cur1["charge"]), default=0.0)
    q_dis = max((q for q, _ in cur1["discharge"]), default=0.0)
    ce = (q_dis / q_chg * 100.0) if q_chg else None
    if not mass_g:
        return {"charge_sp": None, "discharge_sp": None, "ce": round(ce, 2)
                if ce is not None else None, "irreversible_sp": None}
    return {"charge_sp": round(q_chg / mass_g, 2) if q_chg else None,
            "discharge_sp": round(q_dis / mass_g, 2) if q_dis else None,
            "ce": round(ce, 2) if ce is not None else None,
            "irreversible_sp": round((q_chg - q_dis) / mass_g, 2)
            if q_chg and q_dis else None}


# ---------------------------------------------------------------------------
# 数据契约构建
# ---------------------------------------------------------------------------

def _is_std_q_pair(labels: dict[str, str]) -> bool:
    """这一对样品是不是"基础电解液 STD vs 1% Q④ 添加剂"？

    为什么值得判断：体系/变量信息来自协会《…第二版》报告（Na‖HC、醚类电解液、
    1 wt% Q④），**只对这两个样品成立**。任意换两个通道时不能把别人的电解液
    写进表 1 —— 那是"张冠李戴"，从结果上看不出来。
    """
    tx = " ".join(labels.values()).upper()
    return ("STD" in tx) and ("Q" in tx)


def _voltage_window(cells: dict[str, dict], labels: dict[str, str]) -> str | None:
    """实测电压窗口（取各样品明细层的并集）。拿不到就 None —— 不许编。

    为什么现算：这个值以前是写死的 "0.02~3.31 V"，那是另一批 3.3 V 体系数据的窗口。
    换到 2 V 体系的样品后，报告表 3 里会留着别的实验的数字 —— 这种错从结果上看不出来。
    """
    los = [c["v_min"] for c in cells.values() if c.get("v_min") is not None]
    his = [c["v_max"] for c in cells.values() if c.get("v_max") is not None]
    if not los or not his:
        return None
    return f"{min(los):.2f}~{max(his):.2f} V（实测范围，待实验室确认）"


# 协会《钠离子电池电解液添加剂 Q④ 性能测试分析报告·第二版》里的体系与工步口径。
# 只在与该报告同一体系（STD vs 1% Q④）时填入报告 —— 见 _is_std_q_pair。
BASELINE_CYCLE = 4          # 保持率基线：第 4 圈（化成后首个 1C 循环）
SYSTEM_STD_Q = {
    "cell_type": "CR2032 扣式半电池（Na‖HC）",
    "chemistry": "钠离子电池（硬碳负极 / 醚类电解液）",
    "counter_electrode": "金属钠片（Na，兼参比与对电极）",
    "working_electrode": "硬碳（HC）",
    "separator": "Whatman GF/A 玻璃纤维",
    "current_collector": "铝箔（Al）",
    "electrolyte_control": "二乙二醇二甲醚 + NaPF₆ + NaOTf（基础电解液）",
    "electrolyte_experimental": "基础电解液 + 1 wt% Q④（含氟分子）",
    "ions": "Na⁺",
}
TEST_COND_STD_Q = {
    "temperature": "25±1 ℃",
    "humidity": "< 10% RH",
    "nominal_specific_capacity": "300 mAh·g⁻¹（硬碳，1C = 300 mA·g⁻¹）",
    "formation": "0.1C × 3 圈（化成活化，形成初始 SEI）",
    "long_cycle_rate": "1C（自第 4 圈起恒定充放电）",
    "rate_program": "0.2C→5C→0.2C，13 个倍率档 × 6 圈 = 78 圈",
}


def build_global_context(labels: dict[str, str], cells: dict[str, dict],
                         mass_mg: float, generated: str) -> dict:
    chs = sorted(labels)
    std_q = _is_std_q_pair(labels)
    reach = [max((c["cycle_range"][1] for c in cells.values()
                  if c.get("cycle_range")), default=0)]
    base_ok = bool(reach) and reach[0] >= BASELINE_CYCLE
    return {
        "report": {
            "title": "电池测试数据分析报告（阶段性）",
            "subtitle": f"{' vs '.join(labels)} 的电化学性能对比（进行中测试）",
            "date": generated,
        },
        "system": dict(SYSTEM_STD_Q) if std_q else {
            "cell_type": "扣式电池（体系信息待实验室提供）",
            "chemistry": None, "counter_electrode": None, "working_electrode": None,
            "separator": None, "current_collector": None,
            "electrolyte_control": None, "electrolyte_experimental": None, "ions": None,
        },
        "variable": {
            "type": "电解液添加剂（含氟分子 Q④，1 wt%）" if std_q else None,
            "control_short": labels[chs[0]] if len(chs) > 1 else chs[0],
            "control_name": f"通道 {chs[0]}（{labels[chs[0]]}）" if len(chs) > 1 else chs[0],
            "experimental_short": labels[chs[1]] if len(chs) > 1 else chs[0],
            "experimental_name": f"通道 {chs[1]}（{labels[chs[1]]}）" if len(chs) > 1 else chs[0],
            "experimental_dosage": "1 wt%" if std_q else None,
        },
        "test_conditions": {
            "temperature": TEST_COND_STD_Q["temperature"] if std_q else None,
            "humidity": TEST_COND_STD_Q["humidity"] if std_q else None,
            "voltage_window": _voltage_window(cells, labels),
            "nominal_specific_capacity": TEST_COND_STD_Q["nominal_specific_capacity"]
            if std_q else None,
            "formation": TEST_COND_STD_Q["formation"] if std_q else None,
            "long_cycle_rate": TEST_COND_STD_Q["long_cycle_rate"] if std_q else None,
            "rate_program": TEST_COND_STD_Q["rate_program"] if std_q else None,
            "retention_baseline_cycle": BASELINE_CYCLE,
            "retention_baseline_note": (
                f"基线为第 {BASELINE_CYCLE} 圈（化成后首个 1C 循环，协会第二版报告口径）"
                + ("" if base_ok else
                   "；**本批数据尚未到达该圈**，故保持率一律记 null，"
                   "不得用第 1 圈当基线凑数")),
            "platform_voltage_threshold": 0.10,
            "data_cleaning": ["末圈未走完（进行中），其容量数据不参与趋势判断",
                              "剔除库伦效率持续超过 100.5% 的异常尾段",
                              "测试进行中，本报告为阶段性数据"],
        },
        "statistics": {"n_parallel": 1, "significance_thresholds": THRESHOLDS},
        "mass": {"mass_mg": mass_mg, "note": "暂定值（2026-10-01 用户给定），待测试记录确认"},
        "generated": generated,
        "generator": f"report_pack {VERSION}",
    }


def build_block_data(block_id: str, cells: dict[str, dict],
                     ctx: dict, labels: dict[str, str]) -> dict:
    """按体系文档第 3 章的输入字段，构建各块的数据包。

    缺数据一律 null + notes 说明 —— 模型按铁律写"（该项数据未提供）"，
    这比编一个数诚实得多（体系文档第 2.2 节的规定）。
    """
    chs = sorted(cells)
    a, b = (chs + [None, None])[:2]
    ca, cb = cells.get(a, {}), cells.get(b, {})
    la, lb = labels.get(a, a), labels.get(b, b)
    base = {"block_id": block_id, "groups": {}, "notes": []}

    if block_id == "S11":
        # 体系背景来自协会《…第二版》1.1 节（同一体系时才有；换通道时如实置空）
        if ctx["variable"].get("type"):
            base["background_points"] = {
                "体系优势": "醚类电解液电化学窗口宽、离子电导率高",
                "现存短板": "在硬碳负极表面界面稳定性差、循环寿命不足",
                "变量作用路径": "引入含氟添加剂构建稳定的电极/电解液界面膜（SEI）",
            }
        else:
            base["background_points"] = {"体系优势": None, "现存短板": None,
                                         "变量作用路径": None}
            base["notes"].append("样品体系信息未提供")
    elif block_id == "S12":
        if ctx["variable"].get("type"):
            base["test_items"] = ["长循环稳定性", "倍率性能", "首圈库伦效率",
                                  "平均电压与极化", "dQ/dV 反应特征"]
            base["statement_goal"] = ("量化两组在各指标上的差异幅度、判断是否超出"
                                      "单颗电芯的个体波动范围，不做显著性判定")
        else:
            base["test_items"] = None
            base["statement_goal"] = None
            base["notes"].append("实验变量未提供")
    elif block_id == "S31A":
        base["rate"] = TEST_COND_STD_Q["long_cycle_rate"] if ctx["variable"].get("type") else None
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = {
                    "effective_cycles": cell["effective_cycles"],
                    "capacity_trend": ("先降后趋稳" if cell["effective_cycles"] >= 3
                                       else "圈数尚少"),
                    "fade_rate_linear_fit": cell["fade_rate_linear_fit"],
                    "fade_rate_unit": "mAh·g⁻¹·百圈⁻¹",
                    "capacity_level": None,
                }
        ma = next((c["ma"] for c in (ca.get("cs") or []) if c.get("ma")), None)
        if ma:
            base["notes"].append(
                f"★ 本批实测平均电流约 {ma:.4g} mA —— 处于**小电流活化阶段**，"
                f"1C 长循环段尚未开始；衰减速率只是活化段的数值，不代表长循环性能")
        base["notes"].append("圈数尚少，衰减速率仅供参考")
    elif block_id == "S31B":
        base["baseline_cycle"] = ctx["test_conditions"]["retention_baseline_cycle"]
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                rel = cell.get("retention_rel_base") or {}
                base["groups"][labels[key]] = {
                    "retention_baseline": rel.get(base["baseline_cycle"]),
                    "retention_last": rel.get(max(rel)) if rel else None,
                    "baseline_is_first_plotted": base["baseline_cycle"] in rel,
                }
        if len(base["groups"]) == 2:
            vs = list(base["groups"].values())
            r1, r2 = vs[0]["retention_last"], vs[1]["retention_last"]
            if r1 is not None and r2 is not None:
                base["diff_pp"] = round(r1 - r2, 2)
                base["crossing"] = False
        # 基线圈（第 4 圈）还没到 → 保持率一律 null，正文必须写"未提供"
        reached = max((c["cycle_range"][1] for c in cells.values()
                       if c.get("cycle_range")), default=0)
        if reached < base["baseline_cycle"]:
            base["notes"].append(
                f"★ 数据仅到第 {reached} 圈，未到达基线圈（第 {base['baseline_cycle']} 圈）："
                "保持率与保持率差一律记 null，正文写「（该项数据未提供）」，"
                "不得改用第 1 圈当基线")
            base["groups"] = {k: {"retention_baseline": None, "retention_last": None}
                              for k in base["groups"]}
    elif block_id == "S31C":
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = {
                    "CE_steady_mean": cell["ce_steady_mean"],
                    "CE_fluctuation": cell["ce_fluctuation"],
                    "CE_anomaly": cell["ce_anomaly"],
                    "curve_smoothness": "圈数尚少，无法判断" if cell["effective_cycles"] < 5
                                        else "基本平滑",
                }
        base["notes"].append("★ 效率口径：设备导出的充放电效率为容量比，放电端提前触及 "
                             "0.01 V 截止电压时该值会超过 100%；协会口径为剔除"
                             "库伦效率持续超过 100.5% 的异常尾段。效率数值由程序给出、"
                             "模型只引用，不得据此判断电池好坏")
    elif block_id == "S32A":
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = cell["first"]
        base["curve_feature"] = "V 形（充电上升、放电下降）"
    elif block_id == "S32B":
        # 3.3 首圈库伦效率 · 第 2 段：不可逆容量与损失定位
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = dict(cell["first"])
        if len(base["groups"]) == 2:
            vs = list(base["groups"].values())
            if vs[0].get("irreversible_sp") is not None and vs[1].get("irreversible_sp") is not None:
                base["irreversible_diff_sp"] = round(
                    vs[1]["irreversible_sp"] - vs[0]["irreversible_sp"], 4)
        base["loss_location"] = "充电曲线低于放电起始平台之间的斜坡段（SEI 成膜耗钠）"
        base["protocol_note"] = "两组电压窗口与化成工步完全一致，首效具备可比性"
        base["notes"].append("首效对充放电截止电压与化成协议高度敏感，不得据此判定"
                             "添加剂改善了首效")
    elif block_id == "S32C":
        # 3.4 平均电压曲线分析 · 第 1 段：平均放电电压（能量平均电压口径）
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = {
                    "avg_v_first": (round(cell["dis_v_first"], 4)
                                    if cell.get("dis_v_first") is not None else None),
                    "avg_v_last": (round(cell["dis_v_last"], 4)
                                   if cell.get("dis_v_last") is not None else None),
                    "avg_v_fade_mv_100c": cell.get("avgv_slope_mv_100c"),
                }
        base["avg_v_basis"] = "能量平均电压口径（放电比能量 ÷ 放电比容量）"
        base["notes"].append("圈数尚少，电压衰减速率仅供参考")
    elif block_id == "S32D":
        # 3.4 · 第 2 段：ΔV 极化、能量效率、平台容量占比
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = {
                    "dV_mean_mv": cell["dv_mean"], "dV_last_mv": cell["dv_last"],
                    "EE_first_pct": (round(cell["ee_first"], 2)
                                     if cell.get("ee_first") is not None else None),
                    "EE_last_pct": (round(cell["ee_last"], 2)
                                    if cell.get("ee_last") is not None else None),
                    "platform_share_last_pct": (round(cell["platform_last"] * 100, 2)
                                                if cell["platform_last"] is not None else None),
                }
        if len(base["groups"]) == 2:
            vs = list(base["groups"].values())
            if vs[0]["dV_mean_mv"] is not None and vs[1]["dV_mean_mv"] is not None:
                base["dV_diff_mv"] = round(vs[0]["dV_mean_mv"] - vs[1]["dV_mean_mv"], 1)
        base["notes"].append("EE 与 CE 的下降幅度要分别陈述，不得混为一谈")
    elif block_id in ("S33A", "S33B"):
        base["notes"].append("★ 本批数据尚未进入 0.2C→5C→0.2C 倍率段（仍在活化段），"
                             "倍率维度数据未提供——正文如实写「（该项数据未提供）」")
    elif block_id == "S34A":
        for key, cell in ((a, ca), (b, cb)):
            if cell:
                base["groups"][labels[key]] = {
                    "peak_positions": peaks(cell["dqdv"]),
                    "peak_positions_by_cycle": cell.get("peaks_multi") or {},
                    "cycles_drawn": cell.get("dqdv_cycles_drawn") or [],
                }
        drawn_txt = "、".join(f"第 {c} 圈" for c in (ca.get("dqdv_cycles_drawn") or []))
        miss = ca.get("dqdv_cycles_missing") or []
        note = f"dQ/dV 逐圈图（图 5/6）已画圈：{drawn_txt or '无'}"
        if miss:
            note += ("；报告要求的第 " + "、".join(str(c) for c in miss)
                     + " 圈数据尚未到达（测试进行中）——图上如实标注，不得外推")
        base["notes"].append(note)
    elif block_id == "S34B":
        base["platform_voltage_threshold"] = ctx["test_conditions"]["platform_voltage_threshold"]
        base["notes"].append("平台区分界 0.10 V 为协会第二版报告口径（已确认）；"
                             "峰宽 FWHM、放电峰高、峰面积占比需要更多圈次的峰分析，"
                             "本轮数据未到，如实写「（该项数据未提供）」")
    elif block_id in ("S35A", "S35B", "S35C", "S41A", "S41B", "S41C", "S41D",
                      "S51A", "S51B", "S51C"):
        base["dimension_summary"] = dimension_summary(cells, labels, chs)
        base["notes"].append("n_parallel = 1，所有差异幅度需与个体波动范围阈值对照")
        if block_id in ("S41A", "S41B", "S41C"):
            base["cross_check"] = None
            base["verdict"] = verdict_for(block_id, cells, labels, chs)
    elif block_id in ("S43A", "S43B", "S43C"):
        base["suggestions"] = {"formulation_validation": None,
                               "characterization": None, "application": None}
        base["notes"].append("后续建议未提供（demo 阶段）")
    elif block_id in ("S52A", "S52B", "S52C"):
        base["outlook"] = {"system_iteration": None, "industrialization": None,
                           "deep_research": None}
        base["notes"].append("展望条目未提供（demo 阶段）")
    return base


def peaks(dq: dict) -> dict:
    """dQ/dV 两分支的主峰位置（幅值最大的点）。"""
    out = {}
    for br in ("chg", "dis"):
        pts = dq.get(br) or []
        if pts:
            v, q = max(pts, key=lambda t: abs(t[1]))
            out[br] = {"voltage_V": round(v, 3), "dQdV": round(q, 3)}
    return out


def dimension_summary(cells: dict[str, dict], labels: dict[str, str],
                      chs: list[str]) -> list[dict]:
    """台账：每个维度的方向与幅度（S35/S41/S51 的输入）。"""
    a, b = (chs + [None, None])[:2]
    ca, cb = cells.get(a, {}), cells.get(b, {})
    out = []
    if ca.get("dis_sp_last") is not None and cb.get("dis_sp_last") is not None:
        d = round(ca["dis_sp_last"] - cb["dis_sp_last"], 2)
        out.append({"name": "放电比容量（最新）", "direction":
                    f"{labels.get(a, a)} 略高" if d > 0 else f"{labels.get(b, b)} 略高",
                    "magnitude": f"{abs(d)} mAh/g"})
    if ca.get("first", {}).get("ce") is not None and cb.get("first", {}).get("ce") is not None:
        d = round(ca["first"]["ce"] - cb["first"]["ce"], 2)
        out.append({"name": "首次库仑效率", "direction":
                    f"{labels.get(a, a)} 略高" if d > 0 else f"{labels.get(b, b)} 略高",
                    "magnitude": f"{abs(d)} pp"})
    if ca.get("dv_mean") is not None and cb.get("dv_mean") is not None:
        d = round(ca["dv_mean"] - cb["dv_mean"], 1)
        out.append({"name": "极化 ΔV", "direction":
                    f"{labels.get(a, a)} 略大" if d > 0 else f"{labels.get(b, b)} 略大",
                    "magnitude": f"{abs(d)} mV"})
    return out


def verdict_for(block_id: str, cells: dict[str, dict], labels: dict[str, str],
                chs: list[str]) -> str:
    """S41A/B/C 的结论词（只能取体系文档给的五选一）。"""
    return "基本相当"      # n=1 且圈数少，demo 阶段的诚实结论


def main() -> int:
    p = argparse.ArgumentParser(description="报告数据包构建（提示词体系数据契约）")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--mass-mg", type=float, default=2.0,
                   help="活性物质质量（mg）—— 暂定值，待测试记录确认")
    p.add_argument("--channels", default="27-188-10-1,27-188-10-2",
                   help="要对比的通道，逗号分隔（至少 2 个）")
    p.add_argument("--out", default="./报告任务")
    args = p.parse_args()

    data = Path(args.data_dir)
    out = Path(args.out)
    chs = [c.strip() for c in args.channels.split(",") if c.strip()]
    if len(chs) < 2:
        print("至少要 2 个通道", file=sys.stderr)
        return 2
    labels = {c: c for c in chs}        # demo 阶段用通道号当样品名

    cells = {}
    for ch in chs:
        cell = load_cell(data, ch, args.mass_mg)
        if not cell:
            print(f"  ⚠️  {ch}：找不到数据目录，跳过", file=sys.stderr)
            continue
        cells[ch] = cell
        print(f"  {ch}: test {cell['testid']}  有效圈 {cell['effective_cycles']}"
              f"  末圈比容量 {cell['dis_sp_last']} mAh/g")

    if len(cells) < 2:
        print("可用的通道不足 2 个", file=sys.stderr)
        return 1

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx = build_global_context(labels, cells, args.mass_mg, generated)

    out.mkdir(parents=True, exist_ok=True)
    (out / "global_context.json").write_text(
        json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
    bd = out / "block_data"
    bd.mkdir(exist_ok=True)
    n = 0
    for bid in ("S11", "S12", "S31A", "S31B", "S31C", "S32A", "S32B", "S32C",
                "S33A", "S33B", "S34A", "S34B", "S35A", "S35B", "S35C",
                "S41A", "S41B", "S41C", "S41D", "S43A", "S43B", "S43C",
                "S51A", "S51B", "S51C", "S52A", "S52B", "S52C"):
        d = build_block_data(bid, cells, ctx, labels)
        (bd / f"{bid}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    print(f"  数据包 {n} 个 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
