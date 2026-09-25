#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BTSDA 对账工具。

用途：把"我们从 API 算出的数字"和"BTSDA 官方软件显示的数字"逐项对齐，
自动算差异并判定 —— 这是验证"数据是否正确"的唯一可靠办法。

两种用法
--------
① 生成对账表（在能连到客户端的机器上跑）：

    python reconcile.py --host 10.201.47.169 --channel 27-188-10-2

   产出 对账表.csv，里面"我们的值"已经填好，"BTSDA 值"留空等你去 BTSDA 抄。
   同时打印每个值该去 BTSDA 的哪个页面找。

② 回填后核对（在任意机器上跑，不需要设备）：

    python reconcile.py --check 对账表.csv

   它会算每项的相对差异，超过容差就标为不一致。

同时会生成 对账说明.md，写明每一项在 BTSDA 哪里找。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from neware_client import (
    DEVTYPE_NAMES,
    NewareClient,
    NewareError,
    STEPTYPE_NAMES,
    build_transport,
    parse_channel,
)
from probe import soh_preview

# (键, 显示名, 单位, 容差%, 去 BTSDA 哪里找, 为什么重要)
ITEMS = [
    ("cycle_count", "总循环数", "圈", 0,
     "【循环数据表】的行数（每行一个循环）",
     "验证我们对'圈'的划分和 BTSDA 一致"),
    ("step_count", "总工步数", "个", 0,
     "【工步数据表】的行数",
     "验证工步层数据的完整性"),
    ("row_count", "数据总条数", "条", 0,
     "【详细数据】的条数，或界面上显示的记录数",
     "验证明细数据一条不漏"),
    ("first_dis_cap", "首圈放电容量", "mAh", 1,
     "【循环数据表】第 1 行的'放电容量'列 —— ★ 顺便看这一列的单位是什么",
     "★ 最关键：验证容量量级和单位。我们要确认是否真是 10⁻⁶ Ah 量级"),
    ("last_dis_cap", "末圈放电容量", "mAh", 1,
     "【循环数据表】最后一行的'放电容量'列",
     "验证末圈数据、以及衰减趋势"),
    ("first_chg_cap", "首圈充电容量", "mAh", 1,
     "【循环数据表】第 1 行的'充电容量'列",
     "验证充电侧的口径"),
    ("first_ce", "首圈库仑效率", "%", 2,
     "【循环数据表】第 1 行的'效率'或'库仑效率'列",
     "★ 我们算出 13.6%，反常。核对是我们的口径错还是数据本身如此"),
    ("last_ce", "末圈库仑效率", "%", 2,
     "【循环数据表】最后一行的效率列",
     "验证效率随循环的变化"),
    ("last_ee", "末圈能量效率", "%", 2,
     "【循环数据表】的效率列（若有'能量效率'）",
     "验证能量侧口径"),
    ("first_dcir", "首圈直流内阻", "（跟 BTSDA 一致）", 5,
     "【循环数据表】第 1 行的'内阻'或'DCIR'列 —— ★ 看它的单位标注",
     "★ 我们算出 2.4×10⁶，按文档说的毫欧就是 2400 欧姆。核对单位到底是什么"),
    ("last_dcir", "末圈直流内阻", "（跟 BTSDA 一致）", 5,
     "【循环数据表】最后一行的内阻列",
     "验证内阻走势"),
    ("first_volt", "首个数据点的电压", "V", 2,
     "【详细数据】第 1 行的电压列",
     "验证时标对齐 —— 确认我们读的第 1 条和 BTSDA 的第 1 条是同一条"),
    ("first_cap", "首个数据点的容量", "（跟 BTSDA 一致）", 2,
     "【详细数据】第 1 行的容量列",
     "验证明细容量口径"),
]


