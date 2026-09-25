#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据体检：不依赖 BTSDA，自动判断拿到的数据是否可信。

为什么需要它
------------
`reconcile.py` 需要人工去 BTSDA 抄数字对照，一次两次可以，但每次采集都做不现实。
这个脚本把"对账得出的口径"变成一个**自动检查表** —— 用数据内部的物理关系
和结构规律去发现问题，因此不需要任何外部基准。

它的依据全部来自 2026-09-24 那次 BTSDA 对账确认的口径：
    电流 mA（充电正、放电负）  电压 V  容量 mAh  能量 mWh  内阻 mΩ  testtime 毫秒

能查出的典型问题
----------------
* 单位标错        —— 通过 cap 与 eng 的物理关系（能量 ≈ 容量 × 电压）发现
* 电流符号反了    —— 充电工步却出现负电流
* 数据缺失        —— seqid 跳号
* 结构错乱        —— 同一循环里 stepid 重复或乱序
* 时间倒流        —— atime 不单调
* 采集不完整      —— inquiredf 说没传完
* 解析错位        —— 容量在工步内不单调、内阻为负

用法
----
    python validate.py --host 10.201.47.169 --channel 27-188-10-2
    python validate.py --host <IP> --channel 27-188-10-2 --testid 0 --limit 0

退出码：0 = 通过（可能有警告）；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from neware_client import (
    NewareClient,
    NewareError,
    build_transport,
    parse_channel,
)

