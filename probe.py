#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neware 通讯一键体检 + SOH 数据可行性预览。

按协议顺序逐步验证 8 个环节，每步给出明确的 PASS / FAIL / WARN 与证据，
最后用 downloadStepLayer 的工步层数据直接算一遍 SOH 指标，验证
"数据能不能支撑 SOH 报告"这件事。

用法
----
    # 本机管道（需管理员权限运行，且 BTS Client 也以管理员启动）
    python probe.py --transport pipe

    # 跨机 TCP
    python probe.py --transport tcp --host 192.168.1.20

    # 指定通道 / 指定历史测试
    python probe.py --transport tcp --host 192.168.1.20 \\
        --channel 25-41-1-1 --testid 11

输出：控制台逐项结果 + probe_report.json（完整证据与原始片段）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
import unicodedata
from datetime import datetime
from typing import Any, Sequence

from neware_client import (
    CHARGING_STEPTYPES,
    DEVTYPE_NAMES,
    DISCHARGING_STEPTYPES,
    STEPTYPE_NAMES,
    Channel,
    NewareClient,
    NewareError,
    PIPE_CLIENT,
    build_transport,
    parse_channel,
)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


class Report:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}
        self.started = datetime.now()

    def add(self, index: int, total: int, name: str, verdict: str,
            detail: str = "", evidence: Any = None) -> None:
        self.steps.append({
            "step": name, "verdict": verdict,
            "detail": detail, "evidence": evidence,
        })
        print(f"[{index}/{total}] {_pad(name, 22)} {verdict}  {detail}")

    def summary(self) -> str:
        bad = [s for s in self.steps if s["verdict"] == FAIL]
        warn = [s for s in self.steps if s["verdict"] == WARN]
        ok = [s for s in self.steps if s["verdict"] == PASS]
        parts = [f"通过 {len(ok)} 项"]
        if warn:
            parts.append(f"警告 {len(warn)} 项")
        if bad:
            parts.append(f"失败 {len(bad)} 项")
        return "，".join(parts)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 单位与结构校验
# --------------------------------------------------------------------------