def collect(args) -> dict:
    transport = build_transport(args)
    try:
        transport.connect()
    except NewareError as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        raise SystemExit(1)

    channel = parse_channel(args.channel)
    try:
        client = NewareClient(transport)
        print("正在采集…")
        info = NewareClient.parse_devinfo(client.getdevinfo())
        statuses = NewareClient.parse_status(client.getchlstatus([channel]))
        rt = NewareClient.parse_inquire(client.inquire([channel]))
        checks = NewareClient.parse_inquiredf(client.inquiredf([channel], args.testid))
        steps = NewareClient.parse_data(
            client.download_steplayer(channel, args.testid, dcir=1))
        print(f"  工步层 {len(steps)} 行")
        detail = client.download_all(channel, testid=args.testid,
                                    max_rows=args.limit or None, progress=False)
        print(f"  明细   {len(detail)} 行")
    finally:
        transport.close()

    soh = soh_preview(steps) if steps else {}
    first_detail = detail[0] if detail else {}

    def pick(src: dict, key: str):
        return src.get(key, "")

    values: dict[str, object] = {
        "cycle_count": soh.get("cycle_count", ""),
        "step_count": len(steps),
        "row_count": checks[0]["uploaded"] if checks else "",
        "first_dis_cap": soh.get("first_discharge_cap_mah", ""),
        "last_dis_cap": soh.get("last_discharge_cap_mah", ""),
        "first_chg_cap": "",
        "first_ce": soh.get("coulomb_efficiency_first_pct", ""),
        "last_ce": soh.get("coulomb_efficiency_last_pct", ""),
        "last_ee": soh.get("energy_efficiency_last_pct", ""),
        "first_dcir": soh.get("dcir_first_mohm", ""),
        "last_dcir": soh.get("dcir_last_mohm", ""),
        "first_volt": pick(first_detail, "volt"),
        "first_cap": pick(first_detail, "cap"),
    }
    # 首圈充电容量需要自己从工步层算
    charge_types = {"cc", "cv", "cccv", "pcccv", "cp", "cr", "pulse"}
    first_cycle = soh.get("cycle_range", [None])[0]
    if first_cycle is not None:
        total = 0.0
        for row in steps:
            if str(row.get("cycleid")) == str(first_cycle) and \
                    str(row.get("steptype", "")).strip().lower() in charge_types:
                try:
                    total += float(row.get("cap", 0) or 0)
                except (TypeError, ValueError):
                    pass
        values["first_chg_cap"] = total or ""

    meta = {
        "channel": channel.key,
        "devtype_name": DEVTYPE_NAMES.get(channel.devtype, "?"),
        "host": args.host,
        "client_version": info.get("client_version") or "未知",
        "barcode": (rt[0].get("barcode") if rt else "") or "（空）",
        "status": statuses[0]["status"] if statuses else "未知",
        "testid": checks[0]["testid"] if checks else args.testid,
        "complete": bool(checks[0]["complete"]) if checks else False,
        "first_atime": pick(first_detail, "atime"),
        "steps": steps,
    }
    return {"values": values, "meta": meta, "soh": soh}


def write_sheet(data: dict, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["项目", "我们的值", "单位", "BTSDA 的值", "差异%", "判定", "备注"])
        for key, label, unit, tol, where, why in ITEMS:
            writer.writerow([label, data["values"].get(key, ""), unit, "", "", "",
                             f"{where} ｜ {why}"])
    print(f"对账表已写出：{path}")


