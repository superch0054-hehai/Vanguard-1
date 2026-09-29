#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BatteryLab 设备控制工具 —— 对测试机的**写**操作，唯一入口。

★ 设计立场（为什么长这样）：

1. **手动、单条、跑完即退**。不做常驻、不监听端口、不定时。
   写操作一旦变成"后台自动发生"，就再也没法回答"这是谁在什么时候干的"。

2. **四道闸**，从外到里：
   ① 通道白名单 —— 只认 CONTROL_CHANNELS 里列的通道，别的通道写了也拒。
   ② 使能文件 —— `.control_enabled` 不存在时整个工具拒绝工作。
      这是总开关：删掉它 = 控制功能整体下线（防"脚本误跑/手滑"）。
   ③ 运行态检查 —— start 默认走协议层安全闸（通道在跑就拒绝，fail-closed）；
      stop 要求显式 `--yes`。
   ④ 审计日志 —— 每次写操作往 logs/control.log 追加一行 JSON：
      时间 / 本机用户 / 命令 / 通道 / 设备回包。数据无法追溯恢复，
      所以至少要能回答"谁在什么时候对哪颗电池做了什么"。

3. **不实现的命令**（协议里有，但故意不做，理由见 docs/控制功能设计.md）：
   broadcaststop（整台设备全停，误伤面最大）、goto（跳过工步=数据永久缺失）、
   reset（也要工步文件，收益为负）、parallel（paralleltype 语义在协议里是空表）、
   chl_ctrl（协议注明"需要中位机和硬件支持"，我们的 BTS85 是否具备未知）。

4. **零依赖**：只用标准库（argparse / json / datetime / pathlib），
   盒子没外网，装不了包。

用法（通道写 10-6 这种简称即可，也可写全 27-188-10-6）：

    python3 control.py --list                      # 看白名单通道的状态（只读）
    python3 control.py light 10-6 --on             # 点灯（零数据风险，最先验证的就是它）
    python3 control.py light 10-6 --off
    python3 control.py clearflag 10-6              # 清标记
    python3 control.py resetalarm 10-6             # 声光报警复位（设备级）
    python3 control.py getpause 10-6               # 读预约暂停（只读）
    python3 control.py setpause 10-6 --cycleid -1 --stepid -1
    python3 control.py cancelpause 10-6
    python3 control.py stop 10-6 --yes             # 停止（高风险，必须 --yes）
    python3 control.py start 10-6 --step-file "D:\\x\\1.xml" --barcode "..." --yes

注意：start 下发的工步文件路径是 **Windows 侧（BTS 客户端那台机）的路径**，
由那边的客户端自己去读文件 —— 本盒子提供不了这个文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from neware_client import Channel, NewareClient, NewareError, TcpTransport

VERSION = "1.0"

# ---------------------------------------------------------------------------
# 闸 ①：通道白名单 —— 控制开发用的三个通道（2026-09-29 由需求方指定）。
# 设备是 BTS85：devtype=27, devid=188, 单元 10。10-1/-2/-3 在跑老化测试，
# 控制实验用 10-6/-7/-8 —— 它们不在采集器的监视列表里，天然与采集隔离。
# ---------------------------------------------------------------------------

DEFAULT_DEVTYPE = 27
DEFAULT_DEVID = 188
CONTROL_CHANNELS = {"10-6", "10-7", "10-8"}

HERE = Path(__file__).resolve().parent

# 闸 ②：使能文件。存在才工作；删掉 = 控制功能整体下线。
ENABLE_FILE = HERE / ".control_enabled"
# 闸 ④：审计日志（一行一条 JSON）。
AUDIT_LOG = HERE / "logs" / "control.log"
BTS_HOST = os.environ.get("BTS_HOST", "10.201.47.169")
BTS_PORT = int(os.environ.get("BTS_PORT", "502"))

# 故意不实现的命令 → 理由（拒绝时把理由告诉用户，而不是含糊地说"不支持"）
EXCLUDED = {
    "broadcaststop": "整台设备一起停，误伤面最大；要停单通道请用 stop",
    "goto":          "跳过工步会在数据里留下永久缺口，和 stop 同级风险且无可挽回",
    "reset":         "同样需要工步文件，且会顶掉当前工步，收益为负",
    "parallel":      "需要多通道物理并联接线；且协议里 paralleltype 的取值表是空的",
    "chl_ctrl":      "协议注明『IGBT通道模块控制，需要中位机和硬件支持』，"
                     "BTS85 是否具备该硬件未知，发了也可能不生效",
}

# 高风险命令：必须显式 --yes 才执行
NEEDS_YES = {"stop", "start"}


class ControlError(RuntimeError):
    """控制层的拒绝（不是设备错误）—— 闸拦下来的都算这类。"""


# ---------------------------------------------------------------------------
# 通道简称解析："10-6" → Channel(27, 188, 10, 6)；"27-188-10-6" 也认
# ---------------------------------------------------------------------------

