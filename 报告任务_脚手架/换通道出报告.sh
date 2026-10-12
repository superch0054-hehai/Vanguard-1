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
# 取通道短名（10-1）：全名 27-188-10-1 和短名 10-1 都能处理
_short() { local a; IFS='-' read -ra a <<< "$1"
           if [ ${#a[@]} -ge 2 ]; then echo "${a[-2]}-${a[-1]}"; else echo "$1"; fi; }
# ★ 2026-10-09：默认改成 auto —— 合并成一个位置后，"进行中/已结束"由桶的
#   progress.json 决定，引用方不必再猜"这轮跑完没有"。想只看某种状态再显式给。
SRC="${3:-auto}"
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
echo "✓ 运行环境自检通过（脚本 · 行尾符 · 依赖）"

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
  # ★ 2026-10-09：cp -a 只拷不删 —— 本地删掉的文件，共享目录这份也得消失，
  #   否则旧文件会一直留着、还会被拷进任务包。
  for f in "$SCAF"/*; do
    b=$(basename "$f")
    [ -e "$AUTH/$b" ] || { rm -rf "$f"; echo "  🧹 脚手架删掉（权威版已无）：$b"; }
  done
  echo "✓ 报告工程已同步并核对一致（模板 · 校验器 · 提示词）"
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
  echo "⚠️ 检测到新数据（$(echo $stale | wc -w) 个数据集落后于源）→ 自动同步共享数据集…"
  ( cd "$HOME/batterylab" && "$PY" make_share.py --data-dir ./data --share-dir "$HERE" --force ) 2>&1 \
    | grep -Ev "读 /home" || true        # 滤掉 make_share 末尾那几行"使用说明"（给人看的，别进演示日志）
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
  echo "✓ 数据新鲜度校验通过（共享数据集与源一致）"
fi

# ── 建包 ────────────────────────────────────────────────────────────────────
echo "── ① 重建任务包 ──"
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
# ★ 2026-10-09：对②（倍率那对）—— 用环境变量传，不动位置参数
[ -n "${CH_C:-}" ] && ARGS+=(--ch-c "$CH_C")
[ -n "${CH_D:-}" ] && ARGS+=(--ch-d "$CH_D")
[ -n "${LABEL_C:-}" ] && ARGS+=(--label-c "$LABEL_C")
[ -n "${LABEL_D:-}" ] && ARGS+=(--label-d "$LABEL_D")
# 变量（进表 1 与 4.2 机制段）。不填则用 report_pack 里本批的默认值（PBSF 1 wt%）
[ -n "${VARIABLE:-}" ] && ARGS+=(--variable "$VARIABLE")
[ -n "${DOSAGE:-}" ] && ARGS+=(--dosage "$DOSAGE")
if [ -n "${LABEL_A:-}" ]; then
  echo "  本次对比：${LABEL_A} vs ${LABEL_B:-${LABEL_A}}（通道 $(_short "$CH_A") / $(_short "$CH_B")）"
  [ -n "${CH_C:-}" ] && echo "  倍率对比：${LABEL_C:-} vs ${LABEL_D:-}（通道 $(_short "$CH_C") / $(_short "$CH_D")）"
else
  echo "  ⚠️  未给样品名（LABEL_A/LABEL_B）—— 体系/变量信息会缺失（见闸门④）"
fi
"$PY" -u "$TOOLS/report_build.py" "${ARGS[@]}" || exit 1

echo "── ② 出报告（含 AI 撰写判断章节）──"
( cd "$TASK" && bash 出报告.sh ) || exit 1

# ── 闸门④：验收闸门（有降级/不合格就大声报错） ──────────────────────────────
# ★ 2026-10-10 重写，两个坑：
#   ① 判据只留一处。原来这里自己 `grep '数据不足'`，结果**两头都错** ——
#      真占位【该项数据未提供】里没有"数据不足"四个字 → **漏检**（安全网是破的）；
#      模型正文"现有数据**不足以**判定…"命中那四个字 → **误报 exit 1**（用户实测踩到）。
#      现在占位的认定交给 validate_paragraphs.py（is_placeholder 是唯一出处），
#      这里只读它给的机器可读几行，不再 grep 中文。
#   ② **验不了 ≠ 通过**。原来 `hard="${hard:-0}"`：校验器一旦跑不起来（比如 段落/ 丢了），
#      stdout 里没有那行，就被当成"0 个不过"静默放行 —— 跟"宁可当场报错"正好相反。
echo "── ③ 验收闸门 ──"
scan_out=$( cd "$TASK" && "$PY" validate_paragraphs.py --scan 2>&1 ); scan_rc=$?
ph_cnt=$(printf '%s\n' "$scan_out" | grep -oE '^PLACEHOLDERS=[0-9]+$' | tail -1 | cut -d= -f2)
ph_files=$(printf '%s\n' "$scan_out" | grep -oE '^PLACEHOLDER_BLOCKS=.*$' | tail -1 | cut -d= -f2-)
hard=$(printf '%s\n' "$scan_out" | grep -oE '^HARD=([0-9]+|\?)$' | tail -1 | cut -d= -f2)
if [ "${ph_cnt:-}" = "0" ] && [ "${hard:-}" = "0" ]; then
  echo "   ✓ 占位 0 · 硬性不合格 0"
else
  printf '%s\n' "$scan_out" | sed 's/^/   /' >&2      # 有问题/没验成 → 原样亮出来
fi

rc=0
if [ -z "${ph_cnt:-}" ] || [ -z "${hard:-}" ]; then
  echo "❌ 无法验证：--scan 没给出结果（rc=$scan_rc）—— 先查 $TASK/段落 与上面的输出" >&2
  echo "   （**没验成不等于通过**，所以这里直接卡住）" >&2
  rc=1
else
  if [ "$hard" = "?" ]; then
    echo "❌ 硬性项这次**算不出来**（缺 block_data/ 或 global_context.json）—— 视为没验成" >&2
    rc=1
  elif [ "$hard" != "0" ]; then
    echo "❌ 有 $hard 段硬性不合格（明细：$TASK/failed_blocks.json）" >&2
    rc=1
  fi
  if [ "$ph_cnt" != "0" ]; then
    echo "❌ 有 $ph_cnt 段是程序写的降级占位：$ph_files" >&2
    echo "   占位 = **程序侧某个输入没给**，不是「数据没有」。常见两类：" >&2
    echo "     · 体系/变量没传对 → 表 1 留空、4.2 机制段降级（查 LABEL_A/B/C/D 与 VARIABLE）" >&2
    echo "     · 该段 block_data 里对应字段为空（看 提示词/<段名>.txt 与 block_data/<段名>.json）" >&2
    if [ "${ALLOW_PLACEHOLDERS:-0}" = "1" ]; then
      echo "   ALLOW_PLACEHOLDERS=1：已知情，继续（报告里仍会显示这些占位）" >&2
    else
      rc=1
    fi
  fi
fi

# ── 闸门⑤：成品自检（渲染期占位） ───────────────────────────────────────────
# 段落扫描**看不到**渲染时才产生的占位：段落文件缺失、**图没生成**、机制段降级警告
# —— 它们由 render.py 直接写进 HTML。项目历史上"图 2 未生成"是靠人眼发现的，
# 这一道就是为了补那个洞（render.py 不转义正文，所以直接按文本找【…】即可）。
echo "── ④ 成品自检 ──"
HTML=$(ls -t "$TASK"/SOH分析报告*.html 2>/dev/null | head -1)
if [ -z "${HTML:-}" ]; then
  echo "❌ 没找到成品 HTML（$TASK/SOH分析报告*.html）—— 渲染那一步没成功？" >&2
  rc=1
else
  marks=$(grep -oh '【[^】]*】' "$HTML" 2>/dev/null | sort -u | tr '\n' ' ')
  if [ -n "${marks:-}" ]; then
    echo "❌ 成品里有 $(printf '%s' "$marks" | grep -o '】' | wc -l) 处占位标记：$marks" >&2
    if [ "${ALLOW_PLACEHOLDERS:-0}" = "1" ]; then
      echo "   ALLOW_PLACEHOLDERS=1：已知情，继续" >&2
    else
      rc=1
    fi
  else
    echo "   ✓ 成品自检通过（无占位 · 无缺图）"
  fi
fi

if [ "$rc" = "0" ]; then
  echo "✅ 验收闸门通过：无硬性不合格、无降级占位、成品干净"
else
  echo "⚠️  报告已生成，但**先别拿给人看**（上面那些缺口要先补）" >&2
fi
exit "$rc"
