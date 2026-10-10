#!/usr/bin/env bash
# 一键部署：本地仓库 → 盒子。**这是代码上盒子的唯一入口**（别再手打 scp）。
#
#   bash 部署.sh              # 全流程：备份 → 传 → 行尾符归一 → md5 核对 → 刷新共享目录
#   bash 部署.sh --check      # 只核对，不写任何东西（查"两边是否一致"）
#   bash 部署.sh --with-tests # 传之前先在本地跑全套 selftest（慢，但推荐改完代码后用）
#
# 为什么要有这个脚本（2026-10-04 的血泪）：
#   当天所有返工都不在生成逻辑里，而在"同一个东西存在多份、**被执行的那份不是我们以为的那份**"：
#     · 带脚手架同步步骤的入口脚本只在 ~/batterylab/，真正执行的是共享目录里那份旧版 → 改进不生效
#     · 那份"权威"脚本是 CRLF 行尾，bash 跑不起来（`$'\r'`）→ 就算放对位置也不执行
#     · render.py / report_build.py 在不同位置行尾符不同，看 md5 像"内容不一致"，误导排查
#     · 共享目录的数据要手动 make_share 才更新，忘了就出一份读旧数据的报告
#   这个脚本把这四件事一次性收口：**一次命令、逐项核对、不一致就出声**。
#
# 单一出处约定（结构）：
#   代码的**唯一出处是本地 git 仓库**（neware_comms/）。
#   盒子上任何 .py/.sh 都是**部署产物**，不许在盒子上改。
#   报告脚手架（compose/render/validate/write_ai/split_batch/出报告.sh/换通道出报告.sh）
#   只允许存在于 `报告任务_脚手架/` 一处；仓库根与 ~/batterylab/ 顶层不该有它们的副本。

set -u
cd "$(dirname "$0")" || exit 1

BOX="${BOX:-ty1200}"
# 注意：$HOME 在 Git Bash 里是**本机**的家目录（/c/Users/...），不能拿来拼远端路径
# —— 2026-10-04 第一版就栽在这，核对时把本机路径发给了盒子，于是一片"内容不同"的假警报。
RHOME="${REMOTE_HOME:-/home/admin}"
LAB="$RHOME/batterylab"
SHARE="$RHOME/jerry/batterylab-data"
TOOLS="$SHARE/报告工具"
SCAFFOLD_DIR="报告任务_脚手架"
STAMP="$(date +%Y%m%d_%H%M%S)"
BK="$LAB/_backup_${STAMP}_部署"
CHECK_ONLY=0
RUN_TESTS=0
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --with-tests) RUN_TESTS=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

# ── 要部署的清单（显式列出，避免把 docs/ 之类也推上去） ──────────────────────
keep=""
RUNTIME_PY="probe.py figures.py report_pack.py report_build.py make_share.py
collector.py console.py control.py neware_client.py reconcile.py validate.py
export_review.py dump_raw.py"
TESTS="selftest.py selftest_cli.py selftest_collector.py selftest_console.py
selftest_control.py selftest_export.py selftest_figures.py selftest_make_share.py
selftest_reconcile.py selftest_report.py selftest_service.py selftest_validate.py"
SHARE_TOOLS_PY="probe.py figures.py report_pack.py report_build.py"
# 报告脚本（render / validate_paragraphs / compose_paragraphs）**只允许存在一处**：
# `报告任务_脚手架/`（盒子上的 ~/batterylab/报告任务_脚手架）。仓库根与 ~/batterylab 顶层
# 都不许有副本 —— `selftest_report.test_scaffold_is_single_home()` 强制这条。
# 用户 2026-10-04 定的模型：电脑上开发 → 推给盒子 → 盒子上验证；**盒子上每个文件只有一份**。
# 注：任务包里的副本是**构建产物**、共享目录里那些是**自动派生**，都不算"第二份"。
SINGLE_HOME_PY="compose_paragraphs.py render.py validate_paragraphs.py"
ENTRY="换通道出报告.sh"
# 盒子 ~/batterylab 根目录还要用的 .sh。
# ★ 2026-10-04 踩到：清单里**漏了 service.sh**，于是我改了通道数、盒子上却没变
#   （日志仍说"盯 6 个通道"）—— 正是这个脚本要防的那类错。教训：**凡是盒子要用的
#   文件都必须进清单**；加文件后请 grep 一遍确认它在清单里。
ROOT_SH="service.sh"

