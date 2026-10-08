#!/usr/bin/env bash
# 换通道出报告：主人点名两颗电芯（如 10-1 与 10-3）时跑这一条。
#
#   bash 换通道出报告.sh 10-1 10-3                 # 用进行中数据（每日增量）
#   bash 换通道出报告.sh 10-1 10-3 official        # 用已结束的正式数据集
#   LABEL_A=STD LABEL_B='1% Q④' bash 换通道出报告.sh 10-1 10-2
#       ↑ 样品名（体系身份）。**不给就等于告诉程序"我不知道这两个是什么"** ——
#         表1 的体系/变量、4.2 机制段等 12 处会降级（见闸门④）。给对了才有真内容。
#   ALLOW_PLACEHOLDERS=1 bash 换通道出报告.sh ...  # 明知有降级段也要出报告（演示兜底）
#
# 内部：① 用共享目录里的数据重建这两个通道的任务包 → ② 跑 出报告.sh
#       （程序段填数据句 + AI 写 3.6/4.1/4.2/4.3/5.2 + 校验 + 渲染）
#
# ★★ 四道闸门（2026-10-04 加）—— 原则：**宁可当场报错，也不静默出一份降级的报告。**
#   当天所有返工都出在"静默"上：圈号算错看不出来、产物名不对看不出来、
#   4.2 降级成占位而校验器说"38/38 通过"看不出来。所以这里逐个卡住：
#     ① .sh 行尾符：CRLF 的 .sh 会安静地崩，先查（当天就是这么栽的）
#     ② 脚手架一致：从 ~/batterylab 同步后再核对两侧完全相同，不一致就退出
#     ③ 数据新鲜：共享目录落后于源数据就自动刷新；仍落后就退出（免得报告读旧数据）
#     ④ 验收闸门：出完检查"硬性不过的段"和"数据不足占位"，有就大声报错并 exit 1

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"          # 共享目录根
PY="${PY:-python3}"
TOOLS="$HERE/报告工具"
DATA="${DATA_DIR:-$HERE}"                      # 数据源=共享目录（含 live/ 与正式数据集）
TASK="$HERE/报告任务"
AUTH="$HOME/batterylab/报告任务_脚手架"         # 脚手架的唯一出处
SCAF="$HERE/报告任务_脚手架"
CH_A="${1:?用法: bash 换通道出报告.sh <通道A> <通道B> [live|official]}"
CH_B="${2:?用法: bash 换通道出报告.sh <通道A> <通道B> [live|official]}"
SRC="${3:-live}"
case "$SRC" in live|official|auto) ;; *) echo "第三个参数只能是 live / official / auto"; exit 2;; esac

# ── 闸门①：.sh 行尾符 ───────────────────────────────────────────────────────
# CRLF 的 .sh 在 bash 下会报 `$'\r': 未找到命令` 之类，且在"没人执行它"时完全无声。
crlf=$(find "$AUTH" "$TOOLS" -maxdepth 1 -name '*.sh' -exec file {} \; 2>/dev/null | grep -c CRLF || true)
if [ "${crlf:-0}" != "0" ]; then
  echo "❌ 有 $crlf 个 .sh 是 CRLF 行尾，先转 LF：" >&2
  find "$AUTH" "$TOOLS" -maxdepth 1 -name '*.sh' -exec file {} \; 2>/dev/null | grep CRLF | sed 's/^/   /' >&2
  echo "   find '$AUTH' '$TOOLS' -maxdepth 1 -name '*.sh' -exec sed -i 's/\\r\$//' {} \\;" >&2
  exit 3
fi

# ── 闸门②：脚手架一致（先同步，再核对两侧真的一模一样） ─────────────────────
if [ -d "$AUTH" ]; then
  mkdir -p "$SCAF"
  cp -a "$AUTH/." "$SCAF/"
  bad=0
  for f in "$AUTH"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    cmp -s "$f" "$SCAF/$b" || { echo "  ❌ 脚手架同步后仍不一致：$b" >&2; bad=1; }
  done
  [ "$bad" = "0" ] || { echo "❌ 脚手架两侧不一致，拒绝建包（先跑 部署.sh）" >&2; exit 3; }
  echo "（脚手架已从 ~/batterylab 同步并核对一致）"
else
  echo "⚠️  找不到权威脚手架 $AUTH，用共享目录里那份继续（结果可能不是最新的）" >&2
fi

