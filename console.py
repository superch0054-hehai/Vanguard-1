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
import html
import json
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

VERSION = "1.0"
DEFAULT_PORT = 8090
DEFAULT_DATA_DIR = "./data"
STEPS_PREVIEW_ROWS = 12

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


def steps_preview(ddir: Path, n: int = STEPS_PREVIEW_ROWS) -> list[list[str]]:
    """工步层前 n 行，用来看"这个测试做了什么"。"""
    p = ddir / "steps.csv"
    if not p.exists():
        return []
    rows: list[list[str]] = []
    for i, line in enumerate(p.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
        if i > n:
            break
        rows.append(line.split(","))
    return rows


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
    for r in recs:
        soh = r.get("soh") or {}
        ret = soh.get("capacity_retention_pct")
        chan, tid = esc(r.get("channel")), esc(r.get("testid"))
        rows.append(
            "<tr>"
            f"<td>{chan}</td>"
            f"<td>{tid}</td>"
            f"<td><code>{esc(r.get('barcode') or '（空）')}</code></td>"
            f"<td>{esc(soh.get('cycle_count', '—'))}</td>"
            f"<td>{esc(ret) + ' %' if ret is not None else '—'}</td>"
            f"<td>{esc(r.get('detail_count', 0))}</td>"
            f"<td>{'✅' if r.get('upload_complete') else '⚠️ 未传完'}</td>"
            f"<td>{esc(r.get('collected_at', ''))}</td>"
            f"<td><a href='/dataset?channel={chan}&testid={tid}'>查看</a></td>"
            "</tr>")

    table = ("<table><tr><th>通道</th><th>测试号</th><th>电池条码</th><th>循环数</th>"
             "<th>容量保持率</th><th>明细行数</th><th>数据完整</th><th>采集时间</th>"
             "<th></th></tr>" + "".join(rows) + "</table>") if rows else \
            "<p class='muted'>还没有采集到任何数据集。等测试跑完一轮，或检查采集服务是否在运行。</p>"

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
    soh = rec.get("soh") or {}
    ddir = find_dataset_dirs(root, rec)

    metrics = []
    for key, label, unit in KEY_METRICS:
        v = soh.get(key)
        if v is not None:
            metrics.append(f"<tr><td>{esc(label)}</td><td><b>{esc(v)}{unit}</b></td></tr>")

    steps = steps_preview(ddir) if ddir else []
    steps_html = ("<pre>" + esc("\n".join(",".join(r) for r in steps)) + "</pre>") \
        if steps else "<p class='muted'>找不到 steps.csv。</p>"

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

<h2>这个测试做了什么（工步层前 {STEPS_PREVIEW_ROWS} 行）</h2>
{steps_html}

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
        elif path == "/health":
            self._json({"ok": True, "version": VERSION})
        elif path == "/api/overview":
            self._json({"status": load_status(self.data_dir),
                        "datasets": load_manifest(self.data_dir)})
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
