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
             rows=1241, complete=True, curve=True),
        dict(ch="27-188-10-2", tid="244", bc="D672035TAA112", cyc=185, ret=17.24,
             rows=59253, complete=False, curve=False),
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
                "dcir_growth_pct": 93.87,
                # 只给第一个数据集逐圈曲线；第二个故意不给，
                # 用来覆盖 make_share 里「缺曲线」的回退分支
                **({"retention_curve": [
                    {"cycle": 1, "dis_cap": 0.000947, "retention_pct": 100.0},
                    {"cycle": 2, "dis_cap": 0.000945, "retention_pct": 99.79},
                    {"cycle": 3, "dis_cap": 0.000943, "retention_pct": 99.58},
                    {"cycle": 4, "dis_cap": 0.000941, "retention_pct": 99.37},
                    {"cycle": 5, "dis_cap": 0.000939, "retention_pct": 99.16},
                    {"cycle": 6, "dis_cap": 0.000937, "retention_pct": 98.94},
                    {"cycle": 7, "dis_cap": 0.000935, "retention_pct": 98.73},
                    {"cycle": 8, "dis_cap": 0.000933, "retention_pct": 98.52},
                    {"cycle": 9, "dis_cap": 0.000931, "retention_pct": 98.31},
                    # 第 10 圈故意掉一大截 —— 用来触发「衰减加速点」启发式
                    {"cycle": 10, "dis_cap": 0.000900, "retention_pct": 95.04},
                    {"note": "…其余圈略，完整曲线见 JSON"},
                ]} if s["curve"] else {}),
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
        ("省事规则一节", "省事规则"),
        ("heredoc 会执行失败", "会失败"),
        ("不要 pip install（不通外网）", "不通外网"),
        ("不要读自己的 memory/chats", "不要读你自己"),
        ("目录结构是固定的", "channel=<通道>/testid=<测试号>"),
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
    print("④b DCIR 增长率 + 逐圈衰减曲线（SOH 报告要用）")
    print(BAR)
    c.ok("DCIR 增长率" in s1, "摘要里有 DCIR 增长率")
    c.ok("93.9" in s1, "DCIR 增长率渲染成 93.9（不是 9e+01 那种科学计数）")
    c.ok("逐圈容量衰减" in s1, "摘要里有逐圈衰减小节")
    c.ok("| 循环 | 放电容量 (mAh) | 容量保持率 (%) | 单圈衰减率 (%) |" in s1,
         "衰减表有「单圈衰减率」列")
    c.ok("| 2 |" in s1 and "0.211" in s1, "曲线数据逐行渲染出来了（含量化后的单圈衰减率）")
    c.ok("工步层 3 行" in s1, "「数据文件」一节的行数没被循环变量遮蔽搞成 0")
    c.ok("明细 1241 行" in s1, "明细行数也正确（这是被 r 遮蔽坑过的地方）")
    c.ok("衰减加速点（启发式）" in s1, "给出了衰减加速点")
    c.ok("**10**" in s1, "加速点定位到第 10 圈（夹具里故意掉在那一圈）")
    c.ok("不是唯一判据" in s1, "标注了这是启发式、不是定论")
    c.ok("其余圈略" in s1, "曲线被截断时把提示 note 也带上")
    s2 = (SHARE / "channel=27-188-10-2" / "testid=244" / "summary.md").read_text(
        encoding="utf-8")
    c.ok("逐圈容量衰减" in s2, "第二个数据集也有这一节")
    c.ok("现场补算" in s2, "manifest 里没曲线的老数据，从 steps.csv 补算出来")

    print()
    print(BAR)
    print("④c 逐圈分析用的三个纯函数（直接调，不走子进程）")
    print(BAR)
    import make_share as M

    # with_decay：第一圈没有「上一圈」，衰减率应为 None；之后按首圈容量折算
    rows = M.with_decay([{"cycle": 1, "dis_cap": 100.0, "retention_pct": 100.0},
                         {"cycle": 2, "dis_cap": 99.0, "retention_pct": 99.0},
                         {"cycle": 3, "dis_cap": 97.0, "retention_pct": 97.0}])
    c.ok(rows[0]["decay_pct"] is None, "首圈不报衰减率（没有上一圈可比）")
    c.ok(abs(rows[1]["decay_pct"] - 1.0) < 1e-9, "第 2 圈衰减率 = 1.0%")
    c.ok(abs(rows[2]["decay_pct"] - 2.0) < 1e-9, "第 3 圈衰减率 = 2.0%")

    # knee_point：平缓然后突然掉一截 → 应指出那一圈
    flat = [{"cycle": i, "dis_cap": 100.0 - 0.2 * (i - 1), "retention_pct": 0}
            for i in range(1, 10)]
    flat.append({"cycle": 10, "dis_cap": 95.0, "retention_pct": 95.0})
    k = M.knee_point(M.with_decay(flat))
    c.ok(k is not None and k["cycle"] == 10, "突然掉一截时定位到那一圈")
    c.ok(k and k["ratio"] > 3, "并给出相对基线的倍数")
    c.ok(M.knee_point(M.with_decay(
        [{"cycle": i, "dis_cap": 100.0 - 0.2 * (i - 1), "retention_pct": 0}
         for i in range(1, 12)])) is None, "匀速衰减时不硬报一个加速点")
    c.ok(M.knee_point([]) is None, "空输入不炸")

    # thin_rows：长了要抽稀，且首尾必须保留
    long = [{"cycle": i} for i in range(1, 201)]
    picked, stride = M.thin_rows(long, limit=40)
    c.ok(stride > 1 and len(picked) <= 42, f"200 行抽稀成 {len(picked)} 行（步长 {stride}）")
    c.ok(picked[0]["cycle"] == 1 and picked[-1]["cycle"] == 200, "抽稀后首尾都在")
    short = [{"cycle": i} for i in range(1, 11)]
    c.ok(M.thin_rows(short) == (short, 1), "行数不多时原样返回、不抽稀")

    # backfill_curve：目录不存在 / steps.csv 缺失时都要安静返回空，不能抛
    c.ok(M.backfill_curve(Path("_no_such_dir_xyz")) == [],
         "目录不存在时补算返回空，不抛异常")
    empty = Path("_backfill_test_empty")
    if empty.exists():
        shutil.rmtree(empty)
    empty.mkdir()
    c.ok(M.backfill_curve(empty) == [], "没有 steps.csv 时补算返回空")
    shutil.rmtree(empty)

    # first_cycle_outlier_ratio：化成（预充）循环会把首圈基准撑大，必须能识别
    plain = [{"cycle": i, "dis_cap": 100.0 - i, "retention_pct": 0} for i in range(1, 11)]
    c.ok(M.first_cycle_outlier_ratio(plain) is None, "正常曲线不误报首圈异常")
    formed = [{"cycle": 1, "dis_cap": 1000.0, "retention_pct": 100.0}] + \
             [{"cycle": i, "dis_cap": 100.0 - i, "retention_pct": 0} for i in range(2, 12)]
    ratio = M.first_cycle_outlier_ratio(formed)
    c.ok(ratio is not None and ratio > 2,
         f"化成循环被识别出来（首圈是后续的 {ratio:.1f} 倍）" if ratio else "没识别出来")
    c.ok(M.first_cycle_outlier_ratio([]) is None, "空曲线不炸")
    c.ok(M.first_cycle_outlier_ratio([{"cycle": 1, "dis_cap": 5.0}]) is None,
         "只有一圈时谈不上异常")

    # 渲染层回归：识别出来了，摘要里就必须出现警示 ——
    # 否则算错的保持率会照原样流到报告里（这正是我们想避免的）
    rec = {"channel": "X", "testid": "1", "barcode": "B", "dir": "_no_such_dir_xyz",
           "soh": {"available": True, "cycle_count": 12, "cycle_range": [1, 12],
                   "first_discharge_cap_mah": 1000.0, "last_discharge_cap_mah": 88.0,
                   "capacity_retention_pct": 8.8,
                   "retention_curve": formed}}
    txt = M.build_summary(rec, Path("_no_such_dir_xyz"), "2026-01-01 00:00")
    c.ok("打个问号" in txt, "化成循环时，摘要里出现「保持率要打问号」的警示")
    c.ok("化成（预充）循环" in txt, "并说明了原因")
    c.ok("不要直接把它当 SOH 用" in txt, "明确写了不要直接用这个数")
    txt2 = M.build_summary({**rec, "soh": {**rec["soh"], "retention_curve": plain}},
                           Path("_no_such_dir_xyz"), "2026-01-01 00:00")
    c.ok("打个问号" not in txt2, "正常曲线不会乱报警示")

    # 容量小幅回升时，衰减率是极小的负数 —— 不能印成 "-0.000"（看着像 bug）
    bump = [{"cycle": 1, "dis_cap": 100.0, "retention_pct": 100.0},
            {"cycle": 2, "dis_cap": 99.0, "retention_pct": 99.0},
            {"cycle": 3, "dis_cap": 99.000001, "retention_pct": 99.0},
            {"cycle": 4, "dis_cap": 99.000002, "retention_pct": 99.0},
            {"cycle": 5, "dis_cap": 99.000003, "retention_pct": 99.0}]
    txt3 = M.build_summary({**rec, "soh": {**rec["soh"], "retention_curve": bump}},
                           Path("_no_such_dir_xyz"), "2026-01-01 00:00")
    c.ok("-0.000" not in txt3, "容量微升时不印出 -0.000")
    c.ok("0.000" in txt3, "而是印成 0.000")

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
