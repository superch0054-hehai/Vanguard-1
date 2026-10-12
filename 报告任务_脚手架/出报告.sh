#!/usr/bin/env bash
# ★ 出报告就这一条命令（Agent 只要跑它，就得到含 AI 撰写章节的完整报告）
#
# 内部五步：
#   ① 程序段按最新 live 数据重填（AI 写过的段落由「出处台账」保护，不会被动）
#   ② 调本机模型写 3.6 / 4.1 / 4.2 / 4.3 / 5.2 共 21 段判断正文（不许写测试数值）
#   ③ 拆段 + 当场过闸（不合格自动回灌重写一次）
#   ④ 全文过闸
#   ⑤ 渲染 HTML
#
# 用法（在任务包目录里）：  bash 出报告.sh
#   仍想跑纯程序版（没有 AI 章节）：python3 compose_paragraphs.py --mode refresh && python3 render.py

set -u
cd "$(dirname "$0")" || exit 1
PY="${PY:-python3}"
export PYTHONUNBUFFERED=1    # 日志不被缓冲吞掉（排查时能看见错在哪一步）
echo "── ① 程序段按最新数据重填 ──"
"$PY" compose_paragraphs.py --mode refresh || exit 1

echo "── ② AI 撰写判断类章节（3.6 / 4.1 / 4.2 / 4.3 / 5.2，共 21 段）──"
"$PY" write_ai_sections.py          # 调模型失败也不中断：报告退化为纯程序段

echo "── ③④ 校验 ──"
# ★ 这一步**故意不因校验失败而中断**（2026-10-10 加过 `set -o pipefail` + `|| exit 1`，
#   是错的，已撤）：一旦中断就连 ⑤ 渲染都不跑 —— 成品根本不存在，人连"看一眼哪里不对"
#   的机会都没有；而控制台「打开报告」会退回到**上一轮的旧报告**（比一份被标红的
#   报告更危险）。**判定权只归外层 换通道出报告.sh 的验收闸门**：它才管
#   ALLOW_PLACEHOLDERS 放行与成品自检。这里只负责把失败大声印出来。
"$PY" validate_paragraphs.py | tail -8

echo "── ⑤ 渲染 ──"
"$PY" render.py
