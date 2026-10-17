#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东华导出数据"收件员"（v0.1 · 2026-10-11）

职责：盯住"收件箱"目录，把新落下的**原生 .dhsln**（.dat 待标定后启用）自动读取、落成规范数据、
维护索引，并生成一份人可读的《数据摘要》。

设计对齐参照项目（BatteryLab）的纪律：
* **零依赖**（只用标准库），可以直接进容器跑；
* **幂等**：按文件内容指纹去重，重启/重复运行不会重复入库；
* **空文件 / 坏文件不崩**：记一笔"跳过"，继续干活；
* **只读输入目录、只写输出目录**——绝不改动原始文件。

用法：
    python dh_collect.py --inbox 收件箱 --outbox 输出 --once        # 单趟（演示/测试）
    python dh_collect.py --inbox 收件箱 --outbox 输出 --interval 30 # 常驻盯守

输出目录结构：
    outbox/parsed/<文件名>.json   每份导出的规范数据（段 / 参数 / 列 / 数值）
    outbox/index.jsonl            索引：一行一条（追加安全，按指纹去重）
    outbox/摘要.md                人可读摘要（每次扫描后重写）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_dh_txt  # noqa: E402
import parse_dhsln  # noqa: E402
import parse_dat  # noqa: E402


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {msg}", flush=True)


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_index(idx_path: Path) -> tuple[list[dict], set[str]]:
    """读索引；返回 (全部记录, 已收录的指纹集合)。坏行跳过，不崩。"""
    records: list[dict] = []
    keys: set[str] = set()
    if not idx_path.exists():
        return records, keys
    for line in idx_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(r)
        if r.get("sha1"):
            keys.add(r["sha1"])
    return records, keys


def append_index(idx_path: Path, record: dict) -> None:
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    with idx_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 摘要（人可读）
# --------------------------------------------------------------------------

def seg_brief(seg: dict) -> str:
    cols = seg["columns"]
    rows = seg["rows"]
    parts = [f"{len(rows)} 点"]
    for name in ("Freq", "E", "I", "Time"):
        if name in cols and rows:
            i = cols.index(name)
            vals = [r[i] for r in rows if len(r) > i]
            if vals:
                unit = {"Freq": "Hz", "E": "V", "I": "A", "Time": "s"}[name]
                parts.append(f"{name} {min(vals):.4g}~{max(vals):.4g} {unit}")
    return "；".join(parts)


def dhsln_brief(p: dict) -> str:
    """原生 .dhsln 的一行概况：段数 + 方法名 + 关键参数"""
    exps = p.get("experiments") or []
    if not exps:
        return "无实验段"
    names: list[str] = []
    for e in exps:
        nm = e.get("name") or "?"
        if nm not in names:
            names.append(nm)
    keys = ("开始频率(Hz)", "结束频率(Hz)", "点数", "扫描速率(V/s)", "起始电位(V)",
            "终止电位(V)", "循环次数", "圈数", "采样间隔(s)", "电流量程(A)")
    kv: list[str] = []
    for k in keys:
        for e in exps:
            v = (e.get("params") or {}).get(k)
            if v not in (None, ""):
                kv.append(f"{k}={v}")
                break
    return "%d 段；方法 %s%s" % (len(exps), " / ".join(names[:3]),
                                ("；" + "，".join(kv[:4])) if kv else "")