def resolve_channel(spec: str) -> Channel:
    """解析通道并过白名单。写 "10-6" 这种简称即可，全称 27-188-10-6 也认。"""
    spec = spec.strip()
    if "@" in spec:
        raise ControlError("控制工具不指定 ip —— 设备就是 BTS_HOST，写别的地址没有意义")
    parts = spec.split("-")
    if len(parts) == 2:                      # 简称：单元-通道
        try:
            subdevid, chlid = int(parts[0]), int(parts[1])
        except ValueError:
            raise ControlError(f"通道简称应为 单元-通道（如 10-6），收到 {spec!r}")
        ch = Channel(DEFAULT_DEVTYPE, DEFAULT_DEVID, subdevid, chlid)
    elif len(parts) == 4:                    # 全称：devtype-devid-subdevid-chlid
        try:
            devtype, devid, subdevid, chlid = (int(p) for p in parts)
        except ValueError:
            raise ControlError(f"通道全称应为 devtype-devid-subdevid-chlid，收到 {spec!r}")
        if (devtype, devid) != (DEFAULT_DEVTYPE, DEFAULT_DEVID):
            raise ControlError(
                f"只允许本设备 {DEFAULT_DEVTYPE}-{DEFAULT_DEVID}，收到 {spec!r}")
        ch = Channel(devtype, devid, subdevid, chlid)
    else:
        raise ControlError(f"通道写 10-6 或 27-188-10-6，收到 {spec!r}")

    if f"{ch.subdevid}-{ch.chlid}" not in CONTROL_CHANNELS:
        raise ControlError(
            f"{ch.key} 不在控制白名单里（只允许 "
            f"{', '.join(sorted(CONTROL_CHANNELS))}）。"
            "白名单写在 control.py 顶部的 CONTROL_CHANNELS —— 改它之前先想清楚。")
    return ch


# ---------------------------------------------------------------------------
# 闸 ② / ④
# ---------------------------------------------------------------------------

def require_enabled(enable_file: Path = ENABLE_FILE) -> None:
    """总开关：使能文件不存在就拒绝整个工具工作。"""
    if not enable_file.exists():
        raise ControlError(
            f"控制功能未使能：找不到 {enable_file}。\n"
            "  要启用，创建这个文件（内容随意）；要永久下线，保持它不存在。\n"
            "  这是防误跑的总闸 —— 不想动控制功能时，请让它保持不存在。")


def audit(command: str, target: str, detail: dict, log_path: Path = AUDIT_LOG) -> None:
    """审计日志：一行一条 JSON。写坏了也不能影响主流程，所以吞异常但打到 stderr。"""
    row = {"time": datetime.now().isoformat(timespec="seconds"),
           "user": os.environ.get("USER") or os.environ.get("USERNAME") or "?",
           "command": command, "target": target, **detail}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  ⚠️ 审计日志写不进去（{e}）—— 这次操作仍然执行了，请手工补记。",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# 设备连接与执行
# ---------------------------------------------------------------------------

def connect() -> tuple[NewareClient, TcpTransport]:
    transport = TcpTransport(BTS_HOST, BTS_PORT)
    transport.connect()
    return NewareClient(transport, 8.0), transport


def show(resp) -> None:
    """把设备回包打印成人能看的样子，并判断 ok/false。"""
    rows = NewareClient.parse_ack(resp)
    for r in rows:
        ch = "{}-{}-{}-{}".format(r.get("devtype"), r.get("devid"),
                                  r.get("subdevid"), r.get("chlid"))
        print(f"    设备回包  {r['tag']:11s} {ch:15s} → {r['ack']}"
              f"{'（成功）' if r['ok'] else '（失败）'}")
    if not rows:
        print("    设备回包  （没有可解析的确认节点 —— 原文如下）")
        print("    " + resp.text[:300].replace("\n", " "))
    return rows


def run_write(args, enable_file: Path = ENABLE_FILE,
              audit_path: Path = AUDIT_LOG) -> int:
    """执行一条写命令。返回进程退出码。"""
    require_enabled(enable_file)
    ch = resolve_channel(args.channel)
    client, transport = connect()
    try:
        if args.command == "light":
            resp = client.light([ch], on=args.on)
        elif args.command == "clearflag":
            resp = client.clearflag([ch])
        elif args.command == "resetalarm":
            resp = client.resetalarm([ch])
        elif args.command == "getpause":
            resp = client.getpause([ch])
        elif args.command == "setpause":
            resp = client.setpause([ch], cycleid=args.cycleid,
                                   stepid=args.stepid, timeout=args.timeout)
        elif args.command == "cancelpause":
            resp = client.cancelpause([ch], cycleid=args.cycleid, stepid=args.stepid)
        elif args.command == "stop":
            resp = client.stop([ch])
        elif args.command == "start":
            resp = client.start([(ch, args.step_file, args.barcode)])
        else:
            raise ControlError(f"未知命令 {args.command}")
    except NewareError as e:
        # 安全闸的拒绝也记进审计 —— "想干但被拦"本身就是重要事实
        audit(args.command, ch.key, {"result": "refused", "reason": str(e)[:200]},
              log_path=audit_path)
        print(f"  ❌ 被拒绝：{e}")
        return 1
    finally:
        transport.close()

    print(f"  命令 {args.command} → {ch.key}")
    rows = show(resp)
    ok = all(r["ok"] for r in rows) if rows else False
    audit(args.command, ch.key, {
        "result": "ok" if ok else "failed",
        "acks": [{k: r.get(k) for k in ("tag", "ack", "ok")} for r in rows],
        "raw": resp.text[:400],
    }, log_path=audit_path)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# --list：只读，看白名单通道的状态（不走闸 ②，看状态不该需要使能）
