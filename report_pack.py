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
              dqdv_cycles: list[int] | None = None,
              source: str = "auto",
              testid: int | None = None) -> dict:
    """找该通道的数据目录并算出报告需要的全部指标。

    source：auto=live 优先（进行中测试的新数据）；live=只取进行中；
    official=只取已结束的正式数据集（两套报告都出，来源必须写明，见 report_build --source）。
    testid：指定用哪一个测试。不给就取**最新的那个**（= 该通道当前在跑的测试）。
    """
    mass_g = mass_mg / 1000.0
    # 先按来源筛掉不看的那些根，再在剩下的根里找 —— 否则会出现
    # "live 里明明有 264、却因为在 official 里找不到而误报没有"（2026-10-04 实测踩到）。
    cands = []
    avail: list[int] = []
    for root, root_kind in ((data_dir / "live", "live"), (data_dir, "official")):
        d = root / f"channel={channel}"
        if not d.is_dir():
            continue
        tids = sorted(int(x.parts[-1].split("=")[1]) for x in d.glob("testid=*")
                      if x.parts[-1].split("=")[1].isdigit())
        if not tids:
            continue
        avail += tids
        pick = testid if testid is not None else tids[-1]
        if pick not in tids:
            continue
        dd = d / f"testid={pick}"
        # ★ 2026-10-09 修：合并成一个位置后，**"进行中 / 已结束"由桶自己的
        #   progress.json 决定，不能靠"在哪个目录"来判断**。
        #   上一版把 --source 的筛选加在"目录"上，于是 --source live 把真正的桶
        #   整个排除了 → 报告报"找不到数据目录"（真机验证当场抓到）。
        kind = root_kind
        pj = dd / "progress.json"
        if pj.exists():
            try:
                if "finished" in json.loads(pj.read_text(encoding="utf-8-sig")):
                    kind = ("official" if json.loads(
                        pj.read_text(encoding="utf-8-sig"))["finished"] else "live")
            except (OSError, ValueError):
                pass
        cands.append((dd, kind, pick))
    # 按来源筛的是**候选**（状态），不是目录
    if source in ("live", "official"):
        cands = [c for c in cands if c[1] == source]
    if testid is not None and not cands:
        return {"error": f"该通道没有 testid={testid}（可选：{sorted(set(avail))}）"}
    if not cands:
        return {}
    # live（进行中的新数据）优先；否则正式
    dd = next((c[0] for c in cands if c[1] == "live"), cands[0][0])
    kind = next((c[1] for c in cands if c[0] == dd))
    tid = next((c[2] for c in cands if c[0] == dd))

    # ★ 2026-10-06：**"是不是还在跑"改从桶里的 progress.json 读**，不再靠"数据在哪个目录"。
    #   水桶模型下只有一个位置，靠目录已经分不出进行中/已结束了 ——
    #   而下游要靠它决定"要不要剔除被截断的末圈"（判错就会出现
    #   "最新比容量骤降"的假象，实测踩过）。老数据集没有这个标记，退回按目录判断。
    _prog = dd / "progress.json"
    if _prog.exists():
        try:
            _pj = json.loads(_prog.read_text(encoding="utf-8-sig"))
            if "finished" in _pj:
                kind = "official" if _pj["finished"] else "live"
        except (OSError, ValueError):
            pass

    detail = F.load_detail(dd / "detail.csv")
    cs = F.cycles_of_detail(detail, mass_g)

    # ★ 最新那个测试可能**才刚起、一圈都没跑完**。以前这里取了 tids[-1] 就往下走，
    #   于是出一份全空的报告（图上没有点、表里没有数，却看着像"报告出来了"）。
    #   实测 2026-10-04：10-1 换了新测试 282（0 圈），报告全空。
    #   现在**明说"没有可报告的圈"**，并把可选的测试号列出来 —— 宁可报错，不交空报告。
    if not cs:
        have = sorted(int(x.parts[-1].split("=")[1]) for x in dd.parent.glob("testid=*")
                      if x.parts[-1].split("=")[1].isdigit())
        return {"error": f"测试 {tid} 还没有跑完任何一圈（0 圈），出不了报告。"
                         f"要么等它跑出圈来，要么显式指定另一个测试"
                         f"（该通道有：{have}）"}

    cur1 = F.vq_curves(detail, cycle=1)

    # ---- 有效圈（有放电容量的圈）----
    done = [c for c in cs if (c.get("dis") or 0) > 0]
    # ★ 体系文档 §2.3 第 7 条：剔除末圈被截断的循环。live（进行中）的最后一圈
    #   必然是被截断的 —— 不剔除的话，"最新比容量骤降"的假象会进报告
    #   （实测 264 末圈 32 mAh/g，剔除后恢复正常水平）。
    trend = done[:-1] if kind == "live" and len(done) >= 2 else done
    effective = [c["cycle"] for c in trend]

    # ---- 衰减速率：放电比容量 vs 圈号的最小二乘斜率（每百圈）----
    # 跳变兜底：某圈容量突然涨三成以上就不给速率。
    # 以前这是**主力**规避 —— 明细层一个 cycleid 里塞两对充放时，按 cycleid 累加
    # 会让某圈容量凭空翻倍，线性拟合就给出 +12523 这种荒唐速率（实测）。
    # 那个根因 2026-10-04 已在 `figures.cycles_of_detail` 修掉（圈号按工步序列重算），
    # 但这条兜底留着：真出现容量跳变（活化、重新装夹、换工步）时同样不该给速率。
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
            ce_anomaly = {"from_cycle": bad[0], "to_cycle": bad[-1],
                          "max": round(max(ces), 2)}
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
    ce_eff = [c["ce"] for c in cs
              if c.get("ce") is not None and c["cycle"] in set(effective)]
    ee_vals = [(c["cycle"], c["ee"]) for c in cs
               if c.get("ee") is not None and c["cycle"] in set(effective)]

    # ---- 方向词（3.4 / 3.5 节用）：模型只念方向，不自己比较首末值 ----
    def _trend(first, last, tol):
        if first is None or last is None:
            return None
        d = last - first
        return "上升" if d > tol else ("下降" if d < -tol else "基本持平")

    avg_v_trend = _trend(cell_first(dis_v), cell_last(dis_v), 0.0005)      # 单位 V
    dv_eff = [dvs[i] for i, c in enumerate(cs)
              if i < len(dvs) and dvs[i] is not None and c["cycle"] in set(effective)]
    if len(dv_eff) >= 2 and dv_eff[-1] - dv_eff[0] > 1.0:
        dv_trend = "增大"
    elif len(dv_eff) >= 2 and dv_eff[0] - dv_eff[-1] > 1.0:
        dv_trend = "减小"
    else:
        dv_trend = "基本持平" if dv_eff else None

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

    # 峰间距（充电峰位 − 放电峰位，mV）逐圈 + 方向。原来要模型自己相减，还得照着
    # 提示词里那句写死的"由首圈 X 增大至第 Y 圈以后的 Z"——数据变小时就写成反的。
    peak_gaps = {}
    for cyc, pk in sorted(peaks_multi.items()):
        c_v = (pk.get("chg") or {}).get("voltage_V")
        d_v = (pk.get("dis") or {}).get("voltage_V")
        if c_v is not None and d_v is not None:
            peak_gaps[cyc] = round((c_v - d_v) * 1000.0, 1)
    gap_keys = sorted(peak_gaps)
    peak_gap_trend = None
    if len(gap_keys) >= 2:
        delta = peak_gaps[gap_keys[-1]] - peak_gaps[gap_keys[0]]
        peak_gap_trend = "增大" if delta > 2 else ("减小" if delta < -2 else "基本不变")


    # ---- 数据截止时间（live 的 progress.json；正式数据集用采集时间）----
    prog = dd / "progress.json"
    data_cutoff = None
    if prog.exists():
        try:
            data_cutoff = json.loads(prog.read_text(encoding="utf-8-sig")).get("updated_at")
        except (json.JSONDecodeError, OSError):
            pass
    if not data_cutoff:
        # 正式数据集没有 progress.json：改读 meta.json 的采集时间，
        # 让报告横幅能写"采集于 2026-09-25T13:52"，而不是"测试 247 的数据"这种糊话
        meta = dd / "meta.json"
        if meta.exists():
            try:
                data_cutoff = json.loads(
                    meta.read_text(encoding="utf-8-sig")).get("collected_at")
            except (json.JSONDecodeError, OSError):
                data_cutoff = None
    if not data_cutoff:
        data_cutoff = f"测试 {cands[0][2]}（采集时间未记录）" if cands else None

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
        "dv_last": (round(cell_last(dvs), 2) if cell_last(dvs) is not None else None),
        "dv_trend": dv_trend,
        "avg_v_trend": avg_v_trend,
        "peak_gaps_mv": peak_gaps,
        "peak_gap_trend": peak_gap_trend,
        "peak_gap_first_mv": peak_gaps[gap_keys[0]] if gap_keys else None,
        "peak_gap_last_mv": peak_gaps[gap_keys[-1]] if gap_keys else None,
        "dis_v_series": dis_v,
        "dis_v_first": (round(cell_first(dis_v), 4) if cell_first(dis_v) is not None else None),
        "dis_v_last": (round(cell_last(dis_v), 4) if cell_last(dis_v) is not None else None),
        # 与容量衰减速率同一条护栏：逐圈电压序列里有"圈号合并"造成的跳变时，
        # 拟合出来的速率没有意义（实测算出 -2034 mV·百圈⁻¹），一律不给出
        "avgv_slope_mv_100c": (avgv_slope if jump is None else None),
        "ee_vs_ce": _ee_vs_ce(ee_vals, ce_eff),
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
    with detail_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # ★ 圈号按工步序列重算，**不按 cycleid**（一个 cycleid 里可能塞两对充放，
    #   按它筛会把两圈的放电行一起算进来 → 份额算到另一圈的数据上）。
    idx = F.cycle_index_of(rows)
    if idx is None:                     # 切不出合格块 → 退回 cycleid 口径
        idx = []
        for r in rows:
            try:
                idx.append(int(r.get("cycleid")) + 1)
            except (TypeError, ValueError):
                idx.append(-1)

    total = below = 0.0
    prev: dict[str, float] = {}
    for r, cyc in zip(rows, idx):
        if cyc != cycle_no or r.get("steptype") not in ("dc", "dv"):
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


