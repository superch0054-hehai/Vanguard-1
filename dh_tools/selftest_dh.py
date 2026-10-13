#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dh_tools 自检（v0.1 · 2026-10-11）——不出门就能验证代码没坏。

跑法：python selftest_dh.py
覆盖：单位换算 / 合成文件解析 / 空文件处理 / 收件员端到端（含幂等）
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dh_collect  # noqa: E402
import parse_dh_txt  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


SYNTH = """Segment1(控制电位EIS)
参数           值
开始频率(Hz)   100000
结束频率(Hz)   0.01
Freq,Zre,Zim,Z,phase
100000Hz,2.78Ω,-344.042mΩ,2.801Ω,-7.055124°
100Hz,10Ω,-2Ω,10.198Ω,-11.31°
0.01Hz,2.206kΩ,-929.899Ω,2.394kΩ,-22.85351°
Segment2(循环伏安法(Multiple CV))
参数           值
扫描速率(V/s)   0.0001
Time,E,I,IRand,Ewe
5,1.871V,-205.07nA,100uA,1.871V
10,1.87V,-73.603nA,10uA,1.87V
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("① 单位换算")
    cases = {"mΩ": 1e-3, "kΩ": 1e3, "Ω": 1.0, "nA": 1e-9, "uA": 1e-6,
             "mA": 1e-3, "mV": 1e-3, "V": 1.0, "Hz": 1.0, "°": 1.0, "pA": 1e-12}
    for unit, mult in cases.items():
        got, _ = parse_dh_txt.unit_multiplier(unit)
        check(f"{unit} -> {mult:g}", got == mult, f"得到 {got}")

    print("② 合成文件解析（2 段、带 SI 前缀单位）")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sample.txt"
        p.write_text(SYNTH, encoding="utf-8")
        segs, enc = parse_dh_txt.parse_file(p)
        check("段数=2", len(segs) == 2, f"得到 {len(segs)}")
        check("段1 列名", segs[0]["columns"] == ["Freq", "Zre", "Zim", "Z", "phase"])
        check("段1 行数=3", len(segs[0]["rows"]) == 3)
        r0 = segs[0]["rows"][0]
        check("行1 换算（-344.042mΩ -> -0.344042）", abs(r0[2] + 0.344042) < 1e-12, str(r0))
        check("行3 换算（2.206kΩ -> 2206）", abs(segs[0]["rows"][2][1] - 2206.0) < 1e-9)
        check("段2 方法名", segs[1]["method"].startswith("循环伏安法"))
        check("段2 行1 电流（-205.07nA -> -2.0507e-7）",
              abs(segs[1]["rows"][0][2] + 2.0507e-7) < 1e-15)
        check("自检函数不报错", isinstance(parse_dh_txt.check_segment(segs[0]), list))

        print("③ 空文件")
        e = Path(td) / "empty.txt"
        e.write_bytes(b"")
        segs2, _ = parse_dh_txt.parse_file(e)
        check("空文件 -> 0 段", len(segs2) == 0)

        print("④ 收件员端到端（含幂等）")
        inbox, outbox = Path(td) / "inbox", Path(td) / "outbox"
        inbox.mkdir()
        (inbox / "sample.txt").write_text(SYNTH, encoding="utf-8")
        (inbox / "empty.txt").write_bytes(b"")
        idx = outbox / "index.jsonl"
        n_new, n_skip = dh_collect.scan_once(inbox, outbox, idx, exts=(".txt",))
        check("首趟：收录 1 / 跳过 1", (n_new, n_skip) == (1, 1), f"得到 ({n_new},{n_skip})")
        recs = [json.loads(x) for x in idx.read_text(encoding="utf-8").splitlines()]
        check("索引 2 条", len(recs) == 2)
        check("摘要已生成", (outbox / "摘要.md").exists())
        check("规范数据已落盘", (outbox / "parsed" / "sample.json").exists())
        n_new2, n_skip2 = dh_collect.scan_once(inbox, outbox, idx, exts=(".txt",))
        check("第二趟：不重复收录（幂等）", (n_new2, n_skip2) == (0, 0), f"得到 ({n_new2},{n_skip2})")

    print()
    print(f"结果：通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