# ---------------------------------------------------------------------------

def run_list() -> int:
    channels = [resolve_channel(s) for s in sorted(CONTROL_CHANNELS)]
    client, transport = connect()
    try:
        rows = client.parse_status(client.getchlstatus(channels))
    finally:
        transport.close()
    print(f"白名单通道（{BTS_HOST}:{BTS_PORT}）：")
    for r in rows:
        k = "{}-{}-{}-{}".format(r["devtype"], r["devid"], r["subdevid"], r["chlid"])
        flag = "（有预约暂停挂着）" if r.get("reservepause") == "1" else ""
        print(f"  {k:14s} {r['status']:9s} {r['status_cn']}{flag}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="control.py",
        description="BatteryLab 设备控制（写操作唯一入口）。手动、单条、跑完即退。")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="看白名单通道的状态（只读，不需使能）")

    def add(name: str, help_: str, yes: bool = False):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("channel", help="通道，如 10-6")
        if yes:
            sp.add_argument("--yes", action="store_true",
                            help="我确认这个操作（高风险命令必须带）")
        return sp

    sp = add("light", "点灯（默认开灯，零数据风险）")
    sp.add_argument("--off", dest="on", action="store_false", default=True,
                    help="关灯")
    add("clearflag", "清除标记")
    add("resetalarm", "声光报警复位（设备级）")
    add("getpause", "读预约暂停状态（只读）")
    sp = add("setpause", "设置预约暂停")
    sp.add_argument("--cycleid", default=-1, help="循环号，-1=当前")
    sp.add_argument("--stepid", default=-1, help="工步号，-1=当前")
    sp.add_argument("--timeout", default="", help="暂停超时")
    sp = add("cancelpause", "取消预约暂停")
    sp.add_argument("--cycleid", default=-1)
    sp.add_argument("--stepid", default=-1)
    add("stop", "停止该通道的测试（不可恢复！）", yes=True)
    sp = add("start", "用工步文件启动测试（不可恢复！）", yes=True)
    sp.add_argument("--step-file", required=True,
                    help="工步文件路径 —— **Windows 侧**（BTS 客户端那台机）的路径")
    sp.add_argument("--barcode", required=True, help="电池条码，写进 BTS 客户端")

    # 故意不实现的命令：**注册出来**，让拒绝文案能真的打出来
    # （不注册的话 argparse 直接报 invalid choice，用户看不到"为什么不做"）
    for name, reason in EXCLUDED.items():
        sp = sub.add_parser(name, help=f"（故意不实现）{reason}")
        sp.add_argument("channel", nargs="?", help="（不适用）")
        sp.set_defaults(excluded_reason=reason)
    return p


def _refuse(args, reason: str, audit_path: Path) -> int:
    """所有拒绝都从这里走 —— **每一次拒绝都要留痕**（"想干但被拦"是要记录的事实）。

    之前"故意不实现"和"缺 --yes"两类在进 try 之前就 return 了，没写审计 ——
    那是个漏洞：审计日志里看不到有人尝试过 broadcaststop 这件事。
    """
    try:
        audit(args.command, getattr(args, "channel", "-"),
              {"result": "blocked", "reason": reason[:200]}, log_path=audit_path)
    except Exception:
        pass
    print(f"❌ {reason}")
    return 2


def main(argv: list[str] | None = None,
         enable_file: Path = ENABLE_FILE,
         audit_path: Path = AUDIT_LOG) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "excluded_reason", None):
        return _refuse(args, f"{args.command} 是故意不实现的：{args.excluded_reason}",
                       audit_path)
    if args.command in NEEDS_YES and not getattr(args, "yes", False):
        return _refuse(args, f"{args.command} 是高风险命令"
                             "（可能顶掉正在跑的测试，数据无法恢复）。"
                             "确认要执行就加 --yes。", audit_path)

    try:
        if args.command == "list":
            return run_list()
        return run_write(args, enable_file=enable_file, audit_path=audit_path)
    except ControlError as e:
        return _refuse(args, str(e), audit_path)


if __name__ == "__main__":
    raise SystemExit(main())
