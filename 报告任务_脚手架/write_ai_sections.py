#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 撰写判断类章节：调本机模型把 3.6/4.1/4.2/4.3/5.2 共 21 段一次写完。

为什么由脚本调模型、而不是等 Agent 自己写：
    实测 QwenPaw **不会每次重读工作流** —— 它记得"生成报告=跑那三条命令"就照旧执行，
    于是"让 AI 写判断章节"这条永远落不了地（用户连续两次验证"无效"）。
    把它变成**流水线内部的一步**，才是机械保证：谁来触发都一样。

要点：
  · 一次调用写完 21 段（逐段调用要一个小时；批量只付一次固定开销）；
  · 数字纪律：AI 段里不许出现测试数值（表/图号、1 wt%、n=1 除外）—— 写进提示词，也由校验器兜底；
  · 写完交给 split_batch.py 拆段 + 过闸；不合格的**自动回灌重写一次**（只重写不合格的那几段）。

用法（在任务包目录里跑）：
    python3 write_ai_sections.py            # 写 21 段 + 拆段 + 校验（失败自动重试一次）
    python3 write_ai_sections.py --dry-run  # 只打印提示词长度与将调用的模型，不真调
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL_BASE = os.environ.get("MODEL_BASE", "http://10.201.9.180:12345/v1")
MODEL = os.environ.get("MODEL_NAME", "Qwen3.8-27B-AWQ-INT4")

# 要 AI 写的章节（判断类）：3.6 横向对比 / 4.1 核心结论 / 4.2 机制 / 4.3 建议 / 5.2 展望
AI_BLOCKS = ["S35A", "S35B", "S35C",
             "S41A", "S41B", "S41C", "S41D",
             "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
             "S43A", "S43B", "S43C", "S43D",
             "S52A", "S52B", "S52C", "S52D"]

NO_NUMBER_RULE = """\
【最重要的一条】你写的内容里**不许出现任何测试数值** —— 容量、比容量、库伦效率、保持率、
ΔV、峰位/峰间距、圈数、衰减速率……一个都不要写（数字由程序段承载，会随每日数据自动更新；
你写了数字，数据一变它就成了过期的假数）。要引用数据就写「见表 X」「见图 X」。
允许出现的只有：章节号与图表号（4.1、表 4、图 3）、变量定义（1 wt%、Q④）、样本量口径（n = 1、单颗电芯）。
"""


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig") if p.exists() else ""


def build_prompt(pkg: Path, only: list[str] | None = None,
                 repair: dict[str, list[str]] | None = None) -> str:
    blocks = only or AI_BLOCKS
    l1 = _read(pkg / "_system_prompt.txt")
    parts = [
        "你是电化学测试数据分析专家。下面要你**一次写完**若干报告段落的正文，"
        "输出一个 JSON（键=单元号，值=该段纯文本，段内换行用 \\n）。",
        "严格纪律：① 只用给定素材里的信息，不重算、不编造；② 缺数据就写「（该项数据未提供）」；"
        "③ 措辞克制，机制类只用「可能/推测/提示」，不得写「证明/表明/证实」；"
        "④ n=1 的段落必须出现「波动范围」或「复测」或「可能」之一。",
        NO_NUMBER_RULE,
        "【系统提示词（L1）节选】" + l1[:2500],
    ]
    if repair:
        parts.append("【上一次这些段不合格，逐条改掉】")
        for bid, reasons in repair.items():
            parts.append(f"- {bid}：" + "；".join(reasons))
    for bid in blocks:
        spec = _read(pkg / "提示词" / f"{bid}.txt")
        data = _read(pkg / "block_data" / f"{bid}.json")
        parts.append(f"\n=== 单元 {bid} ===\n【写法要求】\n{spec}\n【可用素材（唯一数值来源）】\n{data[:2600]}")
    parts.append(f"\n【输出】只输出 JSON，包含这 {len(blocks)} 个键："
                 + "、".join(blocks) + "；不要解释、不要代码块。")
    return "\n".join(parts)


def call_model(prompt: str, max_tokens: int = 12000) -> str:
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(MODEL_BASE + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def parse_blocks(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍代码块围栏、前后多余解释）。"""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("输出里找不到 JSON")
    return json.loads(s[i:j + 1])


def main() -> int:
    ap = argparse.ArgumentParser(description="调本机模型写判断类章节（21 段）")
    ap.add_argument("--package", default=".")
    ap.add_argument("--only", default=None, help="只写点名的单元，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pkg = Path(args.package).resolve()
    only = [x.strip() for x in args.only.split(",")] if args.only else None

    prompt = build_prompt(pkg, only)
    print(f"提示词 {len(prompt)} 字，模型 {MODEL} @ {MODEL_BASE}，待写 {len(only or AI_BLOCKS)} 段")
    if args.dry_run:
        return 0

    try:
        out = call_model(prompt)
    except Exception as e:                       # 模型不可用不该让整条链断掉
        print(f"⚠️ 调模型失败（{type(e).__name__}: {str(e)[:120]}）—— 跳过 AI 撰写，"
              "报告将只有程序段", file=sys.stderr)
        return 0
    try:
        blocks = parse_blocks(out)
    except Exception as e:
        print(f"⚠️ 解析模型输出失败：{str(e)[:120]}（原样存 /tmp/ai_raw.txt 供排查）", file=sys.stderr)
        Path("/tmp/ai_raw.txt").write_text(out, encoding="utf-8")
        return 0
    got = {k: v for k, v in blocks.items() if k in (only or AI_BLOCKS)}
    batch = pkg / "批量稿.json"
    batch.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"模型返回 {len(blocks)} 段，采信 {len(got)} 段 → 批量稿.json")

    # 拆段 + 过闸；不合格就带原因回灌重写一次（只重写不合格的那几段）
    for attempt in (1, 2):
        r = subprocess.run([sys.executable, "split_batch.py", str(batch)],
                           cwd=str(pkg), capture_output=True, text=True, encoding="utf-8")
        print((r.stdout or "").strip()[-600:])
        if r.returncode == 0:
            return 0
        bad = re.findall(r"✗ (\w+): (.+)", r.stdout or "")
        if not bad or attempt == 2:
            print("⚠️ 仍有不合格段落（已尽力），报告会照常生成，请看上面的原因", file=sys.stderr)
            return 0
        repair = {bid: [msg.strip()] for bid, msg in bad}
        print(f"→ 回灌重写不合格的 {len(repair)} 段：{'、'.join(repair)}")
        try:
            out2 = call_model(build_prompt(pkg, list(repair), repair))
            got2 = {k: v for k, v in parse_blocks(out2).items() if k in repair}
            got.update(got2)
            batch.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            print(f"⚠️ 重写调用失败：{str(e)[:120]}", file=sys.stderr)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
