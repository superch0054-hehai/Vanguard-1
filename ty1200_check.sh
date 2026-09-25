#!/usr/bin/env bash
# TY1200 首次登机自检 —— 只读，不修改任何配置，不安装任何东西
# 用法：把整个文件粘贴到盒子的终端里执行，然后把输出发回来

TY_HOST="10.201.47.169"      # 电池测试机所在的 Windows 主机（客户端）
TY_PORT="502"

hr() { printf '%s\n' "------------------------------------------------------------"; }
sec() { echo; hr; echo "  $1"; hr; }
has() { command -v "$1" >/dev/null 2>&1; }

echo "============================================================"
echo "  TY1200 首次登机自检    $(date '+%Y-%m-%d %H:%M:%S')"
echo "  身份：$(whoami)@$(hostname)"
echo "============================================================"

# ---------------------------------------------------------------- 系统
sec "1. 系统"
echo "架构      : $(uname -m)"
echo "内核      : $(uname -r)"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "发行版    : ${PRETTY_NAME:-未知}"
  echo "版本代号  : ${VERSION_CODENAME:-未知}"
fi
echo "运行时间  : $(uptime -p 2>/dev/null || uptime)"
echo "图形界面  : ${XDG_SESSION_TYPE:-（无此变量，可能是命令行会话）}"
[ -n "$DISPLAY" ] && echo "DISPLAY   : $DISPLAY"

# ---------------------------------------------------------------- 账号
sec "2. 账号与权限"
echo "当前用户  : $(id)"
echo "用户列表  : $(awk -F: '$3>=1000 && $3<65534 {printf "%s ", $1}' /etc/passwd)"
echo "当前登录  : $(who | tr '\n' ' ')"
echo "最近登录  :"
last -n 5 2>/dev/null | sed 's/^/            /'
if sudo -n true 2>/dev/null; then
  echo "sudo      : 免密可用"
else
  echo "sudo      : 需要密码（或不可用）"
fi

# ---------------------------------------------------------------- 网络
sec "3. 网络  ★ 最关键"
echo "--- 网卡与地址 ---"
ip -4 addr show 2>/dev/null | awk '/^[0-9]+:/{ifc=$2} /inet /{print "  " ifc "  " $2}'
echo "--- 默认路由 ---"
ip route 2>/dev/null | awk '/default/{print "  " $0}'
echo "--- DNS 配置 ---"
[ -f /etc/resolv.conf ] && grep -E '^nameserver' /etc/resolv.conf | sed 's/^/  /'

echo
echo "--- 能否访问测试机 $TY_HOST ---"
if has ping; then
  if timeout 5 ping -c 2 -W 2 "$TY_HOST" >/dev/null 2>&1; then
    echo "  ping      : 通 ✅"
  else
    echo "  ping      : 不通 ❌"
  fi
else
  echo "  ping      : 系统没有 ping 命令"
fi
echo -n "  502 端口  : "
if timeout 5 bash -c "echo > /dev/tcp/$TY_HOST/$TY_PORT" 2>/dev/null; then
  echo "可达 ✅ ← ★这是采集链路的命门"
else
  echo "不可达 ❌"
fi

echo
echo "--- 能否上外网 ---"
echo -n "  pypi.org  : "
if timeout 8 bash -c "echo > /dev/tcp/pypi.org/443" 2>/dev/null; then
  echo "可达 ✅（能 pip 装包）"
else
  echo "不可达 ❌（需要离线准备 wheel）"
fi
echo -n "  天数镜像库: "
if timeout 8 bash -c "echo > /dev/tcp/harbor.iluvatar.com.cn/10443" 2>/dev/null; then
  echo "可达 ✅"
else
  echo "不可达（可能要走内网，或需要先配 /etc/hosts）"
fi

