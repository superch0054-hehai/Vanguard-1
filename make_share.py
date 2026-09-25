#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把采集到的数据导出成"给 AI 助手看的共享目录"。

为什么需要它
------------
采集服务落盘的数据是给机器看的（CSV + manifest.jsonl）。
但要让 AI 助手（QwenPaw）用好，缺三样东西：

  1. **一份「口径规则」** —— 否则它自己用 pandas 重算，会算出不一样的结果
     （我们已经吃过亏：明细和工步层的 cycleid 差 1、cap 是工步内累计、
      单位是 mAh 不是 Ah……这些都是对账才搞清的）
  2. **一份索引** —— 它得先知道有哪些数据集
  3. **每份数据的摘要** —— 一眼看清这颗电池怎么样，不用去读几 MB 的明细

这个脚本就是生成这三样。**只读数据目录、只写共享目录，不动原始数据。**

用法
----
    python3 make_share.py \\
        --data-dir ./data \\
        --share-dir /home/admin/jerry/batterylab-data

    # 只生成文档不复制数据（快，用来反复改说明）
    python3 make_share.py --data-dir ./data --share-dir ... --docs-only

设计原则
--------
* **共享目录里只放数据，不放能操作设备的代码。**
  Agent 有 shell 权限，如果给它 neware_client.py，它理论上能对测试机发
  stop 之类的命令。虽然 QwenPaw 有安全审批会拦，但从设计上就不给这个机会
  更稳妥 —— 取数是我们的职责，分析是它的。
* **口径规则写在 README.md 里**，而不是指望它自己悟出来。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from export_review import FIELD_DICT_DF, FIELD_DICT_STEP
from neware_client import DEVTYPE_NAMES, STEPTYPE_NAMES

VERSION = "1.0"
SHARE_TAG = "batterylab-data"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def fmt(v, nd: int = 6) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if x == 0:
        return "0"
    if abs(x) < 0.001 or abs(x) >= 100000:
        return f"{x:.4e}"
    return f"{x:.{nd}g}"


def size_str(p: Path) -> str:
    if not p.exists():
        return "—"
    b = p.stat().st_size
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if b >= div:
            return f"{b/div:.1f} {unit}"
    return f"{b} B"


def resolve_dataset_dir(r: dict, data_dir: Path) -> Path | None:
    """把 manifest 里的 dir 字段解析成真实存在的目录。

    为什么要这么绕：manifest 里存的可能是
      * 绝对路径            /home/admin/batterylab/data/channel=../testid=..
      * 相对路径（含数据目录）data/channel=../testid=..
      * 相对路径（不含）      channel=../testid=..
    直接 `data_dir / dir` 在第二种情况下会拼成 data/data/... 而找不到。
    所以逐个试，谁存在用谁。
    """
    raw = Path(str(r.get("dir", "") or ""))
    if not raw.name:
        return None
    cands: list[Path] = []
    if raw.is_absolute():
        cands.append(raw)
    else:
        cands.append(raw)                       # 相对当前工作目录
        cands.append(data_dir / raw)            # 相对数据目录
        parts = raw.parts
        if len(parts) >= 2:                     # 兜底：只取最后两级
            cands.append(data_dir / Path(*parts[-2:]))
    for c in cands:
        if c.exists():
            return c
    return None


def share_rel_path(dsrc: Path) -> Path:
    """数据集在共享目录里的相对路径 —— 统一成 channel=../testid=.. 两级。"""
    parts = dsrc.parts
    if len(parts) >= 2 and parts[-2].startswith("channel="):
        return Path(*parts[-2:])
    return Path(dsrc.name)


def load_manifest(data_dir: Path) -> list[dict[str, Any]]:
    mf = data_dir / "manifest.jsonl"
    if not mf.exists():
        return []
    out = []
    for line in mf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# README.md —— 给 AI 助手的总说明（最重要的一份）
# ---------------------------------------------------------------------------

