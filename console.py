#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BatteryLab 数据控制台 —— 给非技术研究员用的网页界面。

设计原则：

1. **零依赖** —— 盒子外网不通，装不了 pip 包，所以只用 Python 标准库。
2. **只读优先** —— 界面只读数据、展示结果。唯一的"动作"是刷新共享数据，
   它只调用 make_share.py（读 data/ 写共享目录），**完全不碰测试机**。
   故意不提供 启动/停止测试 的按钮：那属于设备控制，风险不对等。
3. **单文件** —— 拷到盒子上就能跑。

用法：

    python3 console.py                                  # 默认 ./data，端口 8090
    python3 console.py --data-dir ~/batterylab/data \\
                       --share-dir /home/admin/jerry/batterylab-data
    python3 console.py --host 0.0.0.0                   # 让同网段的人也能看

然后在浏览器打开 http://<盒子IP>:8090
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from figures import (MASS_G, CycleChart, ce_never_healthy, cycles_of, load_steps,
                     sp_implausible)
from make_share import first_cycle_outlier_ratio
from neware_client import STEPTYPE_NAMES
from probe import soh_preview

VERSION = "1.0"
DEFAULT_PORT = 8090
DEFAULT_DATA_DIR = "./data"
STEPS_PREVIEW_ROWS = 12

# ECharts 静态库（Apache 2.0）。**不是装包** —— 就是一个本地文件，
# 由本控制台作为静态资源提供，所以打开页面的人不需要外网。
ECHARTS_FILE = Path(__file__).resolve().parent / "echarts.min.js"
ECHARTS_ROUTE = "/assets/echarts.min.js"

# 指标里我们希望优先展示的几项（顺序即展示顺序）
KEY_METRICS = [
    ("cycle_count", "循环数", ""),
    ("capacity_retention_pct", "容量保持率", " %"),
    ("coulomb_efficiency_last_pct", "库仑效率（末圈）", " %"),
    ("energy_efficiency_last_pct", "能量效率（末圈）", " %"),
    ("dcir_growth_pct", "DCIR 增长率", " %"),
]


# ---------------------------------------------------------------------------
# 读数据
# ---------------------------------------------------------------------------

def load_manifest(root: Path) -> list[dict]:
    """读 manifest.jsonl。坏行跳过，不要让一行脏数据把整个界面搞崩。

    ★ 2026-10-09：**按 key 去重、最后一行算数**。
    水桶模型下同一轮测试会被记多次（第一次加水记一笔、结束时再记一笔更新）；
    不去重的话，数据集列表里会出现几十行重复（用户实测看到一大堆）。
    """
    p = root / "manifest.jsonl"
    if not p.exists():
        return []
    latest: dict[str, dict] = {}
    order: list[str] = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            k = rec.get("key") or f"#{len(order)}"
            if k not in latest:
                order.append(k)
            latest[k] = rec
    return [latest[k] for k in order]


def load_status(root: Path) -> dict:
    p = root / "status.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) else {}


def human_size(p: Path) -> str:
    if not p.exists():
        return "—"
    n = float(p.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def find_dataset_dirs(root: Path, rec: dict) -> Path | None:
    """定位某个数据集目录。

    manifest 里的 dir 可能是绝对路径、也可能带/不带数据目录前缀，
    三种都试一遍 —— 换台机器部署就不至于找不到。
    """
    raw = Path(str(rec.get("dir") or ""))
    cands = [raw] if raw.is_absolute() else [root / raw, raw]
    chan, tid = str(rec.get("channel", "")), str(rec.get("testid", ""))
    if chan and tid:
        cands.append(root / f"channel={chan}" / f"testid={tid}")
    for c in cands:
        if c and c.is_dir():
            return c
    return None


def steps_preview(ddir: Path, n: int = STEPS_PREVIEW_ROWS) -> list[dict]:
    """工步层前 n 条（读成 dict，好按字段取名）。"""
    p = ddir / "steps.csv"
    if not p.exists():
        return []
    import csv as _csv
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            out = []
            for i, row in enumerate(_csv.DictReader(f)):
                if i >= n:
                    break
                out.append(dict(row))
            return out
    except (OSError, UnicodeDecodeError):
        return []


def human_qty(v, base: str, sign: bool = False) -> str:
    """把协议原始值换成人读得懂的单位。

    协议原始单位是 **A / Ah / Wh**（协议文档写的就是这个），
    ×1000 才是 mA/mAh/mWh。这里按量级自动挑：0.000284 A → 284 µA。
    """
    try:
        x0 = float(v)
    except (TypeError, ValueError):
        return "—"
    if x0 == 0:
        return "0"
    a = abs(x0)
    for prefix, mul in (("m", 1e3), ("µ", 1e6), ("n", 1e9)):
        x = a * mul
        if x >= 1:
            sgn = ("+" if x0 > 0 else "−") if sign else ""
            return f"{sgn}{x:.4g} {prefix}{base}"
    return f"{a * 1e12:.3g} p{base}"


def human_dur(ms) -> str:
    """毫秒 → 人读得懂的时长。"""
    try:
        sec = int(float(ms) / 1000)
    except (TypeError, ValueError):
        return "—"
    if sec >= 3600:
        return f"{sec // 3600} 时 {sec % 3600 // 60} 分"
    if sec >= 60:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec} 秒"


def resolve_soh(root: Path, rec: dict) -> dict:
    """算这个数据集的 SOH 指标 —— **优先用 steps.csv 现算**。

    为什么不信 manifest 里缓存的 `soh`：那是**采集那一刻**按当时的代码算的。
    口径改过之后（比如分圈逻辑、单位换算），缓存值就过期了 ——
    照抄会让页面显示的数字和当前代码不一致。

    （make_share.py 已经踩过这个坑并改成现算；这里必须保持一致，
      否则同一个数据集在共享目录里显示 576、在控制台显示 575。）
    """
    ddir = find_dataset_dirs(root, rec)
    if ddir:
        sp = ddir / "steps.csv"
        if sp.exists():
            try:
                rows = load_steps(sp)
                if rows:
                    fresh = soh_preview(rows)
                    if fresh.get("available"):
                        return fresh
            except Exception:
                pass
    return rec.get("soh") or {}


def retention_caveat(soh: dict) -> float | None:
    """首圈是化成（预充）循环时，返回「首圈 ÷ 后续圈中位数」的倍数，否则 None。

    为什么要在网页上显式标出来：这种错**从数字上看不出来**。第 1 圈是化成
    循环时容量比后续大 3~10 倍，而「容量保持率 = 末圈 ÷ 首圈」拿第 1 圈当基准，
    于是算出 17%（实测）—— 曲线从第 2 圈起其实是平的，但读者会以为电池报废了。

    判据**直接复用 make_share 里的那一份**，不在这里重写：
    同一个数据集在共享目录里标了 ⚠️、在控制台上却没标，比不标更糟。
    （基准该用第 1 圈还是第一个正常循环 —— 这是待定问题，要问王涛老师/协会。）
    """
    return first_cycle_outlier_ratio(soh.get("retention_curve") or [])


def latest_status_age(status: dict) -> tuple[str, str]:
    """心跳时间距今多久，以及该用什么颜色。"""
    ts = status.get("updated_at")
    if not ts:
        return "未知", "warn"
    try:
        t = datetime.fromisoformat(str(ts))
    except ValueError:
        return str(ts), "warn"
    secs = (datetime.now() - t).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 120:
        return f"{int(secs)} 秒前", "ok"
    if secs < 900:
        return f"{int(secs // 60)} 分钟前", "warn"
    return f"{secs / 3600:.1f} 小时前（服务可能已停）", "bad"


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

CSS = """
/* 配色取自河海大学校徽本身（#0D71AD，由 assets/生成校徽.py 取样得到）。
   背景不做纯色：一层极淡的蓝→白渐变，加一枚固定的校徽水印。
   水印只在留白处透出来 —— 卡片和表格都是实底，数字照旧好读。 */
:root { --bg:#f7f9fc; --panel:#ffffff; --fg:#1b2430; --mut:#6b7686; --line:#e3e9f0;
        --ok:#1a7f37; --warn:#9a6700; --bad:#b42318;
        --accent:#0d71ad; --tint:#eaf3fa;
        --seal:url("__SEAL__"); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#11161d; --panel:#171e27; --fg:#eef2f7; --mut:#9aa7b6; --line:#28323e;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149;
          --accent:#6cb6e0; --tint:#16222e; } }
* { box-sizing:border-box; }
body { margin:0; padding:0 24px 40px; background:var(--bg); color:var(--fg);
       font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background-image:linear-gradient(180deg,var(--tint) 0,transparent 460px); }
/* 校徽水印：固定居中、纯装饰、不挡交互（pointer-events:none） */
.wm { position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.055;
      background:var(--seal) center/620px no-repeat; }
@media (prefers-color-scheme: dark) {
  .wm { opacity:.10; filter:brightness(1.75) saturate(.85); } }
.wrap { position:relative; z-index:1; max-width:1080px; margin:0 auto; }
/* 页头品牌行：校徽 + 标题 */
.brand { display:flex; align-items:center; gap:15px; padding:26px 0 10px; }
.brand .seal { flex:0 0 auto; width:58px; height:58px; border-radius:10px;
               background:var(--seal) center/contain no-repeat; }
@media (prefers-color-scheme: dark) {
  .brand .seal { filter:brightness(1.6) saturate(.85); } }
.brand h1 { margin:0; }
.brand .sub { margin:2px 0 0; }
h1 { font-size:23px; margin:0 0 2px; letter-spacing:.01em; }
h2 { font-size:17px; margin:34px 0 12px; padding-left:11px;
     border-left:4px solid var(--accent); line-height:1.3; }
.sub { color:var(--mut); font-size:13px; margin:0 0 4px; }
.banner { background:var(--line); border-radius:8px; padding:10px 14px;
          font-size:13px; color:var(--mut); margin-bottom:18px; }
/* 数据口径警告 —— 要比普通 banner 显眼，因为它标的是"这个数字别直接用"。
   文字用正常前景色（黄色正文对比度不够），靠左边框和底色提示。 */
.banner.warn { background:color-mix(in srgb, var(--warn) 12%, transparent);
               border-left:4px solid var(--warn); color:var(--fg);
               line-height:1.75; }
.banner.warn b { color:var(--warn); }
.cards { display:flex; flex-wrap:wrap; gap:12px; }
.card { flex:1 1 150px; background:var(--panel); border:1px solid var(--line);
        border-radius:12px; padding:14px 16px; box-shadow:0 1px 2px rgba(16,32,56,.05); }
.card .k { font-size:12px; color:var(--mut); letter-spacing:.04em; }
.card .v { font-size:24px; font-weight:600; margin-top:4px; }
.panel { border:1px solid var(--line); border-radius:12px; padding:16px;
         background:var(--panel); box-shadow:0 1px 2px rgba(16,32,56,.05); }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); }
th { font-size:12px; color:var(--mut); font-weight:600; letter-spacing:.04em;
     background:var(--tint); }
tbody tr:hover td { background:var(--tint); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
code { background:var(--line); padding:2px 6px; border-radius:5px; font-size:13px; }
pre { background:var(--line); padding:12px 14px; border-radius:8px; overflow-x:auto;
      font-size:12.5px; line-height:1.5; }
/* 总进度条（传统样式：底槽 + 填充 + 右侧百分比） */
.pwrap { display:flex; align-items:center; gap:10px; margin-top:12px; }
.pbar { flex:1; height:10px; background:var(--line); border-radius:999px; overflow:hidden; }
.pbar > i { display:block; height:100%; width:0; background:var(--accent);
            border-radius:999px; transition:width .45s ease, background .3s ease; }
.ppct { flex:0 0 auto; min-width:86px; text-align:right; font-size:12px;
        color:var(--mut); font-variant-numeric:tabular-nums; }
@media (prefers-reduced-motion: reduce) { .pbar > i { transition:none; } }
/* 实时产出面板：深底等宽，逐字打出。点一下可以立刻铺完（不用等）。 */
.log { margin-top:10px; min-height:64px; max-height:320px; overflow:auto;
       background:#0f1720; color:#cfe3f2; padding:12px 14px; font-size:12px;
       line-height:1.6; border-radius:8px; white-space:pre-wrap; cursor:text;
       font-family:ui-monospace,Consolas,"Cascadia Mono",monospace;
       border:1px solid rgba(255,255,255,.06); }
button { font:inherit; padding:9px 16px; border-radius:8px; cursor:pointer;
         border:1px solid var(--accent); background:var(--accent); color:#fff; }
button:hover:not(:disabled) { filter:brightness(1.08); }
button:disabled { opacity:.45; cursor:default; }
a.btn { display:inline-block; padding:9px 16px; margin:0 10px 8px 0; border-radius:8px;
        border:1px solid var(--accent); background:var(--accent); color:#fff;
        text-decoration:none; font-size:14px; }
a.btn:hover { text-decoration:none; filter:brightness(1.08); }
a.btn.ghost { background:transparent; color:var(--accent); }
/* 深色下 --accent 是**浅**蓝，白字压上去看不清 —— 换成深色字 */
@media (prefers-color-scheme: dark) {
  button, a.btn { color:#0b1a24; } }
footer { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;
         margin-top:44px; padding-top:16px; border-top:1px solid var(--line);
         color:var(--mut); font-size:12.5px; }
.muted { color:var(--mut); font-size:13px; }
"""

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BatteryLab 数据控制台</title><style>{css}</style></head>
<body>
<div class="wm" aria-hidden="true"></div>
<div class="wrap">{body}
<footer>
  <span>河海大学 · 电池实验室</span>
  <span>BatteryLab 控制台 v{ver} ｜ 只读界面</span>
