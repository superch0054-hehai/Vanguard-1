#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""段落校验器 —— 协会《提示词体系》第 5 章的 5-A/5-B 落到程序侧。

为什么要有：原来这 8 条只写在任务说明里，靠**模型自检**。实测两次事故都漏了 ——
① S31C 把数据包 notes 里给模型的约束句（"不得据此判断电池好坏"）原样抄进了正文；
② S34A 照着提示词里写死的句式写成"峰间距由 0.021 增大至 0.015"（数据其实**变小**），
   数字全对、方向反了，肉眼极难发现。
这两类是"跑得通、看不出来"的错，只能由程序拦。

用法：
    python3 validate_paragraphs.py [--package .] [--json failed_blocks.json]
退出码 0 = 全部通过；1 = 有硬性项不过（可用它当流水线闸门）。

口径依据：5-A 硬性 8 条（数值溯源/未提供字段/禁用词/n=1 结论句/机制措辞/格式污染/
长度/图表号引用）＋ 5-B 的"方向一致性"，外加本项目自己踩出来的
"instructions 泄漏"（数据包里给模型的约束不得出现在正文）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION = "1.0"

BANNED = ["显著提升", "显著改善", "证明了", "表明该变量有效", "大幅", "最优", "完美"]
MECH_WORDS = ("可能", "推测", "提示", "倾向")
N1_WORDS = ("波动范围", "复测", "需验证", "可能", "不确定")
META_WORDS = ("如下所示", "综上，本模型", "作为 AI", "本报告将")
MECH_BLOCKS = ("S42",)
N1_BLOCKS = ("S41", "S42", "S43", "S51", "S52")
# 方向词：正/反两组，用来做"方向一致性"检查
UP, DOWN = ("增大", "上升", "升高", "变大"), ("减小", "下降", "降低", "变小")
# 块 → 该方向字段管的是哪个指标（出现这些词才判方向）
DIR_KEYWORDS = {
    "avg_v_trend": ("平均电压", "平均放电电压"),
    "dV_trend": ("ΔV", "极化", "电压滞后"),
    "peak_gap_trend": ("峰间距",),
}

NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# ★ AI 写的段落里**允许**出现的数字（其余一律不许 —— 数字只留在程序段里）：
#   章节号、图表号、变量定义（1 wt%）、样本量口径（n = 1 / 单颗）。测试结果类数值
#   （容量/效率/ΔV/峰位/圈数…）必须由程序段承载，AI 要引用就写"见表 X"。
AI_DIGIT_OK = [
    r"[图表]\s*\d+",              # 图 3 / 表 4
    r"\d+(?:\.\d+)*\s*[章节]",     # 4.1 节 / 2.3 节
    r"\d+\.\d+\.\d+",           # 4.1.1 这类编号
    r"1\s*wt\s*%",                  # 添加量（变量定义，不是测量值）
    r"n\s*_?\s*parallel\s*=\s*1", # 样本量口径
    r"n\s*=\s*1",
    r"1\s*颗",                       # "每条件 1 颗"
]
AI_SECTION = re.compile(r"\d+\.\d+")


def _numbers(text: str) -> list[str]:
    return NUM_RE.findall(text)


def _data_numbers(obj, acc: set[float]) -> None:
    """递归收集数据包里出现过的**数值**（用于溯源比对）。

    存 float 而不是字符串：正文写 "1.73"，数据里是 "-1.73"（差值带负号）、
    正文写 "100.0%"，数据里是 100.0 —— 字符串比对会把这些**合规**输出判成编造
    （实测踩过一轮）。所以按数值比，并同时收绝对值与单位换算变体。
    """
    if isinstance(obj, dict):
        for v in obj.values():
            _data_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _data_numbers(v, acc)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        base = float(obj)
        for v in (base, -base, abs(base)):
            for scale in (1.0, 1000.0, 100.0, 0.001, 0.01):
                acc.add(round(v * scale, 6))
                for nd in range(0, 5):
                    acc.add(round(v * scale, nd))
    elif isinstance(obj, str):
        for tok in NUM_RE.findall(obj):
            try:
                _data_numbers(float(tok), acc)
            except ValueError:
                pass


