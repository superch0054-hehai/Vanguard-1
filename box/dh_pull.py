#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盒子端「拉取器」dh_pull.py —— 方案 B：程序全部在盒子，由盒子通过 SSH 主动拉取
====================================================================
🚨 纪律：对采集 PC **只读** —— 只"列目录"和"scp 读取"，绝不写/移/删/重命名 PC 上任何文件。
    采集 PC 上唯一需要的：Windows OpenSSH 服务 + 一个只读账号（无任何自定义程序）。

行为（每 N 秒一趟）：
  1) ssh + PowerShell 列出远端 .dat/.dhsln（只读）→ {相对路径: [大小, 修改时间]}
  2) 与本地清单比对 → 新增 / 改动 / 消失
  3) 新增+改动 → scp 拉取到 inbox（保持目录结构；正在写的先跳过）
  4) 消失 → 移入 _trash/<时间戳>/（回收，不硬删）
  5) 两个安全阀：远端目录空/不可达 → 本趟跳过；消失超过 20% → 只告警不动

用法：
  python3 dh_pull.py --pc admin@10.201.x.x --src 'D:\\测试数据\\HK'
  （默认 --inbox ~/dh-lab/inbox，--state ~/dh-lab/_state，--interval 60）

systemd（可选，常驻）：
  /etc/systemd/system/dh-pull.service
    [Unit]
    Description=DH7006B puller (box -> acquisition PC)
    After=network-online.target
    [Service]
    ExecStart=/usr/bin/python3 /home/admin/dh-lab/tools/dh_pull.py --pc user@PC_IP --src "D:\\数据目录"
    Restart=always
    [Install]
    WantedBy=multi-user.target
  sudo systemctl enable --now dh-pull
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

