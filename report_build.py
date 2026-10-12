#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告任务包编排器 —— 任选两个通道，一键重建整套报告任务包。

做什么（一条命令完成）：
  1. 解析两个通道的最新数据（live 优先，正式兜底）
  2. 逐通道算指标（明细层口径，剔除截断末圈）
  3. 生成 6 张对比图（第一通道蓝、第二通道红）
  4. 构建数据契约（global_context + 28 个 block_data）
  5. 组装任务包到共享目录（整目录替换）

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
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import figures as F
import report_pack as RP

VERSION = "1.1"
HERE = Path(__file__).resolve().parent
# 静态脚手架（与通道无关，每次组装原样复制）：提示词/模板/render.py
SCAFFOLD = HERE / "报告任务_脚手架"



def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写 .tmp 再 os.replace。

    ★ 为什么必须这样：Agent（容器内 root）在任务包里写过文件后，admin 直接
    `write_text` 覆盖同名文件会 PermissionError（实测把重建打断在 global_context.json 上，
    而它先 rmtree 后重建 → 任务包留在半成品状态）。改名只看**目录**写权限，目录是我们的。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    p.add_argument("--testid-a", type=int, default=None,
                   help="指定通道 A 用哪一个测试（默认取最新的那个 = 当前在跑的）")
    p.add_argument("--testid-b", type=int, default=None, help="同上，通道 B")
    # ★ 2026-10-09：支持**两对** —— 对① = ch-a/ch-b（长循环），对② = ch-c/ch-d（倍率）。
    #   哪张图用哪一对由 report_pack.FIGURE_PAIRS 决定（唯一出处），这里只管取数。
    p.add_argument("--ch-c", default=None, help="对② 通道 C（如 10-3）")
    p.add_argument("--ch-d", default=None, help="对② 通道 D（如 10-4）")
    p.add_argument("--label-c", default=None)
    p.add_argument("--label-d", default=None)
    p.add_argument("--testid-c", type=int, default=None)
    p.add_argument("--testid-d", type=int, default=None)
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--source", choices=["auto", "live", "official"], default="auto",
                   help="数据来源：auto=live 优先；live=只用进行中的每日增量；"
                        "official=只用已结束的正式数据集（两套报告都出，各自注明来源）")
    p.add_argument("--variable", default=None,
                   help="本批实验的变量（进表 1 与 4.2 机制段）；不填则用 report_pack 的本批默认值（电解液添加剂 PBSF 1 wt%）")
    p.add_argument("--dosage", default=None, help="变量用量，如 1 wt%")
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
    p.add_argument("--compose", choices=["off", "missing", "refresh", "all"], default="off",
                   help="填空式正文合成（5-D 主路径）：off=不由程序填（让 Agent 写）；"
                        "missing=只补空缺；refresh=重填非 AI 段（换通道/数据更新后用）；"
                        "all=全部重填（AI 段受台账保护）。**秒级出全篇**")
    p.add_argument("--paragraphs-from", default=None,
                   help="段落种子目录：把里面的 *.txt 拷进新任务包的 段落/"
                        "（用于从备份恢复；任务包自己的 段落/ 优先）")
    p.add_argument("--out", default="/home/admin/jerry/batterylab-data/报告任务",
                   help="任务包输出目录（默认 Agent 可见的共享目录）")
    p.add_argument("--scaffold-dir", default=str(SCAFFOLD),
                   help="静态脚手架目录（提示词/模板/render.py）")
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
    _slots = ((args.ch_a, args.label_a or args.ch_a, args.testid_a),
              (args.ch_b, args.label_b or args.ch_b, args.testid_b),
              (args.ch_c, args.label_c or args.ch_c, args.testid_c),
              (args.ch_d, args.label_d or args.ch_d, args.testid_d))
    for entry, label, want_tid in _slots:
        if not entry:
            continue
        key = normalize_channel(entry)
        _mi = len(cells) if len(cells) < len(mass_parts) else len(mass_parts) - 1
        cell = RP.load_cell(data_dir, key, mass_parts[_mi], dqdv_cycles,
                            args.source, testid=want_tid)
        if not cell or cell.get("error"):
            why = (cell.get("error")
                   if cell else "找不到数据目录。该通道得有测试数据（live 或正式）")
            print(f"  ❌ 通道 {entry}：{why}", file=sys.stderr)
            continue
        cells[key] = cell
        labels[key] = label
        # ★ 2026-10-11：演示日志说"人话"—— 不打绝对路径、不打"缺哪些圈"（负面信息），
        #   说做了什么：载入谁的数、截至何时、覆盖多少圈、dQ/dV 取了哪些圈。
        _cut = str(cell.get("progress_updated") or "—")[:16].replace("T", " ")
        _r = cell.get("cycle_range") or []
        _rng = f" · 第 {_r[0]}~{_r[1]} 圈" if (len(_r) == 2 and _r[0] is not None) else ""
        _dq = cell.get("dqdv_cycles_drawn") or []
        _dqs = f" · dQ/dV 取第 {'、'.join(str(c) for c in _dq)} 圈" if _dq else ""
        _short = "-".join(str(key).split("-")[-2:])
        print(f"  ✓ {label}（通道 {_short}）· 数据截至 {_cut}{_rng}{_dqs}")

    if len(cells) < 2:
        print("可用的通道不足 2 个 —— 每个通道都得有测试数据", file=sys.stderr)
        return 1

    # ---- 2. 数据契约 ----
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx = RP.build_global_context(labels, cells, mass_parts[0], generated,
                                variable_type=args.variable, dosage=args.dosage)
    # 产物名要能一眼看出"这是哪两颗的"（身份放文件名，不放目录 —— 见 AGENTS.md 七之四）
    _keys = list(cells)
    ctx["selection"] = {
        "ch_a": _keys[0], "ch_b": _keys[1],
        "short_a": "-".join(_keys[0].split("-")[-2:]),
        "short_b": "-".join(_keys[1].split("-")[-2:]),
        "label_a": labels[_keys[0]], "label_b": labels[_keys[1]],
    }
    if len(_keys) >= 4:
        ctx["selection"]["ch_c"] = _keys[2]
        ctx["selection"]["ch_d"] = _keys[3]
        ctx["selection"]["short_c"] = "-".join(_keys[2].split("-")[-2:])
        ctx["selection"]["short_d"] = "-".join(_keys[3].split("-")[-2:])
        ctx["selection"]["label_c"] = labels[_keys[2]]
        ctx["selection"]["label_d"] = labels[_keys[3]]
    cutoff = max((c.get("progress_updated") or "" for c in cells.values()))
    ctx["data_cutoff"] = cutoff or generated
    # ---- 数据来源标注（两套报告都出：必须让读者一眼看出这份是哪套）----
    kinds = sorted({c.get("kind") or "?" for c in cells.values()})
    if len(kinds) > 1:
        _zh = {"live": "进行中", "official": "正式数据集"}
        print(f"  ⚠️ 数据来源不一致（{'、'.join(_zh.get(k, k) for k in kinds)}）"
              " —— 报告里会如实标注，横向对比请谨慎看", file=sys.stderr)
    ctx["data_source"] = kinds[0] if len(kinds) == 1 else "mixed"
    import re as _re2
    _cut = str(ctx["data_cutoff"])
    _ts = bool(_re2.match(r"\d{4}-\d{2}-\d{2}", _cut))
    ctx["data_source_label"] = {
        "live": f"每日增量（进行中测试，数据截至 {_cut}）",
        "official": (f"正式数据集（测试已结束，采集于 {_cut}）" if _ts
                     else f"正式数据集（测试已结束；{_cut}）"),
        "mixed": "两颗电芯来源不一致（一颗进行中、一颗正式）—— 不作横向对比",
    }.get(ctx["data_source"], "（来源未标注）")
    # 正式数据集里两颗若不是同一次测试，明确写"非同批"（实测踩过：旧正式集 10-1 只有 1 圈）
    if ctx["data_source"] == "official":
        import re as _re
        days = {_re.sub(r"T.*", "", str(c.get("progress_updated") or ""))
                for c in cells.values()}
        if len(days) > 1:
            ctx["batch_warning"] = ("两份正式数据集的采集日期不同，**不是同一次测试**，"
                                    "本报告只作单颗参考，不作横向对比")

    # ---- 3. 六张图（模板的 FIGURE:1..6）----
    # 图 1 长循环 / 图 2 首效VQ / 图 3 平均电压 / 图 4 倍率 / 图 5、6 dQ/dV（每样品一张）
    fig_cells = []
    # ★ 2026-10-12：倍率换算用**标称容量**（300 mAh/g × 活性物质质量）——
    #   给了它，图 2 的档位才按 C 倍率标注（与报告 2.3 的口径一致）；不给只能标 mA。
    nominal = args.nominal_mah or (ctx.get("statistics") or {}).get("nominal_mah")
    for key in cells:
        c = cells[key]
        fig_cells.append({
            "label": labels[key], "cs": c["cs_plot"],
            "rates": F.rates_from_cs(c["cs_plot"], nominal),
            "dqdv": c["dqdv"], "curves": c["vq1"],
            "dqdv_multi": c["dqdv_multi"],
            "dqdv_cycles_drawn": c["dqdv_cycles_drawn"],
            "dqdv_cycles_missing": c["dqdv_cycles_missing"],
        })
    # ★ 图号 ↔ 用哪一对：**唯一出处**在契约里（report_pack.FIGURE_PAIRS）
    #   图1/3/4/5 = 对①（长循环）；图2 = 对②（倍率）；图 6 已取消 —— 共 5 张。
    fmap = ctx.get("figures") or {}
    vs1 = fmap.get("pair1") or "_vs_".join(labels[k] for k in cells)
    vs2 = fmap.get("pair2") or vs1
    # ★ 出图要的是"带 label 的 cell"（fig_cells），不是裸 cell —— 别取错来源
    _by_lab = {c["label"]: c for c in fig_cells}
    _l1 = [labels[k] for k in (fmap.get("ch1") or list(cells)[:2]) if k in labels]
    _l2 = [labels[k] for k in (fmap.get("ch2") or list(cells)[2:4]) if k in labels]
    p1 = [_by_lab[x] for x in _l1 if x in _by_lab]
    p2 = [_by_lab[x] for x in _l2 if x in _by_lab] or p1
    mass_flag = mass_parts[0] / 1000.0
    figs = {
        "对比_长循环_" + vs1: F.chart_long_cycle(f"{vs1}　（长循环）", p1,
                                                 mass_g=mass_flag),
        "对比_倍率_" + vs2: F.chart_rate(f"{vs2}　（倍率）", p2, mass_g=mass_flag),
        "对比_首效VQ_" + vs1: F.chart_vq(f"{vs1}　（首效VQ）", p1,
                                             mass_g=mass_flag),
        "对比_平均电压_" + vs1: F.chart_avgv(f"{vs1}　（平均电压）", p1),
    }
    # 图注用**裸 cell**（dropped_cycles 在它上面；fig_cells 是给画图用的另一种形状）
    drop_txt = "、".join(f"{labels[k]} 第 {'、'.join(map(str, cells[k]['dropped_cycles']))} 圈"
                        for k in cells if cells[k].get("dropped_cycles"))
    caps = ["活性物质质量：" + "、".join(
                f"{labels[k]} {mass_parts[min(n, len(mass_parts) - 1)]:g} mg"
                for n, k in enumerate(cells))
            + "（暂定值，待测试记录确认）"]
    if drop_txt:
        caps.append(f"末圈未走完（进行中），已从图上剔除：{drop_txt}")
    for f in figs.values():
        f.set_caption(caps)
    # 图 5、6：dQ/dV **每样品一张**（★ 2026-10-12 用户指定）——
    #   同一张里氧化(充电)与还原(放电)都画，按圈号**红/蓝渐变色阶**区分圈次；
    #   只画数据里真有的圈（第 200 圈没到就画到第 100 圈为止）。
    for _i, cell in enumerate(p1[:2], start=5):
        dm = cell.get("dqdv_multi") or {}
        if not dm:
            continue
        f = F.chart_dqdv_sample(f"{cell['label']}　（dQ/dV 逐圈）",
                                {"label": cell["label"], "dqdv_multi": dm},
                                reduction_positive=(args.dqdv_sign == "dis"),
                                idx=_i - 5)
        f.set_caption([f"符号口径：还原（嵌入/放电）为正峰；已画圈 "
                       + "、".join(f"第 {c} 圈" for c in sorted(dm))] + caps[:1])
        figs[f"dQdV_{cell['label']}"] = f
    # ★ 2026-10-11：图行说人话 —— 报"第几张图·画的是什么"，不报文件名
    _FIG_ZH = (("对比_长循环", "长循环性能对比"), ("对比_倍率", "倍率性能对比"),
               ("对比_首效VQ", "首圈效率与比容量曲线"),
               ("对比_平均电压", "平均电压与电压滞后"))
    _nf = len(figs)
    for _i, (name, fig) in enumerate(figs.items(), 1):
        _zh = next((z for pre, z in _FIG_ZH if name.startswith(pre)), None)
        if _zh is None and name.startswith("dQdV_"):
            _zh = f"dQ/dV 逐圈图谱 · {name.split('_', 1)[1]}"   # 每样品一张
        _zh = _zh or name
        print(f"  ✓ 图 {_i}/{_nf} · {_zh}")

    # ---- 4. 组装任务包（整目录替换；但 Agent 已写的段落默认保住）----
    # ★ 先校验输入再删包：原来顺序反了 —— rmtree 之后才发现脚手架不存在，
    #   于是"删了旧的、建不出新的"，任务包留在半成品（2026-10-04 实测踩到）。
    scaffold = Path(args.scaffold_dir)
    if not scaffold.is_dir():
        print(f"❌ 脚手架目录不存在：{scaffold}（**未动**已有任务包）", file=sys.stderr)
        return 1
    keep: dict[str, str] = {}
    seed = Path(args.paragraphs_from) if args.paragraphs_from else None
    if seed and seed.is_dir():
        for f in seed.glob("*.txt"):
            keep[f.name] = f.read_text(encoding="utf-8-sig")
        print(f"  ✓ 沿用段落种子：{len(keep)} 段")
    if args.keep_paragraphs and (out / "段落").is_dir():
        n_old = len(keep)
        for f in (out / "段落").glob("*.txt"):
            try:
                keep[f.name] = f.read_text(encoding="utf-8-sig")   # 任务包自己的更新，覆盖种子
            except OSError:
                pass
        if len(keep) > n_old:
            print(f"  ✓ 保留 AI 已写正文 {len(keep) - n_old} 段")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for item in scaffold.iterdir():
        if item.is_dir():
            shutil.copytree(item, out / item.name)
        else:
            shutil.copy2(item, out / item.name)
    # 段落回拷（放最后：脚手架里的空 段落/ 不覆盖已有内容）
    for name, text in keep.items():
        dest = out / "段落" / name
        dest.parent.mkdir(exist_ok=True)
        _atomic_write(dest, text)

    _atomic_write(out / "global_context.json",
                  json.dumps(ctx, ensure_ascii=False, indent=1))
    bd = out / "block_data"
    bd.mkdir(exist_ok=True)
    n = 0
    for bid in ("S11", "S12", "S31A", "S31B", "S31C", "S32A", "S32B", "S32C",
                "S32D", "S33A", "S33B", "S34A", "S34B", "S34C", "S35A", "S35B", "S35C",
                "S41A", "S41B", "S41C", "S41D",
                "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
                "S43A", "S43B", "S43C", "S43D",
                "S51A", "S51B", "S51C", "S52A", "S52B", "S52C", "S52D"):
        d = RP.build_block_data(bid, cells, ctx, labels)
        _atomic_write(bd / f"{bid}.json", json.dumps(d, ensure_ascii=False, indent=1))
        n += 1

    fig_dir = out / "图"
    fig_dir.mkdir(exist_ok=True)
    for fname, fig in figs.items():
        _atomic_write(fig_dir / f"{fname}.svg", fig.svg())

    # ---- 4b. 填空式正文（可选）：程序把"数据句"直接填好 ----
    if args.compose != "off":
        # compose_paragraphs.py 在脚手架里（也随包拷进去）；report_build 可能从别处运行
        # （共享目录的 报告工具/），所以先把这两个目录挂进 sys.path —— 实测漏了这步
        # 会 ModuleNotFoundError，而且它发生在 rmtree 之后，包会留在半成品。
        for _cand in (scaffold, out):
            if _cand.is_dir() and str(_cand) not in sys.path:
                sys.path.insert(0, str(_cand))
        import compose_paragraphs as CP
        n, skipped = CP.compose(out, args.compose, None)
        print(f"  ✓ 按最新数据重填 {n} 段程序正文" + (f"（沿用 {len(skipped)} 段）" if skipped else ""))


    print(f"\n✅ 分析任务包就绪：{len(cells)} 颗电芯 · 数据截至 {ctx.get('data_cutoff')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