def _structural(text: str) -> set[str]:
    """结构性数字：章节号、图号表号、圈号、阈值这类不是"数据"的数。"""
    s = set()
    s.update(re.findall(r"第\s*(\d+(?:\.\d+)?)\s*圈", text))
    s.update(re.findall(r"[图表]\s*(\d+)", text))
    s.update(re.findall(r"(\d+)\.\d+\.\d+", text))
    s.update(re.findall(r"(\d+)\s*个?百分点", text))
    return s


# 建议(4.3)/展望(5.2)是**固定提案清单**：里面的浓度梯度、温区是方案参数，不是测试结果，
# 允许照写（0.5~3%、−20~60 ℃ 这类）；判"不许写测试数值"只对 3.6/4.1/4.2 这三章生效。
AI_DIGIT_BLOCKS = ("S35", "S41", "S42")


# AI 写作的章节（3.6 / 4.1 / 4.2 / 4.3 / 5.2）—— 与 write_ai_sections.AI_BLOCKS 必须一致（有测试钉住）
AI_BLOCKS = ("S35A", "S35B", "S35C",
             "S41A", "S41B", "S41C", "S41D",
             "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
             "S43A", "S43B", "S43C", "S43D",
             "S52A", "S52B", "S52C", "S52D")


# ---------------------------------------------------------------------------
# 降级占位的认定 —— **唯一出处**（闸门不再自己 grep）
#
# ★ 约定：程序侧的降级占位一律**整行写成一对【】** ——
#   【该项数据未提供】（compose._mech）、【图 N 未生成…】（render.figure_html）、
#   【本段未通过自动校验…】（render）。以后新增占位也照这个写，扫描才看不见漏。
#
# 为什么 2026-10-10 改掉原来的「<40 字 + 关键词表」：
#   闸门当年用裸 `grep '数据不足'` 判占位，结果**两头都错** ——
#   ① 真占位【该项数据未提供】里没有"数据不足"四个字 → **漏检**（安全网是破的）；
#   ② 模型正文里"现有数据**不足以**判定…"命中那四个字 → **误报 exit 1**（实测踩到）。
#   靠关键词表判形状，换个措辞（比如【机制库为空】）照样漏；**结构才是稳定的**。
#   老规则保留为并集的一支，只为不改既有行为：它挡住"短段落里只写了（该项数据未提供）"
#   这种（若判成正文，N1/长度等规则会把它误判成硬性失败）。
#   ⚠ 但**裸「数据不足」故意不收**：它是正常中文短语（"现有数据**不足以**判定…"），
#     收进来就会对**短正文**误报 —— 实测就是这么被自己的测试抓出来的。
PLACEHOLDER_LINE_RE = re.compile(r"^【[^】]*】$")
LEGACY_MARKER_WORDS = ("未通过自动校验", "该项数据未提供", "该块数据不足", "暂不生成")


def placeholder_lines(text: str) -> list[str]:
    """文本里那些"整行就是一对【】"的行（报错时列出来用）。"""
    return [ln.strip() for ln in (text or "").splitlines()
            if PLACEHOLDER_LINE_RE.match(ln.strip())]


def is_placeholder(text: str) -> bool:
    """整段是不是程序写的降级占位（而不是正文）。"""
    t = (text or "").strip()
    if not t:
        return False
    if placeholder_lines(t):
        return True
    return len(t) < 40 and any(k in t for k in LEGACY_MARKER_WORDS)


