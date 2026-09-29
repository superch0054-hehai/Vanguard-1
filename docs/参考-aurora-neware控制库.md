# 参考：aurora-neware（Empa 的开源新威控制库）调研

> 调研日期 2026-09-29。目的：为"控制功能"（写操作）开发做准备。
> 上游：<https://github.com/empaeconversion/aurora-neware>

## 一、这是什么

**Empa（瑞士联邦材料科学与技术实验室）能源转换材料实验室**做的开源库。
MIT 许可，版本 v0.2.3（Alpha，9 star，最后更新 2026-07-07）。

自我定位（README 原话）：*"A standalone Python API and command line interface (CLI)
to control Neware battery cyclers"* —— 关键词是 **control**。
它明确支持：连接、取状态/数据/日志、**启动和停止实验**。

**所以它解决的就是我们正要做的那个问题，而且在真实硬件上跑过。**

## 二、它和我们用的是同一套协议（这是最重要的结论）

| 项 | aurora-neware | 我们 |
|---|---|---|
| 帧头 | `<?xml version="1.0" encoding="UTF-8" ?><bts version="1.0">` | `<?xml version="1.0" encoding="UTF-8" ?>\n<bts version="1.0">` |
| 帧尾 | `</bts>` | `</bts>` |
| 终止符 | `</bts>` + `\n\n#\r\n` | `</bts>` + `#\r\n`（少两个换行） |
| 默认端口 | 502 | 502 |
| 命令集 | getdevinfo / getchlstatus / inquire / inquiredf / download / downloadStepLayer / downloadlog / **start / stop / light / clearflag** | 同一整套 |

**只差"声明后有没有换行"和"结尾少两个换行"。** 我们的读流量已经跑了 45 小时，
说明这个帧格式是被客户端接受的。

它的示例输出里 `devtype` 是 **27** —— 和我们现场 BTS85 的 devtype 一致。

## 三、四样值得直接借用/对照的东西

### 1. `start()` 里的"状态闸"（我们已按它实现）

上游在启动前先查通道状态，只允许三种状态：

```python
allowed_states = ["finish", "stop", "protect"]
```

我们的 `neware_client.assert_startable()` 沿用同一判据（见 `STARTABLE_STATUSES`），
并且比上游更严：**fail-closed** —— 目标通道读不到状态也拒绝，而不是"没看到在跑就当它闲着"。

### 2. 写命令的**真实回包样本**（已抄成 `selftest_control.py` 的夹具）

上游 `tests/mocks.py` 把"命令 → 真实回应"做成了假 socket，连成功和失败都有：

```xml
<cmd>start_resp</cmd><list count="1">
  <start ip="127.0.0.1" devtype="27" devid="21" subdevid="1" chlid="1">ok</start></list>
```

元素文本是 `ok` 或 `false`。`reset_resp` 例外：**没有 `<list>` 包裹**。

### 3. 一个已知坑（写在它的代码注释里）

> `getchlstatus` 的回包有时把 `subdevid` **错报成 1**（查 13-5-5 会告诉你返回的是 13-1-5）。
> 它的处理：把回包和自己的通道表合并，**以自己缓存的通道信息为准**。

我们 `collector.py` 的 `wait_until_status()` 已经在做同类过滤（只认自己请求的通道）。

### 4. 通道标识 `{devid}-{subdevid}-{chlid}`（如 `220-10-1`）

比我们的 `27-188-10-2` 字符串更干净。做写操作时"目标是哪个通道"必须无歧义。

## 四、三个差异 —— 动手前必须验证

### ① 它连上后会先"登录"，我们从不登录（**最大的未知量**）

上游第一包是：

```
<cmd>connect</cmd><username>admin</username><password>neware</password><type>bfgs</type>
```

回包 `<cmd>connect_resp</cmd><result>ok</result>`。默认凭据就是新威的 `admin` / `neware`。

我们的代码里 `<cmd>connect` 出现 **0 次**。读操作不发也一直通，
但**写操作很可能被客户端当作权限门槛** —— 必须先用零风险命令试出来。

### ② 运行位置不同（硬约束）

上游默认 `ip="127.0.0.1"`，即**脚本跑在装了 BTS 服务端的那台 Windows 机上**；
我们跑在盒子上、跨网连 `10.201.47.169:502`。

它的 `start(xml_file)` 会做 `if not all(f.exists() ...): raise FileNotFoundError` ——
**工步文件必须在脚本所在这台机器上存在**。协议里下发的是**路径字符串**，
由 Windows 侧的客户端自己去读文件 —— **盒子提供不了这个文件**。

### ③ 它没做的部分

上游代码注释里明确标了 REMAINING（未实现）：
`broadcaststop, continue, chl_ctrl, goto, parallel, getparallel, resetalarm, reset`。

我们的覆盖面反而更广（封装了 `resetalarm` / `broadcaststop` / `cancelpause` / `reset`），
但**这些方法从未被真机验证过**。

## 五、不能直接装，也不需要装

依赖只有 `defusedxml>=0.7.1` 和 `typer>=0.21.1`。**盒子没外网**，两个都没装，
`~/wheels/` 里也没有。但这两个都不是必需的：

- `defusedxml` 只是"更安全的 XML 解析"，Python 自带的 `xml.etree.ElementTree` 可替代（我们本就在用）
- `typer` 只是 CLI 框架，我们用的是 argparse

**结论：借鉴设计，不引入依赖。**

## 六、我们据此已经做的（2026-09-29）

1. **`neware_client.parse_ack()`** —— 写命令回包解析器。
   补的是一个真实缺口：读命令全都有解析器，**写命令一个都没有**（因为写方法从没被调用过）。
2. **`neware_client.assert_startable()` + `start(force=False)`** —— 启动前安全闸。
   `force=True` 是唯一的逃生口，必须显式传。
3. **`selftest_control.py`** —— 64 项断言，全程不碰设备：
   报文拼装（6 个写命令，黄金标准是协议文档）、回包解析（ok/false）、
   安全闸（放行/拒绝/fail-closed/混入通道）、
   以及最强的一条：**被拒绝时一个字节都不发**。

## 七、真机上的推进顺序（未做，待授权）

1. **`light`（点灯）** —— 零数据风险。用它验证那两个未知量：要不要发登录、帧头差异是否要紧。
2. **`setpause` / `cancelpause`（预约暂停）** —— 可取消，比立即停安全。
3. **`start` / `stop`** —— 最后做，且必须有工步文件、实验室知情、有人在场。

**跨过第 1 步直接做 stop，就是拿"可能拼错的 XML"去停正在跑的电池。**
