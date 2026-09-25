# neware_comms —— Neware 测试机通讯验证工具

针对《新威尔电池测试系统BTSAPI协议 v1.19》实现的通讯层与验证工具。
**目的：把"测试机通讯调通"这件事，从"写代码"变成"跑脚本 + 看结果"。**

准备工作和现场采集清单见 [`CHECKLIST.md`](CHECKLIST.md)。

---

## 文件

| 文件 | 作用 |
|---|---|
| `neware_client.py` | 通讯库 + 命令行工具。22 条协议命令全部实现（含备注、自动备份、等待结束） |
| `probe.py` | 一键体检：8 个环节逐项验证 + 时间单位校验 + **SOH 数据可行性预览** |
| `selftest.py` | 离线自测，94 项断言。验证命令拼装、解析、分页、SOH 计算，**不需要真机** |
| `export_review.py` | **导出专家审查包**：数据 CSV + 字段字典 + 待审查问题 + 4 张曲线图 |
| `selftest_export.py` | `export_review.py` 的离线端到端测试，**不需要真机** |
| `dump_raw.py` | 原始字节诊断。解析失败时用它看清客户端到底发了什么 |
| `CHECKLIST.md` | 准备清单、现场信息采集表、验证路径、故障速查、时间倒排 |

无需第三方依赖（Parquet 导出用到 `pandas`+`pyarrow`，缺失时自动回落 CSV）。

---

## 快速开始

### 0. 先跑离线自测（现在就能做，不需要设备和客户端）

```bash
python selftest.py
```
预期：`通过 70 项，失败 0 项`。这证明工具本身没问题，
之后现场调不通就一定是设备/客户端/配置的问题，不用怀疑代码。

### 1. 确认用管道还是 TCP

| 场景 | 方式 | 约束 |
|---|---|---|
| BTS Client 与脚本**同机** | `--transport pipe` | **必须以管理员权限运行**此脚本；客户端也须管理员权限运行 |
| BTS Client 在**另一台机器** | `--transport tcp --host <IP>` | **管道方式不支持远程客户端**，只能 TCP；客户端版本须 ≥ 2023/3/17 |

### 2. 一键体检（推荐从这里开始）

```bash
# 跨机
python probe.py --transport tcp --host 192.168.1.20

# 同机（在管理员权限的终端里）
python probe.py --transport pipe

# 指定通道 / 查看历史测试（testid=0 表示当前测试）
python probe.py --transport tcp --host 192.168.1.20 --channel 25-41-1-1 --testid 11
```

输出示例：

```
=== Neware 通讯体检 ===  传输：tcp 192.168.1.20:502

[1/8] 连接                   PASS  tcp 192.168.1.20:502 已建立
[2/8] getdevinfo             PASS  服务器 2 台，通道 16 个
     目标通道：25-41-1-1@127.0.0.1（BTS83）
[3/8] getchlstatus           PASS  working / 测试中
[4/8] inquire                PASS  3.7021V / 1.0A / workstatus=working / barcode=G48100230100
[5/8] download               PASS  拉取 5000 条 DF 明细
[6/8] downloadStepLayer      PASS  1324 个工步，工步类型 4 种，DCIR 有值
[7/8] inquiredf              PASS  testid=11 已上传 5000 条，完整=是
[8/8] downloadlog            PASS  12 条日志；解读需 LogCode.csv，该文件未随协议提供

--- 单位与结构校验 ---
  testtime 单位判定：ms  比值 ≈ 1000 → 单位毫秒（与协议一致）
  seqid 单调递增：是
  辅助通道列名：['N14', 'T1', 'Thk1', 'V1']
  （辅助通道列名随硬件配置变化，解析时需自适应，不可硬编码）

--- SOH 数据可行性预览（来自 downloadStepLayer）---
  循环数              100（1 → 100）
  首圈放电容量        3.2134 Ah
  末圈放电容量        2.8765 Ah
  容量保持率          89.52 %
  库仑效率 首/末      99.81 % / 99.64 %
  能量效率 首/末      94.12 % / 92.35 %
  DCIR 首/末 (mΩ)     12.3 / 18.7  增长 152.03 %
  结论：SOH 报告所需指标均可从协议字段算出。
```

完整证据（含原始样例与全部步骤结果）写入 `probe_report.json`。

> **SOH 预览这一节是重点**：它能出数，就说明"数据读取 → 指标计算"
> 这条链路已经验证完了，FR-03 不再是未知数。

### 3. 单条命令

