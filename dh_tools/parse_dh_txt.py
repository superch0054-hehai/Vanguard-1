#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东华 DH7006B 导出 txt 解析器（v0.1 · 2026-10-11）

用途：把东华主程序"文件导出"生成的 txt 读成规范化数据（JSON / CSV）。

文件格式（实测自 2026-10-11 的三份真实导出，UTF-8 / CRLF）：
    Segment1(控制电位EIS)            ← 段头：Segment<序号>(方法名)
    参数           值                ← 参数块表头
    信号源滤波     Auto              ← 参数行：名称 + 2 个以上空格 + 值
    ...
    Freq,Zre,Zim,Z,phase             ← 列名行（英文、逗号分隔）
    100000Hz,2.78Ω,-344.042mΩ,...    ← 数据行：逗号分隔；值可带单位后缀

单位：值形如 2.78Ω / -344.042mΩ / 2.201kΩ / -205.07nA / 1.871V / -7.055124°
     SI 前缀换算到基本单位（Ω / A / V / Hz / s）；无后缀的值原样保留。
     注意：Time 列通常不带后缀（按秒理解）。

用法：
    python parse_dh_txt.py 导出文件.txt                # 打印摘要 + 自检
    python parse_dh_txt.py 导出文件.txt --json out.json
    python parse_dh_txt.py 导出文件.txt --csvdir out/   # 每段一个 CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 正则与单位表
# --------------------------------------------------------------------------

SEG_RE = re.compile(r"^Segment(\d+)\((.*)\)\s*$")
PARAM_SPLIT = re.compile(r"\s{2,}|\t+")             # 参数行：2+ 空格或制表符
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*(,[A-Za-z][A-Za-z0-9_\-]*)+$")
FIELD_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-zµμ°Ω%]*)\s*$")

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "μ": 1e-6, "m": 1e-3,
          "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
# 可以被加前缀的单位（后缀部分）
PREFIXABLE = {"Ω", "A", "V", "Hz", "s", "F", "W", "g", "m"}
# 不带前缀的单位
PLAIN = {"°": 1.0, "%": 1.0, "Hz": 1.0, "s": 1.0, "V": 1.0, "A": 1.0, "Ω": 1.0}


def unit_multiplier(unit: str):
    """返回 (倍率, 基本单位)；无法识别返回 (None, unit)。"""
    if not unit:
        return (1.0, "")
    if unit in PLAIN:
        return (PLAIN[unit], unit)
    head, rest = unit[0], unit[1:]
    if head in PREFIX and rest in PREFIXABLE:
        return (PREFIX[head], rest)
    return (None, unit)


# --------------------------------------------------------------------------
# 读取与解析
# --------------------------------------------------------------------------

def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def parse_file(path: Path):
    text, enc = read_text(path)
    segments: list[dict] = []
    cur: dict | None = None
    state = None  # params -> data

    for line in text.splitlines():
        s = line.rstrip()
        m = SEG_RE.match(s.strip())
        if m:
            if cur:
                segments.append(cur)
            cur = {"index": int(m.group(1)), "method": m.group(2),
                   "params": {}, "columns": [], "rows": [], "raw_units": {}}
            state = "params"
            continue
        if cur is None:
            continue
        if state == "params":
            if HEADER_RE.match(s.strip()) and s.count(",") >= 1:
                cur["columns"] = [c.strip() for c in s.strip().split(",")]
                state = "data"
                continue
            parts = PARAM_SPLIT.split(s.strip(), maxsplit=1)
            if len(parts) == 2 and parts[0].strip() and parts[0].strip() != "参数":
                cur["params"][parts[0].strip()] = parts[1].strip()
            continue
        # data
        if not s.strip():
            continue
        row, ok = [], True
        for i, f in enumerate(s.split(",")):
            fm = FIELD_RE.match(f)
            if not fm:
                ok = False
                break
            val, unit = float(fm.group(1)), (fm.group(2) or "")
            mult, base = unit_multiplier(unit)
            row.append(val * mult if mult is not None else val)
            if unit and i not in cur["raw_units"]:
                cur["raw_units"][i] = unit
        if ok and row:
            cur["rows"].append(row)

    if cur:
        segments.append(cur)
    return segments, enc


# --------------------------------------------------------------------------
# 自检（物理常识）
# --------------------------------------------------------------------------