def _ee_vs_ce(ee_vals: list, ce_eff: list) -> str | None:
    """能量效率与库仑效率谁下降更快（3.4 节用，模型不再自己比）。

    为什么值得算：EE 反映的是能量损耗，CE 反映的是容量损耗；"EE 比 CE 下降更快"
    说明极化对能量损耗的影响比对容量损耗更早显现 —— 这是参考报告里的一个结论句，
    以前要靠模型自己相减比较，现在由程序给。
    """
    if len(ee_vals) < 2 or len(ce_eff) < 2:
        return None
    ee_drop = ee_vals[0][1] - ee_vals[-1][1]
    ce_drop = ce_eff[0] - ce_eff[-1]
    if ee_drop > ce_drop + 1.0:
        return "EE 下降更快"
    if ce_drop > ee_drop + 1.0:
        return "CE 下降更快"
    return "两者相当"


def cell_first(vals):
    return next((v for v in vals if v is not None), None)


def cell_last(vals):
    return next((v for v in reversed(vals) if v is not None), None)


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
    # ★ 首效口径：**充电 ÷ 放电**（负极半电池 Na‖HC，每圈先放电后充电）——
    #   协会《提示词体系》L1 的"防错关键"。原来写成 放电÷充电，得 111.79%（>100%），
    #   正确值应与参考报告同量级（89.95 / 90.83%）。
    ce = F.ce_pct(q_chg, q_dis)
    if not mass_g:
        return {"charge_sp": None, "discharge_sp": None,
                "ce": round(ce, 2) if ce is not None else None,
                "irreversible_sp": None}
    # 不可逆容量按协会术语表定义取**绝对值**（首圈放电与充电比容量之差）
    irr = abs(q_dis - q_chg) / mass_g if (q_chg and q_dis) else None
    return {"charge_sp": round(q_chg / mass_g, 2) if q_chg else None,
            "discharge_sp": round(q_dis / mass_g, 2) if q_dis else None,
            "ce": round(ce, 2) if ce is not None else None,
            "irreversible_sp": round(irr, 2) if irr is not None else None}


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