```bash
# 设备信息：会自动列出全部通道，通道四元组不用手工猜
python neware_client.py --transport tcp --host 192.168.1.20 info

# 通道状态
python neware_client.py --transport tcp --host 192.168.1.20 status 25-41-1-1

# 实时数据
python neware_client.py --transport tcp --host 192.168.1.20 rt 25-41-1-1

# DF 明细，全量拉取并落 Parquet
python neware_client.py --transport tcp --host 192.168.1.20 df 25-41-1-1 \
    --testid 71 --out df.parquet

# 工步层（含 DCIR），这是算 SOH 最省事的数据源
python neware_client.py --transport tcp --host 192.168.1.20 step 25-41-1-1 --out steps.csv

# 上传完整度检查
python neware_client.py --transport tcp --host 192.168.1.20 dfcheck 25-41-1-1

# 启动测试：通道:工步文件:条码[:备注]，可顺带下发自动备份
# 注意：工步文件路径是【客户端那台电脑上】的路径，不是本机的路径
python neware_client.py --transport tcp --host 192.168.1.20 start \
    "25-41-1-1:D:\\stepManager\\test\\1.xml:D672035TAA11:SC20-第3批" \
    --backup-dir "D:\\NewareBackup" --backup-type nda

# 等测试结束（轮询状态，默认等 finish/stop/protect）
python neware_client.py --transport tcp --host 192.168.1.20 wait 25-41-1-1 --timeout 7200

# 停止
python neware_client.py --transport tcp --host 192.168.1.20 stop 25-41-1-1

# 发送任意命令体，用于试探协议未文档化的命令（见 CHECKLIST 第五节“联机消息”）
python neware_client.py --transport tcp --host 192.168.1.20 raw "<cmd>login</cmd>"
```

通道写法：`devtype-devid-subdevid-chlid`，可加 `@ip`，例如 `25-41-1-1`、`22-3-2-1@127.0.0.1`。

---

## 作为库使用

```python
from neware_client import Channel, NewareClient, TcpTransport

transport = TcpTransport("192.168.1.20", 502, timeout=30)
with transport:
    client = NewareClient(transport)

    # 自动发现通道
    info = NewareClient.parse_devinfo(client.getdevinfo())
    channels = [Channel(c["devtype"], c["devid"], c["subdevid"], c["chlid"], c["ip"])
                for c in info["channels"]]

    # 实时状态
    for row in NewareClient.parse_inquire(client.inquire(channels[:4])):
        print(row["dev"], row["voltage"], row["current"], row["workstatus"])

    # 全量 DF 明细（自动分页，count 上限 1000 由库处理）
    rows = client.download_all(channels[0], testid=71)

    # 工步层汇总（含 DCIR）
    steps = NewareClient.parse_data(
        client.download_steplayer(channels[0], testid=71, dcir=1))
```

辅助方法：`download_all`（分页拉全量，返回列表）、`iter_download`（**生成器版本，内存友好，大数据量用这个**）、`wait_until_status`（轮询等测试结束）。

实现的命令：`getdevinfo` `light` `start` `getchlstatus` `stop` `download`
`inquire` `broadcaststop` `continue` `chl_ctrl` `goto` `inquiredf`
`downloadStepLayer` `downloadlog` `parallel` `getparallel` `resetalarm`
`clearflag` `reset` `setpause` `cancelpause` `getpause`。

---

## 实现中已处理的坑

这些是从协议文档里逐条核对出来的，细节见 `电池文档分析报告.md` 第 2.5 节：

1. **时间单位不一致**：`download.testtime` 是 **ms**，`inquire.totaltime/relativetime` 是 **s**。
   `probe.py` 用真实数据反推 `testtime` 单位并打印结论，不靠假设。
2. **协议里两张状态表不一致**：`finish`（完成）在 2.1.4 的回应说明里有定义，但 2.3 的总表里没有；
   `synCtrl`/`ligth`/`waitTimeOut`/`waitStart` 则相反。代码里把两张表合并收录。
3. **分页上限**：`download` 的 `count` 不能超过 1000，超出直接抛错；
   `download_all` 用 `startpos`（**数据序号，不是偏移量**）自动翻页。
4. **辅助通道列名动态变化**：`V1/T1/Thk1/N14` 随硬件配置变化，
   `probe.py` 自动发现列名，绝不硬编码。
5. **`getdevinfo` 用 `Channelid` 而非 `chlid`**：属性名与其他命令不一致，解析时两个都兜。
6. **厂商文档里的中文引号**：文档示例中混入了 `"` `"`，真实客户端输出正常，
   但解析器仍做了宽容处理。
7. **`dev` 字段五段式**：`inquire` 回应的 `dev="22-3-2-1-10"` 是
   `设备类型-设备号-单元号-通道号-辅助通道号`，末段是辅助通道号，已单独解析。
8. **管道超时后失效**：管道一旦超时，句柄即不可用，必须重连。
   已用定时器兜底并给出明确提示。

---

---

## 在本地开发时的"盒子适配约束"