def check_segment(seg: dict) -> list[str]:
    notes: list[str] = []
    cols = seg["columns"]
    rows = seg["rows"]
    if not rows:
        notes.append("⚠️ 无数据行")
        return notes
    ncol = len(cols)
    bad = [i for i, r in enumerate(rows) if len(r) != ncol]
    if bad:
        notes.append(f"❌ {len(bad)} 行的列数与表头不一致（首例 第{bad[0]+1}行）")
    if "Freq" in cols and "Zre" in cols and "Zim" in cols:
        i_f, i_re, i_im = cols.index("Freq"), cols.index("Zre"), cols.index("Zim")
        d_z = d_p = 0.0
        if "Z" in cols:
            i_z = cols.index("Z")
            d_z = max(abs(math.hypot(r[i_re], r[i_im]) / r[i_z] - 1.0) for r in rows)
        if "phase" in cols:
            i_p = cols.index("phase")
            d_p = max(abs(math.degrees(math.atan2(r[i_im], r[i_re])) - r[i_p]) for r in rows)
        freqs = [r[i_f] for r in rows]
        mono = all(b <= a for a, b in zip(freqs, freqs[1:]))
        notes.append(f"✅ |Z| 与 √(Zre²+Zim²) 最大相对偏差 {d_z*100:.3f}%（导出值仅 4 位有效数字，0.5% 内属正常舍入）")
        notes.append(f"✅ phase-atan2 最大偏差 {d_p:.2e}°")
        notes.append(("✅" if mono else "⚠️") + f" 频率单调递减（{freqs[0]:.6g} → {freqs[-1]:.6g} Hz，{len(rows)} 点）")
    if "Time" in cols and len(rows) > 1:
        i_t = cols.index("Time")
        ts = [r[i_t] for r in rows]
        mono = all(b >= a for a, b in zip(ts, ts[1:]))
        notes.append(("✅" if mono else "⚠️") + f" 时间单调递增（{ts[0]:.6g} → {ts[-1]:.6g}，{len(rows)} 点）")
    if "E" in cols:
        i_e = cols.index("E")
        es = [r[i_e] for r in rows]
        notes.append(f"✅ 电位范围 {min(es):.4g} ~ {max(es):.4g} V")
    if "I" in cols:
        i_i = cols.index("I")
        is_ = [r[i_i] for r in rows]
        notes.append(f"✅ 电流范围 {min(is_):.4g} ~ {max(is_):.4g} A")
    return notes


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def dump_json(path: Path, src: Path, enc: str, segments: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": str(src), "encoding": enc,
               "segments": [{k: v for k, v in seg.items() if k != "raw_units"} | {
                   "unit_hint": {str(i): u for i, u in seg["raw_units"].items()}}
                            for seg in segments]}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def dump_csv(dirpath: Path, src: Path, segments: list[dict]) -> list[Path]:
    dirpath.mkdir(parents=True, exist_ok=True)
    out = []
    for seg in segments:
        p = dirpath / f"{src.stem}_S{seg['index']:02d}.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(seg["columns"])
            w.writerows(seg["rows"])
        out.append(p)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="东华 DH7006B 导出 txt 解析器")
    ap.add_argument("file", type=Path)
    ap.add_argument("--json", type=Path, help="输出 JSON 路径")
    ap.add_argument("--csvdir", type=Path, help="每段输出一个 CSV 的目录")
    ap.add_argument("--show", type=int, default=2, help="每段打印前 N 行数据（默认 2）")
    args = ap.parse_args()

    segments, enc = parse_file(args.file)
    print(f"文件：{args.file.name}")
    print(f"编码：{enc}；段数：{len(segments)}")
    for seg in segments:
        print(f"\n── Segment{seg['index']}（{seg['method']}）──")
        print(f"   参数 {len(seg['params'])} 项；列 = {','.join(seg['columns'])}；数据 {len(seg['rows'])} 行")
        for i, r in enumerate(seg["rows"][: max(0, args.show)]):
            print(f"   [{i}] " + ", ".join(f"{v:.6g}" for v in r))
        for note in check_segment(seg):
            print("   " + note)
    if args.json:
        dump_json(args.json, args.file, enc, segments)
        print(f"\n已输出 JSON：{args.json}")
    if args.csvdir:
        outs = dump_csv(args.csvdir, args.file, segments)
        print(f"已输出 {len(outs)} 个 CSV 到 {args.csvdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
