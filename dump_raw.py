#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原始回应诊断工具。

用途：当某个命令的回应解析失败时，把**原始字节**抓下来看清到底收到了什么。

关键设计：**不用 neware_client 的读取逻辑**（它遇到第一个 `</bts>` 就停，
如果是多帧或提前截断就看不出真相），而是自己读到"连接安静下来为止"，
把客户端发的一切都收全。

    python dump_raw.py --host 10.201.47.169 info
    python dump_raw.py --host 10.201.47.169 df --channel 27-188-10-1

原始字节存到 raw_dumps/，同时打印诊断信息。
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from neware_client import Channel, parse_channel

OUT_DIR = Path("raw_dumps")
IDLE_TIMEOUT = 2.0      # 连续多久没数据就认为发完了
HARD_CAP = 30.0         # 总时长上限，防止卡死


def build_payload(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<bts version="1.0">\n' + body + "\n</bts>"
    ).encode("utf-8")


def recv_all(sock: socket.socket) -> bytes:
    """读到安静为止，不在 </bts> 处提前停。"""
    chunks: list[bytes] = []
    sock.settimeout(IDLE_TIMEOUT)
    started = time.monotonic()
    while True:
        if time.monotonic() - started > HARD_CAP:
            print(f"  (达到 {HARD_CAP:g}s 上限，停止读取)", file=sys.stderr)
            break
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break                       # 安静下来了，认为发完
        if not chunk:
            chunks.append(b"<CONNECTION-CLOSED>")
            break                       # 对端主动关闭
        chunks.append(chunk)
    return b"".join(chunks)


def diagnose(raw: bytes) -> None:
    print(f"总字节数            : {len(raw):,}")
    print(f"行数                : {raw.count(b'\n') + 1:,}")

    try:
        raw.decode("utf-8")
        print("UTF-8 严格解码       : 通过")
    except UnicodeDecodeError as exc:
        print(f"UTF-8 严格解码       : 失败 -> {exc}")
        for enc in ("gbk", "gb18030", "big5", "latin-1"):
            try:
                raw.decode(enc)
                print(f"                       但可以用 {enc} 解码成功")
                break
            except UnicodeDecodeError:
                continue

    n_open = len(re.findall(rb"<bts[\s>]", raw))
    n_close = raw.count(b"</bts>")
    print(f"<bts 出现次数        : {n_open}")
    print(f"</bts> 出现次数      : {n_close}")
    if n_open != n_close:
        print("                       ⚠️ 开闭标签数量不等 -> 回应被截断，或存在多帧")
    if n_close > 1:
        print("                       ⚠️ 收到多个消息帧（客户端把回应拆成多段发）")

    print(f"以两个换行结尾       : {raw.endswith(b'\n\n')}")
    print(f"以 </bts> 结尾       : {raw.rstrip().endswith(b'</bts>')}")
    print(f"以 # 换行结尾        : {raw.endswith(b'#\r\n')}")
    print(f"连接被对端关闭       : {b'<CONNECTION-CLOSED>' in raw}")
    print(f"含 NUL 字节          : {raw.count(b'\x00')}")

    print("\n--- 开头 500 字符 ---")
    print(raw[:500].decode("utf-8", "replace"))
    print("\n--- 结尾 500 字符 ---")
    print(raw[-500:].decode("utf-8", "replace"))

    names = re.findall(rb"<([A-Za-z_][\w.-]*)", raw)
    counts: dict[str, int] = {}
    order: list[str] = []
    for n in names:
        key = n.decode("ascii", "replace")
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    print("\n--- 出现的元素（按首次出现顺序）---")
    for key in order:
        print(f"  {key:<20} x{counts[key]}")


def main() -> int:
    p = argparse.ArgumentParser(description="原始回应诊断工具")
    p.add_argument("command", help="info / status / rt / df / step，或直接写 <cmd>...</cmd>")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--channel", help="通道 devtype-devid-subdevid-chlid")
    p.add_argument("--testid", type=int, default=0)
    p.add_argument("--connect-timeout", type=float, default=10.0)
    args = p.parse_args()

    ch = parse_channel(args.channel) if args.channel else Channel(27, 188, 10, 1)
    bodies = {
        "info": "<cmd>getdevinfo</cmd>",
        "status": (f'<cmd>getchlstatus</cmd>\n<list count="1">\n'
                   f'<status {ch.attrs()}>true</status>\n</list>'),
        "rt": (f'<cmd>inquire</cmd>\n<list count="1">\n'
               f'<inquire {ch.attrs({"aux": 0, "barcode": 1})}>true</inquire>\n</list>'),
        "df": (f'<cmd>download</cmd>\n'
               f'<download {ch.attrs({"auxid": 0, "testid": args.testid, "startpos": 1, "count": 10})}/>'),
        "step": (f'<cmd>downloadStepLayer</cmd>\n'
                 f'<downloadStepLayer {ch.attrs({"testid": args.testid, "dcir": 1})}/>\n'
                 f'<V1I1 previousstep="1" type="1" value=""/>\n'
                 f'<V2I2 previousstep="0" type="0" value=""/>'),
    }
    body = bodies.get(args.command)
    if body is None:
        if "<" in args.command:
            body = args.command
        else:
            print(f"未知命令 {args.command!r}，可用：{list(bodies)}", file=sys.stderr)
            return 2

    payload = build_payload(body)
    print("=" * 68)
    print(f"发送（{len(payload)} 字节）:")
    print(payload.decode("utf-8"))
    print("=" * 68)

    try:
        sock = socket.create_connection((args.host, args.port),
                                        timeout=args.connect_timeout)
    except OSError as exc:
        print(f"连接 {args.host}:{args.port} 失败：{exc}", file=sys.stderr)
        return 1

    try:
        sock.sendall(payload + b"#\r\n")
        raw = recv_all(sock)
    finally:
        sock.close()

    print("诊断结果：")
    diagnose(raw)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bin_path = OUT_DIR / f"{args.command}_{stamp}.bin"
    txt_path = OUT_DIR / f"{args.command}_{stamp}.txt"
    bin_path.write_bytes(raw)
    txt_path.write_text(raw.decode("utf-8", "replace"), encoding="utf-8")
    print(f"\n原始字节已存到 : {bin_path}")
    print(f"文本形式已存到 : {txt_path}（可用编辑器打开看全貌）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
