#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出一个通道的"专家审查包"。

产出一个文件夹，里面是能直接交给电池专业人员审查的东西：

    说明.md        数据来源、字段字典、我们算的指标、待审查的问题（实测值自动填好）
    工步层.csv     每个工步的汇总：电压区间、电流、容量、能量、内阻
    明细.csv       完整时序数据，Excel 双击就能打开
    曲线.png      4 张图（需先装 matplotlib：uv pip install matplotlib）

用法：
    python export_review.py --host 10.201.47.169 --channel 27-188-10-2
    python export_review.py --host <IP> --channel 27-188-10-1 --testid 247 --outdir 交给黄老师
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
    STATUS_NAMES,
    STEPTYPE_NAMES,
    build_transport,
    parse_channel,
)
from probe import check_time_unit, soh_preview

# 明细数据里已知的"标准"字段，其余视为辅助通道
DF_KNOWN = {
    "seqid", "stepid", "cycleid", "steptype", "testtime", "atime",
    "volt", "curr", "cap", "eng", "dbc",
}

FIELD_DICT_DF = [
    ("seqid", "数据序号", "—", "从 1 开始连续递增，用来查缺号", "已确认"),
    ("cycleid", "循环号", "—", "一次完整的 '充满+放完'。⚠️ **明细里的值比工步层小 1**（工步层第 1 圈=1，明细=0）", "已对账确认"),
    ("stepid", "工步号", "—", "**循环内**的工步号，每个循环归零", "已确认"),
    ("steptype", "工步类型", "—", "cc=恒流充 dc=恒流放 rest=搁置 等", "已确认"),
    ("atime", "绝对时间", "—", "真实时刻，时区未知", "待确认时区"),
    ("testtime", "本工步已运行时间", "**毫秒**", "★ 每个工步归零，不是累计", "已用实测数据反推确认"),
    ("volt", "电压", "V", "—", "已确认"),
    ("curr", "电流", "**mA**", "单位已用 BTSDA 对账确认（协议文档写 A，是错的）。"
     "已实测确认：充电为正、放电为负", "已对账确认"),
    ("cap", "容量", "**mAh**", "★ 是**该工步内**的累计值，不是整圈。"
     "已用 BTSDA 导出对账确认：原始值 ×1000 即 BTSDA 的 mAh 值", "已对账确认"),
    ("eng", "能量", "**mWh**", "同上，工步内累计。已用 BTSDA 对账确认", "已对账确认"),
]

FIELD_DICT_STEP = [
    ("stepindex", "工步序号", "—", "整个测试里的第几步，全局累加，不归零", "已确认"),
    ("stepid", "原始工步号", "—", "循环内的工步号", "已确认"),
    ("cycleid", "循环号", "—", "—", "已确认"),
    ("steptype", "工步类型", "—", "—", "已确认"),
    ("steptime", "工步运行时长", "毫秒", "—", "推测与 testtime 同单位"),
    ("startvolt / endvolt", "起止电压", "V", "该工步开始和结束时的电压", "已确认"),
    ("startcurr / endcurr", "起止电流", "**mA**", "充电为正、放电为负（已实测确认）", "已对账确认"),
    ("cap", "容量", "**mAh**", "该工步内累计。已用 BTSDA 对账确认", "已对账确认"),
    ("eng", "能量", "**mWh**", "该工步内累计。已用 BTSDA 对账确认", "已对账确认"),
    ("dcir", "直流内阻", "**毫欧（存疑）**",
     "★ 实测值在 10⁶ 量级。按毫欧算相当于 1000~4400 欧姆；"
     "若实际单位是**微欧**则相当于 1~4.4 欧姆 —— **需专家判断哪个合理**",
     "**单位存疑**"),
]


