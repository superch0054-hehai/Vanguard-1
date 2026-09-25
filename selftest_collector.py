#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.py 的离线测试：模拟一个完整的"测试跑完 → 自动采集"过程。

不需要真机。重点验证七件事：
  1. 测试进行中会记下测试号
  2. 状态变成 finish 后会自动采集
  3. 文件按预期落盘（steps.csv / detail.csv）
  4. manifest 记录了条码和派生指标
  5. 心跳 status.json 写了
  6. 幂等 —— 再跑一次不重复采
  7. 通道被复用时能报警

    python selftest_collector.py
"""

import json
import re
import shutil
import sys
from pathlib import Path

import collector as C
from neware_client import NewareError, Transport

BAR = "=" * 66
OUT = Path("_collector_test_out")
TESTID = "244"
BARCODE = "D672035TAA112"


# ---------------------------------------------------------------------------
# 模拟客户端
# ---------------------------------------------------------------------------

class FakeTransport(Transport):
    """一台可以改状态的假客户端。"""

    name = "fake"

    def __init__(self) -> None:
        self.status = "working"
        self.testid = TESTID
        self.barcode = BARCODE
        self.uploaded = 1200
        self.complete = True
        self.detail_rows = 30
        self.calls: list[str] = []

    def connect(self) -> None:
        pass

    def send_recv(self, payload: bytes, timeout=None) -> bytes:
        text = payload.decode("utf-8")
        m = re.search(r"<cmd>([^<]+)</cmd>", text)
        cmd = m.group(1) if m else ""
        self.calls.append(cmd)
        if cmd == "getdevinfo":
            body = ('<cmd>getdevinfo_resp</cmd>'
                    '<client version="BTS Client 2025.11.24(R3) (64)" />'
                    '<serverip count="1"><server ip="127.0.0.1" port="3306"/></serverip>'
                    '<middle count="3">'
                    '<channel ip="127.0.0.1" devtype="27" devid="188" subdevid="10" Channelid="1">true</channel>'
                    '<channel ip="127.0.0.1" devtype="27" devid="188" subdevid="10" Channelid="2">true</channel>'
                    '<channel ip="127.0.0.1" devtype="27" devid="188" subdevid="10" Channelid="3">true</channel>'
                    '</middle>')
        elif cmd == "getchlstatus":
            chs = re.findall(r'chlid="(\d+)"', text)
            rows = "".join(
                f'<status ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
                f'chlid="{c}" reservepause="0">{self.status}</status>' for c in chs)
            body = f'<cmd>getchlstatus_resp</cmd><list count="{len(chs)}">{rows}</list>'
        elif cmd == "inquire":
            body = ('<cmd>inquire_resp</cmd><list count="1">'
                    f'<inquire dev="27-188-10-2-0" cycle_id="55" step_id="10" '
                    f'step_type="rest" workstatus="{self.status}" '
                    f'barcode="{self.barcode}" current="0" voltage="1.5" '
                    f'capacity="0" energy="0" totaltime="43726" relativetime="143.9" '
                    f'auxvol="0" open_or_close="0" log_code="0" CPU1="34.7" />'
                    '</list>')
        elif cmd == "inquiredf":
            body = ('<cmd>inquiredf_resp</cmd><list count="1">'
                    f'<chl devtype="27" devid="188" subdevid="10" chlid="2" '
                    f'testid="{self.testid}" count="{self.uploaded}">'
                    f'{"true" if self.complete else "false"}</chl></list>')
        elif cmd == "downloadStepLayer":
            rows = "".join(
                f'<data startseqid="{i*10}" endseqid="{i*10+9}" stepindex="{i}" '
                f'stepid="{i}" cycleid="{i}" steptype="{"cc" if i % 2 else "dc"}" '
                f'steptime="60000" endatime="2026-09-24 10:00:00" startvolt="3.0" '
                f'endvolt="3.3" startcurr="{"0.2" if i % 2 else "-0.2"}" '
                f'endcurr="0.2" cap="{0.2 - i*0.001:.6f}" eng="{0.8 - i*0.004:.6f}" '
                f'dcir="{1200000 + i*1000}" />' for i in range(1, 6))
            body = ('<cmd>downloadStepLayer_resp</cmd>'
                    f'<list count="5">{rows}</list>')
        elif cmd == "download":
            sp = int(re.search(r'startpos="(\d+)"', text).group(1))
            cnt = int(re.search(r'count="(\d+)"', text).group(1))
            end = min(sp + cnt, self.detail_rows + 1)
            rows = "".join(
                f'<data seqid="{i}" stepid="1" cycleid="1" steptype="cc" '
                f'testtime="{i*1000}" atime="2026-09-24 10:00:{i:02d}" '
                f'volt="3.5" curr="0.2" cap="{i*1e-5:.9f}" eng="{i*2e-5:.9f}" '
                f'CPU="34.7" />' for i in range(sp, end))
            body = f'<cmd>download_resp</cmd><list count="{end-sp}">{rows}</list>'
        else:
            raise NewareError(f"测试未准备该命令：{cmd}")
        return ('<?xml version="1.0" encoding="UTF-8"?><bts version="1.0">'
                + body + '</bts>').encode("utf-8") + b"\n\n#\r\n"


# ---------------------------------------------------------------------------

class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, cond: bool, label: str, extra: str = "") -> None:
        if cond:
            self.passed += 1
            print("  PASS  " + label)
        else:
            self.failed.append(label)
            print("  FAIL  " + label + (f"   <- {extra}" if extra else ""))

    def report(self) -> int:
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        for f in self.failed:
            print("  -", f)
        return 1 if self.failed else 0


def make_collector(fake: FakeTransport, settle: float = 0.0) -> C.Collector:
    cfg = C.Config(
        host="1.2.3.4",
        channels=["27-188-10-2"],
        data_dir=OUT,
        poll_interval=0,
        settle_seconds=settle,
        max_wait_upload=5,
    )
    col = C.Collector(cfg)
    col.transport = fake
    from neware_client import NewareClient
    col.client = NewareClient(fake, 5.0)
    col.channels = [C.parse_channel("27-188-10-2")]
    col.collected = set()
    return col


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    c = Checker()

    fake = FakeTransport()
    col = make_collector(fake)

    print(BAR)
    print("① 测试进行中：应该记下测试号，不采集")
    print(BAR)
    n = col.poll_once()
    c.ok(n == 0, "没结束就不采集")
    c.ok(col.running_testid.get("27-188-10-2") == TESTID,
         f"记下了进行中的测试号（{col.running_testid}）")
    c.ok(col.status_path.exists(), "心跳文件已写")

    print()
    print(BAR)
    print("② 测试结束：应该自动采集")
    print(BAR)
    fake.status = "finish"
    n = col.poll_once()
    c.ok(n == 1, "采集了 1 份数据集", f"实际 {n}")

    ddir = OUT / "channel=27-188-10-2" / f"testid={TESTID}"
    c.ok((ddir / "steps.csv").exists(), "工步层已落盘")
    c.ok((ddir / "detail.csv").exists(), "明细已落盘")

    recs = col.manifest.load_all()
    c.ok(len(recs) == 1, "manifest 有 1 条记录")
    if recs:
        r = recs[0]
        c.ok(r["barcode"] == BARCODE, f"记录了条码（{r['barcode']}）")
        c.ok(r["testid"] == TESTID, "记录了测试号")
        c.ok(r["upload_complete"] is True, "记录了上传完整性")
        c.ok(r["step_count"] == 5, f"工步数正确（{r['step_count']}）")
        c.ok(r["detail_count"] == 30, f"明细行数正确（{r['detail_count']}）")
        c.ok(r["soh"].get("available") is True, "算了 SOH 指标")
        c.ok("collected_at" in r and "parser_version" in r, "元数据字段齐全")

    hb = json.loads(col.status_path.read_text(encoding="utf-8"))
    c.ok(hb["collected_datasets"] == 1, "心跳里数据集数正确")
    c.ok("updated_at" in hb, "心跳有时间戳")

    print()
    print(BAR)
    print("③ 幂等：再跑一轮不应重复采集")
    print(BAR)
    n = col.poll_once()
    c.ok(n == 0, "不重复采集")
    c.ok(len(col.manifest.load_all()) == 1, "manifest 仍只有 1 条")

    print()
    print(BAR)
    print("④ 通道被复用：应报警")
    print(BAR)
    fake.testid = "999"          # 模拟通道被下一次测试占用了
    fake.status = "working"
    col.last_status = {"27-188-10-2": "finish"}
    n = col.poll_once()
    c.ok(col.running_testid.get("27-188-10-2") == "999",
         "识别出新测试 999 开始占用通道")
    c.ok(n == 0, "working 状态不采集")

    print()
    print(BAR)
    print("⑤ 重启后恢复：从 manifest 读回已采记录")
    print(BAR)
    col2 = make_collector(fake)
    col2.collected = col2.manifest.collected_keys()
    c.ok(f"{'27-188-10-2'}|{TESTID}" in col2.collected, "重启后仍知道已采过")

    print()
    print(BAR)
    print("⑥ 已采过的测试：应当立即跳过，不能先白等 settle 时间")
    print(BAR)
    import time as _t
    fake2 = FakeTransport()
    fake2.status = "finish"
    col3 = make_collector(fake2, settle=8.0)
    col3.collected = {f"27-188-10-2|{TESTID}"}      # 假装已经采过
    t0 = _t.monotonic()
    got = col3.collect(C.parse_channel("27-188-10-2"))
    dt = _t.monotonic() - t0
    c.ok(got is False, "返回 False（跳过了）")
    c.ok(dt < 3.0, f"耗时 {dt:.1f}s < 3s（没白等 8s 的 settle）")

    print()
    print(BAR)
    print("验证：")
    rc = c.report()

    print()
    print("--- 落盘结构 ---")
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(OUT)}  ({p.stat().st_size:,} 字节)")

    print()
    print("--- manifest.jsonl ---")
    print(OUT.joinpath("manifest.jsonl").read_text(encoding="utf-8").strip()[:400])

    print()
    print("--- status.json ---")
    print(OUT.joinpath("status.json").read_text(encoding="utf-8")[:300])

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
