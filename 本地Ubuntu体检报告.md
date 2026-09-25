# 本地 Ubuntu 体检报告

检查时间：2026-09-23
检查对象：本机 WSL2 上的 Ubuntu

---

## 一、结论

**环境可用，可以直接开始开发。** 有两处需要你自己处理（都需要 sudo 密码），
另有一处我顺手修掉了（CRLF 行尾），还有一个**关于离线装包的重要更正**（见第五节）。

---

## 二、系统概况

| 项 | 实测值 | 评价 |
|---|---|---|
| 形态 | **WSL2**（发行版名 `Ubuntu`，默认发行版） | ✅ 可用，但注意第 5.3 节的坑 |
| 系统版本 | **Ubuntu 26.04 LTS** | ✅ 很新 |
| 内核 | 6.6.114.1-microsoft-standard-WSL2 | — |
| 架构 | **x86_64** | ✅ 和盒子一致，这点很好 |
| CPU | 32 核 | ✅ 远超需要 |
| 内存 | **7.6 GiB**（WSL 默认取宿主机一半） | ✅ 本地开发够用（盒子是 16GB） |
| 根分区 | 1007 GB 逻辑，已用 1.7 GB | ⚠️ **见 5.4 节的真实上限** |
| Python | **3.14.4** | ⚠️ 见 5.2 节 |
| **systemd** | **已启用**（`/etc/wsl.conf` 里 `systemd=true`，PID 1 就是 systemd） | ✅ **能本地验证 systemd 服务文件** |
| 默认用户 | `poponinja`（在 sudo 组） | ✅ |
| sudo | 需要密码 | ⚠️ 见第三节 |

---

## 三、发现的问题

### 3.1 ⚠️ `python3 -m venv` 用不了（ensurepip 缺失）

**实测**：

```
$ python3 -m venv /tmp/vt
The virtual environment was not created successfully because ensurepip is not
available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.

    apt install python3.14-venv
```

原因：`ensurepip` 模块不存在（`ModuleNotFoundError: No module named 'ensurepip'`），
所以标准方式建不出带 pip 的虚拟环境。

### 3.2 ⚠️ apt 包索引是空的

`apt-cache policy python3-venv python3-pip` 的"已装"和"可装版本"都是空的，
说明**没跑过 `apt update`**。所以想装包得先更新索引。

**这两条一起的修法**（需要你自己执行，因为要输密码）：

```bash
sudo apt update
sudo apt install -y python3.14-venv python3-pip
```

装完之后 `python3 -m venv` 就正常了。不过我已经用另一条不需要密码的路绕过了，
见第四节 —— **你可以不装，也能干活**。

### 3.3 ✅ CRLF 行尾（已修）

从 Windows 拷过去的文件是 CRLF 行尾。**这个坑真实存在，我实测复现了**：

```
$ ./selftest.py
env: $'python3\r': No such file or directory
env: use -[v]S to pass options in shebang lines
```

shebang 里的 `python3\r` 被当成解释器名，所以**直接执行 `.py` 文件会失败**。
用 `python3 selftest.py` 则正常（Python 本身容忍 CRLF）。

这就是我在开发工作流文档里警告过的情况 —— 现在你知道它长什么样了。

**已修复**：把 `neware_comms` 下 19 个文本文件全部转为 LF，
并加了 `.gitattributes`（`* text=auto eol=lf`）防止以后再回归。

修复后：

```
$ ./selftest.py
通过 81 项，失败 0 项
```

---

## 四、我已经做的改动（请知悉）

为了把环境调到能用，我做了三件事。**都是可逆的。**

| # | 改动 | 位置 | 怎么撤销 |
|---|---|---|---|
| 1 | 装了 **uv**（Python 包管理器） | `~/.local/bin/uv`，PATH 写进了 `~/.bashrc` 和 `~/.profile` | `rm ~/.local/bin/uv*`，并删掉 .bashrc/.profile 里那一行 |
| 2 | 建了 `~/batterylab`，把代码拷进去，建了 `.venv`，装了 pandas/pyarrow/duckdb | `~/batterylab/` | `rm -rf ~/batterylab` |
| 3 | 把源码 19 个文件的行尾改成 LF，加了 `.gitattributes` | Windows 上的 `neware_comms/` | 影响很小，不建议撤 |

### 为什么用 uv 而不是标准 venv

因为 `python3 -m venv` 坏了（3.1），而 uv：

- **不需要 sudo**，装到用户目录
- 自带虚拟环境能力，**绕开 ensurepip 问题**
- 是一个静态二进制，不碰系统 Python
- **还能装指定版本的 Python**（这对第 5.2 节那个问题很关键）
- 现在几乎成了 Python 生态的事实标准

**它和标准 venv 不冲突** —— 你可以两个都用。如果以后装了 `python3.14-venv`，
`python3 -m venv` 会恢复正常，uv 也照样能用。

### 实测结果：装包和运行都正常

```
$ uv venv .venv --python 3.14
$ uv pip install pandas pyarrow duckdb
   → pandas 3.0.6 / pyarrow 25.0.1 / duckdb 1.5.5，耗时 37 秒

$ ./selftest.py
   通过 81 项，失败 0 项
```

还实测跑了一次真实链路（写 Parquet → DuckDB 算容量保持率）：

```
 cycleid   cap  retention
       1 0.190 100.000000
       2 0.185  97.368421
       3 0.180  94.736842
```

结果和自测里的算法口径一致。

---

## 五、对项目的适配性评估

### 5.1 ✅ 架构一致，这是好事

本地是 **x86_64 Linux**，盒子也是 **x86_64 Linux**（TY1200 是 16 核 x86）。
同构能消掉一整类问题，这正是我推荐用 Ubuntu 而不是 Windows 的首要理由。