def _ai_digit_violation(text: str, ctx: dict | None = None,
                        data: dict | None = None) -> list[str]:
    """返回 AI 段里"不该出现的数字"（去掉白名单后剩下的）。

    ⚠️ 白名单必须包含**样品名与添加量**：实测 AI 写"STD 组略高于 1%Q4 组"，
    其中的 1、4 被当成测试数值误报（14 段全红）。样品名是变量定义，不是测量值。
    """
    clean = text
    # 样品名（1%Q④ / STD 这类）与添加量先剥掉 —— 它们来自 global_context，是变量定义
    var = (ctx or {}).get("variable") or {}
    for lab in (var.get("control_short"), var.get("experimental_short"),
                var.get("experimental_dosage")):
        if lab:
            clean = clean.replace(str(lab), " ")
            clean = clean.replace(str(lab).replace("④", "4"), " ")
            clean = clean.replace(str(lab).replace("④", ""), " ")
            clean = clean.replace(str(lab).replace("%", "％"), " ")
    # 机制段的"现象短语"是**给定素材**（如"稳态 CE 贴近 100%"），必须原样引用 → 先剥掉
    for g in (data.get("mechanism_kb") or []) if isinstance(data, dict) else []:
        for ph in (g.get("matched_phenomena") or []):
            clean = clean.replace(str(ph), " ")
    for pat in list(AI_DIGIT_OK) + [r"\d+\s*%\s*[Qq][④4]?", r"\d+\s*％\s*[Qq][④4]?"]:
        clean = re.sub(pat, " ", clean)
    return sorted(set(NUM_RE.findall(clean)))