def _parse_atime(value: Any) -> datetime | None:
    """解析 atime。协议文档里出现过点和冒号两种分隔，都兜住。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H.%M.%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def check_time_unit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """判定 testtime 的单位（毫秒还是秒）。

    ★ 这里很容易搞错：testtime 是"**当前工步**已运行时间"，**每个工步都会归零**。
    所以不能拿整段数据的第一条和最后一条比 —— 跨了几十个工步之后，
    那样算出来的比值没有任何意义（实测在跨 55 圈的数据上会给出 66 这种废数字）。

    正确做法：按 (cycleid, stepid) 分组，在**同一个工步内部**算比值，再取中位数。
    如果每工步只有一条记录（样本太稀），退回全样本比较并明确标注是退路。
    """
    groups: dict[tuple, list[tuple[datetime, float]]] = {}
    for row in rows:
        stamp = _parse_atime(row.get("atime"))
        tt = _as_float(row.get("testtime"))
        if stamp is None or tt is None:
            continue
        groups.setdefault((row.get("cycleid"), row.get("stepid")), []).append((stamp, tt))

    ratios: list[float] = []
    for samples in groups.values():
        if len(samples) < 2:
            continue
        samples.sort(key=lambda x: x[0])
        span = (samples[-1][0] - samples[0][0]).total_seconds()
        if span > 0:
            ratios.append((samples[-1][1] - samples[0][1]) / span)

    scope = "同工步内"
    if not ratios:
        scope = "全样本（退路：每工步样本不足）"
        flat = sorted((s for grp in groups.values() for s in grp), key=lambda x: x[0])
        if len(flat) < 2:
            return {"ratio": None, "unit": "unknown", "scope": scope,
                    "note": "有效时间样本不足，无法判定"}
        span = (flat[-1][0] - flat[0][0]).total_seconds()
        if span <= 0:
            return {"ratio": None, "unit": "unknown", "scope": scope,
                    "note": "绝对时间跨度为零，无法判定"}
        ratios = [(flat[-1][1] - flat[0][1]) / span]

    ratios.sort()
    ratio = ratios[len(ratios) // 2]
    result: dict[str, Any] = {"ratio": round(ratio, 3), "scope": scope,
                              "samples": len(ratios)}
    if 300 <= ratio <= 3000:
        result["unit"] = "ms"
        result["note"] = (f"{scope}比值中位数 ≈ {ratio:.0f}（{len(ratios)} 个样本）"
                          f" → 单位为毫秒，与协议一致")
    elif 0.3 <= ratio <= 3:
        result["unit"] = "s"
        result["note"] = (f"{scope}比值中位数 ≈ {ratio:.2f}"
                          f" → testtime 疑似以秒为单位，与协议描述不符，需核对")
    else:
        result["unit"] = "unknown"
        result["note"] = (f"{scope}比值中位数 ≈ {ratio:.2f}，无法判定，需人工核对")
    return result


def check_monotonic(rows: list[dict[str, Any]], key: str) -> bool | None:
    values = [_as_int(r.get(key), -1) for r in rows if r.get(key) is not None]
    values = [v for v in values if v >= 0]
    if len(values) < 2:
        return None
    return all(b >= a for a, b in zip(values, values[1:]))


def check_stepid_within_cycle(rows: list[dict[str, Any]]) -> bool | None:
    """stepid 是**循环内**的工步号，每个循环都会归零。

    所以全局看它一定不是单调递增的 —— 那是正常现象，不是数据错误。
    只能在同一个循环内部检查它是否递增。
    """
    by_cycle: dict[int, list[int]] = {}
    for row in rows:
        cycle = _as_int(row.get("cycleid"), -1)
        step = _as_int(row.get("stepid"), -1)
        if cycle < 0 or step < 0:
            continue
        by_cycle.setdefault(cycle, []).append(step)
    if not by_cycle:
        return None
    for values in by_cycle.values():
        if any(b < a for a, b in zip(values, values[1:])):
            return False
    return True


def aux_columns(rows: list[dict[str, Any]]) -> list[str]:
    """找出辅助通道列名（V1/T1/Thk1/N14 之类），随硬件配置变化。"""
    known = {"seqid", "stepid", "cycleid", "steptype", "testtime", "atime",
             "volt", "curr", "cap", "eng", "dbc", "startseqid", "endseqid",
             "stepindex", "steptime", "endatime", "startvolt", "endvolt",
             "startcurr", "endcurr", "dcir", "log_code"}
    names: set[str] = set()
    for row in rows[:50]:
        for key in row:
            if key not in known:
                names.add(key)
    return sorted(names)


# --------------------------------------------------------------------------
# SOH 预览（完全基于工步层数据）
# --------------------------------------------------------------------------

# 逐圈曲线在 manifest 里保留多少圈。
# 定 300 是因为：容量跳水点经常出现在第 100 圈之后，只留 20 圈看不出趋势；
# 而 300 圈 × 每条几十字节，也不会把 manifest 撑到读不动。
CURVE_CAP = 300


def _sig(v: float, digits: int = 6) -> float:
    """按**有效数字**取整，而不是按小数位数。

    为什么不能用 `round(v, 6)`：容量可能是 1e-7 这种量级，
    `round` 到小数点后 6 位会把 4.89e-07 变成 **0**、把 5.05e-07 变成 **1e-06**
    —— 既丢数据又歪曲数值（最多差一倍），而且**从数字上看不出来**
    （实测在一份真实数据上，逐圈放电容量整列都被抹成 0）。

    按有效数字取整则与量级无关，不会出这种问题。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, digits - 1 - math.floor(math.log10(abs(x))))


