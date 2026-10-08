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
from datetime import datetime, timedelta
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
    MAX_DOWNLOAD_COUNT,
    parse_channel,
    write_rows,
)
from probe import soh_preview

# ★ 本项目只关心这七颗：10-1~10-4 是实验数据（1/2 长循环、3/4 倍率），10-6/-7/-8 是控制演示通道。
# ⚠️ 2026-10-03 踩过：手动跑 `--incremental-now` 没带 `--channels`，旧默认是
#   「自动发现设备上全部通道」，于是把别人 58 个通道的测试数据全拉进了 data/live/。
#   现在不填 `--channels` 就只扫下面这七颗 —— 别人的测试一概不碰。
WATCH_CHANNELS = ("27-188-10-1,27-188-10-2,27-188-10-3,27-188-10-4,"
                  "27-188-10-6,27-188-10-7,27-188-10-8")

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
    # ---- 每日增量读取（live 区）----
    # 需求："一天能更新一次数据就行，不要等电池停下来才能读取数据"。
    # 增量数据写 live 区，**绝不混进正式数据集** —— 结束时那次正式采集照旧，
    # 口径、对账结果、已有报告都不受影响。
    incremental_enabled: bool = True
    incremental_hour: int = 2       # 每天几点跑一轮（失败则每 30 分钟重试到当天成功）
    incremental_now: bool = False   # 手动跑一轮就退出（验收 / 临时用）

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        env = os.environ
        cfg = cls()
        cfg.host = args.host or env.get("BTS_HOST", cfg.host)
        cfg.port = args.port or int(env.get("BTS_PORT", cfg.port))
        cfg.transport = args.transport or env.get("BTS_TRANSPORT", cfg.transport)
        cfg.pipe = args.pipe or env.get("BTS_PIPE")

        # ★ 不填 --channels / 环境变量时，只用本项目的六颗（见 WATCH_CHANNELS）：
        #   以前这里是"空 → 自动发现设备上全部通道"，2026-10-03 因此误拉了别人 58 个通道。
        raw_channels = (args.channels or env.get("BTS_CHANNELS")
                        or WATCH_CHANNELS)
        cfg.channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
        if raw_channels == WATCH_CHANNELS and not args.channels:
            print(f"  通道未指定 → 只采集本项目通道：{WATCH_CHANNELS}"
                  "（要别的通道请显式给 --channels）")

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
        cfg.incremental_enabled = not args.no_incremental
        cfg.incremental_hour = getattr(args, "incremental_hour", 2)
        cfg.incremental_now = args.incremental_now
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

    def load_all(self, dedupe: bool = True) -> list[dict[str, Any]]:
        """读出索引。

        ★ 2026-10-06：默认**按 key 去重、后来的那一行算数**。
        为什么：水桶模型下同一轮测试会被记多次（第一次加水时记一笔、
        结束时再记一笔更新），JSONL 只能追加。不去重的话，
        "数据集清单"里同一份数据会重复出现好几次。
        """
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
        if not dedupe:
            return out
        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for r in out:
            k = r.get("key") or f"#{len(order)}"
            if k not in latest:
                order.append(k)
            latest[k] = r          # 后来的覆盖先前的
        return [latest[k] for k in order]

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
        # 每日增量读取：下一次该跑的时间点。None = 还没排过（启动时先立即补拉一轮）。
        self.next_incremental: datetime | None = None

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
                # ★ 水桶模型：结束时**不是**"整采一份到另一个位置"，
                #   而是把"上一次加水 → 结束"这最后一段补进同一个桶（finish_pass）
                if self.finish_pass(ch, expected_testid=self.running_testid.get(ch.key)):
                    count += 1
            except NewareError as exc:
                log(f"通道 {ch.key} 补最后一段失败：{exc}", "ERROR")

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
                # ★ 已经建过桶（我们盯着它跑过）→ 只需补最后一勺，不必整份重拉；
                #   完全没见过的已结束测试 → 只能整份拉一次。
                tid = self._current_testid(ch)
                if tid and self._count_rows(self.bucket_dir(ch, tid) / "detail.csv") > 0:
                    if self.finish_pass(ch):
                        count += 1
                elif self.collect(ch):
                    count += 1
            except NewareError as exc:
                log(f"  通道 {ch.key} 补采失败：{exc}", "ERROR")
        return count

    def _current_testid(self, ch: Channel) -> str:
        """问设备：这个通道上现在是哪个测试号。拿不到就返回空。"""
        assert self.client is not None
        checks = NewareClient.parse_inquiredf(self.client.inquiredf([ch], 0))
        return str(checks[0].get("testid") or "") if checks else ""

    def _barcode_of(self, ch: Channel) -> str:
        """问设备：这个通道的条码（样品身份挂在这儿）。

        拿不到就返回空串 —— 条码本来就是可空的（这批测试一直是空的），
        但**不能因为拿不到就整个记录失败**：那会把这条测试从索引里弄丢。
        """
        assert self.client is not None
        try:
            rt = NewareClient.parse_inquire(self.client.inquire([ch], barcode=True))
            return (rt[0].get("barcode") if rt else "") or ""
        except (NewareError, Exception):      # noqa: BLE001
            return ""

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

    def collect(self, ch: Channel, expected_testid: str | None = None,
                force: bool = False) -> bool:
        """**整份重拉**一个通道刚结束的测试（覆盖写进同一个桶）。返回是否真的拉了。

        ★ 2026-10-06 起它不再是常态：常态是"天天往桶里加水 + 结束时补最后一勺"。
        它现在只在两种情况下被调用：
          ① 兜底：`finish_pass` 补完发现"桶里条数 ≠ 设备条数"（宁缺勿乱，重来一遍）
          ② 补采：启动时发现某个通道上有个**我们没见过的**已结束测试（`backfill`）
        """
        assert self.client is not None
        log(f"开始整份采集 通道 {ch.key}（状态已结束）")

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
        if key in self.collected and not force:
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
        # 桶状态：整份拉完 = 这轮已结束（报告据此决定要不要剔除被截断的末圈）
        self._write_progress(ddir, testid, len(detail),
                             int(checks[0].get("uploaded") or 0), finished=True)
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

    # -- 数据桶：一个测试 = 一个目录，天天往里加水 ------------------------
    #
    # ★ 2026-10-06 改（用户的"水桶模型"）：以前是**两个位置**——
    #   天天增量写 `live/`，测试结束时再整采一份到 `channel=/testid=`，
    #   同一轮测试的数据会存在两处，报告还要选 "live 还是正式"。
    #   现在合成**一个桶**：
    #     · 每天 02:00 往里追加一段（incremental_pass）
    #     · 实验人员在 BTS 上点结束时，把"上一次加水 → 结束"这最后一段也加进去（finish_pass）
    #     · 加完**核对总条数**；对不上就整份重拉一次（collect）—— 宁可重来，不留半桶
    #   桶里除数据外还有两样东西：
    #     · progress.json —— 水位（取到第几条）+ 这轮跑完没有（finished）
    #     · steps.csv     —— 工步层，每次加水都整份刷新（它很小）

    def bucket_dir(self, ch: Channel, testid: str) -> Path:
        """这一轮测试的"桶"。数据天天往里加，结束时补齐，始终只有这一份。"""
        return dataset_dir(self.root, ch, testid)

    @staticmethod
    def _count_rows(path: Path) -> int:
        """CSV 的数据行数（不含表头）。文件不在就是 0。"""
        if not path.exists():
            return 0
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return max(0, sum(1 for _ in fh) - 1)

    def _refresh_steps(self, ch: Channel, testid: str,
                       ddir: Path) -> list[dict[str, Any]]:
        """整份刷新桶里的工步层（它很小，覆盖写最省心），**返回工步行**。

        为什么每次加水都刷：报告/自检在**任何时刻**都可能读这个桶，
        工步层缺了它们就只能等"结束"—— 那就又回到"必须等结束"的老路了。
        返回值用来算 soh（索引记录里要有它，否则下游 KeyError）。
        """
        assert self.client is not None
        steps = NewareClient.parse_data(
            self.client.download_steplayer(ch, int(testid) if testid.isdigit() else 0, dcir=1))
        if steps:
            write_rows(steps, str(ddir / "steps.csv"))
        return steps

    @staticmethod
    def _read_progress(path: Path) -> dict[str, Any] | None:
        """读水位。返回 None 表示水位文件坏了 —— 这时**拒绝拉取**。

        为什么不当作 0：当 0 会从头重拉并追加，live 文件里全是重复行。
        宁可缺一天，不可写乱数据。
        """
        if not path.exists():
            return {}
        try:
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else None
        except (json.JSONDecodeError, OSError) as exc:
            log(f"  [live] 水位文件损坏（{path.name}：{exc}），本轮跳过该通道。"
                "修复或删除后下轮会重新从头拉。", "ERROR")
            return None

    @staticmethod
    def _append_rows(rows: list[dict[str, Any]], path: Path) -> int:
        """追加写 CSV。列名规则与 write_rows 一致（首行键序）。

        ★ 编码的坑：write_rows 用 utf-8-sig（文件头带 BOM，Excel 才不乱码）。
        但 utf-8-sig 在 **append 模式**下每次写入都会再吐一个 BOM 到文件中间，
        所以这里新文件才用 utf-8-sig，追加必须用 utf-8。
        """
        import csv as _csv
        if not rows:
            return 0
        flat = [{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                 for k, v in row.items()} for row in rows]
        fieldnames: list[str] = []
        for row in flat:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        new_file = not path.exists()
        enc = "utf-8-sig" if new_file else "utf-8"
        with open(path, "a" if not new_file else "w", newline="", encoding=enc) as fh:
            writer = _csv.DictWriter(fh, fieldnames=fieldnames)
            if new_file:
                writer.writeheader()
            writer.writerows(flat)
        return len(flat)

    @staticmethod
    def _write_progress(pdir: Path, testid: str, rows_collected: int,
                        total_on_device: int, finished: bool) -> None:
        """落水位 + **"这轮跑完没有"**。

        ★ finished 是水桶模型的关键：它替代了原来靠"数据在哪个目录"
        （`live/` = 进行中、`channel=/` = 已结束）来判断状态的做法。
        报告要靠它决定"要不要剔除被截断的末圈"，判错了就会出现
        "最新比容量骤降"的假象（实测踩过）。
        """
        prog = {
            "testid": testid,
            "rows_collected": rows_collected,
            "total_on_device": total_on_device,
            "finished": bool(finished),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        (pdir / "progress.json").write_text(
            json.dumps(prog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def _manifest_upsert(self, ch: Channel, testid: str, ddir: Path,
                         steps: list[dict[str, Any]], barcode: str,
                         complete: bool) -> None:
        """在索引（manifest.jsonl）里记一笔 —— **字段与 collect() 的记录完全一致**。

        为什么必须同格式：下游（make_share 的 summary、报告）会读 `soh`、`barcode`
        这些字段，缺一个就是 KeyError。2026-10-06 第一版就漏了 `soh`，测试当场抓到。

        JSONL 只能追加，所以同一 key 会出现多行；**读的一侧按 key 取最后一行**（= 更新）。
        """
        key = f"{ch.key}|{testid}"
        soh = soh_preview(steps) if steps else {"available": False,
                                                "reason": "无工步层数据"}
        self.manifest.append({
            "key": key, "channel": ch.key, "testid": testid,
            "devtype": ch.devtype, "devid": ch.devid,
            "subdevid": ch.subdevid, "chlid": ch.chlid,
            "devtype_name": DEVTYPE_NAMES.get(ch.devtype, "?"),
            "barcode": barcode,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "upload_complete": bool(complete),
            "step_count": len(steps),
            "detail_count": self._count_rows(ddir / "detail.csv"),
            "dir": str(ddir),
            "parser_version": f"collector {VERSION}",
            "soh": soh,
        })
        self.collected.add(key)

    def finish_pass(self, ch: Channel, expected_testid: str | None = None) -> bool:
        """通道上的测试**跑完了**：把最后一段水加进桶 → 核对 → 标记完成。

        这就是"水桶的最后一勺"：实验人员在 BTS 上点结束 → 这里把
        "上一次加水 → 结束"那一段也加进去，桶就满了。

        兜底：加完**核对总条数**；对不上就整份重拉一次（`collect(force=True)`）
        —— 宁可重来，也不留半桶。原来"结束时整采一份到另一个位置"那条路，
        现在降级成这里的兜底，不再是常态。
        """
        assert self.client is not None
        log(f"测试结束：通道 {ch.key} —— 补最后一段")
        checks = NewareClient.parse_inquiredf(self.client.inquiredf([ch], 0))
        if not checks:
            log("  inquiredf 没返回，跳过", "WARN")
            return False
        testid = str(checks[0].get("testid") or "")
        if not testid:
            log("  拿不到测试号，跳过", "WARN")
            return False
        if expected_testid and expected_testid != testid:
            log(f"  ⚠️ 通道已被复用！测试中记录的是 {expected_testid}，"
                f"现在查到的是 {testid} —— 那一轮的数据可能已经拿不到了", "ERROR")

        # 结束瞬间可能还在传，先等它传完（否则拿到的是偏小的总条数）
        complete = self._wait_upload_complete(ch, testid)
        # 把尾巴加进去：和日常增量**同一套水位逻辑**，所以不会重复加
        self.incremental_pass([ch])

        pdir = self.bucket_dir(ch, testid)
        got = self._count_rows(pdir / "detail.csv")
        total = int(checks[0].get("uploaded") or 0)
        if total > 0 and got >= total:
            self._write_progress(pdir, testid, got, total, finished=True)
            self._manifest_upsert(ch, testid,
                                  pdir, self._refresh_steps(ch, testid, pdir),
                                  self._barcode_of(ch), complete=complete)
            log(f"  ✅ 桶已满：测试 {testid} 共 {got} 行（设备 {total}），标记为已结束")
            return True
        log(f"  ⚠️ 桶里 {got} 行、设备 {total} 行 —— 对不上，整份重拉一次", "WARN")
        ok = self.collect(ch, expected_testid=testid, force=True)
        if ok:
            self._write_progress(pdir, testid, self._count_rows(pdir / "detail.csv"),
                                 total, finished=True)
        return ok

    def incremental_pass(self, channels: list[Channel]) -> int:
        """对正在跑测试的通道做一轮增量读取，追加到 live 区。返回新增行数。

        每个通道：inquiredf 拿 testid 和数据点总数 → 和水位比 →
        只 download 新增那段（分块，单次 ≤ MAX_DOWNLOAD_COUNT）→ 追加 → 更新水位。
        单个通道出错只影响它自己，不抛出 —— 调用方靠返回值和日志感知。
        """
        assert self.client is not None
        added_total = 0
        for ch in channels:
            try:
                checks = NewareClient.parse_inquiredf(self.client.inquiredf([ch], 0))
                if not checks:
                    log(f"  [live] {ch.key}：inquiredf 没返回，跳过", "WARN")
                    continue
                c = checks[0]
                testid = str(c.get("testid") or "")
                count = int(c.get("uploaded") or 0)
                if not testid or count <= 0:
                    log(f"  [live] {ch.key}：还没有可读的测试数据，跳过")
                    continue
                # ★ 2026-10-06：这里原来有一条 `if complete: 跳过，等正式采集接住`。
                #   水桶模型下它是错的 —— "传完了"恰恰是**最该把最后一段加进去**的时刻。
                #   现在不跳：结束那一勺由 finish_pass 补，水位保证不会重复加。

                pdir = self.bucket_dir(ch, testid)
                prog_path = pdir / "progress.json"

                # ★ 没有水位记录、但桶里已经有数据 —— 绝不能假定水位是 0！
                #   那种桶是"迁移过来的老正式数据集"或"手工拷进来的"，按 0 从头追加
                #   会把已有的行再来一遍（重复行，且从结果上看不出来）。
                #   已有行数 == 设备条数 → 只补一条水位记录，一行都不拉；
                #   对不上 → 交给整份重拉（覆盖写，最安全）。
                if not prog_path.exists() and self._count_rows(pdir / "detail.csv") > 0:
                    have = self._count_rows(pdir / "detail.csv")
                    if have == count:
                        self._write_progress(pdir, testid, have, count,
                                             finished=bool(c.get("complete")))
                        log(f"  [桶] {ch.key}：桶已有 {have} 行、与设备一致 "
                            "→ 只补水位，不重复拉")
                    else:
                        log(f"  [桶] {ch.key}：桶有 {have} 行、设备 {count} 行、且无水位记录 "
                            "→ 整份重拉（覆盖写）", "WARN")
                        self.collect(ch, force=True)
                    continue

                prog = self._read_progress(prog_path)
                if prog is None:
                    continue
                wm = int(prog.get("rows_collected", 0))
                old_testid = prog.get("testid")
                if old_testid and old_testid != testid:
                    # 通道被复用了：换了一个桶（按 testid 分目录），水位从 0 起
                    log(f"  [桶] {ch.key}：测试号变了（{old_testid} → {testid}），"
                        "另起一个桶、水位清零")
                    wm = 0
                if count < wm:
                    log(f"  [桶] {ch.key}：设备端条数 {count} 比水位 {wm} 还小 —— "
                        "数据异常，拒绝拉取（宁缺勿乱）", "ERROR")
                    continue
                if count == wm:
                    continue        # 没有新增，什么都不做

                rows: list[dict[str, Any]] = []
                pos = wm + 1
                while pos <= count:
                    n = min(MAX_DOWNLOAD_COUNT, count - pos + 1)
                    rows.extend(self.client.parse_data(
                        self.client.download(ch, auxid=0, testid=0,
                                             startpos=pos, count=n)))
                    pos += n
                if self.cfg.dry_run:
                    log(f"  [桶] {ch.key}：dry-run，本应新增 {len(rows)} 行")
                    continue

                pdir.mkdir(parents=True, exist_ok=True)
                n_added = self._append_rows(rows, pdir / "detail.csv")
                added_total += n_added
                steps = self._refresh_steps(ch, testid, pdir)
                self._write_progress(pdir, testid, wm + n_added, count,
                                     finished=bool(c.get("complete")))
                # 桶一出现就要在索引里 —— 否则报告/Agent 看不到这轮测试
                self._manifest_upsert(ch, testid, pdir, steps, self._barcode_of(ch),
                                      complete=bool(c.get("complete")))
                log(f"  [桶] {ch.key}：测试 {testid} 新增 {n_added} 行"
                    f"（{wm} → {count}）" + ("　已传完" if c.get("complete") else ""))
            except NewareError as exc:
                log(f"  [live] {ch.key}：增量读取失败（{exc}），下轮重试", "ERROR")
            except Exception as exc:      # noqa: BLE001
                log(f"  [live] {ch.key}：未预期异常 {exc}，下轮重试", "ERROR")
        return added_total

    def _next_incremental_time(self, now: datetime) -> datetime:
        """下一次增量时间点：今天（或明天）的 incremental_hour 点整。"""
        nxt = now.replace(hour=self.cfg.incremental_hour, minute=0,
                          second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt

    def _run_incremental_safely(self) -> None:
        """包一层安全：增量失败绝不影响 30 秒轮询主流程。"""
        now = datetime.now()
        try:
            working = [c for c in self.channels
                       if self.last_status.get(c.key) == "working"]
            if not working:
                log("增量读取：当前没有正在跑测试的通道，跳过")
            else:
                added = self.incremental_pass(working)
                log(f"增量读取完成：本轮新增 {added} 行")
            self.next_incremental = self._next_incremental_time(now)
            log(f"下一次增量读取：{self.next_incremental:%Y-%m-%d %H:%M}")
        except Exception as exc:      # noqa: BLE001
            log(f"增量读取失败（{exc}），30 分钟后重试", "ERROR")
            self.next_incremental = now + timedelta(minutes=30)

    def _maybe_incremental(self) -> None:
        if not self.cfg.incremental_enabled:
            return
        now = datetime.now()
        if self.next_incremental is None:
            # 启动补拉：服务重启期间错过的数据在这里补上，然后排明天的点
            self._run_incremental_safely()
            return
        if now >= self.next_incremental:
            self._run_incremental_safely()

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

        if self.cfg.incremental_now:
            # 手动跑一轮增量就退出（--incremental-now，验收 / 临时补拉用）
            self.connect()
            self.discover()
            self.poll_once()          # 先拿一次状态，last_status 就绪
            self._run_incremental_safely()
            self.disconnect()
            return 0

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
                self._maybe_incremental()
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
    p.add_argument("--channels",
                   help="要盯的通道，逗号分隔；不填=只扫本项目自己的通道"
                        f"（{WATCH_CHANNELS}），**不会**再去扫设备上别人的测试")
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
    p.add_argument("--no-incremental", action="store_true",
                   help="关闭每日增量读取（live 区）")
    p.add_argument("--incremental-hour", type=int, default=2,
                   help="每天几点跑一轮增量读取（默认 2 点；失败每 30 分钟重试）")
    p.add_argument("--incremental-now", action="store_true",
                   help="立即手动跑一轮增量读取然后退出（验收 / 临时补拉用）")
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