# ★ 2026-10-10 修改清单 B1：电压窗口改用**设定窗口**。
#   以前这里放的是从明细层现算的"实测范围"（0.01~2.14 V，还带着"待实验室确认"）——
#   实测最高会比设定上限高一点（极化/过充测量），把实测当设定写进 2.2 是口径错误。
#   设定窗口由用户给定（2026-10-10 批注：0.01~2.0 V）。实测范围仍在 3.x 各节出现，
#   那里写的是"实测"，两不混淆。
VOLTAGE_WINDOW_SET = "0.01~2.0 V（设定窗口）"


# 协会《钠离子电池电解液添加剂 Q④ 性能测试分析报告·第二版》里的体系与工步口径。
# 只在与该报告同一体系（STD vs 1% Q④）时填入报告 —— 见 _is_std_q_pair。
BASELINE_CYCLE = 4          # 保持率基线：第 4 圈（化成后首个 1C 循环）

# ★ 2026-10-09：**本批实验的变量** —— 由用户给定，不再靠标签去猜。
#   实验样 = 电解液里额外加了质量分数 1% 的 PBSF；对比样 = 未加（基线）。
#   它决定表 1 的"变量"栏与 4.2 机制段（机制知识库按 variable.type 注入）。
DEFAULT_VARIABLE = "电解液添加剂（PBSF，1 wt%）"
DEFAULT_DOSAGE = "1 wt%"

