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
    """读 manifest.jsonl。坏行跳过，不要让一行脏数据把整个界面搞崩。"""
    p = root / "manifest.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


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
:root { --bg:#f7f7f5; --fg:#1f1f1f; --mut:#70706c; --line:#e3e3df;
        --ok:#1a7f37; --warn:#9a6700; --bad:#b42318; --accent:#0b6bcb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141414; --fg:#f4f4f1; --mut:#a6a6a0; --line:#2e2e2e;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#58a6ff; } }
* { box-sizing:border-box; }
body { margin:0; padding:28px; background:var(--bg); color:var(--fg);
       font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
.wrap { max-width:1080px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:17px; margin:32px 0 10px; }
.sub { color:var(--mut); font-size:13px; margin-bottom:22px; }
.banner { background:var(--line); border-radius:8px; padding:10px 14px;
          font-size:13px; color:var(--mut); margin-bottom:22px; }
/* 数据口径警告 —— 要比普通 banner 显眼，因为它标的是"这个数字别直接用"。
   文字用正常前景色（黄色正文对比度不够），靠左边框和底色提示。 */
.banner.warn { background:color-mix(in srgb, var(--warn) 12%, transparent);
               border-left:4px solid var(--warn); color:var(--fg);
               line-height:1.75; }
.banner.warn b { color:var(--warn); }
.cards { display:flex; flex-wrap:wrap; gap:12px; }
.card { flex:1 1 150px; background:var(--bg); border:1px solid var(--line);
        border-radius:10px; padding:14px 16px; }
.card .k { font-size:12px; color:var(--mut); letter-spacing:.04em; }
.card .v { font-size:24px; font-weight:600; margin-top:4px; }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); }
th { font-size:12px; color:var(--mut); font-weight:600; letter-spacing:.04em; }
tr:hover td { background:var(--line); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
code { background:var(--line); padding:2px 6px; border-radius:5px; font-size:13px; }
pre { background:var(--line); padding:12px 14px; border-radius:8px; overflow-x:auto;
      font-size:12.5px; line-height:1.5; }
button { font:inherit; padding:9px 16px; border-radius:8px; cursor:pointer;
         border:1px solid var(--line); background:var(--fg); color:var(--bg); }
button:disabled { opacity:.5; cursor:default; }
a.btn { display:inline-block; padding:9px 16px; margin:0 10px 8px 0; border-radius:8px;
        border:1px solid var(--line); background:var(--fg); color:var(--bg);
        text-decoration:none; font-size:14px; }
a.btn:hover { text-decoration:none; opacity:.85; }
a.btn.ghost { background:transparent; color:var(--fg); }
.muted { color:var(--mut); font-size:13px; }
"""

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BatteryLab 数据控制台</title><style>{css}</style></head>
<body><div class="wrap">{body}</div></body></html>
"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def page(title: str, body: str) -> bytes:
    return PAGE.format(css=CSS, body=body).encode("utf-8")


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

    body = f"""
<h1>BatteryLab 数据控制台</h1>
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

<h2>数据集（{len(recs)} 个）</h2>
{table}

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
    ce_bad = None       # 整段测试的最高库仑效率（低于 50% = 电芯没在放电）
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
                    f"整段测试的最高库仑效率只有 <b>{ce_bad:.1f} %</b>，"
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
<h1>数据集详情</h1>
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

    def do_POST(self):
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


if __name__ == "__main__":
    raise SystemExit(main())
