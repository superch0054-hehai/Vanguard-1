#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_share.py 的离线测试。

造一份假的采集数据，验证导出的共享目录内容正确 ——
重点不是"能跑"，而是**给 AI 助手看的说明里，该有的口径规则一条都不能少**。

    python selftest_make_share.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

DATA = Path("_share_test_data")
SHARE = Path("_share_test_out")
BAR = "=" * 66


def make_fake_data() -> None:
    """造两个数据集：一个完整、一个未传完。"""
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)

    sets = [
        dict(ch="27-188-10-1", tid="247", bc="D:测试数据黄坤1", cyc=1, ret=100.0,
             rows=1241, complete=True),
        dict(ch="27-188-10-2", tid="244", bc="D672035TAA112", cyc=185, ret=17.24,
             rows=59253, complete=False),
    ]
    recs = []
    for s in sets:
        d = DATA / f"channel={s['ch']}" / f"testid={s['tid']}"
        d.mkdir(parents=True)
        (d / "steps.csv").write_text(
            "startseqid,endseqid,stepindex,stepid,cycleid,steptype,steptime,"
            "endatime,startvolt,endvolt,startcurr,endcurr,cap,eng,dcir,dbc\n"
            "1,781,1,1,1,cc,21026000,2026-09-23 15:54:02,0.49,2.00,"
            "0.000284,0.000284,0.00166,0.00234,0,[]\n"
            "782,798,2,2,1,rest,300000,2026-09-23 15:59:02,1.95,1.82,"
            "0,0,0,0,176118.7,[]\n"
            "799,1241,3,3,1,dc,11995600,2026-09-23 19:18:57,1.77,0.93,"
            "-0.000284,-0.000284,0.000947,0.00112,169166.9,[]\n",
            encoding="utf-8")
        (d / "detail.csv").write_text(
            "seqid,stepid,cycleid,steptype,testtime,atime,volt,curr,cap,eng,CPU,dbc\n"
            "1,1,0,cc,0,2026-09-23 10:03:36,0.4932,0.000284,0,0,34.9,[]\n",
            encoding="utf-8")
        recs.append({
            "key": f"{s['ch']}|{s['tid']}", "channel": s["ch"], "devtype": 27,
            "devtype_name": "BTS85", "devid": 188, "subdevid": 10,
            "chlid": int(s["ch"].split("-")[-1]), "testid": s["tid"],
            "barcode": s["bc"], "collected_at": "2026-09-25T13:52:55",
            "upload_complete": s["complete"], "step_count": 3,
            "detail_count": s["rows"], "dir": str(d),
            "parser_version": "collector 1.0",
            "soh": {
                "available": True, "cycle_count": s["cyc"],
                "cycle_range": [1, s["cyc"]],
                "first_discharge_cap_mah": 0.000947,
                "last_discharge_cap_mah": 0.000947 * s["ret"] / 100,
                "capacity_retention_pct": s["ret"],
                "coulomb_efficiency_first_pct": 57.05,
                "coulomb_efficiency_last_pct": 97.42,
                "energy_efficiency_first_pct": 47.81,
                "energy_efficiency_last_pct": 35.88,
                "dcir_first_mohm": 169166.86,
                "dcir_last_mohm": 158800.47,
            },
        })
    (DATA / "manifest.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")


class Checker:
    def __init__(self):
        self.passed, self.failed = 0, []

    def ok(self, cond, label, extra=""):
        if cond:
            self.passed += 1
            print("  PASS  " + label)
        else:
            self.failed.append(label)
            print("  FAIL  " + label + (f"   <- {extra}" if extra else ""))

    def report(self):
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        for f in self.failed:
            print("  -", f)
        return 1 if self.failed else 0