EXTS = (".dat", ".dhsln")
KEY_PATH = os.path.expanduser(os.environ.get("DH_PC_KEY", "~/.ssh/id_rsa_pc"))
VALVE = 0.2          # 消失比例安全阀


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def list_remote(pc, src, timeout=90):
    """只读列出远端文件：返回 {相对路径(用/): [size, mtime_epoch]}；失败返回 None"""
    ps = (
        "Get-ChildItem -LiteralPath '%s' -Recurse -File -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Extension -eq '.dat' -or $_.Extension -eq '.dhsln' } | "
        "ForEach-Object { [pscustomobject]@{ p = $_.FullName; s = $_.Length; "
        "t = [int64]($_.LastWriteTimeUtc - [datetime]'1970-01-01').TotalSeconds } } | "
        "ConvertTo-Json -Compress" % src
    )
    try:
        out = subprocess.run(
            ["ssh", "-i", KEY_PATH, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15", pc,
             'powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ' + ps + '"'],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as exc:
        log("ssh 列目录异常：%s" % exc)
        return None
    if out.returncode != 0:
        log("ssh 列目录失败（%s）：%s" % (out.returncode, (out.stderr or "").strip()[:200]))
        return None
    txt = (out.stdout or "").strip()
    if not txt:
        return {}                       # 目录为空（也可能是挂载异常 → 由安全阀兜住）
    try:
        data = json.loads(txt)
    except Exception as exc:
        log("列目录输出无法解析：%s" % exc)
        return None
    if isinstance(data, dict):
        data = [data]
    src_norm = src.replace("\\", "/").rstrip("/")
    out_map = {}
    for it in data:
        p = (it.get("p") or "").replace("\\", "/")
        if not p.lower().startswith(src_norm.lower()):
            continue
        rel = p[len(src_norm):].lstrip("/")
        if "DH_export" in rel:
            continue
        out_map[rel] = [int(it.get("s") or 0), int(it.get("t") or 0)]
    return out_map


def pull_one(pc, src, rel, dest, expect=None):
    """scp 拉取单个文件（Windows OpenSSH 的路径写法：/D:/...）"""
    remote_full = "/" + src.replace("\\", "/").rstrip("/") + "/" + rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = subprocess.run(
            ["scp", "-O", "-p", "-i", KEY_PATH, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
             "%s:%s" % (pc, remote_full), str(dest)],
            capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            return False
        if expect is not None:
            try:
                real = dest.stat().st_size
            except Exception:
                return False
            if real != expect:
                try:
                    dest.unlink()
                except Exception:
                    pass
                log("大小不符（%s）：期望 %d，实际 %d → 已删除，下趟重拉" % (rel, expect, real))
                return False
        return True
    except Exception as exc:
        log("scp 异常（%s）：%s" % (rel, exc))
        return False


def scan_once(pc, src, inbox, state, min_age=30):
    manifest_path = state / "manifest.json"
    sizes_path = state / "sizes.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    prev_sizes = {}
    if sizes_path.exists():
        try:
            prev_sizes = json.loads(sizes_path.read_text(encoding="utf-8"))
        except Exception:
            prev_sizes = {}

    remote = list_remote(pc, src)
    if remote is None:
        log("远端不可达 → 本趟跳过（不动清单）")
        return 0, 0
    if not remote:
        log("远端目录为空 → 本趟跳过（防挂载异常）")
        return 0, 0

    now = time.time()
    n_new = 0
    for rel, (size, mtime) in sorted(remote.items()):
        key = rel
        t = [str(size), str(mtime)]
        if manifest.get(key) == t:
            continue                                    # 未变化
        if now - mtime < min_age:
            continue                                    # 太新，可能还在写
        if prev_sizes.get(key) != size:
            continue                                    # 大小与上一趟不同 → 再等一趟（写稳才拉）
        if pull_one(pc, src, rel, inbox / rel, size):
            manifest[key] = t
            n_new += 1
            log("已拉取：%s（%d 字节）" % (rel, size))

    # 消失检测（安全阀）
    gone = [k for k in manifest if k not in remote]
    if len(remote) > 10 and len(gone) > VALVE * (len(remote) + len(gone)):
        log("警告：%d/%d 个文件消失 → 跳过删除同步（防误删）" % (len(gone), len(remote) + len(gone)))
        gone = []
    n_gone = 0
    if gone:
        trash = state.parent / "_trash" / datetime.now().strftime("%Y%m%d-%H%M%S")
        for rel in gone:
            srcp = inbox / rel
            if srcp.exists():
                dst = trash / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    srcp.replace(dst)
                    n_gone += 1
                except Exception as exc:
                    log("移入回收目录失败（%s）：%s" % (rel, exc))
                    continue
            manifest.pop(rel, None)
        log("删除同步：%d 个文件已移入 %s" % (n_gone, trash))

    sizes_path.parent.mkdir(parents=True, exist_ok=True)
    sizes_path.write_text(json.dumps({k: v[0] for k, v in remote.items()}),
                          encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return n_new, n_gone


def main():
    ap = argparse.ArgumentParser(description="盒子端拉取器（SSH 只读拉取采集 PC 的文件）")
    ap.add_argument("--pc", required=True, help="user@PC_IP")
    ap.add_argument("--src", required=True, help='采集 PC 上的数据目录，如 "D:\\测试数据\\HK"')
    ap.add_argument("--inbox", default=str(Path.home() / "dh-lab" / "inbox"))
    ap.add_argument("--state", default=str(Path.home() / "dh-lab" / "_state"))
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--min-age", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    inbox, state = Path(args.inbox), Path(args.state)
    inbox.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    if args.once:
        n_new, n_gone = scan_once(args.pc, args.src, inbox, state, args.min_age)
        log("完成：新增/更新 %d，移除 %d" % (n_new, n_gone))
        return 0
    log("拉取器启动：pc=%s src=%s 每 %gs 一趟" % (args.pc, args.src, args.interval))
    while True:
        try:
            n_new, n_gone = scan_once(args.pc, args.src, inbox, state, args.min_age)
            if n_new or n_gone:
                log("本趟：新增/更新 %d，移除 %d" % (n_new, n_gone))
        except Exception as exc:
            log("本趟异常（下趟重试）：%s" % exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