def write_notes(data: dict, path: Path) -> None:
    NL = "\n"
    meta, soh = data["meta"], data["soh"]
    L: list[str] = []
    a = L.append
    a("# BTSDA 对账操作说明")
    a("")
    a(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    a("")
    a("## 一、先确认对的是同一个测试")
    a("")
    a("| 项 | 值 |")
    a("|---|---|")
    a(f"| 通道 | `{meta['channel']}`（{meta['devtype_name']}） |")
    a(f"| 测试号 testid | **{meta['testid']}** |")
    a(f"| 电池条码 | `{meta['barcode']}` |")
    a(f"| 通道状态 | {meta['status']} |")
    a(f"| 数据是否传完 | {'是' if meta['complete'] else '否（可能在跑）'} |")
    a(f"| 我们读到的首个时间 | {meta['first_atime']} |")
    a(f"| 客户端版本 | {meta['client_version']} |")
    a("")
    a("**在 BTSDA 里按这些信息找到同一个测试**：优先按**通道**找，")
    a(f"其次看**条码 `{meta['barcode']}`**，或文件名/列表里的**测试号 {meta['testid']}**。")
    a("")
    a("> 注意：`data` 目录里的 NDA 文件名格式之一是")
    a("> `服务器IP_设备号_单元号_通道号_测试ID.nda`，")
    a("> 所以文件名里可能直接带着测试号。")
    a("")
    a("## 二、逐项对照")
    a("")
    a("打开 `对账表.csv`，按下面的提示在 BTSDA 里找到对应值填进去：")
    a("")
    a("| 项目 | 我们的值 | 去 BTSDA 哪里找 |")
    a("|---|---|---|")
    for key, label, unit, tol, where, why in ITEMS:
        val = data["values"].get(key, "")
        a(f"| {label} | {val} | {where} |")
    a("")
    a("## 三、已经对过账的结论（2026-09-24）")
    a("")
    a("这一轮已经和 BTSDA 官方导出对过账，下面几条**不用再核**了：")
    a("")
    a("| 项 | 结论 |")
    a("|---|---|")
    a("| 容量单位 | **mAh**（协议文档写 Ah 是错的）。我们的原始值 ×1000 = BTSDA 的显示值 |")
    a("| 能量单位 | **mWh** |")
    a("| 电流单位 | **mA** |")
    a("| 内阻单位 | **mΩ**（文档这条是对的） |")
    a("| 电流符号 | **充电为正、放电为负**（实测确认） |")
    a("| 库仑效率 | 我方 13.61% = BTSDA 13.61%，**完全一致**，算法正确 |")
    a("| 电压 | 逐位一致 |")
    a("")
    a("完整证据见项目里的《对账报告-BTSDA.md》。")
    a("")
    a("## 三之二、还没对上的项")
    a("")
    a("| 项 | 为什么没对上 |")
    a("|---|---|")
    a("| **DCIR 数值** | BTSDA 导出的循环表和工步表**都没有内阻列**，无法比对。"
      "单位（mΩ）已确认，但 2400 欧姆这个数值是否合理，只能靠专业判断 |")
    a("")

    a("## 四、回填后自动核对")
    a("")
    a("把 BTSDA 的值填进 `对账表.csv` 的第四列，然后执行：")
    a("")
    a("```bash")
    a("python reconcile.py --check 对账表.csv")
    a("```")
    a("")
    a("它会算每项的相对差异，超过容差的标为不一致，并给出总判定。")
    a("")
    a("## 五、对完之后的意义")
    a("")
    a("- **全部一致** → '数据正确'这一项有证据了。字段字典可以定稿，")
    a("  然后放心去写采集服务、建归属映射。")
    a("- **有若干项不一致** → 说明我们的某个口径理解错了，现在就能改。")
    a("  把不一致的项发给我，我改算法。")
    a("")
    a("> 提醒：如果这个测试还在跑（数据未传完），末圈的数字可能不准。")
    a("> 那种情况下重点看**首圈**和**总循环数**。")
    a("")
    path.write_text(NL.join(L), encoding="utf-8")
    print(f"对账说明已写出：{path}")


def do_check(csv_path: Path) -> int:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    if not rows:
        print("对账表是空的", file=sys.stderr)
        return 2

    print(f"{'项目':<20}{'我们的值':>18}{'BTSDA 值':>18}{'差异%':>10}  判定")
    print("-" * 78)
    bad, missing, ok = [], [], 0
    for row in rows:
        label = row.get("项目", "")
        ours_s = (row.get("我们的值") or "").strip()
        theirs_s = (row.get("BTSDA 的值") or "").strip()
        tol = 0.0
        for key, lbl, unit, t, _, _ in ITEMS:
            if lbl == label:
                tol = t
                break
        if not theirs_s:
            missing.append(label)
            print(f"{label:<20}{ours_s:>18}{'（未填）':>18}{'—':>10}  待填")
            continue
        try:
            ours, theirs = float(ours_s), float(theirs_s)
        except ValueError:
            print(f"{label:<20}{ours_s:>18}{theirs_s:>18}{'—':>10}  非数值，人工看")
            continue
        if ours == theirs:
            diff = 0.0
        elif ours == 0:
            diff = float("inf")
        else:
            diff = abs(theirs - ours) / abs(ours) * 100
        verdict = "一致" if diff <= tol else f"不一致（容差 {tol:g}%）"
        if diff <= tol:
            ok += 1
        else:
            bad.append((label, ours, theirs, diff))
        print(f"{label:<20}{ours_s:>18}{theirs_s:>18}{diff:>10.3f}  {verdict}")

    print("-" * 78)
    print(f"一致 {ok} 项，不一致 {len(bad)} 项，未填 {len(missing)} 项")
    if bad:
        print("\n不一致的（需要改我们的算法或口径）：")
        for label, ours, theirs, diff in bad:
            print(f"  {label}: 我们 {ours} vs BTSDA {theirs}  （差 {diff:.2f}%）")
    if bad:
        return 1
    if missing:
        print(f"\n还有 {len(missing)} 项没填，填完再跑一次。")
        return 0
    print("\n✅ 全部一致 —— 数据口径验证通过")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="BTSDA 对账工具")
    p.add_argument("--check", metavar="对账表.csv",
                   help="核对已回填的对账表（不需要设备）")
    p.add_argument("--transport", choices=["pipe", "tcp"], default="tcp")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--channel")
    p.add_argument("--testid", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--outdir", default=".")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="单条命令超时秒数，协议上限 30")
    p.add_argument("--pipe", help="管道名（transport=pipe 时用，Linux 上无效）")
    args = p.parse_args()

    if args.check:
        return do_check(Path(args.check))

    if not args.host or not args.channel:
        print("生成对账表需要 --host 和 --channel；核对用 --check 对账表.csv",
              file=sys.stderr)
        return 2

    data = collect(args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ch = data["meta"]["channel"].replace("-", "_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    write_sheet(data, outdir / f"对账表_{ch}_{stamp}.csv")
    write_notes(data, outdir / f"对账说明_{ch}_{stamp}.md")

    print("\n我们这边的关键值：")
    for key, label, unit, tol, _, _ in ITEMS:
        print(f"  {label:<20} {data['values'].get(key, '')}  {unit}")

    if not data["meta"]["complete"]:
        print("\n⚠️ 这个测试的数据还没传完（可能在跑），末圈的数字可能不准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
