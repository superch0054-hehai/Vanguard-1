#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neware 数据采集服务 —— 交付物的主体。

职责：**把测试跑完的数据完整、正确地拿到并落盘，并关联到具体电池。**

它做四件事
----------
1. **盯着测试**：定时查通道状态，发现哪些测试跑完了
2. **抓身份**：拿条码和测试号 —— ★ 必须在通道被复用之前抓到，否则永久丢失
3. **拉数据**：等上传完整 → 拉工步层 + 明细 → 落盘
4. **记元数据**：哪份数据对应哪颗电池、什么时候采的、用什么版本解析的

设计要点（都是从现场教训来的）
------------------------------
* **状态变化触发**，不是无脑全量轮询 —— 省资源，也不给客户端添压力
* **拉之前先等上传完整**（`inquiredf`）—— 没传完就拉会缺尾部数据
* **条码只在"测试结束后、下次测试开始前"这个窗口内有效** ——
  所以测试还在跑的时候就要把 `(通道, testid)` 记下来，结束时校验一致才采
* **幂等**：同一 `(通道, testid)` 只采一次，重启后不重复采
* **数据一律落盘到 DATA_DIR**，容器化时把这个目录挂出来就行
* **心跳写 status.json** —— 否则服务静默卡死你发现不了

用法
----
    # 最简单：自动发现所有通道，用默认参数
    python3 collector.py --host 10.201.47.169

    # 只盯三个通道，每 20 秒轮询一次
    python3 collector.py --host 10.201.47.169 \
        --channels 27-188-10-1,27-188-10-2,27-188-10-3 --interval 20

    # 试跑一次（不落盘，只看会发现什么）
    python3 collector.py --host 10.201.47.169 --dry-run --once

环境变量也可以（容器里更方便）：
    BTS_HOST, BTS_PORT, BTS_CHANNELS, DATA_DIR, POLL_INTERVAL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from neware_client import (
    NewareClient,
    NewareError,
    Channel,
    TcpTransport,
    PipeTransport,
    DEVTYPE_NAMES,
    STATUS_NAMES,
    STEPTYPE_NAMES,
    parse_channel,
    write_rows,
)
from probe import soh_preview

VERSION = "1.0"

# 认为"测试已经结束"的状态
DONE_STATUSES = {"finish", "stop", "protect"}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    host: str = "10.201.47.169"
    port: int = 502
    transport: str = "tcp"
    pipe: str | None = None
    channels: list[str] = field(default_factory=list)   # 空 = 自动发现全部
    data_dir: Path = Path("./data")
    poll_interval: float = 30.0      # 轮询间隔，协议实时性不优于 5 秒
    settle_seconds: float = 30.0     # 发现结束后先等一会儿，给中位机时间
    max_wait_upload: float = 900.0   # 等上传完整的最长秒数
    reconnect_delay: float = 15.0    # 断线后重连间隔
    timeout: float = 30.0
    detail_enabled: bool = True      # 是否拉明细（数据量大，可按需关）
    dry_run: bool = False
    once: bool = False
    backfill: bool = False      # 启动时补采"已结束但没采过"的测试
    backfill_max: int = 5       # 一次最多补采几个通道，防止一上来拉爆
    log_json: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        env = os.environ
        cfg = cls()
        cfg.host = args.host or env.get("BTS_HOST", cfg.host)
        cfg.port = args.port or int(env.get("BTS_PORT", cfg.port))
        cfg.transport = args.transport or env.get("BTS_TRANSPORT", cfg.transport)
        cfg.pipe = args.pipe or env.get("BTS_PIPE")

        raw_channels = args.channels or env.get("BTS_CHANNELS", "")
        cfg.channels = [c.strip() for c in raw_channels.split(",") if c.strip()]

        cfg.data_dir = Path(args.data_dir or env.get("DATA_DIR", cfg.data_dir))
        cfg.poll_interval = args.interval or float(
            env.get("POLL_INTERVAL", cfg.poll_interval))
        cfg.settle_seconds = float(env.get("SETTLE_SECONDS", cfg.settle_seconds))
        cfg.max_wait_upload = float(
            env.get("MAX_WAIT_UPLOAD", cfg.max_wait_upload))
        cfg.timeout = args.timeout or cfg.timeout
        cfg.dry_run = args.dry_run
        cfg.once = args.once
        cfg.backfill = args.backfill
        cfg.backfill_max = args.backfill_max
        cfg.detail_enabled = not args.no_detail
        return cfg


