# Windows 侧远程开发：已完成的准备

针对"从笔记本的 Windows 连天数 TY1200"，以下是**已经替你配好的东西**，
以及**还差一步**。

---

## 一、已完成（2026-09-24）

| # | 项 | 状态 |
|---|---|---|
| 1 | **VS Code 扩展 Remote - SSH** | ✅ 已装 `ms-vscode-remote.remote-ssh` v0.128.0（附带 `remote-ssh-edit`、`remote-explorer`） |
| 2 | **SSH 密钥** | ✅ 已生成 `C:\Users\gzz25\.ssh\id_ed25519`（ed25519，无密码短语） |
| 3 | **SSH 配置** | ✅ 已建 `C:\Users\gzz25\.ssh\config`，含 `Host ty1200` |
| 4 | **VS Code 远程设置** | ✅ 已写入 `settings.json`（见下） |

### 写进 VS Code 的设置

```json
"remote.SSH.localServerDownload": "always",   // 盒子不能上外网时，让笔记本下载 vscode-server 再传过去
"remote.SSH.showLoginTerminal": true,         // 登录过程显示在终端，方便第一次输密码
"remote.SSH.remotePlatform": { "ty1200": "linux" },  // 跳过平台探测
"remote.SSH.connectTimeout": 30
```

**第 1 条是防一个真实的坑**：VS Code 首次连接会在远程机器下载约 100MB 的
`vscode-server`，**如果盒子不能上外网就会卡在 "Setting up SSH Host"**。
设成 `always` 后改由笔记本下载再传过去。

### 你的公钥（等下要贴到盒子上）

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE+K8idVKJr2F88xmeZXgobrWx/4XpLw3lqTEC3KjMBS gzz25-laptop
```

---

## 二、还差一步：填盒子的 IP

`C:\Users\gzz25\.ssh\config` 里这一行现在是占位符：

```
Host ty1200
    HostName 192.168.1.100      ← ★ 改成盒子的真实 IP
    User admin
```

**IP 从哪来**：到盒子上跑自检脚本，或直接 `hostname -I` / `ip -4 addr`。

---

## 三、到机器前的完整顺序

**第 1 步 · 接线开机登录**

电源适配器 + HDMI 显示器 + USB 键鼠 + 网线 → 按电源键 →
用 `admin` / `admin` 登录。

**第 2 步 · 跑自检，拿到 IP**

```bash
hostname -I
```
（或跑完整自检脚本 `ty1200_check.sh`，顺便把其他问题一次问完）

**第 3 步 · 开启 SSH**

```bash
# 先试直接启动（可能包装了但没启用）
sudo systemctl enable --now ssh

# 如果报 "Unit ssh.service not found"，说明没装，再装
sudo apt install -y openssh-server
sudo systemctl enable --now ssh

# 确认在监听
ss -ltn | grep :22
```

> ⚠️ `apt install` 需要盒子能上外网。**如果上不了外网**，这一步会失败 ——
> 那时的退路是用 U 盘拷 `openssh-server` 的 .deb 离线安装，
> 或者干脆**直接在盒子上开发**（用 `nano`，难受但能干活）。

**第 4 步 · 把公钥贴上去（免密登录）**

在盒子上执行：

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE+K8idVKJr2F88xmeZXgobrWx/4XpLw3lqTEC3KjMBS gzz25-laptop' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**这步可以跳过** —— 不贴公钥就用密码登录（`admin`），VS Code 会弹出来让你输。
但配上免密以后每次连接都省事。

**第 5 步 · 回笔记本，填 IP 并连接**

改 `C:\Users\gzz25\.ssh\config` 里的 `HostName`，然后在 PowerShell 里先验一下：

```powershell
ssh ty1200
```

能进就说明通了。然后 VS Code：`Ctrl+Shift+P` → `Remote-SSH: Connect to Host` → 选 `ty1200`。

---

## 四、连上之后要做的事

```bash
# 确认环境
python3 --version          # 预期 3.11（Debian 12）
nproc; free -h; df -h /

# 建隔离环境（★ 别用系统 Python，这台机器跑着别人的大模型）
mkdir -p ~/batterylab && cd ~/batterylab
sudo apt install -y python3-venv python3-pip     # 如果 venv/pip 不可用
python3 -m venv .venv
source .venv/bin/activate
pip install pandas pyarrow duckdb matplotlib

# 确认能连测试机
timeout 5 bash -c "echo > /dev/tcp/10.201.47.169/502" && echo "502 可达" || echo "502 不可达"

# 把代码传过去（在笔记本的 Git Bash 里执行）
# scp -r "/c/Users/gzz25/Desktop/电池/neware_comms/." ty1200:~/batterylab/

# 跑自测（不需要设备）
./selftest.py
```

---

## 五、常见卡点

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `Connection refused` | 盒子 SSH 没启动 | 回第 3 步 |
| `Connection timed out` | 不在同一网络 | 确认两台设备连的是同一个网络 |
| 反复要密码 | 没配公钥，或公钥贴错了 | 回第 4 步 |
| **卡在 "Setting up SSH Host"** | 盒子下不了 `vscode-server` | 已设 `localServerDownload: always`；若仍卡，检查笔记本能否上网 |
| 连上后 Python 找不到 | 解释器没选 | `Ctrl+Shift+P` → `Python: Select Interpreter` → 选 `~/batterylab/.venv/bin/python` |