def check_block(bid: str, para: str, data: dict, ctx: dict,
                ai_written: bool = False) -> tuple[list[str], list[str]]:
    """→ (硬性失败原因, 告警)。空列表 = 该块通过。"""
    hard: list[str] = []
    warn: list[str] = []
    text = para.strip()
    if not text:
        return [f"段落为空"], []
    # 程序级的降级标记（整段就是"缺数据/需人工复核"）不是正文，内容类规则不适用。
    # ⚠ 只有**整段就是标记**时才豁免：句子里顺带提一句"（该项数据未提供）"的**长正文**
    #   照样要查（否则 S31C 那种"既缺数据、又抄了指令"的段落会整段溜过去 —— 实测）。
    #   判据见 is_placeholder()（唯一出处，闸门用的是同一条）。
    if is_placeholder(text):
        return [], []

    # 5-A#1/#2 数值溯源（用**数值比较**，不是字符串比对：55.79 要能对上 55.79172…）
    pool: set[float] = set()
    _data_numbers(data, pool)
    _data_numbers(ctx, pool)
    struct = _structural(text)
    untraced = []
    for tok in _numbers(text):
        if tok in struct:
            continue
        try:
            v = float(tok)
        except ValueError:
            continue
        if not any(abs(v - f) < 1e-9 for f in pool):
            untraced.append(tok)
    if untraced:
        hard.append("数值不可溯源：" + "、".join(sorted(set(untraced))[:6]))

    # 5-A#3 禁用词
    for w in BANNED:
        if w in text:
            hard.append(f"禁用词：{w}")

    # 5-A#6 格式污染 / 元话语
    for tok in ("#", "|", "```", "{", "}", "<", ">"):
        if tok in text:
            hard.append(f"格式污染：{tok}")
            break
    for w in META_WORDS:
        if w in text:
            hard.append(f"元话语：{w}")

    # 5-A#8 图号/表号引用必须与数据包给的一致
    fig_ref, tab_ref = data.get("figure_ref"), data.get("table_ref")
    for kind, ref in (("图", fig_ref), ("表", tab_ref)):
        if not ref:          # 数据包没给编号 → 不做越界判定（任务包版本问题，非模型之过）
            continue
        used = set(re.findall(kind + r"\s*(\d+)", text))
        allowed = set(re.findall(r"(\d+)", ref))
        if used and not used <= allowed:
            hard.append(f"{kind}号引用越界：正文用了 {sorted(used)}，数据包只给了 {ref}")

    # 5-A#4 n=1 结论句
    # 例外：S42INTRO 是 4.2 的**开场句**（一句话引出），口径由配对的收尾句 S42E 承载 ——
    # 强制一句开场白也写"需复测"会把句子撑成病句（实测试了两轮模型都写不进去）。
    if (bid.startswith(N1_BLOCKS) and bid != "S42INTRO"
            and not any(w in text for w in N1_WORDS)):
        hard.append("n=1 结论句缺失（需含 波动范围/复测/需验证/可能）")

    # 5-A#5 机制措辞
    if bid.startswith(MECH_BLOCKS) and not any(w in text for w in MECH_WORDS):
        hard.append("机制措辞缺失（需含 可能/推测/提示）")

    # ★ 分工纪律：AI 只写判断，数字归程序段（2026-10-04 定的路线）
    if ai_written:
        bad_digits = (_ai_digit_violation(text, ctx, data)
                      if bid.startswith(AI_DIGIT_BLOCKS) else [])
        if bad_digits:
            hard.append("AI 段里不该出现测试数值：" + "、".join(bad_digits[:6])
                        + "（数字由程序段承载；要引用就写「见表 X」/「见图 X」）")

    # 5-B 方向一致性：方向字段说"减小"，正文就不能写"增大"
    for field, kws in DIR_KEYWORDS.items():
        want = None
        for g in (data.get("groups") or {}).values():
            if isinstance(g, dict) and g.get(field):
                want = g[field]
                break
        if not want:
            continue
        if not any(k in text for k in kws):
            continue
        # 取包含关键词的那句话，看其中的方向词
        for sent in re.split(r"[。；;]", text):
            if any(k in sent for k in kws):
                has_up = any(w in sent for w in UP)
                has_down = any(w in sent for w in DOWN)
                if want in DOWN and has_up and not has_down:
                    hard.append(f"方向与数据相反：数据是「{want}」，正文写的是「增大/上升」")
                elif want in UP and has_down and not has_up:
                    hard.append(f"方向与数据相反：数据是「{want}」，正文写的是「减小/下降」")

    # 本项目自加：instructions（给模型的约束）一个字都不许进正文。
    # ⚠ 用**长子串**匹配，不能用前缀：模型抄的时候常把开头改两个字
    #   （实测把"效率口径："改写成"效率口径说明，"），前缀一比就漏过去了。
    flat = re.sub(r"\s", "", text)
    for ins in data.get("instructions") or []:
        core = re.sub(r"[★\s]", "", ins)
        hit = False
        for i in range(len(core)):
            for n in range(len(core) - i, 11, -1):     # 找长度 ≥12 的共同片段
                if core[i:i + n] in flat:
                    hit = True
                    break
            if hit:
                break
        if hit:
            hard.append("抄了数据包里给模型的约束句（instructions 泄漏）")
            break

    # 5-A#7 长度（告警，不判失败）
    rng = data.get("length_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        lo, hi = rng
        if not (lo * 0.5 <= len(text) <= hi * 1.5):
            warn.append(f"长度 {len(text)} 超出区间 {lo}~{hi} 的 ±50%")
    return hard, warn


def check_package(pkg: Path) -> tuple[dict, dict, int]:
    """跑一遍内容校验 → (failed, warned, ok)。调用方要自己保证 段落/ 存在。"""
    ctx_p = pkg / "global_context.json"
    ctx = json.loads(ctx_p.read_text(encoding="utf-8-sig")) if ctx_p.exists() else {}
    para_dir, bd_dir = pkg / "段落", pkg / "block_data"
    failed: dict = {}
    warned: dict = {}
    ok = 0
    for p in sorted(para_dir.glob("*.txt")):
        bid = p.stem
        bd_p = bd_dir / f"{bid}.json"
        data = json.loads(bd_p.read_text(encoding="utf-8-sig")) if bd_p.exists() else {}
        hard, warn = check_block(bid, p.read_text(encoding="utf-8-sig"), data, ctx,
                                 ai_written=bid in AI_BLOCKS)
        if hard:
            failed[bid] = {"reasons": hard, "repair": repair_prompt(bid, hard)}
        else:
            ok += 1
        if warn:
            warned[bid] = warn
    return failed, warned, ok


def placeholder_blocks(pkg: Path) -> list[str]:
    """哪些段落整段/整行就是程序的降级占位（闸门判定用；与 check_block 同一条规则）。"""
    return [p.stem for p in sorted((pkg / "段落").glob("*.txt"))
            if placeholder_lines(p.read_text(encoding="utf-8-sig"))]


def scan(pkg: Path) -> int:
    """闸门专用：把"这份报告能不能信"压成机器可读的几行（给 .sh 用，别再 grep 中文）。

    为什么和内容校验分开：占位扫描**只读 段落/** —— 任务包半残（compose 挂了、
    block_data 没生成）时它照样能跑，而那恰恰是最该出声的时候。

    输出（.sh 靠这几行判定；**取不到 = 没验成 ≠ 通过**）：
        PLACEHOLDERS=<n>         整行【…】的段落数
        PLACEHOLDER_BLOCKS=a,b   段名（没有就整行只剩等号）
        HARD=<n> | HARD=?        硬性不过的段数；=? = 这次**算不出来**（缺 block_data/
                                 global_context —— 硬算只会得到一堆假的"数值不可溯源"）
    退出码：0 = 三行都给了；3 = 连 段落/ 都没有（什么都验不了）。
    """
    para_dir = pkg / "段落"
    if not para_dir.is_dir():
        print(f"❌ 没有 段落/ 目录，无法验证：{pkg}", file=sys.stderr)
        return 3
    ph = placeholder_blocks(pkg)
    print(f"PLACEHOLDERS={len(ph)}")
    print("PLACEHOLDER_BLOCKS=" + ",".join(ph))
    if (pkg / "block_data").is_dir() and (pkg / "global_context.json").exists():
        failed, _warned, _ok = check_package(pkg)
        print(f"HARD={len(failed)}")
    else:
        print("HARD=?")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="段落校验（5-A 八条 + 方向一致性 + instructions 泄漏）")
    ap.add_argument("--package", default=".")
    ap.add_argument("--json", default="failed_blocks.json")
    ap.add_argument("--scan", action="store_true",
                    help="闸门模式：只打印机器可读的 PLACEHOLDERS=/HARD= 几行")
    args = ap.parse_args()
    pkg = Path(args.package).resolve()
    if args.scan:
        return scan(pkg)

    if not (pkg / "段落").is_dir() or not (pkg / "block_data").is_dir():
        print(f"❌ 不是任务包目录（缺 段落/ 或 block_data/）：{pkg}", file=sys.stderr)
        return 1
    failed, warned, ok = check_package(pkg)

    if failed:
        print(f"全文校验：{len(failed) + ok + len(warned)} 段 · 通过 {ok} · "
              f"硬性不过 {len(failed)} · 告警 {len(warned)}")
    else:
        print(f"  ✅ 全文校验通过：{len(failed) + ok + len(warned)}/{len(failed) + ok + len(warned)} 段"
              f"（硬性 0 · 告警 {len(warned)}）")
    for bid, info in failed.items():
        print(f"  ✗ {bid}: " + "；".join(info["reasons"]))
    for bid, ws in warned.items():
        if bid not in failed:
            print(f"  ⚠ {bid}: " + "；".join(ws))
    out = pkg / args.json
    out.write_text(json.dumps({"failed": failed, "warned": warned},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("  （不合格明细见 failed_blocks.json）")
    return 1 if failed else 0


def repair_prompt(bid: str, reasons: list[str]) -> str:
    """按协会 5-B 的模板生成回灌提示（只重发原提示词是没用的，要说清错在哪）。"""
    lines = ["【修正任务】你上一次的输出存在以下问题："]
    lines += [f" {i + 1}) {r}；" for i, r in enumerate(reasons)]
    lines += ["请重写本段，并严格遵守：",
              "- **不要写任何测试数值**（容量/效率/保持率/ΔV/峰位/圈数等）；"
              "数字由程序段承载，要引用就写「见表 X」或「见图 X」；",
              "- 只使用【本块数据】中出现的数值，不要做加减乘除；",
              "- 方向（增大/减小、上升/下降）必须与数据包给出的方向字段一致；",
              "- 不使用“显著、证明、大幅、最优”等词；",
              "- 只输出纯文本正文，不含任何标签、标题符号、表格或编号；",
              "- 数据包里 instructions 字段的内容一个字都不要写进正文。",
              "", f"【重写目标】{bid} 的提示词见 提示词/{bid}.txt", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
