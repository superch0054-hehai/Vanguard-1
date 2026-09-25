# 模型与 Agent 配置说明

> **用途**：供 **SOH 报告模板制作方**按同等配置试做报告模板使用。
> **记录日期**：2026-09-25
> **数据来源**：对 TY1200（彤央 1200 边缘计算终端）现场运行环境的实际巡检，非文档推测。

---

## 一、一句话结论

盒子上跑的是一个 **27B 参数、AWQ INT4 量化的大模型**，通过 **vLLM** 提供 OpenAI 兼容接口；
上层 Agent 框架是 **QwenPaw**。

**★ 最关键的一点：这是「推理模型」，默认会先思考再回答 —— 试做时务必关掉思考，否则会超时。**

---

## 二、硬件环境

| 项 | 配置 |
|---|---|
| 整机 | 天数智芯 彤央 **TY1200** 边缘计算终端 |
| CPU | Intel Core Ultra 7 255H（16 核 22 线程 = 6 P核 + 8 E核 + 2 LPE核） |
| 加速卡 | 天数智芯数据中心级 GPGPU，**300 TOPS @ INT8**，**32GB HBM2e** |
| 系统内存 | 2× DDR5 5600MHz（默认 2×8GB = **16GB**） |
| 存储 | 512GB PCIe 4.0 NVMe |
| 操作系统 | **Debian 12**（bookworm），内核 6.12.90，Python 3.11 |

> 真正跑模型的是那块 **32GB HBM2e 的天数 GPGPU**，不是 CPU。
> 16GB 系统内存是整机的紧约束（与模型服务共享）。

---

## 三、模型

| 项 | 值 |
|---|---|
| 型号（服务中注册名） | **`Qwen3.8-27B-AWQ-INT4`** |
| 参数量 | 27B |
| 量化方式 | **AWQ INT4** |
| 获取方式 | **由项目方提供模型镜像**（非公网公开权重） |
| 类型 | **推理模型（reasoning model）** —— 输出含独立的思考段 |

---

## 四、推理引擎（vLLM）

| 项 | 值 |
|---|---|
| 引擎 | vLLM **0.23.0** |
| 服务端口 | **12345** |
| 启动命令 | `python3 -m vllm.entrypoints.openai.api_server` |
| 上下文长度 | `max_model_len = 120000`（约 12 万 token） |
| 并发序列数 | `max-num-seqs = 1` ← **★ 同一时刻只处理 1 个请求（串行）** |
| 思考解析器 | `--reasoning-parser qwen3` |
| 接口协议 | **OpenAI 兼容**（`/v1/chat/completions`） |

> 复核命令（在盒子上执行）：`ps -ef | grep vllm`

---

## 五、Agent 框架（QwenPaw）

| 项 | 值 |
|---|---|
| 框架 | **QwenPaw v2.2.1** |
| Web 控制台 | 端口 **8088** |
| 运行方式 | Docker 容器 |
| 启动命令 | `/app/venv/bin/qwenpaw app --host 0.0.0.0 --port 8088` |
| 流式空闲超时 | **30 秒**（环境变量 `QWENPAW_LLM_STREAM_IDLE_TIMEOUT`） |

---

## 六、怎么调用（试做用）

模型服务是 OpenAI 兼容的，用 curl 就能直接测：

