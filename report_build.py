#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告任务包编排器 —— 任选两个通道，一键重建整套报告任务包。

做什么（一条命令完成）：
  1. 解析两个通道的最新数据（live 优先，正式兜底）
  2. 逐通道算指标（明细层口径，剔除截断末圈）
  3. 生成 6 张对比图（第一通道蓝、第二通道红）
  4. 构建数据契约（global_context + 28 个 block_data）
  5. 组装任务包到共享目录（整目录替换，供 Agent 按任务说明执行）

用法：
  python3 report_build.py --ch-a 10-1 --ch-b 10-2
  python3 report_build.py --ch-a 10-3 --ch-b 10-7 --label-a 改性 --label-b 基线 \
      --mass-mg 2,1.8

质量：默认 2 mg（暂定值，2026-10-01 用户给定）。可给一个值（两通道共用）
或两个值（分别对应两通道）。**这数是比容量的分母，必须来自测试记录。**
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import figures as F
import report_pack as RP

VERSION = "1.1"
HERE = Path(__file__).resolve().parent
# 静态脚手架（与通道无关，每次组装原样复制）：提示词/模板/render.py/任务说明
SCAFFOLD = HERE / "报告任务_脚手架"


def normalize_channel(entry: str) -> str:
    """'10-1' → '27-188-10-1'；全键原样返回。"""
    e = entry.strip()
    if e.count("-") == 1:
        return f"27-188-{e}"
    return e