def log(msg: str, level: str = "INFO") -> None:
    """日志走 stdout —— 容器里 journalctl/docker logs 直接收。"""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {msg}", flush=True)


# ---------------------------------------------------------------------------
# 落盘结构
# ---------------------------------------------------------------------------

def channel_dir(root: Path, ch: Channel) -> Path:
    return root / f"channel={ch.key}"


def dataset_dir(root: Path, ch: Channel, testid: str) -> Path:
    return channel_dir(root, ch) / f"testid={testid}"


class Manifest:
    """索引：每份数据集一行 JSON，方便后续检索和追溯。

    用 JSONL（每行一个 JSON）而不是一个大 JSON —— 追加安全，
    服务被强杀也不会把整个索引写坏。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def load_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def collected_keys(self) -> set[str]:
        """已经采过的 (通道|测试号)，用来做幂等。"""
        return {r.get("key", "") for r in self.load_all() if r.get("key")}


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root: Path = cfg.data_dir
        self.manifest = Manifest(self.root / "manifest.jsonl")
        self.status_path = self.root / "status.json"
        self.collected: set[str] = set()
        self.channels: list[Channel] = []
        self.last_status: dict[str, str] = {}
        # ★ 测试进行中记录的 (通道 -> 测试号)，用于结束后校验没有被复用
        self.running_testid: dict[str, str] = {}
        self.client: NewareClient | None = None
        self.transport: Any = None
        self._started_at = time.monotonic()

    # -- 基础 -------------------------------------------------------------

    def _build_transport(self):
        if self.cfg.transport == "pipe":
            return PipeTransport(self.cfg.pipe or "", self.cfg.timeout)
        return TcpTransport(self.cfg.host, self.cfg.port, self.cfg.timeout)

    def connect(self) -> None:
        self.transport = self._build_transport()
        self.transport.connect()
        self.client = NewareClient(self.transport, self.cfg.timeout)
        self.collected = self.manifest.collected_keys() if not self.cfg.dry_run else set()
        log(f"已连接 {self.cfg.transport}"
            + (f" {self.cfg.host}:{self.cfg.port}" if self.cfg.transport == "tcp" else "")
            + f"，历史已采 {len(self.collected)} 份数据集")

    def disconnect(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.client = None

    def discover(self) -> None:
        """确定要盯哪些通道。"""
        assert self.client is not None
        if self.cfg.channels:
            self.channels = [parse_channel(s) for s in self.cfg.channels]
            log(f"按参数指定，盯 {len(self.channels)} 个通道")
            return
        info = NewareClient.parse_devinfo(self.client.getdevinfo())
        self.channels = [
            Channel(c["devtype"], c["devid"], c["subdevid"], c["chlid"], c["ip"])
            for c in info["channels"]
        ]
        log(f"自动发现 {len(self.channels)} 个通道"
            f"（客户端 {info.get('client_version')}）")
        if not self.channels:
            raise NewareError("没有发现任何通道 —— 检查客户端是否连上了设备")

    # -- 轮询 -------------------------------------------------------------

    def poll_once(self) -> int:
        """跑一轮：查状态、找出刚结束的测试、采集。返回本轮采集数量。"""
        assert self.client is not None
        rows = NewareClient.parse_status(self.client.getchlstatus(self.channels))
        now: dict[str, str] = {}
        barcode_by_ch: dict[str, str] = {}

        for r in rows:
            key = f"{r['devtype']}-{r['devid']}-{r['subdevid']}-{r['chlid']}"
            now[key] = r["status"]

        # 找出"从测试中变成结束"的通道
        finished: list[tuple[Channel, str]] = []
        for ch in self.channels:
            prev = self.last_status.get(ch.key)
            cur = now.get(ch.key, "")
            if prev == "working" and cur in DONE_STATUSES:
                finished.append((ch, cur))
            if prev != cur and prev is not None:
                log(f"通道 {ch.key} 状态变化：{prev}({STATUS_NAMES.get(prev,'')}) "
                    f"→ {cur}({STATUS_NAMES.get(cur,'')})")

        # ★ 测试进行中就记下测试号 —— 结束后才能确认通道没被复用
        working = [ch for ch in self.channels if now.get(ch.key) == "working"]
        if working:
            self._note_running_testids(working)

        self.last_status = now

        count = 0
        for ch, status in finished:
            try:
                if self.collect(ch, expected_testid=self.running_testid.get(ch.key)):
                    count += 1
            except NewareError as exc:
                log(f"通道 {ch.key} 采集失败：{exc}", "ERROR")

        # 心跳放在本轮**最后**写 —— 否则里面的"已采数据集数"是上一轮的旧值
        self._write_heartbeat(now)
        return count

    def backfill(self) -> int:
        """补采"已经结束但还没采过"的测试。

        为什么需要：collector 平时只在"看着测试从 working 变成结束"时才采。
        但服务刚启动时，通道上可能已经有一个跑完的测试 —— 那个就永远采不到了。

        限制：`inquire`/`inquiredf` 只能拿到通道**最近一次**测试，
        所以补采最多只能补到每个通道的最后一个测试，更早的历史拿不到
        （那些要靠 NDA 文件或 BTSDA 导出）。

        `backfill_max` 用来兜住"通道很多、全是已完成"的情况，
        免得服务一启动就把几百个通道的测试全拉下来。
        """
        assert self.client is not None
        rows = NewareClient.parse_status(self.client.getchlstatus(self.channels))
        done = [r for r in rows if r["status"] in DONE_STATUSES]
        if not done:
            log("补采检查：没有已结束的测试")
            return 0

        todo = []
        for r in done:
            ch = Channel(r["devtype"], r["devid"], r["subdevid"], r["chlid"], r["ip"])
            todo.append(ch)
        if len(todo) > self.cfg.backfill_max:
            log(f"补采检查：发现 {len(todo)} 个已结束的通道，"
                f"按 backfill_max={self.cfg.backfill_max} 只补前几个")
            todo = todo[:self.cfg.backfill_max]

        log(f"补采检查：{len(todo)} 个通道有已结束的测试，开始补采")
        count = 0
        for ch in todo:
            try:
                if self.collect(ch):
                    count += 1
            except NewareError as exc:
                log(f"  通道 {ch.key} 补采失败：{exc}", "ERROR")
        return count

    def _note_running_testids(self, working: list[Channel]) -> None:
        """测试还在跑的时候，把 (通道 -> 测试号) 记下来。

        为什么必须这么做：条码和测试号只在**通道被下一次测试复用之前**有效。
        等测试结束了再去查，如果通道已经被复用，查到的就是新测试的身份 ——
        而旧测试的数据会变成"没有归属的孤儿"。所以进行中就要盯住。
        """
        assert self.client is not None
        try:
            checks = NewareClient.parse_inquiredf(
                self.client.inquiredf(working, 0))
        except NewareError:
            return
        for c in checks:
            key = f"{c['devtype']}-{c['devid']}-{c['subdevid']}-{c['chlid']}"
            tid = str(c["testid"])
            if self.running_testid.get(key) != tid:
                self.running_testid[key] = tid
                log(f"通道 {key} 正在跑测试 {tid}")

    # -- 采集 -------------------------------------------------------------

    def collect(self, ch: Channel, expected_testid: str | None = None) -> bool:
        """采集一个通道刚结束的那个测试。返回是否真的采了。"""
        assert self.client is not None
        log(f"开始采集 通道 {ch.key}（状态已结束）")

        # ---- 1. 先抓身份（最时间敏感的一步）----
        # 条码和测试号只在"通道被下一次测试复用之前"有效，所以第一件事就是抓它，
        # 不能先去睡几十秒 —— 万一期间通道被复用，这个测试的身份就永久丢了。
        rt = NewareClient.parse_inquire(self.client.inquire([ch], barcode=True))
        barcode = (rt[0].get("barcode") if rt else "") or ""
        checks = NewareClient.parse_inquiredf(self.client.inquiredf([ch], 0))
        if not checks:
            log("  inquiredf 没返回，跳过", "WARN")
            return False
        testid = str(checks[0]["testid"])

        # ---- 2. 幂等检查（放在等上传之前，免得白等）----
        key = f"{ch.key}|{testid}"
        if key in self.collected:
            log(f"  测试 {testid} 已经采过了（条码 `{barcode or '（空）'}`），跳过")
            return False

        # ★ 校验：通道有没有在我们眼皮底下被复用
        if expected_testid and expected_testid != testid:
            log(f"  ⚠️ 通道已被复用！测试中记录的是 {expected_testid}，"
                f"现在查到的是 {testid}。测试 {expected_testid} 的数据很可能没采到",
                "ERROR")
        log(f"  测试号 {testid}，条码 `{barcode or '（空）'}`")

        # ---- 3. 等上传完整（这一步本身就包含了必要的等待）----
        if self.cfg.settle_seconds > 0:
            time.sleep(self.cfg.settle_seconds)
        complete = self._wait_upload_complete(ch, testid)
        if not complete:
            log(f"  等上传完整超时（{self.cfg.max_wait_upload:g}s），"
                f"仍按现有数据采集，但会标记 validation 不完整", "WARN")

        # ---- 3. 拉数据 ----
        steps = NewareClient.parse_data(
            self.client.download_steplayer(ch, int(testid) if testid.isdigit() else 0,
                                           dcir=1))
        log(f"  工步层 {len(steps)} 行")

        detail: list[dict] = []
        if self.cfg.detail_enabled:
            for batch in self.client.iter_download(
                    ch, testid=int(testid) if testid.isdigit() else 0):
                detail.extend(batch)
            log(f"  明细 {len(detail)} 行")

        if self.cfg.dry_run:
            log("  （dry-run：不落盘）")
            return True

        # ---- 4. 落盘 ----
        ddir = dataset_dir(self.root, ch, testid)
        ddir.mkdir(parents=True, exist_ok=True)
        write_rows(steps, str(ddir / "steps.csv"))
        if detail:
            write_rows(detail, str(ddir / "detail.csv"))

        # ---- 5. 算派生指标 + 记元数据 ----
        soh = soh_preview(steps) if steps else {"available": False,
                                                "reason": "无工步层数据"}
        record = {
            "key": key,
            "channel": ch.key,
            "devtype": ch.devtype,
            "devtype_name": DEVTYPE_NAMES.get(ch.devtype, "?"),
            "devid": ch.devid,
            "subdevid": ch.subdevid,
            "chlid": ch.chlid,
            "testid": testid,
            "barcode": barcode,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "upload_complete": complete,
            "step_count": len(steps),
            "detail_count": len(detail),
            "dir": str(ddir),
            "parser_version": f"collector {VERSION}",
            # retention_curve（逐圈容量+保持率）保留：SOH 报告的衰减趋势分析要用它，
            # 丢掉就只能回头从 steps.csv 重算。曲线在 soh_preview 里已截断为前 20 圈。
            "soh": soh,
        }
        self.manifest.append(record)
        self.collected.add(key)
        log(f"  已落盘 → {ddir}"
            + (f"  容量保持率 {soh.get('capacity_retention_pct')}%"
               if soh.get("available") else ""))
        return True

    def _wait_upload_complete(self, ch: Channel, testid: str,
                              interval: float = 15.0) -> bool:
        """等中位机把数据传完。没传完就拉会缺尾部数据。"""
        assert self.client is not None
        deadline = time.monotonic() + self.cfg.max_wait_upload
        last_count = -1
        while True:
            try:
                checks = NewareClient.parse_inquiredf(
                    self.client.inquiredf([ch],
                                          int(testid) if testid.isdigit() else 0))
            except NewareError as exc:
                log(f"  inquiredf 出错：{exc}", "WARN")
                return False
            if not checks:
                return False
            c = checks[0]
            count = c["uploaded"]
            if c["complete"] and count == last_count:
                log(f"  上传完整（{count} 条）")
                return True
            if count != last_count:
                log(f"  上传中… 已上传 {count} 条"
                    f"{'（已完整）' if c['complete'] else ''}")
                last_count = count
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    # -- 心跳 -------------------------------------------------------------

    def _write_heartbeat(self, statuses: dict[str, str]) -> None:
        """写一个"当前状态快照"。

        它比翻日志有用得多：如果 updated_at 停在几小时前，
        说明服务卡死了 —— 这是发现"静默卡死"的唯一办法。
        """
        if self.cfg.dry_run:
            return
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "service": f"collector {VERSION}",
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "watched_channels": len(self.channels),
            "collected_datasets": len(self.collected),
            "working_now": sum(1 for v in statuses.values() if v == "working"),
            "statuses": statuses,
        }
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.status_path)     # 原子替换，避免读到半个文件
        except OSError as exc:
            log(f"心跳写入失败：{exc}", "WARN")

    # -- 主循环 -----------------------------------------------------------

    def run(self) -> int:
        self._started_at = time.monotonic()
        log(f"=== Neware 采集服务 v{VERSION} ===")
        log(f"测试机 {self.cfg.host}:{self.cfg.port}  "
            f"数据目录 {self.root.resolve()}  "
            f"轮询间隔 {self.cfg.poll_interval:g}s"
            + ("  【dry-run】" if self.cfg.dry_run else ""))

        while True:
            try:
                if self.client is None:
                    self.connect()
                    self.discover()
                    if self.cfg.backfill:
                        b = self.backfill()
                        if b:
                            log(f"启动补采了 {b} 份数据集")
                n = self.poll_once()
                if n:
                    log(f"本轮采集了 {n} 份数据集")
                if self.cfg.once:
                    log("--once：本轮结束，退出")
                    return 0
                time.sleep(self.cfg.poll_interval)
            except KeyboardInterrupt:
                log("收到中断，退出")
                return 0
            except NewareError as exc:
                log(f"通讯出错：{exc}", "ERROR")
                log(f"  {self.cfg.reconnect_delay:g} 秒后重连…")
                self.disconnect()
                time.sleep(self.cfg.reconnect_delay)
            except Exception as exc:      # noqa: BLE001
                import traceback
                log(f"未预期异常：{exc}\n{traceback.format_exc()}", "ERROR")
                self.disconnect()
                time.sleep(self.cfg.reconnect_delay)


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Neware 数据采集服务",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="测试机客户端 IP（默认 10.201.47.169）")
    p.add_argument("--port", type=int, help="端口（默认 502）")
    p.add_argument("--transport", choices=["tcp", "pipe"], help="连接方式")
    p.add_argument("--pipe", help="管道名（transport=pipe 时用）")
    p.add_argument("--channels", help="要盯的通道，逗号分隔；不填=自动发现全部")
    p.add_argument("--data-dir", help="数据落盘根目录（默认 ./data）")
    p.add_argument("--interval", type=float, help="轮询间隔秒（默认 30）")
    p.add_argument("--timeout", type=float, help="单条命令超时秒（默认 30）")
    p.add_argument("--no-detail", action="store_true",
                   help="只采工步层，不采明细（明细数据量大）")
    p.add_argument("--dry-run", action="store_true",
                   help="试跑：只打印会发现什么，不落盘")
    p.add_argument("--once", action="store_true",
                   help="只跑一轮就退出（配合 --dry-run 用来验证）")
    p.add_argument("--backfill", action="store_true",
                   help="启动时补采通道上「已结束但没采过」的测试（首次运行建议加）")
    p.add_argument("--backfill-max", type=int, default=5,
                   help="一次最多补采几个通道（默认 5，防止一上来拉爆）")
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = Config.from_args(args)
    collector = Collector(cfg)
    try:
        return collector.run()
    finally:
        collector.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