```bash
curl http://<盒子IP>:12345/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.8-27B-AWQ-INT4",
    "messages": [
      {"role": "user", "content": "把下面的数据整理成 Markdown 表格：……"}
    ],
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

**★ 最后那行 `chat_template_kwargs` 是必须的**，理由见第七节第 2 条。

> ⚠️ 上面是 **curl 直调**的写法。如果你用 Python 的 `openai` 包调用，
> 写法**不一样**（要包一层 `extra_body`），否则会抛 `TypeError` ——
> 区别和原因见第七节第 2 条的表。

---

## 七、★★★ 三个必须知道的行为特性（直接影响模板设计）

### 1. 它是推理模型：默认先「思考」再回答

输出流长这样：

```
第 1 个 chunk         delta.content   = ""          ← 空
之后 N 个 chunk       delta.reasoning = "……"        ← 全是思考内容
思考结束后            delta.content   = "……"        ← 才轮到正式回答
```

**后果**：简单任务思考快，没问题；**复杂任务（如跨多张表做归因分析）思考可能超过 30 秒**，
而 Agent 框架等的是 `content` 字段，等不到就报错：

```
MODEL_TIMEOUT
Model 'Qwen3.8-27B-AWQ-INT4' timed out.
Reason: LLM stream ... produced no content for 30s.
```

### 2. 关掉思考的写法 —— **看走哪条路，两种写法不一样**

| 调用方式 | 正确写法 |
|---|---|
| **直接 HTTP（curl / requests）** | 请求体顶层放 `"chat_template_kwargs": {"enable_thinking": false}` |
| **经 OpenAI SDK**（Python `openai` 包） | `extra_body={"chat_template_kwargs": {"enable_thinking": false}}` |

**为什么不一样**：`extra_body` 是 OpenAI SDK 的正式参数，SDK 会把它的内容
**解开、合并到请求体顶层**。所以经 SDK 时，你不写 `extra_body` 直接传
`chat_template_kwargs=` 会抛 `TypeError: unexpected keyword argument`。

> 本文档第六节给的是 **curl 直调**的例子，用第一种写法。
> 如果用 Python 的 `openai` 包，改用第二种。

**关掉思考后的实测对照**（同一个简单问题，经 SDK）：

| | 思考关 | 思考开（不传参数） |
|---|---|---|
| 输出 tokens | **2** | **28**（其中 26 个是思考） |
| 思考段 | **无** | 88 字符 |

> ⚠️ **不要只看耗时判断思考有没有关**。短回答时两者耗时差不多，
> 要看**输出 token 数**和**有没有思考段** —— 这才是判据。

### 3. 并发是 1

`max-num-seqs = 1` —— 同一时刻只能处理一个请求。试做时**不要并发压测**，会排队。

---

## 八、同等配置试做方法

### 路线 1（推荐）：直接用盒子上的服务

拿到盒子 IP 后，按第六节的 curl 直接调用。

**这是唯一能保证「完全同等配置」的方式**，因为模型镜像是项目方私有的，
公开渠道下载的 Qwen 权重**不能假定与之等价**。

### 路线 2：本地起同等环境

若需离线试做，用项目方提供的**同一模型镜像**，按第四节参数启动 vLLM：

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model <模型镜像路径> \
  --served-model-name Qwen3.8-27B-AWQ-INT4 \
  --port 12345 \
  --max-model-len 120000 \
  --max-num-seqs 1 \
  --reasoning-parser qwen3
```

> 显卡要求：需能容纳 27B INT4 权重 + 12 万 token 上下文的 KV Cache。
> 盒子上的卡是 **32GB HBM2e**，可作为容量参考。

---

## 九、对报告模板设计的直接建议

1. **模板要明确分成「数据概览」和「关键指标分析」两段** —— 需求书 FR-03 原文就要求这两块。

2. **结构上「厚摘要、薄推理」**：由我们预先算好指标写进 `summary.md`，
   模型只负责读取和表述，**不让它现场做重计算**。两个理由：
   - 现场算会触发第 7.1 条的超时；
   - 口径由我们控制，结果才可复现。

3. **输出格式建议用 Markdown** —— 已验证模型能准确复述并生成 Markdown 表格，数字不会串行。

4. **若模板要求确定性输出**（同输入必同输出），需要额外固定 `temperature`。
   该参数当前取值**未确认**，见第十节。

5. **单次报告引用的数据量要控制**：上下文虽有 12 万 token，但明细数据动辄上千行，
   建议模板**只引用汇总后的指标**，明细留在 CSV 里按需查。

---

## 十、待确认项（试做前建议先明确）

| 项 | 状态 |
|---|---|
| `temperature` / `top_p` / `max_tokens` 实际取值 | ❓ 未知（当前取 Agent 框架默认值） |
| vLLM 服务是否要求 API Key | ❓ 未确认 |
| 是否启用 `--tensor-parallel-size`（多卡并行） | ❓ 未确认（盒子只有一块 GPGPU，大概率是 1） |
| 模型镜像的分发方式与获取途径 | ❓ 需项目方提供 |
| 「关闭思考」是否作为**默认**配置落地 | ⏳ 待解决（当前每次请求都要显式传参） |

---

*本文档由现场巡检结果整理，参数可直接用于复现；标注 ❓ 的项需进一步确认后再写入模板设计依据。*