# ★ 图号 ↔ 用哪一对通道 —— **唯一出处**：出图（report_build）与渲染（render.py）都查它。
#   图 6 已取消（用户 2026-10-09 决定）：报告共 5 张图。
#   对① = 长循环（10-1 实验样 vs 10-2 对比样）；对② = 倍率（10-3 实验样 vs 10-4 对比样）。
FIGURE_PAIRS = {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}


def figure_map(labels: dict[str, str], chs: list[str]) -> dict:
    """把"哪张图用哪一对"落成契约字段。**不要在图里写死通道号** —— 改分配只改这里。"""
    def vs(keys):
        return "_vs_".join(labels.get(k, k) for k in keys)
    p1, p2 = list(chs)[:2], list(chs)[2:4]
    return {"pair1": vs(p1), "pair2": vs(p2) if p2 else vs(p1),
            "map": dict(FIGURE_PAIRS),
            "ch1": p1, "ch2": p2}
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
# ★ 2026-10-10 修改清单 B1/B2：本批实验的**设定条件**由用户给定，不再挂在
#   "标签像不像 STD/Q④"上 —— 以前 std_q 不成立时这里全给 None，报告 2.2/2.3
#   一片"（未提供）"。温度 28 ℃ 是用户批注给的（协会报告的 25±1 ℃ 是**它们那批**
#   的条件，不能抄）；湿度用户没给，按 A2 留占位。
#   ⚠ formation / rate_program / long_cycle_rate 的值**不带括号** —— 它们在 2.3
#   正文里会被套进"（…）"里用，值里再带括号就成"（（…））"（实测出过）。
TEST_COND = {
    "temperature": "28 ℃",
    "humidity": None,            # A2：待实验室提供（协会口径 <10% RH，给了再填）
    "nominal_specific_capacity": "300 mAh·g⁻¹（硬碳，1C = 300 mA·g⁻¹）",
    "formation": "0.1C × 3 圈",
    "long_cycle_rate": "1C 恒流充放电，自第 4 圈起",
    "rate_program": "0.2C→5C→0.2C，每档 6 圈、共 13 档 78 圈",
}