# ---------------------------------------------------------------- 资源
sec "4. 计算与存储资源"
echo "CPU 核数  : $(nproc)   ($(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | sed 's/^ *//'))"
echo "内存      :"
free -h 2>/dev/null | sed 's/^/            /'
echo "磁盘      :"
df -h 2>/dev/null | grep -vE 'tmpfs|udev|loop' | sed 's/^/            /'
echo "大目录    :"
du -sh /home/* /data /opt 2>/dev/null | sort -h | tail -6 | sed 's/^/            /'
if has ixsmi; then
  echo "GPGPU     : ixsmi 可用"
  ixsmi 2>/dev/null | head -12 | sed 's/^/            /'
else
  echo "GPGPU     : ixsmi 不可用（不影响我们，我们不用 GPU）"
fi

# ---------------------------------------------------------------- 软件
sec "5. 软件环境  ★ 决定怎么装包"
echo "python3   : $(python3 --version 2>&1)"
for p in python3.11 python3.12 python3.13 python3.14; do
  has $p && echo "  另有 $p : $($p --version 2>&1)"
done
echo -n "pip3      : "
if has pip3; then pip3 --version 2>&1 | head -1; else echo "未安装"; fi
echo -n "venv 模块 : "
python3 -c "import venv; print('可用')" 2>/dev/null || echo "不可用（需要 apt install python3-venv）"
echo -n "pip 能否解析包 : "
timeout 25 python3 -m pip install --dry-run --quiet pip 2>&1 | tail -1 || echo "（pip 不可用时无法判断）"

echo
echo "--- Docker ---"
if has docker; then
  echo "  Docker 版本 : $(docker --version 2>&1)"
  echo "  服务状态    : $(systemctl is-active docker 2>/dev/null || echo 未知)"
  echo "  当前用户能否直接调 docker : $(docker ps >/dev/null 2>&1 && echo '能' || echo '不能（需要 sudo 或加 docker 组）')"
  echo "  已有镜像 :"
  docker images 2>/dev/null | sed 's/^/            /' || echo "            （看不到，可能需要 sudo）"
  echo "  正在运行的容器 :"
  docker ps 2>/dev/null | sed 's/^/            /' || echo "            （看不到）"
else
  echo "  Docker : 未安装"
fi

# ---------------------------------------------------------------- 远程
sec "6. 远程访问能力  ★ 决定能不能用 VS Code 远程开发"
echo -n "SSH 服务  : "
if systemctl is-active ssh >/dev/null 2>&1 || systemctl is-active sshd >/dev/null 2>&1; then
  echo "已启用 ✅"
else
  echo "未启用 ❌（需要 apt install openssh-server 并 enable）"
fi
echo -n "22 端口监听: "
if ss -ltn 2>/dev/null | grep -q ':22 '; then
  echo "在监听 ✅"
else
  echo "没监听 ❌"
fi
echo -n "防火墙     : "
if has ufw && ufw status 2>/dev/null | grep -q 'Status: active'; then
  echo "ufw 已启用"; ufw status 2>/dev/null | head -8 | sed 's/^/            /'
elif has firewall-cmd; then
  echo "firewalld: $(firewall-cmd --state 2>/dev/null)"
else
  echo "未启用（或没装防火墙工具）"
fi
echo "本机 IP（供笔记本连）: $(hostname -I 2>/dev/null)"

# ---------------------------------------------------------------- 汇总
sec "7. 结论摘要"
echo "  1) 测试机 502         : $(timeout 5 bash -c "echo > /dev/tcp/$TY_HOST/$TY_PORT" 2>/dev/null && echo '可达 —— 采集链路通' || echo '不可达 —— 先解决网络')"
echo "  2) 外网               : $(timeout 8 bash -c "echo > /dev/tcp/pypi.org/443" 2>/dev/null && echo '通 —— 可直接 pip 装包' || echo '不通 —— 需离线 wheel')"
echo "  3) Python             : $(python3 --version 2>&1 | awk '{print $2}')"
echo "  4) Docker             : $(has docker && echo 已装 || echo 未装)"
echo "  5) SSH                : $(systemctl is-active ssh >/dev/null 2>&1 || systemctl is-active sshd >/dev/null 2>&1 && echo 已启用 || echo 未启用)"
echo
echo "============================================================"
echo "  自检结束。以上全部为只读检查，未做任何修改。"
echo "============================================================"
