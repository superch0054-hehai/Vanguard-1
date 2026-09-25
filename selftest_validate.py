#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate.py 的离线测试：既要能通过干净数据，也要能抓出坏数据。

只测"能跑通"是不够的 —— 关键是验证它**真的能发现问题**。
所以这里故意构造几种坏数据，逐一看它是否报警。

    python selftest_validate.py
"""

import sys
from pathlib import Path

import validate as V


def build_steps(scale_cap: float = 1.0, flip_sign: bool = False) -> list[dict]:
    """构造工步层数据。

    scale_cap 只放大 cap（不改 eng）—— 模拟"容量与能量两个字段单位不一致"。
    注意：若 cap 与 eng 同步放大，比值不变，那本身不是错误（比率类指标不受影响），
    所以那种情形**不该**被判定为失败。
    """
    rows = []
    seq = 1
    for cycle in (1, 2, 3):
        spec = [
            ("rest", 0.28, 0.28, 0.0, 0.0, 0.0, 0.0),
            ("cc",   0.29, 3.30, 0.0142, 0.0211, 0.0405, 1206000.0),
            ("rest", 3.24, 2.09, 0.0, 0.0, 0.0, 4374800.0),
            ("dc",   2.06, 0.50, -0.0142, 0.0029, 0.0026, 2400300.0),
        ]
        for i, (stype, v1, v2, curr, cap, eng, dcir) in enumerate(spec, 1):
            rows.append({
                "startseqid": str(seq), "endseqid": str(seq + 9),
                "stepindex": str(len(rows) + 1), "stepid": str(i),
                "cycleid": str(cycle), "steptype": stype, "steptime": "60000",
                "endatime": f"2026-09-23 10:{i:02d}:00",
                "startvolt": str(v1), "endvolt": str(v2),
                "startcurr": str(-curr if flip_sign else curr),
                "endcurr": str(-curr if flip_sign else curr),
                "cap": str(cap * scale_cap), "eng": str(eng),
                "dcir": str(dcir),
            })
            seq += 10
    return rows


def build_detail(skip_seqid: bool = False) -> list[dict]:
    rows = []
    for i in range(1, 41):
        stype = "cc" if i <= 20 else "dc"
        curr = 0.0142 if stype == "cc" else -0.0142
        seq = i + (100 if skip_seqid and i >= 20 else 0)
        rows.append({
            "seqid": str(seq), "stepid": str(1 if i <= 20 else 2),
            "cycleid": "1", "steptype": stype, "testtime": str((i % 20) * 1000),
            "atime": f"2026-09-23 10:00:{i % 60:02d}",
            "volt": f"{0.3 + i * 0.07:.4f}", "curr": str(curr),
            "cap": f"{i * 1.5e-5:.9f}", "eng": f"{i * 3e-5:.9f}", "CPU": "34.7",
        })
    return rows


class Log:
    def __init__(self):
        self.items = []

    def add(self, verdict, name, detail=""):
        self.items.append((verdict, name, detail))

    @property
    def failed(self):
        return sum(1 for v, _, _ in self.items if v == "FAIL")

    @property
    def warned(self):
        return sum(1 for v, _, _ in self.items if v == "WARN")


def run_checks(steps, detail, complete=True, uploaded=None):
    c = Log()
    V.check_completeness(c, uploaded if uploaded is not None else len(detail),
                         complete, detail)
    V.check_seqid(c, detail)
    V.check_cycle_structure(c, detail)
    V.check_time_monotonic(c, detail)
    V.check_capacity_monotonic(c, detail)
    V.check_current_sign(c, steps, detail)
    V.check_energy_consistency(c, steps)
    V.check_value_ranges(c, steps, detail)
    return c


def main() -> int:
    checks = []

    print("=" * 70)
    print("① 干净数据（应全部通过）")
    print("=" * 70)
    c = run_checks(build_steps(), build_detail())
    for v, n, d in c.items:
        print(f"  {v:<5} {n:<34} {d}")
    checks.append(("干净数据无失败项", c.failed == 0))

    print()
    print("=" * 70)
    print("② 注入「电流符号反了」（应报 FAIL）")
    print("=" * 70)
    c = run_checks(build_steps(flip_sign=True), build_detail())
    for v, n, d in c.items:
        if v != "PASS":
            print(f"  {v:<5} {n:<34} {d}")
    checks.append(("能抓出符号反了", c.failed > 0))

    print()
    print("=" * 70)
    print("③ 只把 cap 放大 1000 倍（模拟两字段单位不一致，应报 FAIL）")
    print("=" * 70)
    c = run_checks(build_steps(scale_cap=1000.0), build_detail())
    for v, n, d in c.items:
        if v != "PASS":
            print(f"  {v:<5} {n:<34} {d}")
    checks.append(("能抓出两字段单位不一致", c.failed > 0))

    print()
    print("=" * 70)
    print("④ 注入「seqid 跳号」（应报 FAIL：数据有缺）")
    print("=" * 70)
    c = run_checks(build_steps(), build_detail(skip_seqid=True))
    for v, n, d in c.items:
        if v != "PASS":
            print(f"  {v:<5} {n:<34} {d}")
    checks.append(("能抓出序号缺号", c.failed > 0))

    print()
    print("=" * 70)
    print("⑤ 注入「数据未上传完」（应报 FAIL）")
    print("=" * 70)
    c = run_checks(build_steps(), build_detail(), complete=False, uploaded=9999)
    for v, n, d in c.items:
        if v != "PASS":
            print(f"  {v:<5} {n:<34} {d}")
    checks.append(("能抓出上传不完整", c.failed > 0))

    print()
    print("=" * 70)
    print("验证：")
    ok = True
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        ok = ok and passed
    print()
    print("全部通过 ✅" if ok else "有失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