</footer>
</div></body></html>
"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def brand(title: str = "BatteryLab 数据控制台",
          sub: str = "河海大学 · 电池实验室") -> str:
    """页头：校徽 + 标题。校徽走 CSS 变量 --seal（整个页面只内嵌一份），不发额外请求。"""
    return ('<div class="brand">'
            '<div class="seal" role="img" aria-label="河海大学校徽"></div>'
            f'<div><h1>{esc(title)}</h1><div class="sub">{esc(sub)}</div></div></div>')


def page(title: str, body: str) -> bytes:
    return PAGE.format(css=CSS.replace("__SEAL__", seal_uri()),
                       body=body, ver=VERSION).encode("utf-8")


def live_progress(root: Path) -> list[dict]:
    """读 live 区的水位文件，给「进行中的测试」栏目用。

    live 区由采集器每日增量填充（跑着的测试也能拿到数据，不用等结束）。
    通道和测试号以**路径**为准（权威），progress 内容为辅；
    坏了的条目跳过，不让一个坏文件拖垮整个页面。
    """
    live_root = root / "live"
    if not live_root.is_dir():
        return []
    out: list[dict] = []
    for pj in sorted(live_root.glob("channel=*/testid=*/progress.json")):
        ch = pj.parts[-3].split("=", 1)[1]
        tid = pj.parts[-2].split("=", 1)[1]
        try:
            prog = json.loads(pj.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue        # 坏条目跳过 —— 显示一行全是"—"的假进度比不显示更糟
        if not isinstance(prog, dict):
            continue
        out.append({
            "channel": ch, "testid": tid,
            "rows": prog.get("rows_collected"),
            "total": prog.get("total_on_device"),
            "updated_at": prog.get("updated_at"),
        })
    return out


def render_home(root: Path, share_dir: Path | None, msg: str = "") -> bytes:
    recs = load_manifest(root)
    st = load_status(root)
    age_txt, age_cls = latest_status_age(st)

    rows = []
    n_flagged = 0
    for r in recs:
        soh = resolve_soh(root, r)
        ret = soh.get("capacity_retention_pct")
        # 首圈是化成循环的，保持率本身不可信 —— 在列表上就标出来，
        # 别让人点进详情才发现数字要打问号。
        if retention_caveat(soh):
            n_flagged += 1
            ret_cell = (f"<span class='warn' title='首圈疑似化成循环，此值不可直接使用'>"
                        f"⚠️ {esc(ret)} %</span>") if ret is not None else "—"
        else:
            ret_cell = f"{esc(ret)} %" if ret is not None else "—"
        chan, tid = esc(r.get("channel")), esc(r.get("testid"))
        rows.append(
            "<tr>"
            f"<td>{chan}</td>"
            f"<td>{tid}</td>"
            f"<td><code>{esc(r.get('barcode') or '（空）')}</code></td>"
            f"<td>{esc(soh.get('cycle_count', '—'))}</td>"
            f"<td>{ret_cell}</td>"
            f"<td>{esc(r.get('detail_count', 0))}</td>"
            f"<td>{'✅' if r.get('upload_complete') else '⚠️ 未传完'}</td>"
            f"<td>{esc(r.get('collected_at', ''))}</td>"
            f"<td><a href='/dataset?channel={chan}&testid={tid}'>查看</a></td>"
            "</tr>")

    table = ("<table><tr><th>通道</th><th>测试号</th><th>电池条码</th><th>循环数</th>"
             "<th>容量保持率</th><th>明细行数</th><th>数据完整</th><th>采集时间</th>"
             "<th></th></tr>" + "".join(rows) + "</table>") if rows else \
            "<p class='muted'>还没有采集到任何数据集。等测试跑完一轮，或检查采集服务是否在运行。</p>"

    if n_flagged:
        table += (
            "<p class='muted'>⚠️ 有 "
            f"<b>{n_flagged}</b> 个数据集的容量保持率带问号："
            "它的第 1 圈疑似<b>化成（预充）循环</b>，拿第 1 圈当基准算出的保持率会明显偏低。"
            "点开「查看」看具体说明。</p>")

    n_incomplete = sum(1 for r in recs if not r.get("upload_complete"))
    incomplete_cls = "bad" if n_incomplete else "ok"

    # ---- 进行中的测试（每日增量，live 区）----
    # 增量由采集器每天自动跑；这里只读水位文件展示进度。
    live = live_progress(root)
    working_n = sum(1 for v in (st.get("statuses") or {}).values() if v == "working")
    if live:
        live_rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                esc(r["channel"]), esc(r["testid"]),
                esc(r["rows"] if r["rows"] is not None else "—"),
                esc(r["total"] if r["total"] is not None else "—"),
                esc(r["updated_at"] or "—"))
            for r in live)
        live_html = (
            "<h2>进行中的测试（每日增量）</h2>"
            "<div class='sub'>采集器每天自动从<b>跑着的测试</b>里追加一次明细，"
            "存放在 live/ 区 —— <b>与正式数据集相互独立</b>，"
            "测试结束后仍会做一次完整采集作为正式数据。</div>"
            "<table><tr><th>通道</th><th>测试号</th><th>已拉行数</th>"
            "<th>设备端总数</th><th>最近更新</th></tr>" + live_rows + "</table>")
    elif working_n:
        live_html = (
            "<h2>进行中的测试（每日增量）</h2>"
            f"<p class='muted'>有 {working_n} 个通道正在跑测试，"
            "但还没有增量数据 —— 等采集器的下一次节拍"
            "（每天 02:00），或在盒子上手动跑 "
            "<code>python3 collector.py --incremental-now</code>。</p>")
    else:
        live_html = ""

    body = f"""
{brand()}
<div class="sub">数据目录 <code>{esc(root)}</code> ｜ 控制台 v{VERSION}</div>

<div class="banner">
  <b>这是只读界面。</b>它只读取已采集的数据并展示结果，
  <b>不会对测试机发任何命令</b> —— 没有"启动/停止测试"这类按钮，是刻意不做。
</div>

{"<div class='banner'>" + esc(msg) + "</div>" if msg else ""}

<h2>采集服务状态</h2>
<div class="cards">
  <div class="card"><div class="k">心跳</div>
    <div class="v {age_cls}">{esc(age_txt)}</div></div>
  <div class="card"><div class="k">正在测试的通道</div>
    <div class="v">{esc(st.get('working_now', '—'))}</div></div>
  <div class="card"><div class="k">已采集数据集</div>
    <div class="v">{esc(st.get('collected_datasets', len(recs)))}</div></div>
  <div class="card"><div class="k">数据不完整</div>
    <div class="v {incomplete_cls}">{n_incomplete}</div></div>
</div>
<div class="sub" style="margin-top:10px">
  服务版本 {esc(st.get('service', '—'))} ｜ 已运行 {esc(st.get('uptime_seconds', '—'))} 秒
  ｜ 监听通道 {esc(st.get('watched_channels', '—'))} 个
</div>

{report_card(recs)}
<h2>数据集（{len(recs)} 个）</h2>
{table}

{live_html}
<h2>给 AI 助手的数据</h2>
<div class="sub">
  共享目录 <code>{esc(share_dir or '（未配置）')}</code>。
  重新生成后，AI 助手就能读到最新的数据集摘要。
</div>
<form method="post" action="/refresh">
  <button type="submit" {"disabled" if not share_dir else ""}>刷新共享数据</button>
  <span class="muted" style="margin-left:10px">
    只读 data/ 重新生成摘要，不重启服务、不碰测试机
  </span>
</form>
"""
    return page("BatteryLab 数据控制台", body)