开发在本地电脑上做，但最终要跑在 **③ 边缘算力盒子**（Linux x86_64，16GB 内存，
512GB 磁盘，同时还要跑大模型）。下面这些约束**在本地写代码时就要守住**，
否则搬到盒子上会出问题。**建议每次提交前扫一眼。**

### 1. Python 版本（最容易踩）

- 本地是 **Python 3.14**，盒子可能是 **3.10 / 3.12**（取决于它的 Ubuntu 版本）
- 所以：**不要用 3.14 才有的语法或标准库特性**（用 `python3.12` 兼容的写法）
- 只用标准库 + `pandas` / `pyarrow` / `duckdb` —— 这几个各版本都有 wheel
- 搬上盒子前，最好在盒子同版本下再跑一遍 `selftest.py`

### 2. 内存（盒子要和模型共享 16GB）

- **明细数据一律用 `iter_download()`**，不要用 `download_all()` 全量进内存：

  ```python
  # ✅ 内存恒定，边拉边写
  for batch in client.iter_download(channel):
      append_to_parquet(batch)

  # ❌ 几百万行会一次性占满内存
  rows = client.download_all(channel)
  ```

- 工步层只有几千行，用 `download_all()` 无所谓
- 读大文件用 `chunksize` / DuckDB 流式查询，别 `pd.read_parquet()` 整个吞下去
- **本地测的时候故意用大数据量试一次**，看内存峰值（`/usr/bin/time -v` 或 `top`）

### 3. 磁盘（盒子 512GB 和模型共享；本机 C 盘只剩 132GB）

- **原始 NDA 文件留在 ②**，不要往本地/盒子搬大文件
- 落盘路径要做成**可配置**，并且能清理（有保留策略）
- 别把测试数据堆在 WSL 里 —— WSL 的虚拟磁盘在 C 盘上，只有 132GB 可涨

### 4. 并发与 CPU

- 盒子 16 核但主要给模型推理用；**采集是低频任务**（协议实时性不优于 5 秒）
- 不要开线程池猛拉数据，不要高并发请求客户端
- 轮询间隔建议 **10 秒以上**

### 5. 路径（必须用 pathlib）

```python
from pathlib import Path
out = Path(cfg.data_dir) / "derived" / "steps.parquet"   # ✅
out = cfg.data_dir + "\\derived\\steps.parquet"          # ❌
```

**唯一的例外**：`start` 命令里的工步文件路径（`D:\stepManager\1.xml`）
**本来就该是 ② 那台 Windows 机器上的路径**，代码里要注释说明，别改。

### 6. 行尾与编码

- 全部 **LF**（`.gitattributes` 已配好）
- 源码 UTF-8；CSV 用 `utf-8-sig`（Excel 打开不乱码）

### 7. 环境隔离

- 用 venv，**不要在系统 Python 里装东西** —— 盒子上跑着大模型，污染它是要出事的事
- **不装任何 GPU 相关的东西**（不需要）
- 本地已装 `uv`，可直接用：`uv venv .venv && uv pip install pandas pyarrow duckdb`

### 8. 别硬编码本地环境

- ② 的 IP、数据目录、轮询间隔 —— **全部做成配置项或命令行参数**
- 不要把 `C:\...`、`/mnt/c/...`、`/home/xxx` 写进代码
- 目标：**同一份代码，换台机器改个配置就能跑**

### 9. 服务化时要能加资源限制

最终在盒子上是 systemd 服务，会带 `MemoryMax` / `CPUQuota`。
所以程序要**能在内存受限时优雅失败**（比如捕获 `MemoryError`），而不是把盒子搞崩。

### 10. 事件与日志

- 日志走 stderr（systemd 会收进 journal），**不要自己写日志文件到奇怪的位置**
- 关键状态写进一个"心跳 JSON"（见《可视化方案》第三节），
  否则服务静默卡死你发现不了

---

### 提交前自查（三条最容易犯的）

1. 有没有用 `download_all()` 拉明细数据？→ 换成 `iter_download()`
2. 有没有硬编码本地路径或 IP？→ 改成配置项
3. 有没有用 3.14 才有的语法？→ 确认在 3.12 下也能跑

---

## 已知限制

- **`download` / `downloadStepLayer` / `downloadlog` 只能下载 127.0.0.1 服务器下的数据**
  （协议硬约束），且每次只能下一个通道。
- **实时性 ≥ 5 秒**，协议明确声明。不适合毫秒级同步场景。
- **`downloadlog` 单次最多返回 5000 条**，需用 `log_lever` 过滤；
  且日志码语义需 `LogCode.csv`（尚未拿到）。
- **"联机消息"未见文档**，见 `CHECKLIST.md` 第五节。
- `selftest.py` 用的是协议文档样例，**不能替代真机验证**；
  但它能把"代码是否有 bug"这一层排除掉。