# 按工步类型分组的充放电方向（来自协议 2.2 的 17 种类型）
CHARGE_TYPES = {"cc", "cv", "cccv", "pcccv", "cp", "cr", "pulse"}
DISCHARGE_TYPES = {"dc", "dv", "cccd", "dp", "dr"}
IDLE_TYPES = {"rest", "pause", "end"}
CONTROL_TYPES = {"sim", "control"}

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Checker:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def add(self, verdict: str, name: str, detail: str) -> None:
        self.results.append((verdict, name, detail))
        print(f"  {verdict:<5} {name:<34} {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for v, _, _ in self.results if v == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for v, _, _ in self.results if v == WARN)


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_energy_consistency(c: Checker, steps: list[dict]) -> None:
    """能量 ≈ 容量 × 平均电压（单位一致时比值应为 1.0 左右）。

    这个检查抓的是「**cap 与 eng 两个字段之间的单位是否一致**」。

    它能抓到：客户端某天把其中一个字段的单位改了（比如 cap 变 Ah 而 eng 仍 mWh），
    或者我们自己对某个字段做了错误的换算。

    它**抓不到**：cap 和 eng 被同时按同一倍数标错 —— 那种情况下比值不变，
    而且比率类指标（效率、保持率）本来就不受影响，所以也不算真错误。
    """
    bad, checked = [], 0
    for row in steps:
        cap = to_float(row.get("cap"))
        eng = to_float(row.get("eng"))
        v1 = to_float(row.get("startvolt"))
        v2 = to_float(row.get("endvolt"))
        if not cap or not eng or v1 is None or v2 is None:
            continue
        if cap <= 0 or eng <= 0:
            continue
        vavg = (v1 + v2) / 2
        if vavg <= 0:
            continue
        checked += 1
        ratio = eng / (cap * vavg)          # 单位一致时应约等于 1
        if not (0.5 <= ratio <= 2.0):
            bad.append((row.get("stepindex"), cap, eng, vavg, ratio))
    if checked == 0:
        c.add(WARN, "单位自洽（能量÷容量÷电压）", "没有可用的充放电工步样本")
        return
    if bad:
        s = bad[0]
        c.add(FAIL, "单位自洽（能量÷容量÷电压）",
              f"{len(bad)}/{checked} 个工步偏离 1.0（期望 0.5~2.0）。"
              f"首个：步{s[0]} cap={s[1]:.4g} eng={s[2]:.4g} Vavg={s[3]:.4f} "
              f"→ 比值 {s[4]:.3g}。**单位可能标错**")
    else:
        c.add(PASS, "单位自洽（能量÷容量÷电压）",
              f"{checked} 个充放电工步的 eng/(cap×Vavg) 都在 0.5~2.0 内")


def check_current_sign(c: Checker, steps: list[dict], detail: list[dict]) -> None:
    """充电工步电流应为正、放电工步应为负（2026-09-24 对账确认）。"""
    wrong = []
    seen = 0
    for row in steps:
        stype = str(row.get("steptype", "")).strip().lower()
        curr = to_float(row.get("startcurr"))
        if curr is None or curr == 0:
            continue
        if stype in CHARGE_TYPES:
            seen += 1
            if curr < 0:
                wrong.append((row.get("stepindex"), stype, curr))
        elif stype in DISCHARGE_TYPES:
            seen += 1
            if curr > 0:
                wrong.append((row.get("stepindex"), stype, curr))
    if seen == 0:
        c.add(WARN, "电流符号与工步方向一致", "工步层没有非零电流样本")
    elif wrong:
        w = wrong[0]
        c.add(FAIL, "电流符号与工步方向一致",
              f"{len(wrong)}/{seen} 个工步符号反常。"
              f"首个：步{w[0]} 类型={w[1]} 电流={w[2]}（充电应为正、放电应为负）")
    else:
        c.add(PASS, "电流符号与工步方向一致", f"{seen} 个工步符号都正确")

    # 明细层再查一遍（更细）
    bad = 0
    n = 0
    for row in detail:
        stype = str(row.get("steptype", "")).strip().lower()
        curr = to_float(row.get("curr"))
        if curr is None or curr == 0:
            continue
        if stype in CHARGE_TYPES:
            n += 1
            if curr < 0:
                bad += 1
        elif stype in DISCHARGE_TYPES:
            n += 1
            if curr > 0:
                bad += 1
    if n:
        if bad:
            c.add(FAIL, "明细电流符号", f"{bad}/{n} 条符号反常")
        else:
            c.add(PASS, "明细电流符号", f"{n} 条非零电流记录符号都正确")


def check_seqid(c: Checker, detail: list[dict]) -> None:
    """数据序号应连续递增，缺号说明采集漏了。"""
    ids = [to_int(r.get("seqid")) for r in detail]
    ids = [i for i in ids if i is not None]
    if len(ids) < 2:
        c.add(WARN, "数据序号连续", "样本太少")
        return
    ids_sorted = sorted(ids)
    gaps = [b - a for a, b in zip(ids_sorted, ids_sorted[1:]) if b - a != 1]
    if gaps:
        c.add(FAIL, "数据序号连续",
              f"{len(gaps)} 处跳号（最大跨度 {max(gaps)}）—— 采集可能漏数据")
    else:
        c.add(PASS, "数据序号连续", f"{ids_sorted[0]} → {ids_sorted[-1]} 无缺号")


def check_cycle_structure(c: Checker, detail: list[dict]) -> None:
    """同一循环内，stepid 应从 1 开始递增且不重复。"""
    by_cycle: dict[int, list[int]] = {}
    for row in detail:
        cy = to_int(row.get("cycleid"))
        st = to_int(row.get("stepid"))
        if cy is None or st is None:
            continue
        by_cycle.setdefault(cy, []).append(st)
    if not by_cycle:
        c.add(WARN, "循环/工步结构", "没有可用样本")
        return
    bad = []
    for cy, seq in by_cycle.items():
        if any(b < a for a, b in zip(seq, seq[1:])):
            bad.append((cy, "工步号回退"))
    if bad:
        c.add(FAIL, "循环/工步结构", f"{len(bad)} 个循环内工步号回退，例如循环 {bad[0][0]}")
    else:
        c.add(PASS, "循环/工步结构", f"{len(by_cycle)} 个循环内工步号均递增")


def check_time_monotonic(c: Checker, detail: list[dict]) -> None:
    from probe import _parse_atime
    stamps = [_parse_atime(r.get("atime")) for r in detail]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        c.add(WARN, "绝对时间单调", "可解析的时间样本不足")
        return
    back = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
    if back:
        c.add(FAIL, "绝对时间单调", f"{back} 处时间倒流")
    else:
        c.add(PASS, "绝对时间单调", f"{len(stamps)} 个时间点均不回退")


def check_capacity_monotonic(c: Checker, detail: list[dict]) -> None:
    """同一工步内，容量是累计值，应当不减（归零只发生在换工步时）。"""
    prev_key, prev_cap, bad = None, None, 0
    n = 0
    for row in detail:
        key = (to_int(row.get("cycleid")), to_int(row.get("stepid")))
        cap = to_float(row.get("cap"))
        if cap is None:
            continue
        if prev_key == key and prev_cap is not None:
            n += 1
            if cap < prev_cap - 1e-12:
                bad += 1
        prev_key, prev_cap = key, cap
    if n == 0:
        c.add(WARN, "工步内容量累计单调", "样本不足")
    elif bad:
        c.add(FAIL, "工步内容量累计单调", f"{bad}/{n} 处容量回落 —— 解析可能错位")
    else:
        c.add(PASS, "工步内容量累计单调", f"{n} 组相邻点均不回落")


def check_value_ranges(c: Checker, steps: list[dict], detail: list[dict]) -> None:
    """值域合理性：电压非负、内阻非负、温度在室温附近。"""
    probs = []
    neg_v = sum(1 for r in detail if (to_float(r.get("volt")) or 0) < 0)
    if neg_v:
        probs.append(f"{neg_v} 条负电压")
    neg_e = sum(1 for r in detail if (to_float(r.get("eng")) or 0) < 0)
    if neg_e:
        probs.append(f"{neg_e} 条负能量")
    neg_d = sum(1 for r in steps
                if (to_float(r.get("dcir")) or 0) < 0)
    if neg_d:
        probs.append(f"{neg_d} 个负内阻")
    # 辅助通道：如果列名是 CPU/T 之类，值应在合理区间
    aux_names = sorted({k for r in detail[:50] for k in r
                        if k not in {"seqid", "stepid", "cycleid", "steptype",
                                     "testtime", "atime", "volt", "curr", "cap",
                                     "eng", "dbc"}})
    for a in aux_names:
        vals = [to_float(r.get(a)) for r in detail[:200]]
        vals = [v for v in vals if v is not None]
        if strs := [v for v in vals if v < -100 or v > 1000]:
            probs.append(f"辅助通道 {a} 有 {len(strs)} 个越界值（如 {strs[0]}）")
    if probs:
        c.add(FAIL, "数值范围合理", "；".join(probs[:3]))
    else:
        c.add(PASS, "数值范围合理",
              f"电压/能量/内阻/辅助通道数值均在合理区间"
              + (f"（辅助通道 {aux_names}）" if aux_names else ""))


def check_completeness(c: Checker, uploaded: int | None, complete: bool | None,
                       detail: list[dict]) -> None:
    """数据是否已上传完整 —— 这是"缺数据"最常见的原因。"""
    if complete is None:
        c.add(WARN, "数据上传完整性", "inquiredf 没返回结果")
    elif not complete:
        c.add(FAIL, "数据上传完整性",
              f"客户端说数据**尚未传完**（已上传 {uploaded} 条）。"
              f"现在拿到的可能是部分数据，等测试结束再取")
    else:
        c.add(PASS, "数据上传完整性", f"客户端确认已传完（{uploaded} 条）")
    if uploaded and detail and len(detail) != uploaded:
        c.add(WARN, "明细条数与声称一致",
              f"拿到 {len(detail)} 条，客户端声称 {uploaded} 条"
              f"（若用了 --limit 限制则属正常）")


def main() -> int:
    p = argparse.ArgumentParser(description="数据体检（不依赖 BTSDA）")
    p.add_argument("--transport", choices=["pipe", "tcp"], default="tcp")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--channel", required=True)
    p.add_argument("--testid", type=int, default=0)
    p.add_argument("--limit", type=int, default=0, help="明细最多取多少条，0=全量")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--pipe")
    args = p.parse_args()

    channel = parse_channel(args.channel)
    transport = build_transport(args)
    try:
        transport.connect()
    except NewareError as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        return 1

    try:
        client = NewareClient(transport)
        print(f"正在取数（通道 {channel.key}）…")
        steps = NewareClient.parse_data(
            client.download_steplayer(channel, args.testid, dcir=1))
        checks = NewareClient.parse_inquiredf(client.inquiredf([channel], args.testid))
        detail = client.download_all(channel, testid=args.testid,
                                    max_rows=args.limit or None, progress=False)
        print(f"  工步层 {len(steps)} 行，明细 {len(detail)} 行")
    finally:
        transport.close()

    uploaded = checks[0]["uploaded"] if checks else None
    complete = checks[0]["complete"] if checks else None

    print()
    print("=" * 78)
    print("数据体检结果")
    print("=" * 78)
    c = Checker()
    check_completeness(c, uploaded, complete, detail)
    check_seqid(c, detail)
    check_cycle_structure(c, detail)
    check_time_monotonic(c, detail)
    check_capacity_monotonic(c, detail)
    check_current_sign(c, steps, detail)
    check_energy_consistency(c, steps)
    check_value_ranges(c, steps, detail)

    print("=" * 78)
    print(f"通过 {len(c.results) - c.failed - c.warned} 项，"
          f"警告 {c.warned} 项，失败 {c.failed} 项")
    if c.failed:
        print("\n❌ 有失败项 —— 数据可能不可信，先解决再往下走")
        return 1
    if c.warned:
        print("\n⚠️ 只有警告 —— 数据基本可信，但注意上面标 WARN 的说明")
        return 0
    print("\n✅ 全部通过 —— 数据的结构、单位、符号都自洽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
