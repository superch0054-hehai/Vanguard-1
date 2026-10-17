#!/usr/bin/env bash
# 东华数据"收件员" 一键部署（在【盒子】上运行）
#
#   bash ~/dh-lab/tools/install_on_box.sh
#
# 纪律：只创建/使用 ~/dh-lab 一个目录；容器名 dh-collect。
# 出问题一键还原：docker rm -f dh-collect && rm -rf ~/dh-lab
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
LAB="$HOME/dh-lab"
IN="$LAB/inbox"
OUT="$LAB/outbox"

echo "════════════════════════════════════════"
echo " 东华收件员 一键部署   $(date '+%F %T')"
echo "   工具目录：$DIR"
echo "   数据目录：$LAB"
echo "════════════════════════════════════════"

mkdir -p "$IN" "$OUT"

echo
echo "① 自检（期望：通过 25，失败 0）"
python3 "$DIR/selftest_dh.py" | tail -2

echo
echo "② 试跑收件员（单趟，读一遍空目录=应当无事发生）"
python3 "$DIR/dh_collect.py" --inbox "$IN" --outbox "$OUT" --once

echo
echo "③ 收件员上岗（容器优先，直跑兜底）"
PIDFILE="$LAB/collect.pid"

# 先停掉上一份"直跑"进程（避免和容器重复运行）
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "   停掉旧的直跑进程 PID $(cat "$PIDFILE")（避免重复运行）"
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  sleep 1
fi
rm -f "$PIDFILE"

STARTED=0
if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  if ! docker images --format '{{.Repository}}' | grep -qx 'python'; then
    echo "   本地没有 python 基础镜像，尝试拉取（最多等 60 秒，失败也无妨）…"
    timeout 60 docker pull python:3.11-slim || true
  fi
  echo "   构建镜像 dh-collect:0.1 …（若用 Dockerfile.localbase，则完全离线）"
  if docker build -t dh-collect:0.1 "$DIR" >/tmp/dh_build.log 2>&1; then
    docker rm -f dh-collect >/dev/null 2>&1 || true
    if docker run -d --name dh-collect --restart unless-stopped \
         -v "$IN:/inbox:ro" -v "$OUT:/outbox" dh-collect:0.1 >/dev/null 2>&1; then
      echo "   ✅ 容器已启动（看日志：docker logs dh-collect）"
      STARTED=1
    else
      echo "   ⚠️ 容器启动失败"
    fi
  else
    echo "   ⚠️ 镜像构建失败，错误摘要："
    tail -6 /tmp/dh_build.log 2>/dev/null | sed 's/^/      /'
    echo "      （完整日志：/tmp/dh_build.log；可改用 Dockerfile.localbase 走离线构建）"
  fi
else
  echo "   docker 不可用（未安装，或当前用户没有权限）"
fi

if [ "$STARTED" = "0" ]; then
  echo "   ⚠️ 未启用容器 → 改为直跑（临时方案；盒子重启后重跑本脚本即可）"
  docker rm -f dh-collect >/dev/null 2>&1 || true   # 清掉可能的同名旧容器，避免重复
  nohup python3 "$DIR/dh_collect.py" --inbox "$IN" --outbox "$OUT" --interval 30 \
        > "$LAB/collect.log" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 1
  echo "   ✅ 已在后台直跑（PID $(cat "$PIDFILE")；看日志：tail -f $LAB/collect.log）"
fi

echo
echo "④ 盒子 IP（抄下来，写进采集 PC 的推送器）："
hostname -I 2>/dev/null | awk '{print "   " $1}'

echo
echo "完成 ✅  接下来："
echo "   · 采集 PC：配置 push_to_box.bat + 任务计划（见 现场操作单.md）"
echo "   · 验收：往收件箱放一份 txt → 等 ≤10 分钟 → tail $OUT/摘要.md"