def main() -> int:
    p = argparse.ArgumentParser(description="任选两通道，一键重建报告任务包")
    p.add_argument("--ch-a", required=True, help="通道 A（如 10-1 或 27-188-10-1）")
    p.add_argument("--ch-b", required=True, help="通道 B（如 10-2）")
    p.add_argument("--label-a", default=None, help="样品 A 的图例名（默认通道号）")
    p.add_argument("--label-b", default=None, help="样品 B 的图例名（默认通道号）")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--mass-mg", default="2",
                   help="活性物质质量 mg：一个值两通道共用，或逗号分隔各给各的（默认 2）")
    p.add_argument("--nominal-mah", type=float, default=None,
                   help="标称容量（mAh）—— 倍率图把电流换算成 C 倍率用；不给则标 mA")
    p.add_argument("--dqdv-cycles", default="1,10,50,100,200,300",
                   help="dQ/dV 图（图 5/6）要画的圈；数据还没跑到就先缺着并在图上标注")
    p.add_argument("--dqdv-sign", choices=["dis", "chg"], default="dis",
                   help="dQ/dV 谁画成正峰：dis=还原/嵌入(放电)为正（默认，与提示词口径一致）")
    p.add_argument("--keep-paragraphs", dest="keep_paragraphs", action="store_true",
                   default=True,
                   help="重建时保留任务包里 Agent 已写的 段落/（默认开）")
    p.add_argument("--no-keep-paragraphs", dest="keep_paragraphs", action="store_false",
                   help="丢弃已有 段落/（只在确认要全部重写时用）")
    p.add_argument("--paragraphs-from", default=None,
                   help="段落种子目录：把里面的 *.txt 拷进新任务包的 段落/"
                        "（用于从备份恢复；任务包自己的 段落/ 优先）")
    p.add_argument("--out", default="/home/admin/jerry/batterylab-data/报告任务",
                   help="任务包输出目录（默认 Agent 可见的共享目录）")
    p.add_argument("--scaffold-dir", default=str(SCAFFOLD),
                   help="静态脚手架目录（提示词/模板/render.py/任务说明）")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    out = Path(args.out)
    mass_parts = [float(x) for x in str(args.mass_mg).split(",")]
    if len(mass_parts) == 1:
        mass_parts = mass_parts * 2
    if len(mass_parts) != 2:
        print("--mass-mg 要么 1 个值，要么 2 个值（逗号分隔）", file=sys.stderr)
        return 2

    # ---- 1. 逐通道解析 + 算指标 ----
    dqdv_cycles = [int(x) for x in str(args.dqdv_cycles).split(",") if x.strip()]
    cells: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for entry, label in ((args.ch_a, args.label_a or args.ch_a),
                         (args.ch_b, args.label_b or args.ch_b)):
        key = normalize_channel(entry)
        cell = RP.load_cell(data_dir, key, mass_parts[len(cells)], dqdv_cycles)
        if not cell:
            print(f"  ❌ 通道 {entry}：找不到数据目录。"
                  "该通道得有测试数据（live 或正式），才能进对比报告。", file=sys.stderr)
            continue
        cells[key] = cell
        labels[key] = label
        print(f"  {label} ← {cell['dir']}  [数据截至 {cell.get('progress_updated')}]"
              f"  dQ/dV 已画圈 {cell.get('dqdv_cycles_drawn')}"
              + (f"，缺 {cell.get('dqdv_cycles_missing')}"
                 if cell.get("dqdv_cycles_missing") else ""))

    if len(cells) < 2:
        print("可用的通道不足 2 个 —— 每个通道都得有测试数据", file=sys.stderr)
        return 1

    # ---- 2. 数据契约 ----
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx = RP.build_global_context(labels, cells, mass_parts[0], generated)
    cutoff = max((c.get("progress_updated") or "" for c in cells.values()))
    ctx["data_cutoff"] = cutoff or generated

    # ---- 3. 六张图（模板的 FIGURE:1..6）----
    # 图 1 长循环 / 图 2 首效VQ / 图 3 平均电压 / 图 4 倍率 / 图 5、6 dQ/dV（每样品一张）
    fig_cells = []
    for key in cells:
        c = cells[key]
        nominal = args.nominal_mah
        fig_cells.append({
            "label": labels[key], "cs": c["cs_plot"],
            "rates": F.rates_from_cs(c["cs_plot"], nominal),
            "dqdv": c["dqdv"], "curves": c["vq1"],
            "dqdv_multi": c["dqdv_multi"],
            "dqdv_cycles_drawn": c["dqdv_cycles_drawn"],
            "dqdv_cycles_missing": c["dqdv_cycles_missing"],
        })
    vs = "_vs_".join(labels.values()) if len(labels) == len(cells) else "_vs_".join(labels[k] for k in cells)
    mass_flag = mass_parts[0] / 1000.0
    figs = {
        "对比_长循环_" + vs: F.chart_long_cycle(f"{vs}　（长循环）", fig_cells,
                                               mass_g=mass_flag),
        "对比_首效VQ_" + vs: F.chart_vq(f"{vs}　（首效VQ）", fig_cells),
        "对比_平均电压_" + vs: F.chart_avgv(f"{vs}　（平均电压）", fig_cells),
        "对比_倍率_" + vs: F.chart_rate(f"{vs}　（倍率）", fig_cells,
                                        mass_g=mass_flag),
    }
    # 图注（画布下方，不会压曲线）：口径 + 剔除说明 + 质量暂定值
    drop_txt = "、".join(f"{labels[k]} 第 {'、'.join(map(str, cells[k]['dropped_cycles']))} 圈"
                        for k in cells if cells[k]["dropped_cycles"])
    caps = [f"活性物质质量：{'、'.join(f'{labels[k]} {mass_parts[i]:g} mg' for i, k in enumerate(cells))}"
            "（暂定值，待测试记录确认）"]
    if drop_txt:
        caps.append(f"末圈未走完（进行中），已从图上剔除：{drop_txt}")
    for f in figs.values():
        f.set_caption(caps)
    for cell in fig_cells:
        # 图 5/6：一个样品一张，氧化(充电)与还原(放电)同图，按圈号配色
        fig = F.chart_dqdv_sample(
            f"{cell['label']}　（dQ/dV）", cell,
            reduction_positive=(args.dqdv_sign == "dis"))
        drawn = cell["dqdv_cycles_drawn"]
        miss = cell["dqdv_cycles_missing"]
        # 圈号说明放**图注**（画布下方）而不是图内说明框：说明框固定画在左上角，
        # 而 dQ/dV 的峰就在左下/左上 —— 实测框会压住峰（渲染出来一眼可见）。
        lines = ["已画圈：" + "、".join(f"第 {c} 圈" for c in drawn)] if drawn else []
        if miss:
            lines.append("⚠ 尚未到达的圈：" + "、".join(f"第 {c} 圈" for c in miss))
        if args.dqdv_sign == "dis":
            lines.append("符号口径：还原（嵌入/放电）为正峰")
        # 这一张只带"质量暂定"那条通用图注（剔除末圈的说明对逐圈图没意义 ——
        # 图上已经写明画了哪几圈）
        fig.set_caption(lines + caps[:1])
        figs[f"dQdV_{cell['label']}"] = fig
    for name, fig in figs.items():
        print(f"  🖼  {name}.svg")

    # ---- 4. 组装任务包（整目录替换；但 Agent 已写的段落默认保住）----
    keep: dict[str, str] = {}
    seed = Path(args.paragraphs_from) if args.paragraphs_from else None
    if seed and seed.is_dir():
        for f in seed.glob("*.txt"):
            keep[f.name] = f.read_text(encoding="utf-8-sig")
        print(f"  ♻ 段落种子目录 {seed}：{len(keep)} 段")
    if args.keep_paragraphs and (out / "段落").is_dir():
        n_old = len(keep)
        for f in (out / "段落").glob("*.txt"):
            try:
                keep[f.name] = f.read_text(encoding="utf-8-sig")   # 任务包自己的更新，覆盖种子
            except OSError:
                pass
        if len(keep) > n_old:
            print(f"  ♻ 保留任务包里已有的 {len(keep) - n_old} 段 Agent 段落")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    scaffold = Path(args.scaffold_dir)
    if not scaffold.is_dir():
        print(f"❌ 脚手架目录不存在：{scaffold}", file=sys.stderr)
        return 1
    for item in scaffold.iterdir():
        if item.is_dir():
            shutil.copytree(item, out / item.name)
        else:
            shutil.copy2(item, out / item.name)
    # 段落回拷（放最后：脚手架里的空 段落/ 不覆盖已有内容）
    for name, text in keep.items():
        dest = out / "段落" / name
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    (out / "global_context.json").write_text(
        json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
    bd = out / "block_data"
    bd.mkdir(exist_ok=True)
    n = 0
    for bid in ("S11", "S12", "S31A", "S31B", "S31C", "S32A", "S32B", "S32C",
                "S32D", "S33A", "S33B", "S34A", "S34B", "S35A", "S35B", "S35C",
                "S41A", "S41B", "S41C", "S41D",
                "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
                "S43A", "S43B", "S43C", "S43D",
                "S51A", "S51B", "S51C", "S52A", "S52B", "S52C"):
        d = RP.build_block_data(bid, cells, ctx, labels)
        (bd / f"{bid}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1

    fig_dir = out / "图"
    fig_dir.mkdir(exist_ok=True)
    for fname, fig in figs.items():
        (fig_dir / f"{fname}.svg").write_text(fig.svg(), encoding="utf-8")

    # ---- 任务说明里的数据截止时间换成动态的 ----
    ts = out / "任务说明.md"
    if ts.exists():
        t = ts.read_text(encoding="utf-8-sig")
        import re
        t = re.sub(r"数据截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}",
                   f"数据截至 {cutoff or generated}", t)
        ts.write_text(t, encoding="utf-8")

    print(f"\n✅ 任务包已重建：{out}")
    print(f"   通道：{' vs '.join(labels[k] for k in cells)}｜数据截至 {ctx.get('data_cutoff')}")
    print(f"   下一步：对 QwenPaw 说「读 报告任务/任务说明.md，按工作流执行」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