### 5.2 ⚠️ 重要更正：Python 版本不匹配，wheel 不能直接复用

我之前说"在 Ubuntu 上下载的 wheel 可以直接拷到盒子上用" —— **这个说法过于乐观，要更正。**

**wheel 是跟 Python 版本绑定的**。本地是 **Python 3.14**，
而盒子如果跑 Ubuntu 22.04 就是 **3.10**、24.04 就是 **3.12**。
**cp314 的 wheel 装不到 cp310/cp312 上，会直接失败。**

**正确做法**（先确认盒子的 Python 版本，再选一条）：

```bash
# 先在盒子上查
python3 --version

# 方案 A：本地装一个同版本的 Python，用它下载匹配的 wheel
uv python install 3.12                      # uv 能装独立的 Python
uv pip download pandas pyarrow duckdb \
    --python 3.12 -d ./wheels

# 方案 B：用 pip download 显式指定目标环境（不依赖本地解释器）
pip download pandas pyarrow duckdb -d ./wheels \
    --python-version 3.12 --only-binary=:all: \
    --platform manylinux2014_x86_64
```

**方案 A 更省事，而这正是 uv 的另一个好处** —— 它自带 Python 版本管理，
能在本地造出一个和盒子一致的环境来准备 wheel。

**不过更简单的思路**：如果盒子能上外网（或者内网有 pip 源），
直接在盒子上 `pip install` 就完了，根本不用离线折腾。
**先确认盒子能不能上外网**（`curl -I https://pypi.org`），这是第一件该试的事。

### 5.3 ⚠️ WSL 的网络是 NAT，需要实测能否连到 ②

实测网络情况：

```
WSL IP: 172.19.74.245/20        ← NAT 网段
默认路由: 172.19.64.1
ping 宿主机局域网 IP: 通 ✅
外网 (pypi.org): 通 ✅
```

**出站访问是通的**（能 ping 通宿主机的局域网 IP），
所以理论上 WSL 能访问实验室的 ②（NAT 会转发）。

**但必须实测**，因为有些实验室网络对非本网段设备有限制。
拿到 ② 的 IP 后第一件事就是：

```bash
ping <②的IP>
python3 neware_client.py --transport tcp --host <②的IP> info
```

如果 WSL 连不上而 Windows 能连上，还有两个办法：
换 Windows 开发，或者在 WSL 里配镜像网络模式（`/etc/wsl.conf` 加 `networkingMode=mirrored`）。

### 5.4 ⚠️ C 盘只剩 132 GB —— "954 GB 可用"是假的

这是个容易误判的地方：

| 看到的 | 真实情况 |
|---|---|
| WSL 里 `df -h` 显示根分区 **954 GB 可用** | 这是虚拟磁盘的**逻辑上限** |
| 实际虚拟磁盘文件 | `C:\Users\gzz25\AppData\Local\wsl\{...}\ext4.vhdx`，当前 2.3 GB |
| **C 盘实际余量** | **132 GB（已用 86%）** ⚠️ |

WSL 的磁盘文件在 C 盘上，**用多少就涨多少**。所以：

> **真实可用上限约 132 GB，不是 954 GB。**
> 而且一旦把 C 盘塞满，**Windows 本身会出问题**。

**建议**：

1. **WSL 里只放代码和工作集**，不要放大量电池数据
2. 数据放别处（比如外接盘、或者干脆只存在盒子/② 上）
3. 定期清理：`uv cache clean`、删掉不用的 venv
4. 如果 C 盘继续变满，考虑把 WSL 迁到 D 盘（`wsl --export` + `wsl --import`）

---

## 六、现在就能用的命令

环境已经配好了，直接开干：

```bash
# 进入 WSL
wsl -d Ubuntu

# 项目目录（代码已在这里，venv 已建好，包已装好）
cd ~/batterylab
source .venv/bin/activate

# 跑自测（不需要设备和网络）
./selftest.py                    # 应该是 81 项全过

# 拿到 ② 的 IP 后
python neware_client.py --transport tcp --host <②的IP> info
python probe.py --transport tcp --host <②的IP>
```

在 VS Code 里：装 **Remote-SSH** 或 **WSL** 扩展，
打开 `~/batterylab`，解释器选 `~/batterylab/.venv/bin/python`。

---

## 七、待你处理的事

| # | 事项 | 命令 | 必需吗 |
|---|---|---|---|
| 1 | （可选）修好标准 venv | `sudo apt update && sudo apt install -y python3.14-venv python3-pip` | 不必需，uv 已能替代 |
| 2 | **确认盒子的 Python 版本** | 在盒子上 `python3 --version` | ★ **必需** —— 决定离线 wheel 怎么准备 |
| 3 | **确认盒子能不能上外网** | 在盒子上 `curl -I https://pypi.org` | ★ **必需** —— 能上就不用折腾离线包 |
| 4 | 拿到 ② 的 IP 后实测连通性 | `ping <②IP>` + `info` | ★ 必需 |
| 5 | 关注 C 盘空间（132 GB） | `df -h /c` | 建议 |

---

## 八、一句话总结

**你的 Ubuntu 环境是好的，现在可以直接开发了。**

- ✅ 架构和盒子一致（x86_64 Linux）、systemd 已启用（能本地验证服务文件）、
  外网通、局域网出站通、81 项自测全过
- ✅ 已装 uv 绕过 ensurepip 问题，代码已就位、环境已建好
- ✅ CRLF 行尾问题已修（并且实测复现过，你知道它长什么样了）
- ⚠️ 三件事要记：**Python 3.14 和盒子的版本可能不一致**（wheel 不能直接复用）、
  **WSL 走 NAT 需实测能否连 ②**、**C 盘只剩 132 GB 才是磁盘的真实上限**