def build_summary(records: list[dict], out_path: Path) -> None:
    ok = [r for r in records if r.get("status") == "ok"]
    skipped = [r for r in records if r.get("status") not in ("ok", "no_data")]
    pending = [r for r in records if r.get("status") == "no_data"]
    ok_txt = [r for r in ok if not r.get("native")]
    ok_nat = [r for r in ok if r.get("native")]
    ok_num = [r for r in ok_nat if r.get("numeric")]
    ok_nat = [r for r in ok_nat if not r.get("numeric")]
    lines = ["# 东华导出数据摘要", "",
             f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　|　"
             f"已收录 {len(ok)} 份，跳过 {len(skipped)} 份，待支持 {len(pending)} 个 .dat", ""]
    for r in ok_txt:
        lines.append(f"## {r['file']}")
        lines.append("")
        lines.append(f"- 来源：`{r['source']}`（{r['size']} 字节，sha1 `{r['sha1'][:10]}…`）")
        for seg in r.get("segments", []):
            lines.append(f"- **Segment{seg['index']}（{seg['method']}）**：{seg['brief']}")
        lines.append("")
    if ok_num:
        lines.append("## 测量数值（.dat：已解码的阻抗谱）")
        lines.append("")
        for r in ok_num:
            lines.append(f"- `{r['file']}`：{r.get('brief', '')}（sha1 `{r['sha1'][:10]}…`）")
        lines.append("")
    if ok_nat:
        lines.append("## 原生实验（.dhsln：实验类型 / 方法 / 参数，无需软件导出）")
        lines.append("")
        for r in ok_nat:
            lines.append(f"### {r['file']}")
            lines.append("")
            lines.append(f"- 来源：`{r['source']}`（{r['size']} 字节，sha1 `{r['sha1'][:10]}…`）")
            if r.get("data_file"):
                lines.append(f"- 数据文件：`{r['data_file']}.dat`（MachineId {r.get('machine_id')}）")
            lines.append(f"- 概况：{r.get('brief', '')}")
            for e in r.get("experiments", []):
                lines.append(f"  - 段{e.get('index')}：{e.get('name')}"
                             f"（类型 {e.get('type')}，{e.get('n_params')} 项参数，信号 {e.get('signals') or '—'}）")
            lines.append("")
    if skipped:
        lines.append("## 跳过（无数据或无法解析）")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['file']}`（{r.get('reason', '未知')}）")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def scan_once(inbox: Path, outbox: Path, idx_path: Path, min_age: float = 0.0,
               exts: tuple[str, ...] = (".dhsln", ".dat")) -> tuple[int, int]:
    """扫一趟收件箱。

    min_age：只处理「落盘满 min_age 秒」的文件——防止把还在写入的半截文件
    当成成品入库（推送器每 1 分钟来一趟，太新的文件留给下一趟再收）。

    exts：要收录的后缀。默认只收原生 .dhsln——txt 已退出生产管道（.dat 待标定后加入）。
    """
    records, seen = load_index(idx_path)
    parsed_dir = outbox / "parsed"
    n_new = n_skip = 0

    txt_files = sorted(inbox.rglob("*.txt")) if ".txt" in exts else []
    for f in txt_files:
        stamp = sha1_file(f)
        if stamp in seen:
            continue
        age = time.time() - f.stat().st_mtime
        if age < min_age:
            log(f"文件太新（{age:.0f}s < {min_age:g}s），下趟再收：{f.name}")
            continue
        rec = {"file": f.name, "source": str(f), "size": f.stat().st_size,
               "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
               "sha1": stamp,
               "parsed_at": datetime.now().isoformat(timespec="seconds")}
        if f.stat().st_size == 0:
            rec.update(status="empty", reason="0 字节（该实验没有数据）")
            log(f"跳过空文件：{f.name}", "WARN")
            n_skip += 1
        else:
            try:
                segments, enc = parse_dh_txt.parse_file(f)
                if not segments:
                    rec.update(status="no_data", reason="未解析出任何段")
                    log(f"跳过（无段）：{f.name}", "WARN")
                    n_skip += 1
                else:
                    rec.update(status="ok", encoding=enc, segments=[
                        {"index": s["index"], "method": s["method"],
                         "columns": s["columns"], "rows": len(s["rows"]),
                         "brief": seg_brief(s)} for s in segments])
                    parsed_dir.mkdir(parents=True, exist_ok=True)
                    payload = {"source": str(f), "encoding": enc,
                               "segments": [{k: v for k, v in s.items() if k != "raw_units"}
                                            for s in segments]}
                    (parsed_dir / f"{f.stem}.json").write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    log(f"已收录：{f.name}（{len(segments)} 段）")
                    n_new += 1
            except Exception as exc:  # 坏文件不崩
                rec.update(status="error", reason=f"{type(exc).__name__}: {exc}")
                log(f"解析失败（已记录）：{f.name} — {exc}", "ERROR")
                n_skip += 1
        records.append(rec)
        seen.add(stamp)
        append_index(idx_path, rec)

    # ---- 原生 .dhsln（XML 配方：实验类型 / 方法 / 参数 / 信号，通道 = FileName）----
    dhsln_files = sorted(inbox.rglob("*.dhsln")) if ".dhsln" in exts else []
    for f in dhsln_files:
        stamp = sha1_file(f)
        if stamp in seen:
            continue
        age = time.time() - f.stat().st_mtime
        if age < min_age:
            log(f"文件太新（{age:.0f}s < {min_age:g}s），下趟再收：{f.name}")
            continue
        rec = {"file": f.name, "source": str(f), "size": f.stat().st_size,
               "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
               "sha1": stamp, "native": True,
               "parsed_at": datetime.now().isoformat(timespec="seconds")}
        try:
            p = parse_dhsln.parse(f)
            exps = p.get("experiments") or []
            rec.update(status="ok", machine_id=p.get("machine_id"), data_file=p.get("data_file"),
                       experiments=[{"index": e.get("index"), "type": e.get("type"),
                                     "name": e.get("name"), "signals": e.get("signals"),
                                     "n_params": len(e.get("params") or {})} for e in exps],
                       brief=dhsln_brief(p))
            parsed_dir.mkdir(parents=True, exist_ok=True)
            (parsed_dir / f"{f.stem}.{stamp[:8]}.dhsln.json").write_text(
                json.dumps(p, ensure_ascii=False), encoding="utf-8")
            log(f"已收录（原生）：{f.name}（{len(exps)} 段）")
            n_new += 1
        except Exception as exc:  # 坏文件不崩
            rec.update(status="error", reason=f"{type(exc).__name__}: {exc}")
            log(f"解析失败（已记录）：{f.name} — {exc}", "ERROR")
            n_skip += 1
        records.append(rec)
        seen.add(stamp)
        append_index(idx_path, rec)

    # ---- 原生 .dat（测量数值：目前解 EIS 段）----
    for f in (sorted(inbox.rglob("*.dat")) if ".dat" in exts else []):
        stamp = sha1_file(f)
        if stamp in seen:
            continue
        age = time.time() - f.stat().st_mtime
        if age < min_age:
            continue
        rec = {"file": f.name, "source": str(f), "size": f.stat().st_size,
               "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
               "sha1": stamp, "native": True, "numeric": True,
               "parsed_at": datetime.now().isoformat(timespec="seconds")}
        try:
            s = parse_dat.decode_summary(f)
            if not s:
                rec.update(status="no_data", reason=".dat 中未定位到已知结构（LSV/CV 待支持）")
                n_skip += 0
            else:
                rel = str(f.relative_to(inbox))
                rec.update(status="ok", brief=(parse_dat.brief(s) + " ← " + rel))
                parsed_dir.mkdir(parents=True, exist_ok=True)
                (parsed_dir / (f.stem + "." + stamp[:8] + ".dat.json")).write_text(
                    json.dumps({"source": rel, "kind": s.get("kind"),
                                "columns": s.get("columns"), "rows": s.get("rows")},
                               ensure_ascii=False), encoding="utf-8")
                log(f"已收录（数值）：{rel}（{rec['brief'].split(' ← ')[0]}）")
                n_new += 1
        except Exception as exc:
            rec.update(status="error", reason=f"{type(exc).__name__}: {exc}")
            n_skip += 1
        records.append(rec)
        seen.add(stamp)
        append_index(idx_path, rec)

    # ---- 完全同步维护：同路径只留最新 + 清理"源已消失"的记录 ----
    maint = False
    latest = {}
    for r in records:
        src = r.get("source") or ""
        if not src:
            continue
        if src not in latest or (r.get("parsed_at") or "") > (latest[src].get("parsed_at") or ""):
            latest[src] = r
    gone = [k for k in latest if not Path(k).exists()]
    if len(latest) > 10 and len(gone) > 0.2 * len(latest):
        log("异常：%d/%d 个源文件不可见 —— 跳过清理（防误删）" % (len(gone), len(latest)), "WARN")
        gone = []
    kept = [r for k, r in latest.items() if k not in set(gone)]
    if len(kept) != len(records):
        log("索引维护：%d → %d 条（同路径只留最新 / 清理已消失 %d 条）"
            % (len(records), len(kept), len(records) - len(gone) - len(kept) + len(gone)))
        idx_path.write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in kept) + chr(10),
                            encoding="utf-8")
        records = kept
        maint = True

    if n_new or n_skip or maint:
        build_summary(records, outbox / "摘要.md")
    return n_new, n_skip


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="东华导出数据收件员")
    ap.add_argument("--inbox", type=Path, required=True, help="收件箱目录（只读）")
    ap.add_argument("--outbox", type=Path, required=True, help="输出目录（可写）")
    ap.add_argument("--once", action="store_true", help="只扫一趟就退出（演示/测试用）")
    ap.add_argument("--interval", type=float, default=30.0, help="常驻模式的扫描间隔秒（默认 30）")
    ap.add_argument("--min-age", type=float, default=30.0,
                    help="只收「落盘满 N 秒」的文件，防止半截文件入库（默认 30）")
    ap.add_argument("--exts", default=".dhsln,.dat",
                    help="要收录的后缀（逗号分隔），默认 .dhsln；txt 不在生产管道内")
    args = ap.parse_args()
    exts = tuple(x.strip().lower() for x in args.exts.split(",") if x.strip())

    args.inbox.mkdir(parents=True, exist_ok=True)
    args.outbox.mkdir(parents=True, exist_ok=True)
    idx = args.outbox / "index.jsonl"

    if args.once:
        n_new, n_skip = scan_once(args.inbox, args.outbox, idx, args.min_age, exts)
        log(f"完成：新收录 {n_new} 份，跳过 {n_skip} 份")
        return 0

    log(f"收件员启动：inbox={args.inbox} outbox={args.outbox} 每 {args.interval:g}s 扫一趟")
    while True:
        try:
            n_new, n_skip = scan_once(args.inbox, args.outbox, idx, args.min_age, exts)
            if n_new or n_skip:
                log(f"本趟：新收录 {n_new} 份，跳过 {n_skip} 份")
        except Exception as exc:  # 主循环不崩
            log(f"扫描异常（下一趟重试）：{exc}", "ERROR")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