def build_readme(recs: list[dict], generated: str) -> str:
    L: list[str] = []
    a = L.append

    a("# 电池实验数据 —— 给 AI 助手的使用说明")
    a("")
    a(f"> 生成时间：{generated}　|　数据集：{len(recs)} 份　|　由 BatteryLab 采集程序自动生成")
    a("")
    a("这是**实验室电池测试系统自动采集**的数据。你可以读这些文件来回答问题、")
    a("做分析、写报告。")
    a("")
    a("**关于设备**：数据由实验室算力盒子上常驻的采集程序从测试机取出并落盘。")
    a("**你没有测试机的访问权限，也不需要** —— 需要设备上的操作时，让人在本机执行，")
    a("把结果给你分析即可。")
    a("")
    a("---")
    a("")
    a("## ★ 一、口径规则（要自己算指标时，必须遵守）")
    a("")
    a("**这一节很重要。算错口径会得出完全错误的结论，而且从结果上看不出来。**")
    a("")
    a("下面每一条都是拿真实数据、和厂商官方软件（BTSDA）逐项对账确认过的，")
    a("不是猜的。")
    a("")
    a("### 1. 圈号：明细和工步层差 1")
    a("")
    a("| 文件 | 第 1 圈的 `cycleid` |")
    a("|---|---|")
    a("| `steps.csv`（工步层） | **1** ← 与 BTSDA 官方软件一致 |")
    a("| `detail.csv`（明细） | **0** ← 比工步层小 1 |")
    a("")
    a("**所以：要对齐圈号就用工步层；如果手上是明细，圈号要 `+1`。**")
    a("这两个表是同一次测试的两份视图，`stepid` 是对得上的，只有 `cycleid` 起点不同。")
    a("")
    a("### 2. 容量 `cap` 是「该工步内」累计的，不是整圈的")
    a("")
    a("要算「某一圈的放电容量」，必须：")
    a("")
    a("1. 先按 `cycleid` 分组")
    a("2. 只保留**放电类**工步：`dc` `dv` `cccd` `dp` `dr`")
    a("3. 把它们的 `cap` **求和**")
    a("")
    a("> ⚠️ **不要对全表取最大值** —— 那是错的。")
    a("> 因为每一行只代表「这一步到目前为止累计了多少」，不是整圈总量。")
    a("")
    a("充电类工步是：`cc` `cv` `cccv` `pcccv` `cp` `cr` `pulse`；")
    a("`rest`（搁置）/`pause`/`end` 不产生容量。")
    a("")
    a("### 3. 单位（协议文档写错了，以这里为准）")
    a("")
    a("| 字段 | 单位 | 说明 |")
    a("|---|---|---|")
    a("| `volt` 电压 | **V** | |")
    a("| `curr` 电流 | **mA** | ⚠️ 不是 A |")
    a("| `cap` 容量 | **mAh** | ⚠️ 不是 Ah |")
    a("| `eng` 能量 | **mWh** | ⚠️ 不是 Wh |")
    a("| `dcir` 内阻 | **mΩ** | |")
    a("| `testtime` | **毫秒** | 是「本工步已运行时间」，**每个工步归零** |")
    a("")
    a("### 4. 电流符号：充电为正、放电为负")
    a("")
    a("所以不要靠符号猜充放电 —— 请用 `steptype` 判断，符号只做交叉验证。")
    a("")
    a("### 5. 算 SOH 指标，优先用工步层 `steps.csv`")
    a("")
    a("| 文件 | 行数量级 | 建议 |")
    a("|---|---|---|")
    a("| `steps.csv` | 几十 ~ 几千行 | ✅ **优先用它算指标** |")
    a("| `detail.csv` | 几千 ~ **几百万行** | ⚠️ **不要整份读进上下文**，用代码筛选 |")
    a("")
    a("**为什么**：两个文件的口径是一回事，但工步层小两三个数量级，")
    a("而且**已经按工步聚合好了**，算容量保持率、库仑效率直接用它就行。")
    a("")
    a("### 6. `summary.md` 里算好的指标，直接引用，不要重算")
    a("")
    a("每个数据集的 `summary.md` 里的指标，都是按上面这套口径算好的。")
    a("**直接引用**。如果你重算，很可能因为口径差异得出不一样的数，")
    a("而且很难判断谁对。")
    a("")
    a("如果你确实需要自己算（比如摘要里没有的指标），**请按上面第 1~4 条的规则来**。")
    a("")
    a("---")
    a("")
    a("## 二、目录结构")
    a("")
    a("```")
    a("README.md               ← 你正在看的这份")
    a("datasets.md             ← ★ 数据集索引：先看这个，知道有哪些数据")
    a("channel=<通道>/testid=<测试号>/")
    a("    ├── summary.md      ← ★ 该数据集的摘要（指标＋规模＋文件清单）")
    a("    ├── steps.csv       ← 工步层（小，可以直接读）")
    a("    ├── detail.csv      ← 明细（大，用代码筛选，别整份读）")
    a("    └── meta.json       ← 原始元数据")
    a("```")
    a("")
    a("**文件名里的 `channel=` 和 `testid=` 是固定格式**，方便按目录过滤。")
    a("")
    a("> ⚠️ **`testid` 不是唯一的**：同一次批量启动的多个通道会共享一个测试号。")
    a("> 所以要唯一标识一份数据，必须用 **(通道, testid)** 这个组合。")
    a("")
    a("---")
    a("")
    a("## 三、字段字典")
    a("")
    a("### `steps.csv`（工步层，推荐用它算指标）")
    a("")
    a("| 字段 | 含义 | 单位 |")
    a("|---|---|---|")
    for name, mean, unit, note, state in FIELD_DICT_STEP:
        a(f"| `{name}` | {mean} | {unit.replace('**', '')} |")
    a("")
    a("### `detail.csv`（明细）")
    a("")
    a("| 字段 | 含义 | 单位 |")
    a("|---|---|---|")
    for name, mean, unit, note, state in FIELD_DICT_DF:
        a(f"| `{name}` | {mean} | {unit.replace('**', '')} |")
    a("")
    a("**辅助通道**：`V1`/`T1`/`Thk1`/`CPU` 这类列是设备额外的测量通道，")
    a("**列名随硬件配置变化**，不要硬编码。当前这台设备的辅助通道是 `CPU`，")
    a("表示**测试设备的 CPU 温度（℃）** —— 注意**不是电池温度**。")
    a("")
    a("---")
    a("")
    a("## 四、常见任务怎么做")
    a("")
    a("### ① 想知道有哪些数据")
    a("")
    a("读 `datasets.md`。")
    a("")
    a("### ② 想知道某颗电池的情况")
    a("")
    a("读它目录下的 `summary.md`。**里面的指标直接用。**")
    a("")
    a("### ③ 要算容量保持率（摘要里没有时）")
    a("")
    a("```python")
    a("import pandas as pd")
    a("df = pd.read_csv('channel=.../testid=.../steps.csv')")
    a("")
    a("# 只保留放电类工步，按圈求和 —— 这是关键")
    a("DIS = ['dc', 'dv', 'cccd', 'dp', 'dr']")
    a("dis = df[df['steptype'].isin(DIS)]")
    a("per_cycle = dis.groupby('cycleid')['cap'].sum().sort_index()")
    a("")
    a("retention = per_cycle.iloc[-1] / per_cycle.iloc[0] * 100")
    a("print(f'{retention:.2f} %')")
    a("```")
    a("")
    a("### ④ 要画容量-循环曲线")
    a("")
    a("用上面得到的 `per_cycle`：")
    a("")
    a("```python")
    a("import matplotlib.pyplot as plt")
    a("plt.plot(per_cycle.index, per_cycle.values)   # 纵轴单位 mAh")
    a("plt.xlabel('循环号'); plt.ylabel('放电容量 (mAh)')")
    a("```")
    a("")
    a("### ⑤ 要看某一圈的充放电曲线（电压-容量）")
    a("")
    a("```python")
    a("df = pd.read_csv('channel=.../testid=.../detail.csv')")
    a("# ★ 明细的 cycleid 比工步层小 1：想看第 55 圈，这里用 54")
    a("one = df[df['cycleid'] == 54].sort_values('seqid')")
    a("plt.plot(one['cap'], one['volt'])      # 容量 mAh，电压 V")
    a("```")
    a("")
    a("### ⑥ 要比较两颗电池")
    a("")
    a("分别读各自的 `summary.md`，比 `capacity_retention_pct`。")
    a("")
    a("---")
    a("")
    a("## 五、数据可信度")
    a("")
    a("这些数据经过了对账和自检，你可以放心用：")
    a("")
    a("- **和厂商官方软件（BTSDA）逐项对账过** —— 电压逐位一致，")
    a("  容量/能量数值一致（只是单位标注修正过），库仑效率完全一致")
    a("- **每个数据集都做过 9 项自检** —— 序号连续性、时间单调性、")
    a("  电流符号、单位自洽、值域合理性等")
    a("- **原始数据未做任何加工** —— 没有清洗、插值、换算")
    a("")
    a("`meta.json` 里的 `upload_complete` 字段表示客户端是否确认数据已传完。")
    a("**如果是 `false`，说明这个测试当时还在跑，数据可能不完整**，分析时要注意。")
    a("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# datasets.md —— 索引
# ---------------------------------------------------------------------------

def build_index(recs: list[dict], generated: str) -> str:
    L: list[str] = []
    a = L.append
    a("# 数据集索引")
    a("")
    a(f"> 生成时间：{generated}　|　共 {len(recs)} 份")
    a("")
    if not recs:
        a("（还没有数据。等测试跑完、采集程序自动采集后会出现在这里。）")
        return "\n".join(L) + "\n"

    a("| 通道 | 测试号 | 电池条码 | 采集时间 | 循环数 | 容量保持率 | 明细行数 | 完整 |")
    a("|---|---|---|---|---|---|---|---|")
    for r in sorted(recs, key=lambda x: x.get("collected_at", ""), reverse=True):
        soh = r.get("soh") or {}
        ret = soh.get("capacity_retention_pct")
        cycles = soh.get("cycle_count", "")
        d = r.get("dir", "")
        # dir 在 data 目录下，共享目录里是同样的相对路径
        a(f"| `{r.get('channel','')}` | {r.get('testid','')} "
          f"| `{r.get('barcode') or '（空）'}` "
          f"| {str(r.get('collected_at',''))[:16]} "
          f"| {cycles} | {ret if ret is not None else '—'} % "
          f"| {r.get('detail_count','')} "
          f"| {'✅' if r.get('upload_complete') else '⚠️ 未传完'} |")
    a("")
    a("**怎么取某个数据集的摘要**：把上表「通道」和「测试号」拼成路径：")
    a("")
    a("```")
    a("channel=<通道>/testid=<测试号>/summary.md")
    a("```")
    a("")
    a("例如 `channel=27-188-10-1/testid=247/summary.md`。")
    a("")
    if any(not r.get("upload_complete") for r in recs):
        a("> ⚠️ 表里有标记「未传完」的数据集 —— 当时测试还在跑，数据可能不完整。")
        a("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# summary.md —— 单个数据集
# ---------------------------------------------------------------------------

def build_summary(r: dict, data_dir: Path, generated: str) -> str:
    soh = r.get("soh") or {}
    ddir = resolve_dataset_dir(r, data_dir) or Path(".")

    L: list[str] = []
    a = L.append
    a(f"# 数据集：通道 {r.get('channel')} / 测试 {r.get('testid')}")
    a("")
    a("## 基本信息")
    a("")
    a("| 项 | 值 |")
    a("|---|---|")
    a(f"| 电池条码 | `{r.get('barcode') or '（空）'}` |")
    a(f"| 通道 | `{r.get('channel')}`（{r.get('devtype_name','')}，设备 {r.get('devid')}） |")
    a(f"| 测试号 testid | **{r.get('testid')}**（注意：同批启动的通道可能共用测试号） |")
    a(f"| 采集时间 | {r.get('collected_at')} |")
    a(f"| 数据完整性 | {'✅ 客户端确认已传完' if r.get('upload_complete') else '⚠️ **未传完**，数据可能不完整'} "
      f"（{r.get('detail_count', 0)} 条） |")
    a(f"| 解析器版本 | {r.get('parser_version','')} |")
    a("")
    a("## SOH 指标")
    a("")
    a("> 下面的指标按 `README.md` 里的口径算好。**直接引用，不要重算。**")
    a("")
    if soh.get("available"):
        rng = soh.get("cycle_range") or ["", ""]
        a("| 指标 | 值 |")
        a("|---|---|")
        a(f"| 循环数 | {soh.get('cycle_count')}（{rng[0]} → {rng[1]}） |")
        a(f"| 首圈放电容量 | {fmt(soh.get('first_discharge_cap_mah'))} mAh |")
        a(f"| 末圈放电容量 | {fmt(soh.get('last_discharge_cap_mah'))} mAh |")
        a(f"| **容量保持率** | **{soh.get('capacity_retention_pct')} %** |")
        a(f"| 库仑效率（首/末） | {soh.get('coulomb_efficiency_first_pct')} % / "
          f"{soh.get('coulomb_efficiency_last_pct')} % |")
        a(f"| 能量效率（首/末） | {soh.get('energy_efficiency_first_pct')} % / "
          f"{soh.get('energy_efficiency_last_pct')} % |")
        a(f"| DCIR（首/末） | {fmt(soh.get('dcir_first_mohm'))} / "
          f"{fmt(soh.get('dcir_last_mohm'))} mΩ |")
        a("")
        a("**指标定义**：")
        a("- 容量保持率 = 末圈放电容量 ÷ 首圈放电容量")
        a("- 库仑效率 = 同圈放电容量 ÷ 充电容量")
        a("- 能量效率 = 同圈放电能量 ÷ 充电能量")
        a("- DCIR = 工步切换点的 |ΔV| ÷ |ΔI|（mΩ）")
    else:
        a(f"⚠️ 算不出：{soh.get('reason', '未知原因')}")
    a("")
    a("## 数据文件")
    a("")
    a("| 文件 | 大小 | 说明 |")
    a("|---|---|---|")
    steps_csv = ddir / "steps.csv"
    detail_csv = ddir / "detail.csv"
    a(f"| `steps.csv` | {size_str(steps_csv)} | 工步层 {r.get('step_count',0)} 行。"
      f"**可以直接读，算指标优先用它** |")
    a(f"| `detail.csv` | {size_str(detail_csv)} | 明细 {r.get('detail_count',0)} 行。"
      f"**不要整份读进上下文**，用 pandas 筛选 |")
    a(f"| `meta.json` | {size_str(ddir / 'meta.json')} | 原始元数据 |")
    a("")
    a("## 这个测试做了什么")
    a("")
    a("（从工步层只取前几个工步看看测试条件。**只读这几行，不要读整份**）")
    a("")
    a("```csv")
    a(_head_of(steps_csv, 6))
    a("```")
    a("")
    a("> `steptype` 含义：`cc` 恒流充电、`dc` 恒流放电、`cv` 恒压充电、")
    a("> `rest` 搁置、`cccv` 恒流恒压充电、`dv` 恒压放电 等，共 17 种。")
    a("")
    return "\n".join(L) + "\n"


def _head_of(csv_path: Path, n: int) -> str:
    if not csv_path.exists():
        return "（文件不存在）"
    lines = csv_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    return "\n".join(lines[:n]) + ("\n…" if len(lines) > n else "")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="导出给 AI 助手看的共享目录")
    p.add_argument("--data-dir", default="./data", help="采集服务的数据目录")
    p.add_argument("--share-dir", required=True, help="共享目录（给 AI 助手读的）")
    p.add_argument("--docs-only", action="store_true",
                   help="只生成说明文档，不复制数据文件")
    p.add_argument("--force", action="store_true",
                   help="共享目录非空也照样写（默认会拒绝，防止误覆盖）")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    share_dir = Path(args.share_dir)
    if not data_dir.exists():
        print(f"数据目录不存在：{data_dir}", file=sys.stderr)
        return 1

    # 安全阀：共享目录可能是别人的目录，非空就先问一声
    if share_dir.exists() and any(share_dir.iterdir()) and not args.force:
        names = [c.name for c in list(share_dir.iterdir())[:5]]
        print(f"⚠️ 共享目录非空：{share_dir}", file=sys.stderr)
        print(f"   里面有：{names}", file=sys.stderr)
        print(f"   如果确认要往里写，加 --force", file=sys.stderr)
        return 1

    recs = load_manifest(data_dir)
    if not recs:
        print(f"⚠️ {data_dir}/manifest.jsonl 里没有数据集记录。")
        print("   （采集服务还没采到数据？先用 collector.py 采一份）")

    share_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---------- 1. 复制数据 ----------
    copied = 0
    if not args.docs_only:
        for r in recs:
            src = resolve_dataset_dir(r, data_dir)
            if src is None:
                print(f"  跳过（找不到源目录）：{r.get('dir')}")
                continue
            dst = share_dir / share_rel_path(src)
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
                    copied += 1
            # 顺手写一份原始元数据
            (dst / "meta.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
    print(f"  复制了 {copied} 个数据文件")

    # ---------- 2. 生成文档 ----------
    (share_dir / "README.md").write_text(
        build_readme(recs, generated), encoding="utf-8")
    (share_dir / "datasets.md").write_text(
        build_index(recs, generated), encoding="utf-8")
    for r in recs:
        src = resolve_dataset_dir(r, data_dir)
        if src is None:
            continue
        dst = share_dir / share_rel_path(src)
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "summary.md").write_text(
            build_summary(r, data_dir, generated), encoding="utf-8")

    # ---------- 3. 汇总 ----------
    print(f"\n完成，共享目录：{share_dir}")
    for f in ["README.md", "datasets.md"]:
        fp = share_dir / f
        if fp.exists():
            print(f"  {f:<20} {size_str(fp)}")
    print(f"  数据集目录           {len(recs)} 个（每个含 summary.md"
          + ("" if args.docs_only else " + CSV") + "）")
    print()
    print("AI 助手的读法：")
    print(f"  1. 读 {share_dir}/README.md         ← 先读这个（口径规则）")
    print(f"  2. 读 {share_dir}/datasets.md       ← 知道有哪些数据")
    print(f"  3. 读 channel=.../testid=.../summary.md  ← 看具体某颗电池")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