def soh_preview(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """用工步层数据算 SOH 关键指标。

    口径说明：工步层的 cap/eng 是"该工步内的累计值"，所以同一循环内
    各充电工步的 cap 求和 = 该循环总充电容量，放电同理。
    容量保持率相对第一圈放电容量计算。
    """
    cycles: dict[int, dict[str, float]] = {}
    dcir_by_cycle: dict[int, float] = {}

    for row in steps:
        cycle = _as_int(row.get("cycleid"), -1)
        if cycle < 0:
            continue
        steptype = (row.get("steptype") or "").strip().lower()
        cap = _as_float(row.get("cap"), 0.0) or 0.0
        eng = _as_float(row.get("eng"), 0.0) or 0.0
        bucket = cycles.setdefault(cycle, {"charge_cap": 0.0, "dis_cap": 0.0,
                                           "charge_eng": 0.0, "dis_eng": 0.0})
        if steptype in CHARGING_STEPTYPES:
            bucket["charge_cap"] += cap
            bucket["charge_eng"] += eng
        elif steptype in DISCHARGING_STEPTYPES:
            bucket["dis_cap"] += cap
            bucket["dis_eng"] += eng
            dcir = _as_float(row.get("dcir"))
            if dcir is not None and dcir > 0:
                dcir_by_cycle[cycle] = dcir

    usable = {c: v for c, v in cycles.items() if v["dis_cap"] > 0}
    if not usable:
        return {"available": False,
                "reason": "工步层数据中找不到有效的放电容量（cap），可能工步类型定义与预期不同"}

    order = sorted(usable)
    first, last = order[0], order[-1]
    first_cap = usable[first]["dis_cap"]
    last_cap = usable[last]["dis_cap"]

    def _ce(cycle: int) -> float | None:
        c = usable[cycle]
        return round(c["dis_cap"] / c["charge_cap"] * 100, 2) if c["charge_cap"] else None

    def _ee(cycle: int) -> float | None:
        c = usable[cycle]
        return round(c["dis_eng"] / c["charge_eng"] * 100, 2) if c["charge_eng"] else None

    retention_curve = [
        {"cycle": c, "dis_cap": _sig(usable[c]["dis_cap"]),
         "retention_pct": round(usable[c]["dis_cap"] / first_cap * 100, 3)}
        for c in order
    ]

    return {
        "available": True,
        "cycle_count": len(order),
        "cycle_range": [first, last],
        "first_discharge_cap_mah": _sig(first_cap),
        "last_discharge_cap_mah": _sig(last_cap),
        "capacity_retention_pct": round(last_cap / first_cap * 100, 2),
        "coulomb_efficiency_first_pct": _ce(first),
        "coulomb_efficiency_last_pct": _ce(last),
        "energy_efficiency_first_pct": _ee(first),
        "energy_efficiency_last_pct": _ee(last),
        "dcir_first_mohm": dcir_by_cycle.get(first),
        "dcir_last_mohm": dcir_by_cycle.get(last),
        "dcir_growth_pct": (
            round(dcir_by_cycle[last] / dcir_by_cycle[first] * 100, 2)
            if dcir_by_cycle.get(first) and dcir_by_cycle.get(last) else None
        ),
        # 曲线截断到 CURVE_CAP 圈。note 里必须说清"截断到多少"，
        # 否则读的人会以为下面就是完整曲线（旧版就是这样误导的）。
        "retention_curve": retention_curve[:CURVE_CAP] + (
            [{"note": f"…曲线仅列前 {CURVE_CAP} 圈，实际共 {len(retention_curve)} 圈"}]
            if len(retention_curve) > CURVE_CAP else []
        ),
    }


def print_soh(soh: dict[str, Any]) -> None:
    print("\n--- SOH 数据可行性预览（来自 downloadStepLayer）---")
    if not soh.get("available"):
        print(f"  不可用：{soh.get('reason')}")
        return
    print(f"  循环数              {soh['cycle_count']}（{soh['cycle_range'][0]} → {soh['cycle_range'][1]}）")
    print(f"  首圈放电容量        {soh['first_discharge_cap_mah']} mAh")
    print(f"  末圈放电容量        {soh['last_discharge_cap_mah']} mAh")
    print(f"  容量保持率          {soh['capacity_retention_pct']} %")
    print(f"  库仑效率 首/末      {soh['coulomb_efficiency_first_pct']} % / {soh['coulomb_efficiency_last_pct']} %")
    print(f"  能量效率 首/末      {soh['energy_efficiency_first_pct']} % / {soh['energy_efficiency_last_pct']} %")
    print(f"  DCIR 首/末 (mΩ)     {soh['dcir_first_mohm']} / {soh['dcir_last_mohm']}"
          + (f"  增长 {soh['dcir_growth_pct']} %" if soh.get("dcir_growth_pct") else ""))
    print("  结论：SOH 报告所需指标均可从协议字段算出。")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    total = 8
    report = Report()
    transport_desc = (f"pipe {args.pipe or PIPE_CLIENT}" if args.transport == "pipe"
                      else f"tcp {args.host}:{args.port}")
    print(f"=== Neware 通讯体检 ===  传输：{transport_desc}  "
          f"开始于 {report.started:%Y-%m-%d %H:%M:%S}\n")

    client: NewareClient | None = None
    try:
        # ---- 1 连接 -----------------------------------------------------
        try:
            transport = build_transport(args)
            transport.connect()
        except NewareError as exc:
            report.add(1, total, "连接", FAIL, str(exc))
            print(f"\n体检中断：通讯层不通，后续步骤无法执行。\n{exc}")
            _write_report(report, args, transport_desc)
            return 1
        client = NewareClient(transport, args.timeout)
        report.add(1, total, "连接", PASS, f"{transport_desc} 已建立")

        # ---- 2 getdevinfo ----------------------------------------------
        channels: list[Channel] = []
        try:
            info = NewareClient.parse_devinfo(client.getdevinfo())
            channels = [Channel(c["devtype"], c["devid"], c["subdevid"], c["chlid"], c["ip"])
                        for c in info["channels"]]
            report.data["devinfo"] = info
            detail = f"服务器 {len(info['servers'])} 台，通道 {len(channels)} 个"
            verdict = PASS if channels else WARN
            report.add(2, total, "getdevinfo", verdict, detail,
                       {"servers": info["servers"], "channel_count": len(channels)})
            if not channels:
                print("  getdevinfo 未返回通道，请用 --channel 显式指定通道后重跑")
                _write_report(report, args, transport_desc)
                return 1
        except NewareError as exc:
            report.add(2, total, "getdevinfo", FAIL, str(exc))
            _write_report(report, args, transport_desc)
            return 1

        target = parse_channel(args.channel) if args.channel else channels[0]
        devname = DEVTYPE_NAMES.get(target.devtype, f"devtype{target.devtype}")
        print(f"  目标通道：{target.key}@{target.ip}（{devname}）")

        # ---- 3 getchlstatus --------------------------------------------
        try:
            statuses = NewareClient.parse_status(client.getchlstatus([target]))
            if statuses:
                s = statuses[0]
                report.add(3, total, "getchlstatus", PASS,
                           f"{s['status']} / {s['status_cn']}",
                           statuses)
            else:
                report.add(3, total, "getchlstatus", WARN, "回应中没有该通道的状态")
        except NewareError as exc:
            report.add(3, total, "getchlstatus", FAIL, str(exc))

        # ---- 4 inquire --------------------------------------------------
        try:
            rt = NewareClient.parse_inquire(client.inquire([target]))
            if rt:
                r = rt[0]
                report.add(4, total, "inquire", PASS,
                           f"{r.get('voltage')}V / {r.get('current')}A / "
                           f"workstatus={r.get('workstatus')} / barcode={r.get('barcode') or '空'}",
                           rt)
            else:
                report.add(4, total, "inquire", WARN, "未返回实时数据（通道可能空闲）")
        except NewareError as exc:
            report.add(4, total, "inquire", FAIL, str(exc))

        # ---- 5 download（DF 明细）--------------------------------------
        df_rows: list[dict[str, Any]] = []
        try:
            df_rows = client.download_all(target, testid=args.testid,
                                         max_rows=args.limit or None,
                                         progress=False)
            if df_rows:
                evidence = {
                    "row_count": len(df_rows),
                    "first": df_rows[0],
                    "last": df_rows[-1],
                }
                report.add(5, total, "download", PASS,
                           f"拉取 {len(df_rows)} 条 DF 明细", evidence)
                report.data["df_sample"] = evidence
            else:
                report.add(5, total, "download", WARN,
                           "返回 0 条：该通道/测试号可能没有数据，试试 --testid 指定历史测试")
        except NewareError as exc:
            report.add(5, total, "download", FAIL, str(exc))

        # ---- 6 downloadStepLayer（工步层 + SOH 预览）---------------------
        steps: list[dict[str, Any]] = []
        try:
            steps = NewareClient.parse_data(
                client.download_steplayer(target, testid=args.testid, dcir=1))
            if steps:
                steptypes = sorted({(r.get("steptype") or "").strip()
                                    for r in steps if r.get("steptype")})
                steptype_cn = [f"{s}({STEPTYPE_NAMES.get(s, '未定义')})" for s in steptypes]
                has_dcir = any(_as_float(r.get("dcir"), 0) for r in steps)
                detail = (f"{len(steps)} 个工步，工步类型 {len(steptypes)} 种，"
                          f"DCIR {'有值' if has_dcir else '为空'}")
                report.add(6, total, "downloadStepLayer", PASS, detail,
                           {"step_count": len(steps), "steptypes": steptype_cn,
                            "first": steps[0]})
                report.data["steptypes_seen"] = steptype_cn
                unknown = [s for s in steptypes if s and s not in STEPTYPE_NAMES]
                if unknown:
                    print(f"  注意：出现协议未定义的工步类型 {unknown}，需现场核对")
            else:
                report.add(6, total, "downloadStepLayer", WARN, "返回 0 个工步")
        except NewareError as exc:
            report.add(6, total, "downloadStepLayer", FAIL, str(exc))

        # ---- 7 inquiredf（上传完整度）-----------------------------------
        try:
            checks = NewareClient.parse_inquiredf(client.inquiredf([target], args.testid))
            if checks:
                c = checks[0]
                verdict = PASS if c["complete"] else WARN
                report.data["upload_complete"] = bool(c["complete"])
                report.add(7, total, "inquiredf", verdict,
                           f"testid={c['testid']} 已上传 {c['uploaded']} 条，"
                           f"完整={'是' if c['complete'] else '否'}",
                           checks)
            else:
                report.add(7, total, "inquiredf", WARN, "未返回结果")
        except NewareError as exc:
            report.add(7, total, "inquiredf", FAIL, str(exc))

        # ---- 8 downloadlog ---------------------------------------------
        try:
            logs = NewareClient.parse_data(client.downloadlog(target, args.testid, 0))
            report.add(8, total, "downloadlog",
                       PASS if logs else WARN,
                       f"{len(logs)} 条日志"
                       + ("" if logs else "（无日志或等级过滤掉了）")
                       + "；解读需 LogCode.csv，该文件未随协议提供",
                       logs[:20])
        except NewareError as exc:
            report.add(8, total, "downloadlog", FAIL, str(exc))

        # ---- 单位与结构校验 ---------------------------------------------
        print("\n--- 单位与结构校验 ---")
        if df_rows:
            unit = check_time_unit(df_rows)
            report.data["time_unit"] = unit
            print(f"  testtime 单位判定：{unit['unit']}  {unit['note']}")
            # seqid 是全局数据序号，cycleid 是循环号，两者都应单调不减
            for key, label in (("seqid", "seqid 单调递增"),
                               ("cycleid", "cycleid 单调不减")):
                mono = check_monotonic(df_rows, key)
                if mono is not None:
                    print(f"  {label}：{'是' if mono else '否（需核对）'}")
                    report.data.setdefault("monotonic", {})[key] = mono
            # stepid 是循环内的工步号，每个循环会归零 —— 全局看必然不单调，
            # 那是正常现象。只在循环内部检查。
            within = check_stepid_within_cycle(df_rows)
            if within is not None:
                verdict = "是" if within else "否（需核对）"
                print(f"  stepid 在循环内递增：{verdict}")
                print("    （stepid 每个循环归零，所以整体看它不是单调的，这是正常的）")
                report.data.setdefault("monotonic", {})["stepid_within_cycle"] = within
            cols = aux_columns(df_rows)
            report.data["aux_columns"] = cols
            print(f"  辅助通道列名：{cols or '无'}")
            print("  （辅助通道列名随硬件配置变化，解析时需自适应，不可硬编码）")
            cycles_seen = sorted({_as_int(r.get("cycleid")) for r in df_rows})
            print(f"  cycleid 取值范围：{cycles_seen[0]} → {cycles_seen[-1]}"
                  f"（样本内 {len(cycles_seen)} 个循环号）")
        else:
            print("  无 DF 明细样本，跳过单位校验")

        # ---- SOH 预览 ---------------------------------------------------
        soh = soh_preview(steps) if steps else {"available": False,
                                                "reason": "没有工步层数据"}
        report.data["soh_preview"] = soh
        print_soh(soh)

        # ---- 数据完整性提示 ---------------------------------------------
        # 如果测试还在跑，中位机还没把数据传完，上面所有分析都是基于部分数据。
        # 这个提示必须显眼，否则容易把"数据没传完"误判成"算法有问题"。
        upload = report.data.get("upload_complete")
        if upload is False:
            print("\n" + "!" * 62)
            print("⚠️  数据尚未上传完整（inquiredf 返回 完整=否）")
            print("    说明：这个测试**可能还在进行中**，中位机的数据还没传完。")
            print("    影响：上面所有分析都基于**部分数据**，SOH 数字仅供参考。")
            print("         循环数、容量、效率都可能因为缺尾部数据而失真。")
            print("    建议：做 SOH 口径验证时，请挑一个**已经跑完**的测试；")
            print("         要拿运行中的测试做进度监控，那是另一回事。")
            print("!" * 62)

        print(f"\n=== 体检结束：{report.summary()} ===")
        path = _write_report(report, args, transport_desc)
        print(f"完整报告：{path}")
        return 0

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception:
        print("\n未预期的异常：", file=sys.stderr)
        traceback.print_exc()
        _write_report(report, args, transport_desc)
        return 2
    finally:
        if client is not None:
            client.transport.close()


def _write_report(report: Report, args: argparse.Namespace, desc: str) -> str:
    path = args.report or "probe_report.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "transport": desc,
        "channel": args.channel,
        "testid": args.testid,
        "summary": report.summary(),
        "steps": report.steps,
        "data": report.data,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Neware 通讯一键体检 + SOH 数据可行性预览")
    p.add_argument("--transport", choices=["pipe", "tcp"], default="tcp",
                   help="pipe=本机命名管道（需管理员）；tcp=跨机 502 端口")
    p.add_argument("--host", help="运行 BTS Client 的电脑 IP（tcp 方式必填）")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--pipe", help=f"管道名，默认 {PIPE_CLIENT}")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--channel", help="通道 devtype-devid-subdevid-chlid；缺省用第一个发现的通道")
    p.add_argument("--testid", type=int, default=0, help="测试号，0=当前测试")
    p.add_argument("--limit", type=int, default=5000,
                   help="DF 明细最多拉多少条（0=全量）。默认 5000，避免体检时拉爆大文件")
    p.add_argument("--report", default="probe_report.json", help="报告输出路径")
    return p


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