die() { echo "❌ $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }

# ── 0. 本地自检（可选） ─────────────────────────────────────────────────────
if [ "$RUN_TESTS" = "1" ]; then
  step "0. 本地跑全套 selftest"
  bad=0
  for f in $TESTS; do
    [ -f "$f" ] || continue
    if python "$f" >/dev/null 2>&1; then printf "  ✅ %s\n" "$f"; else printf "  ❌ %s\n" "$f"; bad=1; fi
  done
  [ "$bad" = "0" ] || die "本地自检有红的，先修再部署（--check 也救不了这个）"
fi

# ── 1. 收集清单 ─────────────────────────────────────────────────────────────
step "1. 清点要部署的文件"
missing=""
for f in $RUNTIME_PY $TESTS; do [ -f "$f" ] || missing="$missing $f"; done
[ -d "$SCAFFOLD_DIR" ] || die "缺少脚手架目录 $SCAFFOLD_DIR"
[ -z "$missing" ] || echo "  ⚠️ 本地缺少（跳过）：$missing"

ssh -o ConnectTimeout=12 "$BOX" true 2>/dev/null || die "连不上 $BOX（ssh 配好了吗）"

# ── 1b. 本地 .sh 先归一为 LF ────────────────────────────────────────────────
# 为什么在这一步做：在 Windows 上编辑出来的 .sh 默认是 CRLF，而 CRLF 的 .sh 到盒子上
# 会报 `$'\r': 未找到命令` 且**在没人执行它时完全无声**（2026-10-04 就是这么栽的）。
# 在本地就归一，两边才可能真正一致 —— 否则 --check 会一直报"不一致"，把人练到不看它。
step "1b. 本地 .sh 行尾符归一（LF）"
fixed=""
for f in *.sh "$SCAFFOLD_DIR"/*.sh; do
  [ -f "$f" ] || continue
  if file "$f" | grep -q CRLF; then sed -i 's/\r$//' "$f"; fixed="$fixed $f"; fi
done
[ -n "$fixed" ] && echo "  🔧 已转 LF：$fixed" || echo "  ✅ 本地 .sh 都已是 LF"

# 内容指纹：忽略行尾符。不然"只有 CRLF 不同"会被误报成"内容被人改过"，
# 2026-10-04 我就被这个假象带偏过一次（以为盒子上的版本领先）。
lhash() { tr -d '\r' < "$1" | md5sum | cut -d' ' -f1; }
rhash() { ssh "$BOX" "tr -d '\r' < '$1' 2>/dev/null | md5sum | cut -d' ' -f1"; }
rexists() { ssh "$BOX" "test -f '$1' && echo yes || echo no"; }

# ── 2. --check：只核对，不写 ────────────────────────────────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
  step "2. 只核对（--check）：本地 vs 盒子（忽略行尾符）"
  fail=0
  check_one() {   # $1=本地文件  $2=盒子文件  $3=显示名
    local lmd rmd
    lmd=$(lhash "$1")
    rmd=$(rhash "$2")
    if [ -z "$rmd" ]; then printf "  ❓ %-34s 盒子上没有\n" "${3:-$1}"; fail=1
    elif [ "$lmd" = "$rmd" ]; then printf "  ✅ %-34s %s\n" "${3:-$1}" "${lmd:0:8}"
    else printf "  ❌ %-34s 内容不同 %s vs %s\n" "${3:-$1}" "${lmd:0:8}" "${rmd:0:8}"; fail=1
    fi
  }
  for f in $RUNTIME_PY $TESTS; do [ -f "$f" ] && check_one "$f" "$LAB/$f" "$f"; done
  for f in $ROOT_SH; do [ -f "$f" ] && check_one "$f" "$LAB/$f" "$f"; done
  for f in $SHARE_TOOLS_PY; do [ -f "$f" ] && check_one "$f" "$TOOLS/$f" "报告工具/$f"; done
  # 报告脚本：只该在脚手架一处；~/batterylab 顶层出现副本就是"第二份"，报错
  for f in $SINGLE_HOME_PY; do
    [ -f "$SCAFFOLD_DIR/$f" ] || continue
    check_one "$SCAFFOLD_DIR/$f" "$LAB/$SCAFFOLD_DIR/$f" "脚手架/$f"
    [ "$(rexists "$LAB/$f")" = "yes" ] && { echo "  ❌ ~/batterylab/$f 不该存在（唯一出处=脚手架）"; fail=1; }
  done
  for f in "$SCAFFOLD_DIR"/*; do
    [ -f "$f" ] || continue
    check_one "$f" "$LAB/$SCAFFOLD_DIR/$(basename "$f")" "脚手架/$(basename "$f")"
  done
  [ -f "$SCAFFOLD_DIR/$ENTRY" ] && check_one "$SCAFFOLD_DIR/$ENTRY" "$SHARE/$ENTRY" "共享根/$ENTRY"
  echo
  [ "$fail" = "0" ] && echo "✅ 两边一致" || echo "⚠️ 有不一致 —— 跑 bash 部署.sh 同步"
  exit "$fail"
fi

# ── 2b.（已取消）原来这里做「仓库根 → 脚手架」的镜像同步 ─────────────────────
# 2026-10-04 用户定的模型：**报告脚本只存在一处**（`报告任务_脚手架/`），不再有"主代码副本"
# → 没有镜像要同步了。`selftest_report.test_scaffold_is_single_home()` 保证这一点。

# ── 3. 备份盒子上将被覆盖的文件（AGENTS.md：覆盖前先备份） ───────────────────
step "3. 备份盒子上将被覆盖的文件 → $BK"
{
  echo "mkdir -p '$BK'"
  for f in $RUNTIME_PY $TESTS; do echo "[ -f '$LAB/$f' ] && cp -a '$LAB/$f' '$BK/'"; done
  echo "mkdir -p '$BK/报告工具'"
  for f in $SHARE_TOOLS_PY; do echo "[ -f '$TOOLS/$f' ] && cp -a '$TOOLS/$f' '$BK/报告工具/'"; done
  echo "mkdir -p '$BK/脚手架'"
  echo "[ -d '$LAB/$SCAFFOLD_DIR' ] && cp -a '$LAB/$SCAFFOLD_DIR/.' '$BK/脚手架/'"
  echo "[ -f '$SHARE/$ENTRY' ] && cp -a '$SHARE/$ENTRY' '$BK/入口脚本.sh'"
} | ssh "$BOX" bash
ssh "$BOX" "ls '$BK' | head -40" | sed 's/^/  /'

# ---- 3b. 备份轮转：只留最近 3 个，免得每次部署都堆一份（2026-10-06 实测堆了 29 个/20M）----
ssh "$BOX" "n=\$(ls -d '$LAB'/_backup_* 2>/dev/null | wc -l)
if [ \"\$n\" -gt 3 ]; then
  ls -d '$LAB'/_backup_* | sort | head -n \$((n-3)) | while read d; do rm -rf \"\$d\"; done
  echo \"  🧹 备份轮转：\$n → \$(ls -d '$LAB'/_backup_* 2>/dev/null | wc -l) 个\"
fi"

# ── 4. 传文件 ───────────────────────────────────────────────────────────────
step "4. 传文件到两个位置"
scp -q $RUNTIME_PY $TESTS "$BOX:$LAB/" || die "传到 ~/batterylab 失败"
echo "  → ~/batterylab/  （$(( $(echo $RUNTIME_PY | wc -w) + $(echo $TESTS | wc -w) )) 个文件）"
scp -q $ROOT_SH "$BOX:$LAB/" || die "传根目录脚本到 ~/batterylab 失败"
echo "  → ~/batterylab/  （根目录脚本：$ROOT_SH）"
scp -q $SHARE_TOOLS_PY "$BOX:$TOOLS/" || die "传到共享目录 报告工具/ 失败"
echo "  → 共享目录/报告工具/"
scp -q -r "$SCAFFOLD_DIR/." "$BOX:$LAB/$SCAFFOLD_DIR/" || die "传脚手架失败"
echo "  → ~/batterylab/$SCAFFOLD_DIR/"
# ★ 2026-10-09：脚手架要**同步**，不是"只拷" —— 本地删掉的文件，盒子上也得消失。
#   否则会出现"本地删了、盒子上还在"，而且报告打包时还会把旧文件一起拷进任务包
#   （实测：任务说明.md 本地已删、盒子上仍在，还进了任务包）。
scaffold_keep=$(ls -1 "$SCAFFOLD_DIR" | tr '
' ' ')
# ★ 2026-10-09：还要同步到**共享目录**那份脚手架 —— report_build 从共享目录跑时
#   用的就是那一份（换通道脚本会先同步，但直接调 report_build 会绕过它）。
#   实测：共享目录那份 render.py 是旧的（还留着第 6 个图位）→ 只渲染出 3 张图。
ssh "$BOX" "mkdir -p '$SHARE/$SCAFFOLD_DIR' && cp -a '$LAB/$SCAFFOLD_DIR/.' '$SHARE/$SCAFFOLD_DIR/' && echo '  -> 共享目录/报告任务_脚手架/ 已同步'"

ssh "$BOX" "cd '$LAB/$SCAFFOLD_DIR' || exit 0
for f in *; do
  case ' $scaffold_keep ' in
    *\" \$f \"*) ;;
    *) rm -rf \"\$f\"; echo \"  🧹 脚手架上删掉（本地已无）：\$f\";;
  esac
done"
scp -q "$SCAFFOLD_DIR/$ENTRY" "$BOX:$SHARE/$ENTRY" || die "传入口脚本失败"
echo "  → 共享目录/$ENTRY"

# ── 5. 行尾符归一：.sh 必须 LF（CRLF 的 .sh 会安静地崩） ────────────────────
step "5. 行尾符归一（.sh 必须 LF；.py 无所谓）"
ssh "$BOX" "bad=0
for f in \$HOME/batterylab/*.sh '$LAB/$SCAFFOLD_DIR/'*.sh '$SHARE/'*.sh; do
  [ -f \"\$f\" ] || continue
  if file \"\$f\" | grep -q CRLF; then sed -i 's/\r\$//' \"\$f\"; echo \"  🔧 已转 LF: \$f\"; bad=1; fi
done
[ \"\$bad\" = 0 ] && echo '  ✅ 所有 .sh 都是 LF'"

# ── 6. 逐项核对（这一步是核心：不接受"我觉得传成功了"） ─────────────────────
step "6. 内容核对（忽略行尾符）"
fail=0
for f in $RUNTIME_PY $TESTS; do
  [ -f "$f" ] || continue
  [ "$(lhash "$f")" = "$(rhash "$LAB/$f")" ] || { printf "  ❌ %s\n" "$f"; fail=1; }
done
for f in $ROOT_SH; do
  [ "$(lhash "$f")" = "$(rhash "$LAB/$f")" ] || { printf "  ❌ ~/batterylab/%s\n" "$f"; fail=1; }
done
for f in $SHARE_TOOLS_PY; do
  [ "$(lhash "$f")" = "$(rhash "$TOOLS/$f")" ] || { printf "  ❌ 报告工具/%s\n" "$f"; fail=1; }
done
# 报告脚本只能待在脚手架一处：~/batterylab 顶层不该有它们（有就是"第二份"）
for f in $SINGLE_HOME_PY; do
  [ "$(lhash "$SCAFFOLD_DIR/$f")" = "$(rhash "$LAB/$SCAFFOLD_DIR/$f")" ] \
    || { printf "  ❌ 脚手架/%s\n" "$f"; fail=1; }
done
for f in "$SCAFFOLD_DIR"/*; do
  [ -f "$f" ] || continue
  [ "$(lhash "$f")" = "$(rhash "$LAB/$SCAFFOLD_DIR/$(basename "$f")")" ] \
    || { printf "  ❌ 脚手架/%s\n" "$(basename "$f")"; fail=1; }
done
[ "$(lhash "$SCAFFOLD_DIR/$ENTRY")" = "$(rhash "$SHARE/$ENTRY")" ] \
  || { echo "  ❌ 共享根/入口脚本"; fail=1; }
[ "$fail" = "0" ] && echo "  ✅ 全部一致" || die "核对不过 —— 上面的文件没传对，别继续"

# ── 7. 清理"多份"孤儿副本（只挪进备份，不删） ───────────────────────────────
step "7. 清理真正多余的副本（只挪进备份，不删）"
# ★ 这里**故意不含 compose/render/validate** —— 它们不是"多余副本"，而是 test_scaffold_in_sync
#   强制的「主代码 ↔ 脚手架」镜像。2026-10-04 我把它们当多余副本挪走过，盒子上的
#   selftest_report 立刻变红。教训：清理前先 grep 有没有测试认它。
ssh "$BOX" "mkdir -p '$BK/多余副本'; moved=0
for f in $SINGLE_HOME_PY; do
  if [ -f '$LAB/'\$f ]; then mv '$LAB/'\$f '$BK/多余副本/'\$f && echo \"  📦 ~/batterylab/\$f → 备份（报告脚本只在脚手架一处）\"; moved=1; fi
done
if [ -f '$LAB/$SCAFFOLD_DIR/make_share.py' ]; then mv '$LAB/$SCAFFOLD_DIR/make_share.py' '$BK/多余副本/脚手架_make_share.py' && echo '  📦 脚手架里的 make_share.py → 备份（它不属于报告脚手架，仓库脚手架里也没有它）'; moved=1; fi
if [ -f '$SHARE/出报告.sh' ]; then mv '$SHARE/出报告.sh' '$BK/多余副本/共享根_出报告.sh' && echo '  📦 共享目录根的 出报告.sh → 备份（唯一入口是 换通道出报告.sh）'; moved=1; fi
if [ -f '$LAB/split_batch.py' ]; then mv '$LAB/split_batch.py' '$BK/多余副本/~/batterylab_split_batch.py' && echo '  📦 ~/batterylab/split_batch.py → 备份（它只属于脚手架）'; moved=1; fi
[ \"\$moved\" = 0 ] && echo '  ✅ 没有多余副本'"

# ── 8. 刷新共享目录（否则报告读的是旧数据） ─────────────────────────────────
step "8. 刷新共享目录（make_share）"
ssh "$BOX" "cd '$LAB' && python3 make_share.py --data-dir ./data --share-dir '$SHARE' --force" \
  | tail -4 | sed 's/^/  /'

echo
echo "✅ 部署完成"
echo "   备份：$BK"
echo "   下一步出报告： ssh $BOX 'cd $SHARE && bash $ENTRY 10-1 10-2'"
echo "   （体系/变量要填对时，先看 LABEL_A/LABEL_B 环境变量，见 AGENTS.md 七之三）"
