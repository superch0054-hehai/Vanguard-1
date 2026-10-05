#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「一次调用产出的 JSON」拆成 段落/<块>.txt，并当场过校验器。

为什么要有它：让 Agent 一口气写 17~21 段，输出一个 JSON 比让它逐段写文件稳得多
（少 20 次工具调用、也不会写错文件名）；但 JSON 必须落成段落文件才能渲染，
所以拆分这一步交给程序，并且**拆完立刻过校验器**，不合格的当场报出来。

用法：
    python3 split_batch.py 批量稿.json            # 拆分 + 校验
    python3 split_batch.py 批量稿.json --dry-run  # 只看能不能拆、不落盘
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def clean(text: str) -> str:
    """去掉模型爱加的壳：代码块围栏、每行前面的引号/编号、首尾空白。"""
    s = str(text).strip()
    s = re.sub(r"^```(?:json|text|markdown)?\s*|\s*```$", "", s).strip()
    out = []
    for line in s.splitlines():
        line = re.sub(r'^\s*(?:[-*]\s+)?"?', "", line).rstrip('"')
        out.append(line.rstrip())
    return "\n".join(out).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="批量稿 JSON → 段落/*.txt + 校验")
    ap.add_argument("json_file")
    ap.add_argument("--package", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pkg = Path(args.package).resolve()
    raw = Path(args.json_file)
    if not raw.exists():
        print(f"❌ 找不到 {raw}", file=sys.stderr)
        return 2
    data = json.loads(raw.read_text(encoding="utf-8-sig"))
    blocks = data.get("blocks") if isinstance(data, dict) and "blocks" in data else data
    if not isinstance(blocks, dict):
        print("❌ JSON 顶层应是 {块号: 正文} 或 {blocks: {块号: 正文}}", file=sys.stderr)
        return 2

    out_dir = pkg / "段落"
    out_dir.mkdir(exist_ok=True)
    written, empty = [], []
    for bid, text in blocks.items():
        body = clean(text)
        if not body:
            empty.append(bid)
            continue
        if not args.dry_run:
            (out_dir / f"{bid}.txt").write_text(body + "\n", encoding="utf-8")
        written.append(bid)
    # 落"出处台账"：这些段是 AI 写的，程序侧任何重填都不能覆盖它们
    if not args.dry_run and written:
        op = out_dir / "_来源.json"
        try:
            d = json.loads(op.read_text(encoding="utf-8-sig")) if op.exists() else {}
        except ValueError:
            d = {}
        for bid in written:
            d[bid] = "AI"
        op.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"拆分 {len(written)} 段" + ("（dry-run，未落盘）" if args.dry_run else "")
          + (f"；空内容跳过：{empty}" if empty else ""))
    if args.dry_run:
        return 0

    # 拆完立刻过闸：不合格的当场列出来（含修复提示），别等到渲染才发现
    sys.path.insert(0, str(HERE))
    try:
        import validate_paragraphs as V
    except ImportError:
        print("（没找到 validate_paragraphs.py，跳过校验）")
        return 0
    ctx_p = pkg / "global_context.json"
    ctx = json.loads(ctx_p.read_text(encoding="utf-8-sig")) if ctx_p.exists() else {}
    bad = {}
    for bid in written:
        bd = pkg / "block_data" / f"{bid}.json"
        d = json.loads(bd.read_text(encoding="utf-8-sig")) if bd.exists() else {}
        hard, _ = V.check_block(bid, (out_dir / f"{bid}.txt").read_text(encoding="utf-8-sig"),
                                d, ctx)
        if hard:
            bad[bid] = hard
    if bad:
        print(f"\n❌ {len(bad)} 段不合格：")
        for bid, reasons in bad.items():
            print(f"  ✗ {bid}: " + "；".join(reasons))
            print("     " + V.repair_prompt(bid, reasons).replace("\n", "\n     ")[:400])
        return 1
    print(f"✅ {len(written)} 段全部通过校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