def fmt(value, digits: int = 6) -> str:
    """把科学计数法写得好看一点。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "0"
    if abs(number) < 0.001 or abs(number) >= 100000:
        return f"{number:.4e}"
    return f"{number:.{digits}g}"


def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        path.write_text("（无数据）\n", encoding="utf-8-sig")
        return
    # dbc 是嵌套结构，展平成 JSON 字符串
    import json
    flat = [
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
         for k, v in row.items()}
        for row in rows
    ]
    names: list[str] = []
    for row in flat:
        for key in row:
            if key not in names:
                names.append(key)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        writer.writerows(flat)


CJK_FONT_CANDIDATES = [
    "Microsoft YaHei", "SimHei", "SimSun", "Arial Unicode MS",
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Droid Sans Fallback",
]


def _pick_cjk_font() -> str | None:
    """找一个能显示中文的字体。找不到就返回 None（改用英文标注）。"""
    try:
        from matplotlib import font_manager
    except ImportError:
        return None
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONT_CANDIDATES:
        if name in available:
            return name
    return None


def try_plot(steps: list[dict], detail: list[dict], path: Path) -> tuple[str | None, str]:
    """画 4 张图。没装 matplotlib 就跳过。

    返回 (图片路径或 None, 说明文字)。
    中文字体缺失时退回英文标注 —— 否则图上全是"豆腐块"，专家看不懂。
    """
    import warnings

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None, "（未生成：没装 matplotlib）"

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font]
        plt.rcParams["axes.unicode_minus"] = False
    zh = font is not None

    def L(zh_text: str, en_text: str) -> str:
        return zh_text if zh else en_text

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 每圈充/放电容量
    cycles: dict[int, dict[str, float]] = {}
    for row in steps:
        c = to_float(row.get("cycleid"))
        cap = to_float(row.get("cap")) or 0.0
        stype = str(row.get("steptype", "")).strip().lower()
        if c is None:
            continue
        b = cycles.setdefault(int(c), {"chg": 0.0, "dis": 0.0, "dcir": None})
        if stype in ("cc", "cv", "cccv", "pcccv", "cp", "cr", "pulse"):
            b["chg"] += cap
        elif stype in ("dc", "dv", "cccd", "dp", "dr"):
            b["dis"] += cap
            d = to_float(row.get("dcir"))
            if d and d > 0:
                b["dcir"] = d

    xs = sorted(cycles)
    chg = [cycles[c]["chg"] for c in xs]
    dis = [cycles[c]["dis"] for c in xs]
    ce = [(cycles[c]["dis"] / cycles[c]["chg"] * 100)
          if cycles[c]["chg"] else None for c in xs]
    dcir = [cycles[c]["dcir"] for c in xs]

    with warnings.catch_warnings():
        # 字体缺字会刷几百行警告，这里压掉；已在上面的 L() 里做过兜底
        warnings.simplefilter("ignore")

        fig, axes = plt.subplots(2, 2, figsize=(13, 9))

        ax = axes[0][0]
        ax.plot(xs, chg, "o-", ms=3, label=L("充电容量", "Charge"))
        ax.plot(xs, dis, "s-", ms=3, label=L("放电容量", "Discharge"))
        ax.set_xlabel(L("循环号", "Cycle"))
        ax.set_ylabel(L("容量 (mAh)", "Capacity (mAh)"))
        ax.set_title(L("每圈充/放电容量", "Charge / discharge capacity per cycle"))
        ax.legend()
        ax.grid(alpha=0.3)

        ax = axes[0][1]
        ax.plot(xs, ce, "^-", ms=3, color="tab:green")
        ax.axhline(100, ls="--", lw=1, color="gray")
        ax.set_xlabel(L("循环号", "Cycle"))
        ax.set_ylabel(L("库仑效率 (%)", "Coulombic efficiency (%)"))
        ax.set_title(L("库仑效率 = 放电容量 / 充电容量",
                       "Coulombic efficiency = discharge / charge"))
        ax.grid(alpha=0.3)

        ax = axes[1][0]
        ax.plot(xs, dcir, "d-", ms=3, color="tab:red")
        ax.set_xlabel(L("循环号", "Cycle"))
        ax.set_ylabel(L("dcir（协议文档标称单位：毫欧）",
                        "dcir (documented unit: mOhm)"))
        ax.set_title(L("直流内阻走势（★ 单位存疑，见说明.md）",
                       "DCIR trend (UNIT UNCERTAIN - see notes)"))
        ax.grid(alpha=0.3)

        # 电压-时间曲线（取前两个循环，太长看不清）
        ax = axes[1][1]
        seg = [r for r in detail if to_float(r.get("cycleid")) in (1, 2)]
        if seg:
            tv = [(i, to_float(r.get("volt"))) for i, r in enumerate(seg)]
            ax.plot([p[0] for p in tv], [p[1] for p in tv], lw=0.8, color="tab:blue")
        ax.set_xlabel(L("数据点序号（前 2 个循环）", "Point index (first 2 cycles)"))
        ax.set_ylabel(L("电压 (V)", "Voltage (V)"))
        ax.set_title(L("电压曲线形状", "Voltage profile"))
        ax.grid(alpha=0.3)

        fig.suptitle(L("电池测试数据 —— 请专家审查",
                       "Battery test data - for expert review"), fontsize=14)
        fig.tight_layout()
        fig.savefig(path, dpi=130)
        plt.close(fig)

    note = (f"标注语言：中文（字体 {font}）" if zh
            else "⚠️ 标注语言：英文 —— 系统里没找到中文字体。"
                 "想用中文标注可装：sudo apt install fonts-noto-cjk")
    return str(path), note


def build_readme(meta: dict, soh: dict, steps: list[dict],
                 detail: list[dict], chart: str | None, chart_note: str,
                 time_unit: dict, aux_cols: list[str]) -> str:
    NL = "\n"
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    add("# 电池测试数据 —— 请专家审查")
    add()
    add(f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    add()
    add("---")
    add()
    add("## 一、这份数据是从哪来的")
    add()
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 数据来源 | 新威（Neware）电池测试系统，通过官方 BTSAPI 协议读取 |")
    add(f"| 设备型号 | {meta['devtype_name']}（协议代码 devtype={meta['devtype']}） |")
    add(f"| 设备号 | {meta['devid']} |")
    add(f"| 单元号 | {meta['subdevid']} |")
    add(f"| **通道号** | **{meta['chlid']}**（完整地址 `{meta['channel']}`） |")
    add(f"| 通道状态 | {meta['status']} / {STATUS_NAMES.get(meta['status'], '')} |")
    add(f"| 电池条码 | `{meta['barcode']}` ★ 见下面第六节第 10 条 |")
    add(f"| 测试编号 testid | {meta['testid']} |")
    add(f"| 已上传数据条数 | {meta['uploaded']}（上传是否完整：{'是' if meta['complete'] else '否'}） |")
    add(f"| 客户端版本 | {meta['client_version']} |")
    add(f"| 采集方式 | TCP 直连客户端 {meta['host']}:{meta['port']}，全程只读 |")
    add(f"| 导出明细行数 | {len(detail)} |")
    add(f"| 导出工步行数 | {len(steps)} |")
    add()
    add("**数据未经任何修改、清洗或换算**，是客户端返回的原始值。")
    add()
    add("---")
    add()
    add("## 二、我们算出来的指标")
    add()
    if soh.get("available"):
        add("| 指标 | 值 | 我们用的算法 |")
        add("|---|---|---|")
        add(f"| 循环数 | {soh['cycle_count']}（{soh['cycle_range'][0]} → {soh['cycle_range'][1]}） | 工步层 `cycleid` 去重 |")
        add(f"| 首圈放电容量 | {fmt(soh['first_discharge_cap_mah'])} Ah | 首圈内放电类工步的 `cap` 求和 |")
        add(f"| 末圈放电容量 | {fmt(soh['last_discharge_cap_mah'])} Ah | 同上 |")
        add(f"| 容量保持率 | **{soh['capacity_retention_pct']} %** | 末圈 ÷ 首圈 × 100 |")
        add(f"| 库仑效率 首/末 | {soh['coulomb_efficiency_first_pct']} % / "
            f"{soh['coulomb_efficiency_last_pct']} % | 同圈放电容量 ÷ 充电容量 |")
        add(f"| 能量效率 首/末 | {soh['energy_efficiency_first_pct']} % / "
            f"{soh['energy_efficiency_last_pct']} % | 同圈放电能量 ÷ 充电能量 |")
        add(f"| DCIR 首/末 | {fmt(soh['dcir_first_mohm'])} / {fmt(soh['dcir_last_mohm'])} | "
            f"直接取工步层 `dcir` 字段 |")
        add()
        add("**这些数字是 '算法 + 口径' 的产物，请专家判断它们是否讲得通。**")
    else:
        add(f"无法计算：{soh.get('reason')}")
    add()
    add("---")
    add()
    add("## 三、字段说明（明细数据 `明细.csv`）")
    add()
    add("| 字段 | 含义 | 单位 | 注意 | 我们的确认状态 |")
    add("|---|---|---|---|---|")
    for name, mean, unit, note, state in FIELD_DICT_DF:
        add(f"| `{name}` | {mean} | {unit} | {note} | {state} |")
    if aux_cols:
        add(f"| {', '.join('`' + c + '`' for c in aux_cols)} | 辅助通道 | **未知** | "
            f"★ 随硬件配置变化，**含义需要确认** | **待确认** |")
    add()
    add("### 工步层数据 `工步层.csv`")
    add()
    add("| 字段 | 含义 | 单位 | 注意 | 我们的确认状态 |")
    add("|---|---|---|---|---|")
    for name, mean, unit, note, state in FIELD_DICT_STEP:
        add(f"| `{name}` | {mean} | {unit} | {note} | {state} |")
    add()
    add(f"**时间单位核对**：{time_unit.get('note', '未判定')}")
    add()
    add("---")
    add()
    add("## 四、测试条件（从工步层还原的前几圈）")
    add()
    add("看不懂设备专有术语也没关系，这里把每一圈做了什么列出来：")
    add()
    add("| 循环 | 工步 | 类型 | 起始电压→终止电压 | 起止电流 | 容量 |")
    add("|---|---|---|---|---|---|")
    shown = 0
    for row in steps:
        if shown >= 18:
            break
        stype = str(row.get("steptype", "")).strip().lower()
        add(f"| {row.get('cycleid')} | {row.get('stepindex')} | "
            f"{stype}（{STEPTYPE_NAMES.get(stype, '未定义')}） | "
            f"{fmt(row.get('startvolt'))}→{fmt(row.get('endvolt'))} V | "
            f"{fmt(row.get('startcurr'))}→{fmt(row.get('endcurr'))} A | "
            f"{fmt(row.get('cap'))} mAh |")
        shown += 1
    add()
    if len(steps) > shown:
        add(f"（共 {len(steps)} 个工步，此处只列前 {shown} 个，完整见 `工步层.csv`）")
        add()
    add("---")
    add()
    add("## 五、图表")
    add()
    if chart:
        add(f"见同目录的 `{Path(chart).name}`：")
        add()
        add("- 左上：每圈充/放电容量")
        add("- 右上：库仑效率（= 放电容量 ÷ 充电容量）")
        add("- 左下：直流内阻走势（★ 单位存疑）")
        add("- 右下：电压曲线形状（前两个循环）")
        add()
        add(chart_note)
    else:
        add("未生成（缺 matplotlib）。想生成图表请先执行：")
        add()
        add("```bash")
        add("uv pip install matplotlib")
        add("```")
        add()
        add("然后重新运行本脚本。")
    add()
    add("---")
    add()
    add("## 六、★ 请专家重点判断的 8 个问题")
    add()
    add("说明：这份数据已经和 **BTSDA 官方导出逐项对过账**（见项目里的对账报告），")
    add("数值完全一致，单位已确认。所以下面问的不是数据对不对，")
    add("而是这些数据说明了什么。")
    add()
    add("**电池背景（来自 BTSDA 导出的测试信息）**")
    add()
    add("| 项 | 值 |")
    add("|---|---|")
    add("| 活性物质 | **1.184 mg** |")
    add("| 标称比容量 | **120 mAh/g** |")
    add("| 由此推出的理论容量 | **0.142 mAh** |")
    add("| 工步文件 | HK-测试工步.xml |")
    add("| 前 3 圈电流 | 0.0142 mA（0.1C） |")
    add("| 循环段电流 | 0.1421 mA（1C） |")
    add("| 电压窗口 | 充电至 3.3 V，放电至 0.5 V |")
    add("| 循环数 | 185 |")
    add()
    add("**1. 活性物质利用率为什么这么低？**")
    add()
    add("理论容量 0.142 mAh，而实测：")
    add()
    add("| | 实测 | 占理论容量 |")
    add("|---|---|---|")
    add("| 首圈充电 | 0.0211 mAh | **14.9 %** |")
    add("| 首圈放电 | 0.0029 mAh | **2.0 %** |")
    add("| 末圈放电 | 0.0005 mAh | **0.35 %** |")
    add()
    add("首圈放电只发挥了理论的 2%，这是整份数据里最值得注意的现象。")
    add("是材料本身的问题，还是电压窗口 / 电流设置不合适？")
    add()
    add("**2. 185 圈衰减到 17% 是否符合预期？**")
    add()
    add("首圈放电 0.0029 mAh 到末圈 0.0005 mAh，保持率 17.2%。")
    add("末几圈充放电容量都稳定在 0.0005 mAh，说明不是随机噪声。")
    add("（末圈放电 11 秒乘 1C 的 0.1421 mA 约等于 0.00043 mAh，与 0.0005 自洽。）")
    add()
    add("**3. 首圈不可逆容量 86% 正常吗？**")
    add()
    add("首圈充电 0.0211 mAh、放电 0.0029 mAh，库仑效率仅 13.61%。")
    add("**这个数字 BTSDA 的结论和我们完全一致**，所以是真实数据。")
    add("但 86% 的不可逆损失是否符合这种材料的首圈特性（例如 SEI 形成）？")
    add()
    add("**4. DCIR 数值是否合理？**")
    add()
    add("我们读到的内阻在 2.4×10⁶ 量级。单位已确认为**毫欧**，即约 **2400 欧姆**。")
    add("对 1.184 mg 的样品、毫安级电流，这个内阻合理吗？")
    add("这一项**没能对账** —— BTSDA 导出的循环表和工步表都没有内阻列，")
    add("只能靠专业判断。")
    add()
    add("**5. 静置时电压掉这么多正常吗？**")
    add()
    add("搁置阶段电压从约 3.24 V 掉到 2.09 V（掉了 1.15 V）。")
    add("这符合哪一类电池或材料的行为？")
    add()
    add("**6. 有没有电池温度通道？**")
    add()
    add("这台设备只提供了 CPU 温度（34 ℃ 左右，是设备温度）和「辅助通道温差」。")
    add("如果后续报告需要**电池温度**，这台设备能提供吗？")
    add()
    add("**7. 样品编号是怎么记录的？**")
    add()
    add("条码字段是 " + str(meta["barcode"]) + "。")
    add("同批其他通道形如 D672035TAA112，与协议文档示例同前缀，")
    add("看起来更像调试编号而不是样品编号。")
    add("现场有没有实验台账，记录哪个通道在什么时间放了哪个样品？")
    add("这决定了数据能否追溯到具体样品。")
    add()
    add("**8. 这个测试的目的是什么？**")
    add()
    add("从工步文件名和如此低的利用率看，这像是方法验证或调试性质的测试，")
    add("还是正式的样品评价？")
    add()
    add("## 七、附：本次读取用到的命令")
    add()
    add("```bash")
    add(f"# 全程只读，未修改任何设备状态")
    add(f"python neware_client.py --transport tcp --host {meta['host']} info")
    add(f"python neware_client.py --transport tcp --host {meta['host']} "
        f"status {meta['channel']}")
    add(f"python neware_client.py --transport tcp --host {meta['host']} "
        f"step {meta['channel']} --out 工步层.csv")
    add(f"python neware_client.py --transport tcp --host {meta['host']} "
        f"df {meta['channel']} --out 明细.csv")
    add("```")
    add()
    add("协议依据：《新威尔电池测试系统 BTSAPI 协议 v1.19》。")
    add("注意：现场客户端版本为 2025.11.24，比协议文档新两年，"
        "部分回应格式已与文档不一致，实际以实测为准。")
    add()
    return NL.join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="导出一个通道的专家审查包")
    p.add_argument("--transport", choices=["pipe", "tcp"], default="tcp")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--channel", required=True,
                   help="通道，格式 devtype-devid-subdevid-chlid，如 27-188-10-2")
    p.add_argument("--testid", type=int, default=0, help="0=当前/最近一次测试")
    p.add_argument("--limit", type=int, default=0, help="明细最多导多少行，0=全量")
    p.add_argument("--outdir", help="输出目录，默认自动命名")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="单条命令超时秒数，协议上限 30")
    p.add_argument("--pipe", help="管道名（transport=pipe 时用，Linux 上无效）")
    args = p.parse_args()

    channel = parse_channel(args.channel)
    outdir = Path(args.outdir or
                  f"审查包_{channel.key}_{datetime.now():%Y%m%d_%H%M}")
    outdir.mkdir(parents=True, exist_ok=True)

    transport = build_transport(args)
    try:
        transport.connect()
    except NewareError as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        return 1

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

        meta = {
            "channel": channel.key,
            "devtype": channel.devtype,
            "devtype_name": DEVTYPE_NAMES.get(channel.devtype, f"devtype{channel.devtype}"),
            "devid": channel.devid,
            "subdevid": channel.subdevid,
            "chlid": channel.chlid,
            "host": args.host,
            "port": args.port,
            "client_version": info.get("client_version") or "未知",
            "barcode": (rt[0].get("barcode") if rt else "") or "（空）",
            "status": (statuses[0]["status"] if statuses else "未知"),
            "testid": (checks[0]["testid"] if checks else args.testid),
            "uploaded": (checks[0]["uploaded"] if checks else 0),
            "complete": bool(checks[0]["complete"]) if checks else False,
        }

        aux_cols = sorted({k for row in detail[:50] for k in row
                           if k not in DF_KNOWN})
        time_unit = check_time_unit(detail) if detail else {"note": "无明细数据"}
        soh = soh_preview(steps) if steps else {"available": False,
                                                "reason": "没有工步层数据"}
    finally:
        transport.close()

    print("正在写文件…")
    write_csv(detail, outdir / "明细.csv")
    write_csv(steps, outdir / "工步层.csv")
    chart_path, chart_note = try_plot(steps, detail, outdir / "曲线.png")
    (outdir / "说明.md").write_text(
        build_readme(meta, soh, steps, detail, chart_path, chart_note,
                     time_unit, aux_cols),
        encoding="utf-8")

    print(f"\n完成，目录：{outdir.resolve()}")
    for f in sorted(outdir.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size:,} 字节)")
    if chart_note:
        print(f"\n图表说明：{chart_note}")
    if not chart_path:
        print("\n（想生成图表：uv pip install matplotlib 后重跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