def build_global_context(labels: dict[str, str], cells: dict[str, dict],
                         mass_mg: float, generated: str, variable_type: str | None = None, dosage: str | None = None) -> dict:
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
        "figures": figure_map(labels, chs),
        "variable": {
            # 由 --variable 传进来（默认本批的 PBSF）。**不再靠标签猜** ——
            # 以前是写死的 if std_q，换个实验就变成"数据不足"，而且从结果上看不出来。
            "type": (variable_type or DEFAULT_VARIABLE) if (variable_type or not std_q)
                    else "电解液添加剂（含氟分子 Q④，1 wt%）",
            "control_short": labels[chs[0]] if len(chs) > 1 else chs[0],
            "control_name": f"通道 {chs[0]}（{labels[chs[0]]}）" if len(chs) > 1 else chs[0],
            "experimental_short": labels[chs[1]] if len(chs) > 1 else chs[0],
            "experimental_name": f"通道 {chs[1]}（{labels[chs[1]]}）" if len(chs) > 1 else chs[0],
            "experimental_dosage": dosage or DEFAULT_DOSAGE,
            # 对②（倍率那一对）的名字；只有两颗时留空
            "pair2_control_short": labels.get(chs[2], "") if len(chs) > 2 else "",
            "pair2_experimental_short": labels.get(chs[3], "") if len(chs) > 3 else "",
        },
        "test_conditions": {
            # ★ 2026-10-10 B1/B2：设定条件**无条件**写入（不再是 std_q 时候才给）。
            "temperature": TEST_COND["temperature"],
            "humidity": TEST_COND["humidity"],
            "voltage_window": VOLTAGE_WINDOW_SET,
            "nominal_specific_capacity": TEST_COND["nominal_specific_capacity"],
            "formation": TEST_COND["formation"],
            "long_cycle_rate": TEST_COND["long_cycle_rate"],
            "rate_program": TEST_COND["rate_program"],
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



# 判定一行 notes 是"可以引用的口径说明"还是"给模型的约束"（后者绝不能进正文）。
# 实测的坑：S31C 把 notes 里那句"效率数值由程序给出、模型只引用，不得据此判断电池好坏"
# 原样抄进了报告正文 —— 所以按措辞机械拆分，可审计、不会再犯。
_PROHIBIT = ("不得", "禁止", "不要", "必须", "只准", "一律记", "一个字都")



# ---- P1b 图/表编号映射（**编号的唯一出处**）----------------------------------
# 以前 12 处"末句写……见图 N/见表 N"写死在提示词里，改一次图序就要重跑十几段正文。
# 现在编号只在这里出现，提示词让模型去读数据包的 figure_ref / table_ref。
BLOCK_REFS = {
    # ★ 2026-10-10 修改清单 B4：原表 3（核心测试工步与数据处理口径）整表删除
    #   （与 2.2/2.3 正文重复），表号**顺延补齐** —— 报告里不留缺口，
    #   后面的表全部前移一位（原表 4→表 3 … 原表 10→表 9）。
    "S31A": ("图 1", None), "S31B": (None, "表 3"), "S31C": (None, None),
    "S33A": ("图 2", None), "S33B": (None, "表 4"),
    "S32A": ("图 3", None), "S32B": (None, "表 5"),
    "S32C": ("图 4", None), "S32D": (None, "表 6"),
    "S34A": ("图 5", None), "S34B": (None, "表 7"), "S34C": (None, None),
    "S35A": (None, "表 8"), "S35B": (None, "表 9"), "S35C": (None, None),
}



# 4.3 建议 与 5.2 展望的条目（引自协会《…第二版》对应章节，属项目既有结论，不是我们编的）
SUGGESTIONS = {
    "formulation_validation": ["每条件制备并测试不少于 3 颗平行样，取均值并给出误差棒",
                               "系统探究添加剂的最佳添加浓度（0.5%、1%、2%、3% 梯度）",
                               "探索与其它成膜添加剂的复配效果"],
    "characterization": ["补做 EIS 电化学阻抗谱，量化界面膜阻抗与电荷转移阻抗",
                         "开展 XPS、FTIR 等表面表征，明确 SEI 膜化学组成与结构",
                         "用 DCIR 与变温测试拆分欧姆极化、电化学极化与浓差极化"],
    "application": ["从半电池拓展到全电池（硬碳负极＋正极材料）",
                    "测试 −20 ℃~60 ℃ 的温度适应性"],
}
OUTLOOK = {
    "system_iteration": ["添加剂浓度梯度优化", "复配添加剂体系开发", "高电压正极适配性研究",
                         "宽温域电解液配方开发", "低成本钠盐体系探索"],
    "industrialization": ["大容量软包电池验证", "储能场景工况适配", "低温性能评估",
                          "安全性能测试（热箱/穿刺）", "量产工艺兼容性评估"],
    "deep_research": ["SEI 膜原位表征研究", "界面反应动力学模拟", "失效机制系统分析",
                      "固态钠电池方向探索", "多尺度理论计算支撑"],
}
NEXT_STEPS = "平行样复测确认添加剂效应、EIS 与 XPS 机理表征、全电池初步验证三者同步推进"


def split_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """→ (notes 可引用, instructions 不可引用)。"""
    keep, instr = [], []
    for n in notes:
        (instr if any(k in n for k in _PROHIBIT) else keep).append(n)
    return keep, instr



# ---------------------------------------------------------------------------
# 机制知识库（《提示词体系》第 4 章）—— S42 段唯一的取材白名单
# ---------------------------------------------------------------------------
# 4.2 是全篇唯一允许引入"输入数据之外知识"的块，所以只能用白名单条目，
# 且每条必须绑定一个**本报告实际存在的**实验现象（matched_phenomena 由程序回填，
# 没这个现象就不匹配 —— 不允许 Agent 自己找现象、更不允许自造机制）。
MECHANISM_KB = [
    {"type": "含氟", "title": "优先成膜，构建稳定 SEI",
     "content": "含氟分子可能具有较低的还原电位，在首圈优先于电解液溶剂在负极表面分解，"
                "参与形成富含无机氟化物（如 NaF）的 SEI 膜；该膜致密均匀，"
                "能有效隔绝电解液与电极的持续接触",
     "phenomena": ["首效略高", "不可逆容量略低"]},
    {"type": "通用", "title": "高离子传导，降低界面阻抗",
     "content": "分解产物中的组分可能具备良好的离子传导能力，使 SEI 膜兼具"
                "「电子绝缘」与「离子导通」特性",
     "phenomena": ["ΔV 均值更低", "高倍率段容量更高"]},
    {"type": "通用", "title": "抑制副反应，减少活性物质损耗",
     "content": "稳定的 SEI 膜可能抑制了电解液在负极表面的持续分解，"
                "减少副反应产物的累积",
     "phenomena": ["稳态 CE 贴近 100%", "容量衰减缓慢"]},
    {"type": "通用", "title": "界面稳定性保障长循环",
     "content": "SEI 膜机械强度高、化学稳定性好，在反复的钠嵌入/脱出过程中"
                "不易破裂重组",
     "phenomena": ["长循环有效圈数更多", "保持率曲线平滑无拐点"]},
]


def _phenomena(cells: dict, labels: dict) -> dict[str, bool]:
    """本报告实际存在的实验现象（matched_phenomena 只从这里取真值为 True 的）。

    现象必须**由数据判定**：模型在这一步最容易顺手写"ΔV 更低"这类参考报告里的结论，
    而本批数据未必成立（实测 1%Q④ 的 ΔV 反而更高）。
    """
    chs = sorted(cells)
    a, b = (chs + [None, None])[:2]
    ca, cb = cells.get(a, {}), cells.get(b, {})
    fen: dict[str, bool] = {}

    def _cmp(x, y):
        return None if x is None or y is None else x - y

    d_ce = _cmp(cb.get("first", {}).get("ce"), ca.get("first", {}).get("ce"))
    fen["首效略高"] = bool(d_ce is not None and d_ce > 0)
    d_irr = _cmp(cb.get("first", {}).get("irreversible_sp"),
                 ca.get("first", {}).get("irreversible_sp"))
    fen["不可逆容量略低"] = bool(d_irr is not None and d_irr < 0)
    d_dv = _cmp(cb.get("dv_mean"), ca.get("dv_mean"))
    fen["ΔV 均值更低"] = bool(d_dv is not None and d_dv < 0)
    fen["高倍率段容量更高"] = False          # 需要倍率段数据，本批没有
    ce_ok = all(isinstance(c.get("ce_steady_mean"), (int, float))
                and abs(c["ce_steady_mean"] - 100.0) <= 2.0 for c in (ca, cb))
    fen["稳态 CE 贴近 100%"] = bool(ce_ok and ca.get("ce_steady_mean") is not None)
    fen["容量衰减缓慢"] = bool(ca.get("fade_rate_linear_fit") is not None)
    reached = max((c.get("cycle_range") or [0, 0])[1] for c in (ca, cb)) or 0
    fen["长循环有效圈数更多"] = reached >= 50    # 圈数太少时谈不上"更多"
    fen["保持率曲线平滑无拐点"] = reached >= 50
    return fen


def mechanism_for(variable_type: str | None, cells: dict,
                  labels: dict) -> list[dict]:
    """按变量类型检索知识库并回填现象（程序侧完成，Agent 只引用）。"""
    if not variable_type:
        return []
    fen = _phenomena(cells, labels)
    out = []
    for e in MECHANISM_KB:
        if e["type"] != "通用" and e["type"] not in variable_type:
            continue
        hit = [p for p in e["phenomena"] if fen.get(p)]
        out.append({"title": e["title"], "content": e["content"],
                    "matched_phenomena": hit,
                    "no_phenomenon_note": (None if hit else
                                           "该条机制在本报告数据中暂无直接对应现象")})
    return out


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
        base["rate"] = TEST_COND["long_cycle_rate"]   # 设定条件 2026-10-10 起无条件提供（B1/B2）
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
        if len(base["groups"]) == 2:
            g1, g2 = list(base["groups"].values())
            if g1.get("ce") is not None and g2.get("ce") is not None:
                base["ce_diff_pp"] = round(g2["ce"] - g1["ce"], 2)
            if (g1.get("discharge_sp") is not None
                    and g2.get("discharge_sp") is not None):
                base["discharge_sp_diff"] = round(
                    g2["discharge_sp"] - g1["discharge_sp"], 2)
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
        base["notes"].append("首效对充放电截止电压与化成协议高度敏感")
        base["notes"].append("不得据此判定添加剂改善了首效")
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
                    "avg_v_trend": cell.get("avg_v_trend"),
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
                    "dV_trend": cell.get("dv_trend"),
                }
                base["groups"][labels[key]]["EE_vs_CE"] = cell.get("ee_vs_ce")
        if len(base["groups"]) == 2:
            vs = list(base["groups"].values())
            if vs[0]["dV_mean_mv"] is not None and vs[1]["dV_mean_mv"] is not None:
                base["dV_diff_mv"] = round(vs[0]["dV_mean_mv"] - vs[1]["dV_mean_mv"], 1)
            # 平台容量占比的差值也由程序给（原来模型自己减出 3.59 个百分点）
            ps = [v.get("platform_share_last_pct") for v in vs]
            if all(x is not None for x in ps):
                base["platform_share_diff_pct"] = round(ps[1] - ps[0], 2)
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
                    "peak_gap_mv_by_cycle": cell.get("peak_gaps_mv") or {},
                    "peak_gap_first_mv": cell.get("peak_gap_first_mv"),
                    "peak_gap_last_mv": cell.get("peak_gap_last_mv"),
                    "peak_gap_trend": cell.get("peak_gap_trend"),
                }
        drawn_txt = "、".join(f"第 {c} 圈" for c in (ca.get("dqdv_cycles_drawn") or []))
        miss = ca.get("dqdv_cycles_missing") or []
        # 可引用的口径事实与"不许外推"的约束分两行写 —— 前者能进正文，后者不能（P1a）
        base["notes"].append(f"dQ/dV 逐圈图已画圈：{drawn_txt or '无'}")
        if miss:
            base["notes"].append("报告要求的第 " + "、".join(str(c) for c in miss)
                                 + " 圈数据尚未到达（测试进行中）")
            base["notes"].append("未到达的圈不得外推")
        base["notes"].append("图 5/6 是每个样品一张、同圈次氧化与还原同图的多圈叠加图")
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
    elif block_id.startswith("S42"):
        base["mechanism_kb"] = mechanism_for(ctx["variable"].get("type"), cells, labels)
        base["phenomena_available"] = {k: v for k, v in _phenomena(cells, labels).items()}
        if not base["mechanism_kb"]:
            base["notes"].append("实验变量未提供 → 机制段无法取材，按降级标记输出")
    elif block_id == "S43A":
        base["suggestions"] = {"formulation_validation": SUGGESTIONS["formulation_validation"]}
    elif block_id == "S43B":
        base["suggestions"] = {"characterization": SUGGESTIONS["characterization"]}
    elif block_id == "S43C":
        base["suggestions"] = {"application": SUGGESTIONS["application"]}
    elif block_id == "S43D":
        base["next_steps"] = NEXT_STEPS
    elif block_id == "S34C":
        base["cycles_used"] = (ca.get("dqdv_cycles_drawn") or [])
        base["cycles_missing"] = (ca.get("dqdv_cycles_missing") or [])
        base["notes"].append("微分容量分析只取数据包给出的圈次，未做全循环连续追踪")
    elif block_id == "S52A":
        base["outlook"] = {"system_iteration": OUTLOOK["system_iteration"]}
    elif block_id == "S52B":
        base["outlook"] = {"industrialization": OUTLOOK["industrialization"]}
    elif block_id == "S52C":
        base["outlook"] = {"deep_research": OUTLOOK["deep_research"]}
    elif block_id == "S52D":
        base["closing"] = "Q④ 添加剂体系在醚类电解液中具备进一步研究的价值"

    # ---- P1b：图号/表号由**数据包**给出（不写死在提示词里）----
    base["figure_ref"], base["table_ref"] = BLOCK_REFS.get(block_id, (None, None))

    # ---- P1a：分离"可引用口径"与"给模型的约束"（后者绝不能进正文）----
    base["notes"], base["instructions"] = split_notes(base.get("notes") or [])
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