def render_dataset(root: Path, rec: dict) -> bytes:
    soh = resolve_soh(root, rec)
    ddir = find_dataset_dirs(root, rec)

    outlier = retention_caveat(soh)

    # 逐圈数据**先算一次**：下面指标表那两颗 ⚠ 要不要显示，取决于 sp_bad / ce_bad。
    # （踩过：在指标表里用了 ce_bad，而它要到后面的图表段才赋值 —— 详情页直接 500，
    #   而且只有真去点页面才看得出来。静态检查不报，测试不跑也发现不了。）
    sp_bad = None       # 比容量超出合理量级时的最大值（质量假设用错了）
    ce_bad = None       # 整段测试的放电/充电容量比最大值（低于 50% = 电芯没在放电）
    cs: list[dict] = []
    if ddir and (ddir / "steps.csv").exists():
        try:
            cs = cycles_of(load_steps(ddir / "steps.csv"), mass_g=MASS_G)
        except Exception:
            cs = []
        if cs:
            sp_bad = sp_implausible(cs)
            ce_bad = ce_never_healthy(cs)

    metrics = []
    for key, label, unit in KEY_METRICS:
        v = soh.get(key)
        if v is not None:
            flag = (" ⚠️" if (outlier and ce_bad is None
                             and key == "capacity_retention_pct") else "")
            metrics.append(f"<tr><td>{esc(label)}</td>"
                           f"<td><b>{esc(v)}{unit}</b>{flag}</td></tr>")

    steps = steps_preview(ddir) if ddir else []
    if steps:
        rows_html = []
        for i, r in enumerate(steps, 1):
            st = (r.get("steptype") or "").strip().lower()
            v1, v2 = r.get("startvolt"), r.get("endvolt")
            try:
                vr = f"{float(v1):.3f} → {float(v2):.3f}"
            except (TypeError, ValueError):
                vr = "—"
            rows_html.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{esc(STEPTYPE_NAMES.get(st, st) or '—')}"
                f"<span class='muted'>（{esc(st)}）</span></td>"
                f"<td>{esc(human_dur(r.get('steptime')))}</td>"
                f"<td>{esc(vr)}</td>"
                f"<td>{esc(human_qty(r.get('startcurr'), 'A', sign=True))}</td>"
                f"<td>{esc(human_qty(r.get('cap'), 'Ah'))}</td>"
                f"<td>{esc(human_qty(r.get('eng'), 'Wh'))}</td>"
                "</tr>")
        total = rec.get("step_count") or len(steps)
        steps_html = (
            "<table><tr><th>#</th><th>工步类型</th><th>时长</th>"
            "<th>电压区间 (V)</th><th>电流</th><th>容量</th><th>能量</th></tr>"
            + "".join(rows_html) + "</table>"
            f"<p class='muted'>共 {total} 个工步，这里只列前 {len(steps)} 个。"
            "原始数据在 <code>steps.csv</code>（单位 A / Ah / Wh），"
            "本表已换算成 mA / mAh / mWh 方便阅读。</p>")
    else:
        steps_html = "<p class='muted'>找不到 steps.csv。</p>"

    files = []
    if ddir:
        for name in ("steps.csv", "detail.csv", "meta.json"):
            f = ddir / name
            if f.exists():
                files.append(f"<tr><td><code>{name}</code></td><td>{human_size(f)}</td></tr>")

    avail = soh.get("available")
    soh_html = ("<table><tr><th>指标</th><th>值</th></tr>" + "".join(metrics) + "</table>") \
        if avail and metrics else \
        f"<p class='muted'>算不出 SOH 指标：{esc(soh.get('reason', '原因未知'))}</p>"

    # 首圈是化成循环时，容量保持率会严重偏低 —— 必须写在数字旁边，
    # 不能只放在共享目录的 README 里（看网页的人不会去翻那个）。
    if outlier and ce_bad is None:
        soh_html += (
            "<div class='banner warn'>"
            f"<b>⚠️ 上面的「容量保持率」要打个问号。</b><br>"
            f"第 1 圈的放电容量是后续圈中位数的 <b>{outlier:.1f} 倍</b>，"
            "这通常意味着<b>第 1 圈是化成（预充）循环</b>，测试条件与后续不同。"
            "而「容量保持率 = 末圈 ÷ 首圈」是<b>拿第 1 圈当基准</b>算的 —— "
            "基准偏大，算出来的值就会明显偏低。"
            "<br><b>不要直接把它当 SOH 用。</b>请先确认第 1 圈是不是化成循环；"
            "如果是，应改用第一个正常循环作基准，或直接看下面曲线的形状。"
            "<br><span class='muted'>基准该用第 1 圈还是第一个正常循环 —— "
            "这一条<b>尚未与电池协会确认</b>，两种算法都还没定稿。</span>"
            "</div>")

    # ---------- 图表数据 ----------
    # cs / sp_bad / ce_bad 已在函数开头算好，这里**不要**再重置成 None
    # （重置会把上面算好的值清掉，指标表那两颗 ⚠ 就永远显示不出来）。
    # 这里只从 cs 派生画图要用的序列。
    chart_html = ""
    if ddir:
        if cs:
            cap = [[r["cycle"], round(r["dis_sp"], 6)] for r in cs]
            ce = [[r["cycle"], (round(r["ce"], 2) if r["ce"] is not None else None)]
                  for r in cs]
            dcir = [[r["cycle"], round(r["dcir_mohm"], 1)]
                    for r in cs if r.get("dcir_mohm")]
            opt_main = {
                "tooltip": {"trigger": "axis"},
                "legend": {"data": ["放电比容量", "库仑效率"], "top": 6, "right": 10},
                "grid": {"left": 76, "right": 92, "top": 44, "bottom": 86},
                # minInterval=1：圈号只能是整数。否则**只有 1 圈**时 ECharts 会按浮点分刻度，
                # 轴上出现 0.2 / 0.4 / 0.6 圈这种没意义的标签（实测就是这么显示的）。
                "xAxis": {"type": "value", "name": "循环号", "minInterval": 1,
                          "nameLocation": "middle", "nameGap": 32},
                "yAxis": [
                    {"type": "value", "name": "放电比容量 (mAh/g)",
                     "nameLocation": "middle", "nameGap": 56,
                     "splitLine": {"lineStyle": {"color": "#eeeeee"}}},
                    {"type": "value", "name": "库仑效率 (%)",
                     "nameLocation": "middle", "nameGap": 56, "min": 0, "max": 110},
                ],
                "series": [
                    {"name": "放电比容量", "type": "line", "data": cap,
                     "yAxisIndex": 0, "symbol": "circle", "symbolSize": 5,
                     "showSymbol": len(cs) <= 120,
                     "lineStyle": {"width": 2, "color": "#d62728"},
                     "itemStyle": {"color": "#d62728"}},
                    {"name": "库仑效率", "type": "line", "data": ce,
                     "yAxisIndex": 1, "symbol": "rect", "symbolSize": 5,
                     "showSymbol": len(cs) <= 120,
                     "lineStyle": {"width": 1.5, "color": "#1f77b4"},
                     "itemStyle": {"color": "#1f77b4"}},
                ],
                "backgroundColor": "transparent",
                # 右上角一个下载小图标 —— 存 PNG 用（浏览器里直接生成，不需要服务端）
                "toolbox": {"right": 10, "top": 4, "feature": {
                    "saveAsImage": {"name": f"{rec.get('channel')}_{rec.get('testid')}_容量与效率",
                                    "pixelRatio": 2, "backgroundColor": "#fff"}}},
                "dataZoom": [{"type": "inside"},
                             {"type": "slider", "height": 18, "bottom": 8}],
                "animation": False,
            }
            opt_dcir = {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 88, "right": 30, "top": 34, "bottom": 68},
                # minInterval=1：圈号只能是整数。否则**只有 1 圈**时 ECharts 会按浮点分刻度，
                # 轴上出现 0.2 / 0.4 / 0.6 圈这种没意义的标签（实测就是这么显示的）。
                "xAxis": {"type": "value", "name": "循环号", "minInterval": 1,
                          "nameLocation": "middle", "nameGap": 30},
                "yAxis": {"type": "value", "name": "DCIR (mΩ)",
                          "nameLocation": "middle", "nameGap": 66,
                          "splitLine": {"lineStyle": {"color": "#eeeeee"}}},
                "backgroundColor": "transparent",
                "toolbox": {"right": 10, "top": 2, "feature": {
                    "saveAsImage": {"name": f"{rec.get('channel')}_{rec.get('testid')}_DCIR",
                                    "pixelRatio": 2, "backgroundColor": "#fff"}}},
                "series": [{"name": "DCIR", "type": "line", "data": dcir,
                            "showSymbol": False,
                            "lineStyle": {"width": 1.8, "color": "#2ca02c"},
                            "itemStyle": {"color": "#2ca02c"}}],
                "animation": False,
            }
            chart_html = f"""
<h2>图表　<span class="muted">（可悬停看数值、拖动下方滑块缩放）</span></h2>"""
            if ce_bad is not None:
                chart_html += (
                    "<div class='banner warn'>"
                    f"<b>⚠️ 这颗电芯本身有问题（已确认），放电一直出不来。</b>"
                    f"整段测试的放电/充电容量比最高只有 <b>{ce_bad:.1f} %</b>，"
                    "下面那条红线的放电容量几乎贴着 0。"
                    "<br>这种情况下的<b>「容量保持率」没有意义</b>"
                    "—— 0.88 % 和 100 % 这两个数都不能用来评价这颗电芯。"
                    "<br><span class='muted'>曲线形状是真实的：它老实地反映了"
                    "「这颗电芯没在工作」。报告里应据此排除它，而不是报一个保持率。</span>"
                    "</div>")
            if sp_bad is not None:
                chart_html += (
                    "<div class='banner warn'>"
                    f"<b>⚠️ 纵轴的「比容量」不可用。</b>"
                    f"它按<b>活性物质 {MASS_G * 1000:g} mg</b> 换算，"
                    f"本数据集算出的最大比容量是 <b>{sp_bad:.0f} mAh/g</b> —— "
                    "超过常见锂电正极的量级（一般 ≤ 300 mAh/g），"
                    "说明这个质量<b>不是本测试的</b>。"
                    "<br>质量是<b>每个测试一份</b>的信息，而采集到的数据里没有这个字段，"
                    "所以只能用别的样品的值去套 —— 这一条<b>需要从测试记录里补</b>。"
                    "<br><span class='muted'>看待这张图时请只看曲线<b>形状</b>和"
                    "库仑效率，不要引用比容量的绝对数值。</span>"
                    "</div>")
            chart_html += f"""
<div id="ch-main" style="height:470px"></div>
<div id="ch-dcir" style="height:250px;margin-top:14px"></div>
<script src="{ECHARTS_ROUTE}"></script>
<script>
// 图表主题跟着页面走 —— 页面支持暗色，图表也得跟着切，否则暗色页面上会贴两块白方块
var ECH_DARK = window.matchMedia('(prefers-color-scheme: dark)').matches;
var ECH_THEME = ECH_DARK ? 'dark' : null;
echarts.init(document.getElementById('ch-main'), ECH_THEME, {{renderer:'svg'}})
       .setOption({json.dumps(opt_main, ensure_ascii=False)});
echarts.init(document.getElementById('ch-dcir'), ECH_THEME, {{renderer:'svg'}})
       .setOption({json.dumps(opt_dcir, ensure_ascii=False)});
</script>
<p class="muted">⚠️ DCIR 那一栏的**单位存疑**：协议标称 mΩ，但实测值在 10⁶ 量级、
按 mΩ 算相当于上万欧姆，明显不合理。详见共享目录 README 的字段字典。</p>
"""
        else:
            chart_html = '<h2>图表</h2><p class="muted">这个数据集算不出有效的逐圈数据，画不了图。</p>'

    band = soh.get("cycle_range") or ["—", "—"]
    body = f"""
{brand("数据集详情", "河海大学 · 电池实验室 ｜ 单颗电芯的指标、曲线与明细")}
<div class="sub">
  <a href="/">← 返回列表</a> ｜ 通道 <code>{esc(rec.get('channel'))}</code>
  ｜ 测试号 <b>{esc(rec.get('testid'))}</b>
</div>

<h2>基本信息</h2>
<table>
  <tr><td>电池条码</td><td><code>{esc(rec.get('barcode') or '（空）')}</code></td></tr>
  <tr><td>设备</td><td>{esc(rec.get('devtype_name'))}（设备号 {esc(rec.get('devid'))}）</td></tr>
  <tr><td>采集时间</td><td>{esc(rec.get('collected_at'))}</td></tr>
  <tr><td>数据完整性</td><td>{"✅ 客户端确认已传完" if rec.get("upload_complete") else "⚠️ <b>未传完</b>，数据可能不完整"}
      （明细 {esc(rec.get('detail_count', 0))} 条，工步 {esc(rec.get('step_count', 0))} 条）</td></tr>
  <tr><td>解析器</td><td>{esc(rec.get('parser_version'))}</td></tr>
</table>

<h2>关键指标</h2>
{soh_html}
<div class="sub">循环数范围 {esc(band[0])} → {esc(band[1])}。
  口径见共享目录的 README.md，<b>不要用别的口径重算</b>。</div>

{chart_html}

<h2>这个测试做了什么</h2>
{steps_html}

<h2>导出　<span class="muted">（点一下就下载，不用敲命令）</span></h2>
<p>
  <a class="btn" href="/export?channel={esc(rec.get('channel'))}&testid={esc(rec.get('testid'))}&what=svg">下载图片（SVG，报告用）</a>
  <a class="btn ghost" href="/export?channel={esc(rec.get('channel'))}&testid={esc(rec.get('testid'))}&what=csv">下载逐圈数据（CSV）</a>
</p>
<p class="muted">想存 PNG：把鼠标移到图右上角，点那个 <b>⬇</b> 图标（图表自带的另存为图片）。
SVG 是矢量图，放大不失真，推荐嵌进 Word 报告；CSV 是本页那些逐圈指标，
可以直接拿去当报告附表。两张图都用和本页同一套口径算的，<b>不会出现"网页和文件对不上"</b>。</p>

<h2>数据文件</h2>
{"<table><tr><th>文件</th><th>大小</th></tr>" + "".join(files) + "</table>" if files else "<p class='muted'>找不到数据集目录。</p>"}
"""
    return page("数据集详情", body)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"BatteryLabConsole/{VERSION}"
    data_dir: Path
    share_dir: Path | None

    def log_message(self, fmt, *args):  # 少刷屏
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, body: bytes, code: int = 200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_echarts(self):
        """提供本地 ECharts 库 —— 这样打开页面的人**不需要外网**。"""
        if not ECHARTS_FILE.exists():
            self._send(b"/* echarts.min.js not found */", 404,
                       "application/javascript; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(ECHARTS_FILE.stat().st_size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(ECHARTS_FILE.read_bytes())

    def _download(self, data: bytes, filename: str, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # filename* 带 UTF-8 —— 中文文件名要这样传，否则浏览器存成乱码
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _export(self, q: dict):
        """导出图片（SVG）或逐圈数据（CSV）—— **和网页同一套口径**，现算。

        这一步就是把"出图"并进网页：之前要另外跑 figures.py，
        现在点按钮就行。SVG 用 figures.CycleChart 的排版，保证和网页/报告一致。
        """
        channel = q.get("channel", "")
        testid = q.get("testid", "")
        what = q.get("what", "svg")
        rec = next((r for r in load_manifest(self.data_dir)
                    if str(r.get("channel")) == channel
                    and str(r.get("testid")) == testid), None)
        if rec is None:
            self._send(page("没找到", "<h1>没有这个数据集</h1>"), 404)
            return
        ddir = find_dataset_dirs(self.data_dir, rec)
        sp = (ddir / "steps.csv") if ddir else None
        if not sp or not sp.exists():
            self._send(page("没找到", "<h1>这个数据集没有 steps.csv</h1>"), 404)
            return
        try:
            cs = cycles_of(load_steps(sp), mass_g=MASS_G)
        except Exception as e:
            self._send(page("出错", f"<h1>算不出逐圈数据</h1><pre>{esc(e)}</pre>"), 500)
            return
        if not cs:
            self._send(page("没找到", "<h1>算不出逐圈数据（可能没有完整的充放电对）</h1>"), 404)
            return
        sp_bad = sp_implausible(cs)
        ce_bad = ce_never_healthy(cs)

        base = f"{channel}_test{testid}"
        # 导出的文件会**离开网页**（贴进 Word、发给别人），所以警告必须跟着文件走：
        # SVG 写进图里的说明框，CSV 写进表头注释。页面上的提示救不了别处的读者。
        outlier = retention_caveat(resolve_soh(self.data_dir, rec))
        caveat: list[str] = []
        if outlier:
            caveat = [f"⚠ 第 1 圈疑似化成（预充）循环（容量是后续圈的 {outlier:.0f} 倍）",
                      "⚠ 以它为基准算出的容量保持率会明显偏低",
                      "⚠ 不要直接把它当 SOH 用"]

        if what == "csv":
            buf = io.StringIO()
            buf.write(f"# 通道 {channel} / 测试 {testid} ｜ 逐圈指标 ｜ "
                      f"导出时间 {datetime.now().isoformat(timespec='seconds')}\n")
            buf.write("# 口径：容量/能量按圈把工步层的累计值求和；本表单位已换算为 mAh / mWh；"
                      "充电为正、放电为负。\n")
            buf.write("# 容量保持率 = 本圈放电容量 ÷ 第 1 圈放电容量 × 100。\n")
            buf.write(f"# 比容量 = 容量 ÷ 活性物质质量。质量取 **{MASS_G * 1000:g} mg**"
                      "——这是**某个样品的值，不是本测试实测的**，采集数据里没有质量字段。\n")
            if sp_bad is not None:
                buf.write(f"# ⚠ 本数据集最大比容量 {sp_bad:.0f} mAh/g 超过常见锂电正极量级"
                          "（≤300 mAh/g 左右），说明上面的质量不适用于本测试。\n")
                buf.write("# ⚠ 比容量两列**不要引用**；容量看 mAh 两列，趋势看形状。\n")
            if ce_bad is not None:
                buf.write(f"# ⚠ 整段测试最高库仑效率只有 {ce_bad:.1f}% —— "
                          "这颗电芯本身有问题（已确认），放电一直出不来。\n")
                buf.write("# ⚠ 此时「容量保持率」无论用哪一圈当基准都没有意义 —— "
                          "不要用它评价电芯，报告里应排除这一颗。\n")
            else:
                # 电芯本身有问题的数据集**不叠加**"换个基准就好"那条 ——
                # 后者会让人以为"改用第 2 圈当基准就有 100%，电池是好的"，自相矛盾。
                for line in caveat:
                    buf.write("# " + line + "\n")
            buf.write("# 带 # 的行是说明，不是数据；用 Excel 直接打开即可，"
                      "程序读取时请跳过（pandas 用 comment='#'）。\n")
            w = csv.writer(buf)
            w.writerow(["循环号", "充电容量(mAh)", "放电容量(mAh)",
                        "充电比容量(mAh/g)", "放电比容量(mAh/g)",
                        "库仑效率(%)", "能量效率(%)", "DCIR(mΩ)",
                        "容量保持率(% 基准=第1圈)"])
            for r in cs:
                w.writerow([
                    r["cycle"], f"{r['chg']:.6g}", f"{r['dis']:.6g}",
                    f"{r['chg_sp']:.6g}", f"{r['dis_sp']:.6g}",
                    "" if r["ce"] is None else f"{r['ce']:.4g}",
                    "" if r["ee"] is None else f"{r['ee']:.4g}",
                    "" if r["dcir_mohm"] is None else f"{r['dcir_mohm']:.6g}",
                    f"{r['retention']:.4g}" if r["retention"] is not None else "",
                ])
            # BOM 开头，Excel 打开不乱码
            self._download(("﻿" + buf.getvalue()).encode("utf-8"),
                           f"{base}_逐圈指标.csv", "text/csv; charset=utf-8")
            return

        # SVG：用 figures 的排版（和网页、和之前手动跑的效果一致）
        fig = CycleChart(
            f"通道 {channel} / 测试 {testid}",
            "放电比容量 (mAh g⁻¹)", "库仑效率 (%)")
        xs = [r["cycle"] for r in cs]
        fig.add_right(xs, [r["ce"] for r in cs], "库仑效率")
        fig.add_left(xs, [r["dis_sp"] for r in cs], "放电比容量")
        # 说明一律放**图下方**（set_caption），不放图里 ——
        # 7 行说明压在图上会把前几十圈的曲线全盖住，而那正是
        # 「首圈是化成循环」这件事最该被看见的地方（实测踩过）。
        cap = [f"Neware BTS85　通道 {channel}",
               f"比容量按活性物质 {MASS_G * 1000:g} mg 换算（假设值，非本测试实测）"
               "　0.1C 化成 3 圈 → 1C 老化　扣式电池  截止 2.5 / 0.5 V  25 ℃"]
        if sp_bad is not None:
            cap.append(f"⚠ 本图最大比容量 {sp_bad:.0f} mAh/g 超出常见正极量级，"
                       "质量假设不适用 —— 只看曲线形状，不要引用比容量的数值")
        if ce_bad is not None:
            cap.append(f"⚠ 整段测试最高库仑效率只有 {ce_bad:.1f}% —— "
                       "这颗电芯本身有问题（已确认），放电一直出不来；"
                       "容量保持率无论用哪一圈当基准都没有意义")
        else:
            # 电芯本身有问题的数据集**不叠加**"换个基准就好"那条 ——
            # 后者会让人以为"改用第 2 圈当基准就有 100%，电池是好的"，自相矛盾。
            cap += caveat
        fig.set_caption(cap)
        self._download(fig.svg().encode("utf-8"), f"{base}_容量与效率.svg",
                       "image/svg+xml; charset=utf-8")

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   code, "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, {}
        if u.query:
            for kv in u.query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = unquote(v)

        if path == "/":
            self._send(render_home(self.data_dir, self.share_dir))
        elif path == ECHARTS_ROUTE:
            self._serve_echarts()
        elif path == "/health":
            self._json({"ok": True, "version": VERSION})
        elif path == "/api/overview":
            self._json({"status": load_status(self.data_dir),
                        "datasets": load_manifest(self.data_dir)})
        elif path == "/export":
            self._export(q)
        elif path == "/dataset":
            self._show_dataset(q.get("channel", ""), q.get("testid", ""))
        elif path == "/report/status":
            self._json(report_status(self.data_dir, self.share_dir, int(q.get("since") or 0)))
        elif path == "/report/file":
            # ★ 四个通道都要转发 —— 早先这里只传 a/b，服务端拼出的文件名少两段，
            #   于是链接明明带了 c/d 却报 404（查了半天才发现是路由把它们吃了）。
            self._serve_report(q.get("a", ""), q.get("b", ""),
                               q.get("c", ""), q.get("d", ""),
                               dl=bool(q.get("dl")))
        else:
            self._send(page("没找到", "<h1>404</h1><p><a href='/'>回到首页</a></p>"), 404)

    def _show_dataset(self, channel: str, testid: str):
        rec = next((r for r in load_manifest(self.data_dir)
                    if str(r.get("channel")) == channel
                    and str(r.get("testid")) == testid), None)
        if rec is None:
            self._send(page("没找到", "<h1>没有这个数据集</h1>"
                                       "<p><a href='/'>回到首页</a></p>"), 404)
            return
        self._send(render_dataset(self.data_dir, rec))

    def _serve_report(self, a: str, b: str, c: str = "", d: str = "",
                      dl: bool = False):
        """把生成好的报告 HTML 发给浏览器。

        dl=False → 直接在浏览器里看（「打开报告」）；dl=True → 存成文件（「下载报告」）。
        """
        if not self.share_dir or not a or not b:
            self._send(page("没找到", "<h1>还没有报告</h1>"), 404)
            return
        _nm = "_vs_".join([x for x in (a, b, c, d) if x])
        task = self.share_dir / "报告任务"
        f = task / ("SOH分析报告_" + _nm + ".html")
        # ★ 找不到就发**最新那份** —— 同一时刻只有一个任务、一份报告。
        #   不要因为"调用方少传一个参数"就报 404（实测踩过：链接只带了前两颗）。
        if not f.exists():
            _cands = sorted(task.glob("SOH分析报告*.html"),
                            key=lambda x: x.stat().st_mtime, reverse=True)
            f = _cands[0] if _cands else None
        if f is None or not f.exists():
            self._send(page("没找到", "<h1>还没有生成过报告</h1>"), 404)
            return
        try:
            data = f.read_bytes()
        except OSError as exc:
            self._send(page("读不了", f"<h1>{exc}</h1>"), 500)
            return
        if dl:
            self._download(data, f.name, "text/html; charset=utf-8")
        else:
            self._send(data, 200, "text/html; charset=utf-8")

    def do_POST(self):
        u0 = urlparse(self.path)
        q0 = {}
        if u0.query:
            for kv in u0.query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q0[k] = unquote(v)
        if u0.path == "/report/start":
            ok, msg = report_start(self.data_dir, self.share_dir, q0.get("a", "").strip(),
                                   q0.get("b", "").strip(),
                                   q0.get("source", "auto").strip() or "auto",
                                   q0.get("c", "").strip(), q0.get("d", "").strip())
            self._json({"ok": ok, "msg": msg})
            return
        if urlparse(self.path).path != "/refresh":
            self._send(page("没找到", "<h1>404</h1>"), 404)
            return
        if not self.share_dir:
            self._send(render_home(self.data_dir, None,
                                   "没有配置共享目录，无法刷新。"
                                   "请用 --share-dir 指定后重启。"), 400)
            return
        ok, out = run_make_share(self.data_dir, self.share_dir)
        head = "共享数据已刷新。" if ok else "刷新失败，下面是原始输出："
        self._send(render_home(
            self.data_dir, self.share_dir,
            f"{head}\n\n{out.strip()[:1500]}"))


def run_make_share(data_dir: Path, share_dir: Path) -> tuple[bool, str]:
    """调 make_share.py 重新生成共享目录。

    --force：共享目录本来就是派生产物，刷新就是要覆盖它。
    """
    script = Path(__file__).resolve().parent / "make_share.py"
    if not script.exists():
        return False, f"找不到 {script}"
    cmd = [sys.executable, str(script),
           "--data-dir", str(data_dir), "--share-dir", str(share_dir), "--force"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "执行超时（300 秒）"
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")



# ---------------------------------------------------------------------------
# 首页上的「生成报告」卡片（按钮 + 进度 + 实时产出）
# ★ 纯字符串拼接，不走 f-string —— 里面的 JS 有大量 {}，走 f-string 会被当占位符。
# ---------------------------------------------------------------------------
REPORT_CARD_TMPL = """
<h2>生成分析报告</h2>
<div class="panel">
  <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
    <label>长循环·实验样 <select id="r_ce"><!--SEL_ce--></select></label>
    <label>长循环·对比样 <select id="r_cc"><!--SEL_cc--></select></label>
    <label>倍率·实验样 <select id="r_re"><!--SEL_re--></select></label>
    <label>倍率·对比样 <select id="r_rc"><!--SEL_rc--></select></label>
    <button id="rgo" onclick="startReport()">生成报告</button>
  </div>
  <div id="rmsg" style="color:var(--mut);font-size:13px;margin-top:8px">四个通道按「实验样 / 对比样 × 长循环 / 倍率」对应，默认已填好。</div>
  <!-- 总进度条：步骤标签是"到哪一步了"，这条是"整体走了多少" -->
  <div class="pwrap">
    <div class="pbar" id="rbar" role="progressbar" aria-valuemin="0" aria-valuemax="100"
         aria-valuenow="0" aria-label="报告生成进度"><i id="rbarfill"></i></div>
    <span class="ppct" id="rpct">0%</span>
  </div>
  <div id="rstages" style="margin-top:10px"></div>
  <pre id="rlog" class="log">点「生成报告」后，这里会实时显示程序的输出（逐字打出；点一下可立刻铺完）。</pre>
  <div id="rlink" style="margin-top:8px;font-size:14px"></div>
</div>
<script>
var NL = String.fromCharCode(10), STAGES = [], rSince = 0, rTimer = null, rT0 = 0, rQ = {};
// ---- 实时产出：一字一字吐 ----
// 服务端是**按行**给的，而程序一跑到某一步就会一口气刷好几行 —— 直接贴到面板上就是
// "一段一段"。这里把到达的行先攒进缓冲区、再按帧慢慢放出来，看上去就是一串连续的字。
// 积压越多吐得越快，所以永远不会越落越多（最多落后一两秒）。
var rText = "", rPend = "", rTypeTimer = null, rCursorOn = false, rTickN = 0, rInstant = false;
// 进度条用：本步内已经来了多少行输出、这一步是什么时候开始的
var rStageLines = 0, rStageT0 = Date.now(), rLastStage = -1;
var REDUCED = !!(window.matchMedia
                 && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
function rRenderLog(){
  var el = document.getElementById("rlog");
  if (!el) return;
  el.textContent = rText + (rTypeTimer ? (rCursorOn ? "▌" : " ") : "");
  el.scrollTop = el.scrollHeight;
}
function rTyping(){
  var n = Math.max(2, Math.ceil(rPend.length / 45));   // 每帧吐几个字（自适应）
  rText += rPend.slice(0, n);
  rPend = rPend.slice(n);
  if (++rTickN % 15 === 0) rCursorOn = !rCursorOn;      // 光标约每半秒闪一次
  if (!rPend){ clearInterval(rTypeTimer); rTypeTimer = null; rCursorOn = false; }
  rRenderLog();
}
function rPush(lines){
  var txt = lines.map(function(l){ return l + NL; }).join("");
  rStageLines += lines.length;                 // 进度条要按"本步来了多少输出"蠕动
  if (REDUCED || rInstant){ rText += txt; rRenderLog(); return; }
  rPend += txt;
  if (!rTypeTimer) rTypeTimer = setInterval(rTyping, 33);
}
function rSkip(){                                        // 点日志 = 不等了，直接铺完
  if (!rPend) return;
  rText += rPend; rPend = "";
  if (rTypeTimer){ clearInterval(rTypeTimer); rTypeTimer = null; }
  rCursorOn = false;
  rRenderLog();
}
(function(){ var el = document.getElementById("rlog"); if (el) el.onclick = rSkip; })();
function rEsc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
// 总进度条：步骤数给"骨架"，本步内的输出量与耗时给"蠕动"。
// 为什么不能只按步骤跳：跑到某一步时会**一口气刷很多输出**，一步之内纹丝不动
// 看起来就是"有输出了还停在 0%"（用户实测反馈）。蠕动用渐近曲线 —— 有输出就一直
// 往前走，但永远越不过下一步的边界；长时间没输出（比如等模型吐字）也不会完全冻住。
function rSetBar(st){
  var total = ((st.stages || STAGES || []).length) || 1;
  var cur = Math.max(0, Math.min(total, st.stage || 0));
  var pct;
  if (st.state === "done") {
    pct = 100;                                   // 只有真跑完才给 100
  } else {
    var byOut  = 0.80 * (1 - Math.exp(-rStageLines / 25));                    // 输出驱动
    var byTime = 0.50 * (1 - Math.exp(-(Date.now() - rStageT0) / 1000 / 240)); // 时间驱动（4 分钟走一半）
    var sub = Math.min(0.92, Math.max(byOut, byTime));
    pct = Math.min(99, Math.round((cur + sub) / total * 100));
  }
  var fill = document.getElementById("rbarfill"), bar = document.getElementById("rbar");
  if (!fill || !bar) return;
  fill.style.width = pct + "%";
  fill.style.background = (st.state === "failed") ? "var(--bad)"
                        : (st.state === "done") ? "var(--ok)" : "var(--accent)";
  bar.setAttribute("aria-valuenow", String(pct));
  var t = document.getElementById("rpct");
  if (t) t.textContent = (st.state === "running")
      ? (pct + "% · 第 " + Math.min(cur + 1, total) + "/" + total + " 步")
      : (st.state === "done") ? "100% · 完成"
      : (st.state === "failed") ? (pct + "% · 中止") : (pct + "%");
}
function rPaint(st){
  var h = "";
  (STAGES || []).forEach(function(lab, i){
    var c = (i < st.stage) ? "#2e7d32" : (i === st.stage ? "#1565c0" : "#bbb");
    h += "<span style='display:inline-block;padding:3px 9px;margin:2px;border-radius:11px;"
       + "font-size:12px;color:#fff;background:" + c + "'>" + (i+1) + ". " + rEsc(lab) + "</span>";
  });
  // 换步了 → 本步的"蠕动量"清零（输出行数、起始时刻）
  if (st.stage !== rLastStage){ rLastStage = st.stage; rStageLines = 0; rStageT0 = Date.now(); }
  document.getElementById("rstages").innerHTML = h;
  if (st.lines && st.lines.length) rPush(st.lines);
  rSetBar(st);
  document.getElementById("rmsg").textContent = (st.state === "running")
      ? ("进行中　已等 " + Math.round((Date.now() - rT0) / 1000) + " 秒")
      : ("状态：" + st.state + (st.report ? "" : "（没有产出报告，看下面日志）"));
}
// 报告链接：四个通道都带上；「下载报告」多一个 &dl=1（服务端据此发 attachment 头）
function rFileUrl(){
  return "/report/file?a=" + encodeURIComponent(rQ.ce) + "&b=" + encodeURIComponent(rQ.cc)
       + "&c=" + encodeURIComponent(rQ.re) + "&d=" + encodeURIComponent(rQ.rc);
}
function rShowLinks(){
  var u = rFileUrl();
  document.getElementById("rlink").innerHTML =
    "<a href='" + u + "' target='_blank' style='font-weight:bold'>▶ 打开报告</a>"
    + "&nbsp;&nbsp;<a href='" + u + "&amp;dl=1'>⬇ 下载报告</a>";
}
function rPoll(){
  fetch("/report/status?since=" + rSince).then(function(r){ return r.json(); }).then(function(st){
    rSince = st.n || rSince;
    if (!STAGES.length) STAGES = st.stages || [];
    rPaint(st);
    if (st.state === "running"){ rTimer = setTimeout(rPoll, 1000); return; }
    clearTimeout(rTimer);
    if (st.report){
      rShowLinks();
    }
  }).catch(function(e){
    document.getElementById("rmsg").textContent = "查询状态失败：" + e;
    rTimer = setTimeout(rPoll, 3000);
  });
}
function rVal(id){ return document.getElementById(id).value; }
function startReport(){
  var m = document.getElementById("rmsg");
  rQ = {ce: rVal("r_ce"), cc: rVal("r_cc"), re: rVal("r_re"), rc: rVal("r_rc")};
  var all = [rQ.ce, rQ.cc, rQ.re, rQ.rc];
  if (all.some(function(x){ return !x; })){ m.textContent = "四个通道都要选"; return; }
  if (new Set(all).size !== 4){ m.textContent = "四个通道必须各不相同（现在有重复）"; return; }
  m.textContent = "已发送请求…";
  fetch("/report/start?a=" + encodeURIComponent(rQ.ce) + "&b=" + encodeURIComponent(rQ.cc)
        + "&c=" + encodeURIComponent(rQ.re) + "&d=" + encodeURIComponent(rQ.rc),
        {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(d){
      m.textContent = d.msg;
      if (!d.ok) return;
      rText = ""; rPend = ""; rRenderLog();          // 新的一轮，面板清空重打
      rStageLines = 0; rStageT0 = Date.now(); rLastStage = -1;
      rSetBar({state: "running", stage: 0, stages: []});   // 进度条归零
      document.getElementById("rlink").innerHTML = "";
      rSince = 0; rT0 = Date.now(); STAGES = []; rPoll();
    })
    .catch(function(e){ m.textContent = "起不来：" + e; });
}
// ★ 2026-10-09：**页面加载时自动恢复**正在跑（或刚跑完）的任务。
//   原来状态只存在 JS 内存里 —— 离开页面去看别的数据再回来，实时显示就"变回去了"。
//   日志文件是从头记的，所以 since=0 就能把整段补回来。
(function(){
  fetch("/report/status?since=0").then(function(r){ return r.json(); }).then(function(st){
    if (!st || (!st.lines || !st.lines.length)) return;
    STAGES = st.stages || [];
    rSince = st.n || 0;
    rQ = {ce: st.ch_a || "", cc: st.ch_b || "", re: st.ch_c || "", rc: st.ch_d || ""};
    rT0 = st.started_at ? Date.parse(String(st.started_at).replace(" ", "T")) : Date.now();
    rText = ""; rPend = ""; rRenderLog();
    rInstant = true;              // 恢复历史：直接铺，别把之前那几十行再"敲"一遍
    rPaint(st);
    rInstant = false;
    if (st.state === "running"){ rTimer = setTimeout(rPoll, 1000); }
    else if (st.report){
      rShowLinks();
    }
  }).catch(function(){});
})();

</script>
"""


# 每个角色下拉的默认通道（对① = 长循环：10-1 实验 / 10-2 对比；对② = 倍率：10-3 实验 / 10-4 对比）
ROLE_DEFAULTS = {
    "ce": "10-1", "cc": "10-2", "re": "10-3", "rc": "10-4",
}


def report_card(recs: list[dict]) -> str:
    """首页的生成报告卡片：四个下拉（角色固定），默认已填好对①/对②。"""
    chans = []
    for r in recs:
        c = short_channel(str(r.get("channel") or ""))
        if c and c not in chans:
            chans.append(c)
    chans.sort()
    html = REPORT_CARD_TMPL
    for role, want in ROLE_DEFAULTS.items():
        opts = "".join(
            f"<option value='{esc(c)}'{' selected' if c == want else ''}>{esc(c)}</option>"
            for c in chans)
        html = html.replace(f"<!--SEL_{role}-->", opts)
    return html


def short_channel(key: str) -> str:
    """27-188-10-1 → 10-1（报告脚本与产物名都用短名）。"""
    parts = str(key).split("-")
    return "-".join(parts[2:]) if len(parts) == 4 else str(key)


# ---------------------------------------------------------------------------
# 生成报告：起一个后台任务 + 跟读它的日志（网页上点按钮用）
#
# ★ 这里**只起进程、只读日志**，报告链路本身一个字不改。
#   进度靠日志里的阶段标记点亮 —— 那些标记是 换通道出报告.sh / 出报告.sh
#   自己打印的，所以进度显示和报告逻辑天然同步，不会"改了一边忘另一边"。
# ---------------------------------------------------------------------------
# ★ 这两个文件**不能放在 报告任务/ 里面** —— 报告程序第一步就是 rmtree 重建那个目录，
#   会把它们清掉（实测：任务在跑、日志和状态却查不到）。放数据目录外面的 logs/。
REPORT_TASK_DIR = "报告任务"
REPORT_LOG_NAME = "报告生成.log"
REPORT_STATE_NAME = "报告生成.json"

REPORT_STAGES = [
    # ★ 2026-10-11：标签说"人话"（演示用），标记串必须与流水线实际打印的一致
    #   （改动任何一边都要同步另一边 —— selftest_console ⑧ 有断言钉着步数与首步）。
    ("✓ 报告工程已同步", "环境自检 · 数据同步"),
    ("── ① 重建任务包", "重建分析任务包"),
    ("── ① 程序段按最新数据重填", "按最新数据重填程序段"),
    ("── ② AI 撰写判断类章节", "AI 撰写判断正文"),
    ("── ③④ 校验", "自动校验 · 不合格回灌"),
    ("── ⑤ 渲染", "渲染报告"),
    ("── ③ 验收闸门", "验收闸门"),
]


def report_paths(data_dir: Path) -> tuple[Path, Path]:
    logs = data_dir.parent / "logs"
    return logs / REPORT_LOG_NAME, logs / REPORT_STATE_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def report_state(data_dir: Path) -> dict:
    """读任务状态。进程已经没了、状态还写着 running → 判为已结束。"""
    _, state_p = report_paths(data_dir)
    if not state_p.exists():
        return {"state": "idle"}
    try:
        st = json.loads(state_p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"state": "idle"}
    if st.get("state") == "running":
        # ★ 优先看日志里的收尾标记（不依赖 pid —— pid 可能是僵尸，永远"活着"）
        _lp, _ = report_paths(data_dir)
        _tail = ""
        if _lp.exists():
            try:
                _tail = "".join(_lp.read_text(encoding="utf-8", errors="replace").splitlines(True)[-20:])
            except OSError:
                _tail = ""
        if "[DONE] rc=" in _tail:
            _rc = _tail.rsplit("[DONE] rc=", 1)[1].strip().split()[0]
            st["state"] = "done" if _rc == "0" else "failed"
            st["finished"] = True
            st["rc"] = _rc
        elif not _pid_alive(int(st.get("pid") or 0)):
            st["state"] = "done"      # 进程没了 → 就当结束（兜底，不看 rc）
            st["finished"] = True
        return st
    if st.get("state") == "running" and not _pid_alive(int(st.get("pid") or 0)):
        st["state"] = "done"
        st["finished"] = True
    return st


def report_start(data_dir: Path, share_dir: Path | None, ch_a: str, ch_b: str,
                 source: str = "auto", ch_c: str = "", ch_d: str = "") -> tuple[bool, str]:
    """起一次「生成报告」。★ **一次只准一个** —— 它们共用同一个输出目录。"""
    if not share_dir:
        return False, "没有配置共享目录（--share-dir），生成报告需要它。"
    script = share_dir / "换通道出报告.sh"
    if not script.exists():
        return False, f"找不到 {script}"
    st = report_state(data_dir)
    if st.get("state") == "running":
        return False, (f"已经有一个报告任务在跑（{st.get('ch_a')} vs {st.get('ch_b')}，"
                       f"开始于 {st.get('started_at')}）—— 等它跑完再点。")
    log_p, state_p = report_paths(data_dir)
    log_p.parent.mkdir(parents=True, exist_ok=True)
    # ★ PYTHONUNBUFFERED：不缓冲，否则日志会憋到最后一次性冒出来，进度就白做了
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    # 对②（倍率那对）用环境变量传下去，不动位置参数
    if ch_c:
        env["CH_C"] = ch_c
    if ch_d:
        env["CH_D"] = ch_d
    # ★ 默认图例名 = 角色名（用户 2026-10-09 拍板）。两对必须不同，否则 vs 名撞车
    env.setdefault("LABEL_A", "长循环实验样")
    env.setdefault("LABEL_B", "长循环对比样")
    env.setdefault("LABEL_C", "倍率实验样")
    env.setdefault("LABEL_D", "倍率对比样")
    with open(log_p, "w", encoding="utf-8") as fh:
        try:
            # ★ 2026-10-09：用一层 shell 包住 —— 跑完把**收尾标记**写进日志。
            #   为什么不用 pid 判断：父进程不回收子进程，子进程会变僵尸，
            #   而 os.kill(pid,0) 对僵尸**返回成功** → 状态永远卡在 running
            #   （实测：报告早跑完了、界面却一直"进行中"、还不让点第二次）。
            cmd = '"$1" "$2" "$3" "$4" ; rc=$? ; printf "\n[DONE] rc=%s\n" "$rc" >> "$5"'
            proc = subprocess.Popen(["bash", "-c", cmd, "run", str(script),
                                    ch_a, ch_b, source, str(log_p)],
                                    cwd=str(share_dir), stdout=fh,
                                    stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True)
        except OSError as exc:
            return False, f"起不来：{exc}"
    state_p.write_text(json.dumps({
        "state": "running", "pid": proc.pid, "ch_a": ch_a, "ch_b": ch_b,
        "ch_c": ch_c, "ch_d": ch_d,   # 对②（倍率那对）；报告升级成两对后就会用上
        "source": source, "started_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return True, f"已开始：{ch_a} vs {ch_b}"


def report_status(data_dir: Path, share_dir: Path | None, since: int = 0) -> dict:
    """状态 + 当前阶段 + 日志**新增**的行（网页每秒拉一次，不重复传旧行）。"""
    st = report_state(data_dir)
    lines: list[str] = []
    stage = -1
    task = None
    if True:
        log_p, _ = report_paths(data_dir)
        if log_p.exists():
            try:
                all_lines = log_p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                all_lines = []
            lines = all_lines[max(0, int(since)):]
            text = chr(10).join(all_lines)
            for i, (marker, _lab) in enumerate(REPORT_STAGES):
                if marker in text:
                    stage = i
    st["stage"] = stage
    st["stages"] = [lab for _m, lab in REPORT_STAGES]
    st["lines"] = lines
    st["n"] = int(since) + len(lines)
    if share_dir and st.get("state") in ("done", "failed"):
        a, b = st.get("ch_a"), st.get("ch_b")
        _nm = "_vs_".join([x for x in (a, b, st.get("ch_c"), st.get("ch_d")) if x])
        _p = share_dir / REPORT_TASK_DIR / ("SOH分析报告_" + _nm + ".html")
        # 与 _serve_report 的兜底保持一致：名字对不上就认最新那份，
        # 否则"报告明明出好了、按钮却不出来"（两边规则不一致就会这样）。
        if not _p.exists():
            _c = sorted((share_dir / REPORT_TASK_DIR).glob("SOH分析报告*.html"),
                        key=lambda x: x.stat().st_mtime, reverse=True)
            _p = _c[0] if _c else _p
        st["report"] = _p.exists()
    return st



# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="BatteryLab 数据控制台（只读网页界面）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="采集服务的数据目录")
    p.add_argument("--share-dir", default=None, help="给 AI 助手读的共享目录（不填则不能刷新）")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址。默认只允许本机；要让同网段访问用 0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()

    root = Path(args.data_dir).expanduser()
    share = Path(args.share_dir).expanduser() if args.share_dir else None

    if not root.exists():
        print(f"⚠️  数据目录不存在：{root}（界面会显示为空，服务照常启动）")

    Handler.data_dir = root
    Handler.share_dir = share

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"BatteryLab 数据控制台 v{VERSION}")
    print(f"  数据目录  {root}")
    print(f"  共享目录  {share or '（未配置，不能刷新）'}")
    print(f"  监听      http://{args.host}:{args.port}")
    print("  Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------------------
# 河海大学校徽（页头 + 背景水印）
#
#   由 assets/生成校徽.py 从原图生成：抠白底 → 单色蓝（取样自校徽本身 #0D71AD）
#   → 硬边两色调色板 PNG。**1024px 只有约 11 KB**（单色 + 硬边，PNG 压得动），
#   所以直接内嵌在这里 —— 不额外加文件、不加路由，也就没有"盒子上那份是不是最新的"这种问题。
#
#   ★ 下面这个常量由脚本写入，两个都不要手改；要换图就重跑 assets/生成校徽.py。
# ---------------------------------------------------------------------------
# SEAL_B64_BEGIN
_SEAL_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAABAAAAAQACAMAAABIw9uxAAADAFBMVEUNca3///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/K4QWAAAAAnRSTlP/AOW3MEoAAC0QSURBVHja7d3tkuO4rQBQ6f1f+lalKnuzM+62RIIgSB79Sna6bZkkTlv8AK7bNea6fro0zdt20ygDm1sTAAAAAHABAAAAcAVH+7dL02k6ABjFmk7TAeCYoWs4azkAGMUA0HIAMIoBoOUAcGzgnzWgNRwAjGIAaDgAGMUA0HAAMH4PGc2aDgBGMQA0HQCMYgBoOgAYwEeMZu0GAAAAQLsBwBA+aUBrNQAAAABaDQAAAIBWA4DY339Uay8AAAAA2gsAAACA9gLA2dGfEhx7hvzInnABAAAAcAEAAAAAgCtkWFcJn/UCPusujGsAAAAALgAEjq69jsqtNzXPAAAAAAAAAAAAAAAAACSNp4M20tSfpWAAAAAAAAAAAAAAAAAA1ov9MxSo0XTGOgAAAAAXAAAAAAC4ng4dh2tX2IOHAAAAAAAAAEBgpEmzsdLWewYAAAAAAAAAAAAAAABg2ehfzAI9CwAAAEDPAgAAANCzAFg8/GUt3ztPOQAAAAAAAAAAAAAAAER/5PCYEEErRXvWWyIAAAAAAAAAkDEyKn+dXuYWcjsFAAAAAAAAAAAAAAAAov/1mFDTunzzMAAAAAAAAAAAAAAA4GwA4obXuZtsCrRP3K0AAAAAAAAAAHDV3Ikr8Ed3GQAAAAAAAAAAAAAAAM4M//rH8MT9sL4DAAAAAAAAAAAAAACA6C+elU/UtzUIAwAAAAAAAAAAAAAAAPC07+MH5cYFOPvve1RLAAAAAAAAAM4DYMigCRpHlR8QKtxY5FvYEAQAAAAAAAAAAAAAAIBC+b2vja+7TjZxAAAAAAAAAAAAAAAAiP5yVabF/ch+MQ0AAAAAAAAAAAAAAACAsvWlBX62AQAAAAAAAIATwn+dYhICP95oAAAAAAAAAAAAAAAAAGCNYnLHR31Ubx0/CwAAAAAAAAAAAAAAYAVwvW0ichL2GQAAAAAAAAAAAAAAAAAArJ818uQCpa9v//DtgAAAAAAAIPoljT609AsAAAAAAAAAAAAAAAAOB0D0H7jr6+QuBwAAAAAAAAAAAAAwFoQ/9QEAAAAAAAAAAAAAAHAOAKKfAaf1PAAAAAAAiH65IQAAAAAAAAAAAAAAAAAAAEwZANjxBwAAAHABAAAAcAEAAAc9A5p8mGXAmcdAAAAAAAAAAAAAAADOSAvzHIC5Ny14Rw4FO0EBAAAAAMD3/zq1IQAwdShcBz4FAAAAAAAAAAAAAADIDG0CwDQAAAAAAAAAAAAAAAAAjkwDZALAjNARBAAAAAAAgO//JXr7cACGfnC1YQAAAAAAAAAAAAAAdgXgZffV6uiTARjfA/3lYQAAAAAAAAAAAAAAALD6gk9ZAO7jl+mmzgKcNA0AAAAAAAAAAAAAACAN0NwuBsDIbggpE79b7wAAAAAAwIkLAAA4DIA4AwAAAAAAAAAAAAAAACD67QJKikoAAAAAAAAAAAAAAAAAwAQAABgAAAAAAAAAAAAAAHAeAK/3bVXs1Ceb048BoAo32+8IBAAAAACAo6J/KQAOSEqR/hG7/1Js1BkAAAAAAAAAAAAAACfM/q8U/kdkpLiKANByMnj97gAAAAAAAAAAAAAA2Onx/3V/lgPgkLzUMz5iyF8MAAAAAAAAAAAAAAAArBD9RScAPgBwToH6OgCcaAAAAAAAAGwW/Vdcx2dNbP8U/aOrZtfqyboVZHY9FgAAAAAAAAAAAAAAsPfjf1r3tbzJ///Mh+i/TskFUjkTyZ7HAgAAAAAAAAAAAAAATgRg+l7zfwPw548DYJFpgEU7CQAAAAAAAAAAAABg+eW/hq1clQD48LN75wmfHkGROwKX7CoAAAAAADjzAMC4HusE4N//cePc4OsBsJsBAAAAAAAAAAAAAAAOAEw7EPDxp66P24O2KUpTInZaRsdGe4IAAAAAAAAAAAAAAHZ8/J+YBbAFgM8nhPesS1mtMHucAQAAAAAAAID50X/P7qP2d/s9R+CuANRKD7L9agAAAAAAAAAAAAAAgOgv97ErPjO/7LW2Qx0zmI4EYJFRBgAAAAAAAAAAAABwyPIfAGaX/L5eXCsbAAAAAAAAAAAAAAAAgLT0P+vXcfllG+BvVUQX6biKAAw5FlB+sAEAAAAAwGbRP/MAwO/3EgpA2eG2KAAnGgAAAAAAAAAAAAAAsE74D5jRrZX24isAlcfahgDsuicIAAAAAAAAAAAAAGDvx/+lMt89BuAumDK8OcJLih1kQE0FAAAAAAAAAAAAAAB2Xv7bEID7XngXZ+mouIYsB1b8tAAAAAAAsH/010k7/eIX7sZCg9U69X/KnC/yOa5TDAAAAAAAAAAAAAAAKP7QeC/2+N8NwEZlqLfIarLhagAAAAAAAAAAAAAAQOlFo0Wjv2WjzrYArJYuZ8RkVK0PDgAAAAAAAAAAAACwYfRXEu5//k/jo+NWAKxelKp/HyQAAAAAAJwNwCnR/1cm78dfNT/vCfL9v7YBiywIAAAAAAAAAAAAAACs9uy/SAHAz/H+4HN8PRks+isXp19hOQcAAAAAAAAAAAAAQPRPA+CnOj///R9L7wZafR/TaAMmtgcAAAAAAAAAAAAAQKV8kQOXZ2oCsEtGzbjEqLsYUHlFEAAAAAAAyqWLHtobALAAED5yUwY+AAAAAAAAAAAAAMBqAAQN60ULzbzJDbJL+D++5/UThARNBKQ3AwAAAAAAAAAAAABA/UqRS0ZESyXvr4XAy4fLmy5aaoYgYVtQXjMAAAAAAAAAAAAAAMyM/f6VlPIj5uXz7ucPt9IHbkyiaUUwryEAAAAAAKD8V/9Vv/3/GfL9d1w743ljNfMtFjiGLAiMbgsAAAAAAAAAAAAAgBkP/hGttchg2RiA6B6+73t3A97FCQAAAAAAAAAAAABAXQCu4Nh/8JKrjpT+V9olDtY9NDz+b9voRgEAAAAAgAmxH9Q66w6T3b//n5E5OOVv3MCWAQAAAAAAAAAAAADICv0rq2UBsEzVj918HzsZENYyAAAAAAAAAAAAAAAVHvwb11dWSAjS2YNbJNfetoTgonMBAAAAAAAAAAAAAABLPPv/Xll744F9IgArfdDEAIhrIQAAAAAAqPHVv+1lFikMAoDdAQjdupZlAAAAAAAAAAAAAADAtDO/PS90IgD3ptfqAMSlSMlSAAAAAAAAAAAAAAAgPdV3RDHspcoDdt4jAPaZCxjCAAAAAAAAAAAAAABANADBtxH0cscBMHaz5uz42wKA/pmtrIKiAAAAAACQ/GlCP85SBUIqVWMvC8I+yx3XlL+ZAAAAAAAAAAAAAAAhAFSe0jxgf8zckN8gK+Mi/ZMdjAAAAAAAAAAAAAAAw/c2Dxpbh47Iq9Ql0LMLgUdpAgAAAAAAAAAAAAAw6I2GjqaThuH4SAXBOtMBIa8BAAAAAACt7zLn60/8JvsNA3/u+wv3pHDorUkIAAAAAAAAAAAAABD96uNHzJmpsqdHHAgKtV/XYXoAAAAAAAAAAAAAgCnRH51qZ4uBt054UaAAA82/DgAAAAAAAAAAAACQG/5Dsm2ekVJ6yfsW8COpBwAAAACAIABGR39W8+02FlKPEIBgsQeplt8FAAAAAAAAAAAAAOh6qTkhuctwG5Ef5Zqa+IMC78v05BsAAAAAAAAAAAAAANC1oPDkI4wPnPVOCOSWWsqm4FgDIjJchUYgAAAAAAD0AFDxW9+fe4AWSxaYdthyqgTHPQt0fsSu433vfhMAAAAAAAAAAAAAoKuvJnTyh2MA66QMH9LFlS1Y4VTjwHmpmgYAAAAAAAAAAAAAANxZTx7xEwB//b+/72yDB/9rgeuOXKWa0x/BPVtpoAAAAAAAAAAAAAAAPAag6CrPn2/yw2dcI4fe6oEf8XnKdEeZJI6j51sAAAAAAGDQ9/+szvvrRv7zP8sZ0NJO1+JX3vflmY8AQzOtBEclAAAAAAAAAAAAAMCV9UYTAKhlwBkP/lGfcnbvFCvcPu4PMwAAAAAAAAAAAABA4CNqarf99Z8+xnu9Wtqbx/3DIVFkLiDvNHbWaiUAAAAAAAAAAAAAwBcAQicAsvvs8Q1NvMenlVoKh+vQ6YACDHSSNQGA/vgEAAAAAIAq3/9fvsJjAD5nDJ8T/RNC/93rJyQiX8eAD++fGvijH9EBAAAAAAAAAADAuQBUmABoeZlvKUB+eMWs8dU58uMfuXO3qocpcBcy4MmnujpSvGetBQAAAAAAAAAAAAAAXLMPc159pXFeZ0PJLlk6I/rzstZE74qvaMBPfz/iQr/reAgAAAAAAAAAAAAAQBAAEycARgMw1oARsX93p29PWa2KUuCqd0L4y+nyMIqjQ+XL4jAAAAAAAExO4xAKwNfvcEMNCI7+OQcMxhY/WeZR4NnxgNxi68F/rAEAAAAAAAAAAMCpANTI5Ba3Eejdhv/Rw759xMw9YDy6CGJzU9335Fwh8bE/J7skAAAAAAAAAAAAAECFki7NR1I7DxaPWwJrjIqglp+QuCZ0sbOuAalP/eMSzAMAAAAAAAAAAAAAVKjq2FiZpT+oh2xmbBlH5QoLZQfQBgS8+52JmWYBAAAAACACgPueWXL7zcTts081IfrjOrzGN9UTDWj5yHfGxjkAAAAAAAAAAAAAgC4AilRy+wZAz6JCf4XouNi/ChYWmqNAmVEXmZ0x4cwJAAAAAAAAAAAAAMCDJGbnAtBiQP+TYGL0B2ORbcCcgffhfWZuq4qcFgIAAAAAAAAAAAAAqL8G+NuBzEEt2/8gFrrrvzm9ULnNglEG5I26hwfYAQAAAAAAAGM/7kAAHhvQGf1ZX/2zVgxmGTAtG8gv95ITIgAAAAAAAAAAAAAAOQBMWAK4HpX4yX6y6tz9kxL9E3JcDDHgrmXA16yA//rXCuXn3/c1AAAAAAAAAAAAAMDcGYBnpb6HbvFuKRmTcPA3MrfQNAYWWQ58t6fqvueXL+3qdwAAAAAAWAiA1OJ+FWb/a0z9JyowxYD2jdWzjiUCAAAAAAAAAAAAAOwAwLe4GHgjjXPQNaP/mnJFGnBH1pOMP14JAAAAAAAAAAAAAACArBWPuTdyj1z+Wz36sw3oevkXx4B//PNQ4E8jAAAAAAAAAAAAAMA2APzevNNWJGtt/yuw9tepQHt+7aCtcC8H1e/HgAEAAAAAAADDDagBwNzdP2VDP9qAxg1B727zzW2nJQIGAAAAAAAAAAAAADgTgIkGtE8AjI/+KyjpXk+ZuzIGvFu++fz6nVuLygGwckIQAAAAAAAAAAAAAICfdn9kp2B+fWp54O6foDDq+K2+FH8RlY7epHBqOHMempunDAAxacEBAAAAAAAAAADALgC8eaH7nktAVznwQUkhp2//C65IHrIqdnXXXW/eEZg1tOfujQUAAAAAgHcA9I/Ee9L1e2HmAtH/fldGQvR3xl3Yyb7+13h5MLAlYfpd/QIAAAAAAAAAAAAAOAyAD5066saCNwBN2/0Tmgc7fDtPvwGPmQYAAAAAAAAAAAAAAMAGAIwzIPoAwOBgHlW2e0BpnXAD2v913nZ+AAAAAAAAAAAAAAAA9B4LCL63K3gH4DUl/WfBcRq3KTCE67CZj5IA/PLRAAAAAADg8ScvP7qCDQg9AvhmjMVV/yg7VINOF0Q9s60HQE/U/vPIDAAAAAAAAAAAAACwIgChu1dCzgB0P6DvFv25BrQsr9Qe3wAAAAAAAAAAAAAA4QC8GTxlR9l4AIbs0Y86kLvI0+sUA6KzPy62CPilBBIAAAAAAAAAAAAAwLoAxD8Fh54BOPzxP2K65gpLEdoDPwAAAAAAHAHA49crPbpGADBqj/7S9drGPwdczQcD7y2vHgD++VcAAAAAAAAAAABwLACdD6mlG2WTCYCo6L8yrjkGLJ7fb8iM19fGAgAAAAAAAAAAAABof8m6TRP/GhMWAOOe/msA0LwcGHwm4NAJAAAAAAAA2BWA+ESDzV9eW7NYNANQIPr7axAGPT4cNEYblwAAAAAAAAAAAADA6QBstBAwB4CeCYCsDUCFAJgwDWAJAAAAAAAAAAAAAADgIQDHTQO0xGr9DUCpAIw+FgCAsDgFAAAAAAAAAAAApwNgGiB2AqD/DPC9yQHgiWcCNjSg8+8KAAAAAADEA7BF+7aUArmlAEk3AADNS0sAAAAAAAAAAAAAAK0nXc8AYOoEwKsSg7sN50gDdv8j1fU3GgAAAAAAAAAAAACgdxbgQADuQWcAsuofrVss6FAAgqfpAAAAAAAAAAAAAAAOXwiMygTa94Pvo39jAJoN+PEHbyeBAQAAAADgDQBnHAnMBSDu+z8ADsoQHpyMEgAAAAAAAAAAAABgo0rhxY4BD50A2PVEcE+L7AlA/AwdAAAAAAAAAAAAAMDRaYGCAAheppmT1LvWdVsJBAAAAAAAAAAAAAAoDMAuqWlCMwFFrOddZ1+3DOFjBhcAAAAAAAwe7ksDUOH7/3UBoPNU4PEA/PCKAAAAAAAAAAAAAADH7gUKygUIgLIArL8OMGRuDgAAAAAAAAAAAADg3JXACACuK7Bii+iPWCDd6kAAAAAAAAAAAAAAAAAAUgCInwC47QDMmwP4pS9tAwQAAAAAAAAMXALo3Du1YeWVuaVFAAAAAAAAAAAAAAAA8BKAXfcChQDQfIDicYcLdACE/0kGAAAAAAAAAAAAAADAw5PAT38wrrdF+dmVwkfNygEAAAAAAAAAAAAAHJocvBGA//zskEo9lv8iqwsBAAAAAAAA8gBYqXFbaoIHFLR419VivMOALQqEPB4VAAAAAAAAAAAAAAAAmAbA0N0eLgAAAAAuAAAAAC4ATARg8afWMgDEryYC4Guf7gbA+4STAAAAAAAAgPe7gOIAGPIwMXHrzVU4peCJAPz2ugAAAAAAAAAAAAAAAEgFYPSSgtDfBIBxS3IAAAAAAAAAAAAAAI0LCrsAkLsGuFwJnj0AWHIvEAAAAAAAAAAAAAAAAAa1bBYAvQ+uEw24LgAAAAAAAMDuAJxxHjAbgOZfBwAAXtecAQAAAAAAAAAAAAAAQBgA97sTqN3Rnxt8JwGw4jAFAAAAAAAAAAAAAADAEAC6lueG7vsHwM933NJrAAAAAAAAAAAAAAAAAMCbDJ3xABQ4RLDYWYD3qVUBAAAAAAAAUQAsXM8iC4ArP5XgAecYG5OrAwAAAAAAAAAAAAAAAAB/3+8SALgAAAAAuAAAAAC4AACAkgCk1BV3HQ9Ay5ACAAAAAAAAAAAAAAAAAA4AYL2cQAAAgAsAAFgAgDtg44w4HpxD+yQAvrwyAAAAAAAAAAAAAMBZADwbIUMA6HlscwEAAABwAQAAAHAB4CwARh2nff3Zm9cAd0qpdNoy4JyeAQAAXAAAAABcANgWgKyMNnMBePfBhXRBAHL7ZWhyXgAAAAAAmAjAdaXntCsHwINmENq1AJhuAAAA4AIAAADgAgAAFgGgeKrt8as0NT5aCACJCgAAAAAAAAAAAAAAACCo6QFQhe11AJhjAAAAAAAAnAJA1uQrANoMuFa8IodT+hoNAAAAAAAAAAAAAMCJpwEBUAWAa9EreDjlGgAAAAAAAAAAAAAAAIB7UlpwAABgyoogAAAAAAAAAAAAAAAANgSgUGQBoNxcAAAAAAAAAAAAAKgFQI4BAAAAAAAAAAAAAAAAOBmAno/67F22ByB+mI4m4OorQw8AAAAAAAAAAAAAAIBmAFrKgwcBkFVe8d4yHdBoAEbPBfTEIQAAAAAAAAAAAAAAAH657yEAPDYgsShQ/1sBIHUuAAAAAAAAqgKQ+6X1LABuV4xEKQC8pBEAAAAAAAAAAAAAAAAAAMCYoHvW6Xd+aUQAAAAAAAAAAAAAAACUAWDCcQAAjIu79BzzAAAAAAAAAAAAAAAnAjCuiwHg6jvhEN+84wqxAgAALgBMAODFLQMAALUJGAdA15nK+HcBAAAAAAAAAAAAANgTgOAXfxncTzKHNB8SnQjAFta8PghwzT1UDQAAAAAAAAAAAACwBACPf3Tiwc937371HQdo6ZhQADrXnfpPqB4IQGLoAwAAAAAAAAAAAACcAMB1Da9vuRIA0QVF22Z5FlmwfL9yWz/2O8bZoyEHAAAAAAAzzwJeV3KF68cDohGAD+8/BICetmlsvGGVC2cAkLjrBwAAAAAAAAAAAAAAAEsD8OBBurFkTBYAjY3X3Pbje/H54G9+/bnedU3IAQAAAAAAAAAAAADsAsDE4wD36+07ZQBoSaO9AgAtK59lqn8DAAAAAEAhADp7t/PjzZ08HgLA448YH5BBAPSE7VAA3rxIEABFFzgAAAAAAAAAAAAAAJYC4C60f2RqYsCxADTW0isLwOsX6Qeg8h4nAAAAAAAAAAAAAIABAJRLZDcLgJZpgM6g7gSgq5x2NwA98xW9LzfiIEAZAwAAAAAAAAAAAAAAZgCwSzmb8gB0ReXrX4sHIPBFWtNDRh00KDQN0J+yCAAAAAAAAFBnISD0u3VjAI3OyDx03n/EEsA6ALx9gAcAAAAAAAAAAAAAiACgSA3oZRcCQubFe8IoryjL8IzxuwPQ/1cZAAAAAAAAAAAAAKD4QYCBwy8FgGeDMCG45qwBpof/z0e714v+UXuBAAAAAAAAAAAAAADqrgEmJAx9XST0gRQtyUFHh1h2RtBpALTUZtoYgB9+FgAAAAAACgNwXVdK0vCW03N3Znrw6LWOeADKfv/fGIA327YAAAAAAAAAAAAAAIYAkNjxjcE1F4BnI3HwBMd5AMRlrV8BgFc/CAAAAAAAAAAAAADw4vkhYQj0nxeNq0+UWic0pWp2dHnwOQdAWvpl0egfshcIAAAAAAAAAAAAAKDSGmD7SzffURkAgoLtil9BnZgACgAjNgMCAAAAAEAQAOM+Zsq07dVWS2NIYpBrWN690Krc02fARycCKZf2MngvEAAAAAAAAAAAAADA83TNqTMAkaV+NigTOBSACUN5AAGDFoYqQBA8SQcAAAAAAAAAAAAAUASAiBdNBOCrAXfbImfnmCsb/j3vnpsLsNmcCgQAAAAAAAAAAAAAAHQBkH0SODj/Tfax4KZpgOCl1rLP/v03kpgJqLsYUT0A/moeAAAAAAAodRDwCs+ANxqAxAzh8WO33Gae0MImI/aTVayS2CgFAAAAAAAAAAAAAEAxAOY48vr5PgiAr50YdadT9rCEPkknTQB8H0GzIIg7EAAAAAAAAAAAAAAAsNEaYNcrJR0Lfp4cqO0JODfuP7/PdQ04klAGgFkQAAAAAAAAAAAAAACEALDbGmAcAI0GxE0DvBtr910nT03P1fJu96BMQBVPDwetBAIAAAAAQB8Ao4bSxFealh6wbeRPN+Aaca0HQMSBx9ynAAAAAAAAAAAAAHAuAG/uN7EmeBkAmnODhAIwbnN/bvnxa1Cd8nEfaUJ5q6zcgAAAAAAAAAAAAAAAGwHQf3uND1f/+tnQSZVRAFyTypD3p/h+3KoTZ6J+2s5VpE4oAAAAAAA8HgAps87BX76GlCccmhsk8fRZCiUJ1x1bVybuQfS+S1cKBwAAAAAAAAAAAKcC8Kb3s9MdzCot+ORlrrBCIZEA3MsUv0uJ/tIRnALA73/gAQAAAAAAAAAAAACKADBwH3/WNMC4k8FD9wBNOV0QvV3okPB/MSEHAAAAAAAAAAAAAPAQgM7HjsjPNnH39XVdQ04Gxz/R5ZS+HBpQIyYAmhplt1mAX5oOAAAAAADKAdDyRW7QvUWcCWg8GNgyrTtz784EAsYdbAQAAAAAAAAAAAAAAMBcAH5/n6FPq+25Qb5W6YhMD1IpmV/PndWI/kWWEfr/YgAAAAAAAAAAAIAjAehPyDZhWWjEjpjVNgQ92C0Vmmgw0YKJZwAqVVsCAAAAAAAAAAAAADgRgPjq0kE75uJOBj/U5MXn6d07X4CAiDMA8UMOAAAAAADOAKDS96OwARb63bDRgNH114OTbZc1YNSiSGJxPwAAAAAAAAAAAAAAAPQP7J7wHXoyOMuAnlBcGoC0PWDVFAAAAAAAAAAAAAAA2BmAng3fcffcfjI44lhAy3mIzu30RQHIKsH97sD3egBcIZXZAQAAAAAAAAAAwN4AbHFcMqjiaI8BXTv0kvZMjjo30P9W95Qs4DUV6AxdAAAAAADYFYAh99q5s/5NcEd0TtaRyeg3nLUBaLGnAQAAAAAAAAAAAAAAALyvmlbbgGuwAauXvZvzeQo1KAAAAAAAAAAAAAAAAHL2K3Vvp3kzzEP2ai0GQOOWpQL3mHsjA/82AAAAAAAAAAAAAAAsA0BWf1xhBlzhBqwe/u1HFu6MIlSVpgMAAAAAAGA2AGVG0tyiJW1H2q6+zN05u3LKCT7qfGekArUG/MjTgAAAAAAAAAAAAAAAhcZN9s7wlnZ/fPMDVgPiq/OlzKmnRP/7FwUAAAAAAAAAAAAA2BaAgguBM590IwzoKSzai09qxfAIpIpG/5uC7IUAeJsWHAAAAAAA6gDw5GNl38IsA+IIuKpcoXt2Hr9M+USXcRkjAQAAAAAAAAAAAAB6X6rSvp+59cuXMaAyAFm7f+5Fr4hlHQAAAAAAAAAAAHAqAHFPE7Wef+Ymi44Oq86gHp9gfMLjf0iH3TcAAAAAAAAAAAAAAAC6Xq1sSeQJtxUfWT0GXMsYMHrleWJ1+YmprwAAAAAAYH0A2ofwsxcfvRqwlAGjKwU2P78E3duK1QCb2uS3s6gAAAAAAAAAAAAAAD0vOeHDx235GJJdevSCQIFwCjWgVvSXTasYEq3/JKUEAAAAAAAAAAAAAIh4yazP/7q5BlSGuadsC/pWWCg1pqIISLnTx69UObM6AAAAAAAAAAAAAEAAAMEveRfeM53zObIO3kYaEN97nSmoh9xgyAzX3CHf0ncAAAAAAPAWgMoHAkJ2f6cWurgH7bgvuiAQ/NU/6N5aDkEUO04YV+QFAAAAAAAAAAAAACD4desakKVXxSx8WZEW/Ow/BOagQ91LTwAAAAAAAAAAAACAowEY8tL1y6jM2QqzggEtLRJRf3p057zMtx63HW3oIO75HQAAAAAAAAAAAACAQ6YB8m63aE2ee2r6z7l/TR5vpgw9kVJrAgAAAAAAALoBWMqAWokMX1de2eMq0DkRh/xqJMMP+QMNAAAAAAAAAAAAjgYg3gDRP2QH92ahP2P4dC8IpY+hcX+dAQAAAAAAAAAAAABi36aGAVVuLPQhbo/Ynwdz54idW3Y29A8zAAAAAAAAAAAAOB2A+GeN6QasUNVxYwaKrx83vnHq7bbHFgAAAAAAZABQcUNQ9N3EfZjggFks8HPGyjXlPOEC0f/3aVAAAAAAAAAAAAAAgJ7eqTQREHkXWXnDH77DyqGfMEZGv0ONx/+4P8gAAAAAAAAAAAAAAGMeOwocm6y4itjZXksFftKfh9GpHxOHdEr0AwAAAAAAAAAAAABsYEDcu2Z9jv4Wqxz1uZNDcTf94adSB3NQtiIAAAAAAEgEYFYFjh/ffZEiInHvVybqs/8g/PjiI1C9y5aweZ+NHQAAAAAAAAAAAABg6BxkaiGe6Lxr4+8/vt1mxPyUIfD9STdsImti/w+ZggMAAAAAAAAAAAAASDNgnAItr/69gmKNDDCFS69OvPn3lUfLrWUP3mn1a1Z2AAAAAACYtFO17JCdWwjiWqT1Zt/um3foHrylm649NyMAAAAAAAAAAAAAwKx96pUTdE+7zaET9Yl3V2PXVzk4U05afm8WAAAAAAAAAAAAAIC5p1VrPfiXGTnjN+6sdiMtqTkKAZCVaQUAAAAAAAAAAAAAoAmAoK3RZRR4/a71K52OatXcd+u4t3L1fBIjIcAPAAAAAAAYnh9h+hBqe8uKW/Cva9GyIPOq811Vi1UODahHLwMAAAAAAAAAAAAAMC5ZwetWqLHjojEJ9lkSzAyjRSYA8qP/aWQDAAAAAAAAAAAAAOS8Z+rY+vayD48BF19u2jjmO2v0lD1zMvRv6PPbAwAAAAAAAAAAAADIe++0sfbuQf9HHMrVj87WYG7INxow93PFt+2A4k0AAAAAALDTZ/rpHV5G7pKzzcvmF3m5g+d9LvC5xUrGVVjv/BgAAAAAAAAAAAAAANM29Vc8Y9ozO3u7hm/5efOvBRaeEpbMmso4AgAAAAAAAAAAAAAMYqDGXEB8viAAJPbD41mXx1WehoyzKQ/+7XcMAAAAAAAAAAAAADB2u9kKcwEhuZmFcBLBH37qVWHM+HDKC4OgIQcAAAAAAIUMCMpEnnbaTPRPff6KH29zv/pnL1oAAAAAAAAAAAAAANSpGFfoHCcACs37R6/dTM053791KeDOAQAAAAAAAAAAAAAWU2B8f4r+yrHfkyy8VvRPK70OAAAAAAAAAAAAAFCPgWtyFdGEAwDHujFwD16V6K8c+gAAAAAAMD0pdUojFwfguCeI8QngFwn/6YkaAQAAAAAAAAAAAADqpqHOfCpq24a+1qNrsegf8srFp/6vEmnaAQAAAAAAAAAAAAAY0JhNZrw2u0T96Mf1lMBJ3Pg/vF8AAAAAAKAIA9N3BQEgpcJfezXr13n+yk39X6UKNAIAAAAAAAAAAAAAVFBg4lxAdvQfU0zka/WO0MbKWiMaF/2ZXQMAAAAAAAAAAAAAUIOBWQY8fJm4vmp7nfWw6ATgfR+nLBKvH/0AAAAAAAAAAAAAAKXmAsbvxWqMx9DuagZgsXmDz3fcUv6zfavd3O1/FWMfAAAAAABqFYKb/RzwEoA7f9f6T79VmYMfP+cvDdDfreOyjewS/QAAAAAAAAAAAAAAC84FjDMgd0hFbB6KHkp/vl5IID5r0LjuHLFha8DU/0ybAQAAAAAAAAAAAAAqMnDXSRk+6uRxxK91Vta+327eGQJAdC9e4dvDrsjV6gJLuQAAAAAAAAAAAAAAS64IZjXuwFWliF/r3C43B4CUNE9zor9+7AMAAAAAwF23VlwlA8blHryDd9SMA6CxGb79Rt342D36AQAAAAAAAAAAAAC2NaBiuZkEAN5tUrnHrTa0AFB7QCacdAEAAAAAAAAAAAAAOAGA/obLMmCWJ7/9Wh4A79viwY+vFP3XPtEPAAAAAAAAAAAAALAIAdes5cC6EwC/fPL2lc3Xh3gnAjB622el8rUAAAAAALApAP3NuMTM60gAnr/+7z/1Ygd/yMcNzo8yZADuFv0AAAAAAAAAAAAAAEsZcK9rQPwSwC8APMtw3r99pyvVets0wKjuHbT7p2qgAQAAAAAAAAAAAABsMhFQ2oAhFUH//NdQAB5kSe8H4E3LjOvZ+PFWvIorAAAAAAAAAAAAAMAJy4HHAfAgfvsBeAJNxOxIXtanmP2ntaMfAAAAAAAYUB6Ar7f/4V9bfuf1YsXXhu2Z2U+M/eYBs3T0AwAAAAAAAAAAAADcB+0JKvJh4tBoqRgePMk/DoCcPhwx+79AdAEAAAAAAAAAAAAA7DYRUK5vZgHw9fxACADvEgtVKqoTH/33GhcAAAAAAAAAAAAAwEkGrANAz66+BAAeL95F9HTu4No9+gEAAAAAYAsD1tkQ1BMSr3/gWWz232gcABPMfj1Arl3CHwAAAAAAAAAAAABg9YmARQxIBeBZbIbc6PXrSkRnP08YVNs//gMAAAAAAAAAAAAA7D4RcFcE4G2WnF4AftonFA3Ah338E6uxJ24Aupe7AAAAAADgAANqPgS8eXKJfkYIBuDzb/QAMG0wHbABCAAAAAAAAAAAAADgDAPKdNvDe2ix7fG0w7v2an92L10aO3YD0JqhBAAAAAAAAAAAAACw+56gotMAnTkwXn/wFAD+/T67R/+ygQQAAAAAAAAAAAAAsIsB93Ing3tG1+sFrEY0Bq2sVWr7+6DHfwAAAAAAWPmK2xBUNtdhVzaOyMx3ZY/zJXz/3zX6AQAAAAAAAAAAAAAYUO5zRJ+dzQLgzS6mxScAlo8fAAAAAAAAAAAAAIAzjgXs1pkAGBn+18bhDwAAAAAAAAAAAABw/I7A7QF4vULX0zhbrADuHf0AAAAAAHCgAZsldSgJwFJj5czv/wAAAAAAAAAAAAAAh20IOhSADx/8IADizgBsEzcAAAAAAAAAAAAAAFv27r37YmBztXEAnB39AAAAAAAAAAAAAACmAbbr3TuqOunGQ+TQ5T8AAAAAALivjQd2Y0ff51zHfVoAAAAAAAAAAAAAAADsBsD7iYBjpwEOvHpHxZZDAwAAAAAAAAAAAADgxA1BABD+uyeNBAAAAAAAAAAAAAA4FoDPPWgWAACnRj8AAAAAAJz9FAAA0X/k/D8AAAAAAAAAAAAAgGkABgBgt3zxAACACwAAAAAAAHASAO/Ocx7X66Jf9AMAAAAwDq7jOt5TgPAHAAAAAAAAAAAAAADAbSnALMBRo+A+/gwAAPQ+AAwBAAAAAADwAMiAs/v/uDMAAAAAAAAAAADofwAYA3YEWgY+sucBAAAAAAAAABD9AAAAAAAAAAAAAAAAMBYYcHBCmDM6HQDGAgAAAABjAQAAAIANQcdViAcAAAAAAAAAAAAAAIDa0Ne11brQ1XqJfgAAAAAAAICngCVGxpV03TvkggcAAAAAAAAAAAAAAAAA1iPgKnKt+vhvyQcAAAAAAAAAAAAAwLHgd+F1dtSv0yahowIAAAAAAAAAAAAAAAAbA1DJgGu5q1DL3FYAAQAAAADgaADa+nvuWL/Wv+ZGf/yAAAAAAAAAAAAAAAAAwO4API9CsZ/XPBFvKfoBAAAAAAAAAAAAABgQFJBlA7+GPVnNE39uGAAAAAAAAAAAAAAAALuljLyC42JW4C+yJXHsm477QwAAAAAAAAA4p2xE8al5CUleN4fv/wAAAAAAAAAAAAAAAHjX+1LwVeVA9AMAAAAAAAAAAAAAAGDcGBD4tSAYnTwQAAAAAAAAAAAAAAAAB+8ITB/sG7ZxxTMOh0YCAAAAAAAAoH0sCP0p7TNNfQAAAAAAAAAAAAAAAGw5LOcO9CPVnRX7B0c/AAAAAAAAAAAAAAAD4oZFhagv61H2XRzqLQAAAAAAAKDEd8PsF1+/0kdW45weAAAAAAAAAAAAAAAACFhghFzXJTmhx38AAAAAAAAAAAAAAADsP0yu68ha4OMay8gHAAAAAAAAAAAAAGBAweFyrXzpTgAAAAC6EwDbHFYT+LUtOPm4JQAAAAAAAAAAAAAAAMYFncCvyMDh+VYAAAAAAAAAAAAAAAAwPv6EfgkI5FsEAAAAAAAAAAAAAABAdiCWjfr6LqXem6ENAAAAwAWAyamrN9h3P+v+FVsBAAAAAAAAAAAAAADAsVtytJTQB4BhraUAAADDWksBAAD7j21NJvwBYDRrMgAAwGjWZAAAwP4DWrsJfQAYyNoNAAA4aThrOrEPAKNY0wEAAEaxpgMAAA4ZzZpO0wHAKNZ0mg4ARrGmAwAAjhjKmk7TAcAo1nSaDgBGsabTdAA4YkBrN00HAABoN00HAEVFNJ3WAwAANJ3WAwAANJ3WG3j9H8Xhbc7FjbBBAAAAAElFTkSuQmCC"
# SEAL_B64_END


def seal_uri() -> str:
    """校徽的 data URI（没生成过就返回空串 —— 页面自动退回无校徽的朴素样式）。"""
    return ("data:image/png;base64," + _SEAL_PNG_B64) if _SEAL_PNG_B64 else ""


# ★ 入口守卫必须放在**文件最后**：main() 会一直阻塞在 HTTP 服务上，
#   写在它后面的定义永远不会执行（2026-10-10 就这么把 seal_uri 弄丢过一次）。
if __name__ == "__main__":
    raise SystemExit(main())