def main() -> int:
    make_fake_data()
    if SHARE.exists():
        shutil.rmtree(SHARE)

    print(BAR)
    print("运行 make_share.py")
    print(BAR)
    r = subprocess.run([sys.executable, "make_share.py",
                        "--data-dir", str(DATA), "--share-dir", str(SHARE)],
                       capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        return 1

    c = Checker()
    readme = SHARE / "README.md"
    index = SHARE / "datasets.md"

    print()
    print(BAR)
    print("① 文件都在吗")
    print(BAR)
    c.ok(readme.exists(), "README.md 生成了")
    c.ok(index.exists(), "datasets.md 生成了")
    d1 = SHARE / "channel=27-188-10-1" / "testid=247"
    c.ok((d1 / "summary.md").exists(), "summary.md 生成了")
    c.ok((d1 / "steps.csv").exists(), "steps.csv 复制过去了")
    c.ok((d1 / "detail.csv").exists(), "detail.csv 复制过去了")
    c.ok((d1 / "meta.json").exists(), "meta.json 生成了")

    print()
    print(BAR)
    print("② ★ README 里的口径规则 —— 一条都不能少")
    print(BAR)
    text = readme.read_text(encoding="utf-8")
    must_have = [
        ("圈号差 1", "明细和工步层差 1"),
        ("容量是工步内累计", "该工步内"),
        ("不要对全表取最大值", "不要对全表取最大值"),
        ("电流单位 mA（不是 A）", "不是 A"),
        ("容量单位 mAh（不是 Ah）", "不是 Ah"),
        ("能量单位 mWh（不是 Wh）", "不是 Wh"),
        ("内阻单位 mΩ", "mΩ"),
        ("testtime 是毫秒", "毫秒"),
        ("每个工步归零", "每个工步归零"),
        ("电流符号 充电正/放电负", "充电为正、放电为负"),
        ("算 SOH 优先用工步层", "优先用它算指标"),
        ("明细不要整份读", "不要整份读"),
        ("摘要里的指标不要重算", "不要重算"),
        ("testid 不唯一", "testid` 不是唯一的"),
        ("放电类工步清单", "cccd"),
        ("放电类工步清单 dc", "dc"),
        ("辅助通道 CPU 不是电池温度", "不是电池温度"),
        ("上传完整性要看 upload_complete", "upload_complete"),
    ]
    for label, needle in must_have:
        c.ok(needle in text, f"README 提到：{label}")

    print()
    print(BAR)
    print("③ README 里有可执行的示例代码")
    print(BAR)
    c.ok("pandas" in text, "给了 pandas 示例")
    c.ok("isin(DIS)" in text or "isin([" in text, "示例里正确用了放电类筛选")
    c.ok("groupby('cycleid')" in text, "示例里正确按圈分组")
    c.ok(".sum()" in text, "示例里正确求和（不是取最大值）")
    c.ok("cycleid'] == 54" in text, "示例里标注了明细圈号要 -1 对齐")

    print()
    print(BAR)
    print("④ 索引和摘要")
    print(BAR)
    idx = index.read_text(encoding="utf-8")
    c.ok("27-188-10-1" in idx and "27-188-10-2" in idx, "索引列了两个数据集")
    c.ok("D672035TAA112" in idx, "索引里有条码")
    c.ok("未传完" in idx, "索引标出了未传完的数据集")
    c.ok("17.24" in idx, "索引里有容量保持率")

    s1 = (d1 / "summary.md").read_text(encoding="utf-8")
    c.ok("D:测试数据黄坤1" in s1, "摘要里有条码")
    c.ok("169166" in s1 or "1.6917e+05" in s1, "摘要里有 DCIR")
    c.ok("直接引用，不要重算" in s1, "摘要里提醒不要重算")
    c.ok("不要整份读" in s1, "摘要里提醒别整份读明细")
    c.ok("恒流充电" in s1 or "cc" in s1, "摘要里带了工步预览")

    print()
    print(BAR)
    print("⑤ 安全阀：共享目录非空时应拒绝（防误覆盖）")
    print(BAR)
    r2 = subprocess.run([sys.executable, "make_share.py",
                         "--data-dir", str(DATA), "--share-dir", str(SHARE)],
                        capture_output=True, text=True)
    c.ok(r2.returncode != 0, "目录非空时拒绝执行")
    c.ok("--force" in (r2.stderr + r2.stdout), "提示了 --force")

    print()
    print(BAR)
    print("验证：")
    rc = c.report()

    print()
    print("--- 生成的目录结构 ---")
    for p in sorted(SHARE.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(SHARE)}  ({p.stat().st_size:,} 字节)")

    print()
    print("--- README.md 开头 40 行（给 AI 助手看的第一屏）---")
    print("\n".join(text.splitlines()[:40]))

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
