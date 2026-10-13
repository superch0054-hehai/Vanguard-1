#!/usr/bin/env bash
# ============================================================
# 盒子端"一键拉取"（实时档 ②："按钮"方案 · 备选，默认不启用）
#
# 用途：在盒子上（或笔记本上）敲一条命令，立刻把采集 PC 导出目录里的
#       文件复制过来——不用等下一个 1 分钟周期。
#
# 前提：采集 PC 已开启 OpenSSH 服务（Windows 可选功能），且盒子/笔记本
#       能从校园网访问到它（22 端口放行）。
#
# 用法：PC_IP=10.201.x.x PC_USER=你的PC用户名 bash sync_now.sh
#
# 性质：仍然只读——只从 PC 复制文件到盒子，不写、不动 PC 上任何文件。
# ============================================================
set -u

PC_IP="${PC_IP:-}"
PC_USER="${PC_USER:-}"
INBOX="$HOME/dh-lab/inbox"

if [ -z "$PC_IP" ] || [ -z "$PC_USER" ]; then
  echo "用法: PC_IP=10.201.x.x PC_USER=你的PC用户名 bash sync_now.sh"
  exit 2
fi

mkdir -p "$INBOX"

# Windows OpenSSH 的盘符路径写作 /D:/DH_export（D 盘根目录）
if scp -p -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
       "$PC_USER@$PC_IP:/D:/DH_export/*" "$INBOX/"; then
  echo "已拉取。inbox 最新 5 个文件："
  ls -lt "$INBOX" | head -6
  echo "（收件员每 30 秒扫一次，稍等即可在 outbox/摘要.md 看到新条目）"
else
  echo "拉取失败，检查：① 采集 PC 的 OpenSSH 服务是否已开启；② PC_IP 是否正确；③ 防火墙 22 端口是否放行。"
  exit 1
fi