def _dim_row(name: str, va, vb, la: str, lb: str, unit: str,
             bigger: str = "略高", ndigits: int = 2) -> dict:
    """一个对比维度：有数 → 方向+幅度；缺数 → 保留行、标（数据未到）（B7：不许砍维度）。"""
    if va is None or vb is None:
        return {"name": name, "direction": "—", "magnitude": "（数据未到）"}
    d = round(va - vb, ndigits)
    if d == 0:
        return {"name": name, "direction": "基本一致", "magnitude": f"0 {unit}"}
    return {"name": name, "direction": f"{la if d > 0 else lb} {bigger}",
            "magnitude": f"{abs(d)} {unit}"}


def _retention_last(cell: dict):
    rel = (cell or {}).get("retention_rel_base") or {}
    return rel.get(max(rel)) if rel else None


def dimension_summary(cells: dict[str, dict], labels: dict[str, str],
                      chs: list[str]) -> list[dict]:
    """台账：每个维度的方向与幅度（S35/S41/S51 的输入 + 报告表 9）。

    ★ 2026-10-10 修改清单 B7：扩成**全维度** —— 数据没到的维度**保留行**、
    标「（数据未到）」，不许砍（协会报告的表 9 就是全维度对齐）。
    ⚠ 两个"速率"维度（容量衰减速率 / 平均放电电压衰减速率）目前都是在
    小电流活化段上拟合的，数值无意义 —— 与 A3 同一结论，先按"暂不统计"呈现，
    等 1C 段开始后由 A3 的拍板方案统一接上。
    ⚠ 维度名会被 compose._verdict_block 按关键词（"容量"/"库仑"/"极化"）匹配：
      · "首次库仑效率"保持"库仑"拼写不动（拼成"库伦"会静默失配，实测踩过）；
      · 新增的"稳态平均库伦效率"用的是"库伦"，恰好不撞"库仑"那个键。
    """
    a, b = (chs + [None, None])[:2]
    ca, cb = cells.get(a, {}), cells.get(b, {})
    la, lb = labels.get(a, a), labels.get(b, b)
    fa, fb = ca.get("first") or {}, cb.get("first") or {}
    out = [
        _dim_row("放电比容量（最新）", ca.get("dis_sp_last"), cb.get("dis_sp_last"),
                 la, lb, "mAh·g⁻¹"),
        _dim_row("首圈放电比容量", fa.get("discharge_sp"), fb.get("discharge_sp"),
                 la, lb, "mAh·g⁻¹"),
        _dim_row("首圈不可逆容量", fa.get("irreversible_sp"), fb.get("irreversible_sp"),
                 la, lb, "mAh·g⁻¹", bigger="略大"),
        _dim_row("首次库仑效率", fa.get("ce"), fb.get("ce"), la, lb, "pp"),
        _dim_row("容量保持率（基线第 4 圈→最新）", _retention_last(ca), _retention_last(cb),
                 la, lb, "pp"),
        # 两个"速率"维度：活化段拟合无意义（同 A3），先不统计
        {"name": "容量衰减速率", "direction": "—",
         "magnitude": "（1C 长循环段未开始，暂不统计）"},
        _dim_row("稳态平均库伦效率", ca.get("ce_steady_mean"), cb.get("ce_steady_mean"),
                 la, lb, "pp"),
        _dim_row("极化 ΔV", ca.get("dv_mean"), cb.get("dv_mean"),
                 la, lb, "mV", bigger="略大", ndigits=1),
        {"name": "平均放电电压衰减速率", "direction": "—",
         "magnitude": "（1C 长循环段未开始，暂不统计）"},
        {"name": "倍率相关保持率", "direction": "—", "magnitude": "（数据未到）"},
    ]
    # dQ/dV 峰特征：主峰位置有数（第 1 圈），峰宽/峰高/面积占比待更多圈次
    pa = peaks(ca["dqdv"]) if ca.get("dqdv") else {}
    pb = peaks(cb["dqdv"]) if cb.get("dqdv") else {}
    va = (pa.get("dis") or {}).get("voltage_V")
    vb = (pb.get("dis") or {}).get("voltage_V")
    if va is not None and vb is not None:
        ddis = round((va - vb) * 1000)
        vca = (pa.get("chg") or {}).get("voltage_V")
        vcb = (pb.get("chg") or {}).get("voltage_V")
        dchg = round((vca - vcb) * 1000) if (vca is not None and vcb is not None) else None
        mag = f"放电主峰差 {abs(ddis)} mV"
        if dchg is not None:
            mag += f"、充电主峰差 {abs(dchg)} mV"
        out.append({"name": "dQ/dV 主峰位置（充电/放电）",
                    "direction": "基本一致" if ddis == 0 else f"{la if ddis > 0 else lb} 略高",
                    "magnitude": mag})
    else:
        out.append({"name": "dQ/dV 主峰位置（充电/放电）",
                    "direction": "—", "magnitude": "（数据未到）"})
    out.append({"name": "dQ/dV 峰宽/峰高/面积占比",
                "direction": "—", "magnitude": "（数据未到）"})
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