# ── 闸门③：数据新鲜（共享目录不得落后于源） ─────────────────────────────────
# ★ 2026-10-06 改：水桶模型下**只有一个位置**，共享目录里就是各个桶。
#   原来这里遍历 `$HERE/live/` —— live/ 已经取消，那道循环会**空转**，
#   等于闸门失效（忘了 make_share 也不出声）。现在直接比"共享的桶 vs 源的桶"。
_csv_rows() { [ -f "$1" ] && echo $(( $(wc -l < "$1") - 1 )) || echo 0; }
stale=""
for d in "$HERE"/channel=*/*; do
  [ -f "$d/detail.csv" ] || continue
  src="$HOME/batterylab/data/$(basename "$(dirname "$d")")/$(basename "$d")/detail.csv"
  [ -f "$src" ] || continue
  s=$(_csv_rows "$src"); h=$(_csv_rows "$d/detail.csv")
  [ "$s" -gt "$h" ] && stale="$stale $(basename "$d")(源$s>共享$h)"
done
if [ -n "$stale" ]; then
  echo "⚠️  共享目录落后于源数据：$stale"
  echo "→ 自动刷新（make_share）"
  ( cd "$HOME/batterylab" && "$PY" make_share.py --data-dir ./data --share-dir "$HERE" --force ) | tail -3
  still=""
  for d in "$HERE"/channel=*/*; do
    [ -f "$d/detail.csv" ] || continue
    src="$HOME/batterylab/data/$(basename "$(dirname "$d")")/$(basename "$d")/detail.csv"
    [ -f "$src" ] || continue
    [ "$(_csv_rows "$src")" -gt "$(_csv_rows "$d/detail.csv")" ] && still="$still $(basename "$d")"
  done
  if [ -n "$still" ]; then
    echo "❌ 刷新后共享目录仍然落后：$still —— 先查 ~/batterylab/data 与 make_share" >&2
    exit 3
  fi
  echo "✅ 共享目录已追平源数据"
fi

# ── 建包 ────────────────────────────────────────────────────────────────────
echo "── ① 重建任务包：$CH_A vs $CH_B（来源 $SRC）──"
ARGS=(--ch-a "$CH_A" --ch-b "$CH_B" --source "$SRC" --mass-mg "${MASS_MG:-2}"
      --data-dir "$DATA" --scaffold-dir "$SCAF" --compose refresh --out "$TASK")
# 样品名：给了才传。不给时程序会判定"体系未知"并把 12 处字段留空（那正是闸门④要报的）
[ -n "${LABEL_A:-}" ] && ARGS+=(--label-a "$LABEL_A")
[ -n "${LABEL_B:-}" ] && ARGS+=(--label-b "$LABEL_B")
# 指定用哪个测试（默认最新那个 = 当前在跑的）。★ 一个通道可能有多个测试：
# 换测试之后，最新那个可能才刚起、一圈都没有 —— 那时默认会**报错**而不是交空报告；
# 想用上一轮的数据就显式给：TESTID_A=263 TESTID_B=264 bash 换通道出报告.sh 10-1 10-2
[ -n "${TESTID_A:-}" ] && ARGS+=(--testid-a "$TESTID_A")
[ -n "${TESTID_B:-}" ] && ARGS+=(--testid-b "$TESTID_B")
[ -n "${LABEL_A:-}" ] && echo "  样品名：A=$LABEL_A  B=${LABEL_B:-$LABEL_A}" \
                      || echo "  ⚠️  未给 LABEL_A/LABEL_B —— 体系/变量信息会缺失（见闸门④）"
"$PY" -u "$TOOLS/report_build.py" "${ARGS[@]}" || exit 1

echo "── ② 出报告（含 AI 撰写判断章节）──"
( cd "$TASK" && bash 出报告.sh ) || exit 1

# ── 闸门④：验收闸门（有降级/不合格就大声报错） ──────────────────────────────
echo "── ③ 验收闸门 ──"
hard=$( ( cd "$TASK" && "$PY" validate_paragraphs.py 2>&1 ) | grep -oE '硬性不过 [0-9]+' | grep -oE '[0-9]+' | tail -1)
hard="${hard:-0}"
# 「数据不足」是程序的诚实占位 —— 但它意味着某个**输入没给**，不能当成合格报告拿给人看
ph_files=$(grep -l '数据不足' "$TASK"/段落/*.txt 2>/dev/null | xargs -r -n1 basename | tr '\n' ' ')
ph_cnt=$(grep -l '数据不足' "$TASK"/段落/*.txt 2>/dev/null | wc -l)

rc=0
if [ "$hard" != "0" ]; then
  echo "❌ 有 $hard 段硬性不合格（明细：$TASK/failed_blocks.json）" >&2
  rc=1
fi
if [ "$ph_cnt" != "0" ]; then
  echo "❌ 有 $ph_cnt 段是「数据不足」占位：$ph_files" >&2
  echo "   原因通常是**没给样品名** → 程序不知道实验变量是什么（report_pack._is_std_q_pair）。" >&2
  echo "   补法：  LABEL_A=STD LABEL_B='1% Q④' bash $0 $CH_A $CH_B $SRC" >&2
  echo "   （占位段是「程序不知道」，不是「数据没有」—— 校验器**认它为合格**，所以只靠校验发现不了）" >&2
  if [ "${ALLOW_PLACEHOLDERS:-0}" = "1" ]; then
    echo "   ALLOW_PLACEHOLDERS=1：已知情，继续（报告里仍会显示这些占位）" >&2
  else
    rc=1
  fi
fi
if [ "$rc" = "0" ]; then
  echo "✅ 验收闸门通过：无硬性不合格、无降级占位"
else
  echo "⚠️  报告已生成，但**先别拿给人看**（上面那些缺口要先补）" >&2
fi
exit "$rc"
