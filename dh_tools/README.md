# dh_tools —— 东华 DH7006B 导出数据读取工具（v0.1 · 2026-10-11）

## 这是什么

一套**零依赖（纯 Python 标准库）**的小工具，负责把东华主程序"文件导出"
生成的 txt 读成规范数据：

| 文件 | 作用 |
|---|---|
| `parse_dh_txt.py` | **解析器**：单个导出文件 → JSON / CSV（含物理自检） |
| `dh_collect.py` | **收件员**：盯目录 → 自动解析 → 索引 → 生成《摘要.md》 |
| `selftest_dh.py` | 自检（不出门验证代码没坏） |
| `Dockerfile` | 上盒就绪物（容器化部署） |

已验证：EIS / CV（23 段、95k 行）/ LSV 三类真实导出全部读通，
数值与东华软件界面显示一致（对账通过）。

## 用法（本机）

```bash
# 解析单个文件
python parse_dh_txt.py 某个导出.txt --json out.json --csvdir out_csv/

# 收件员（单趟，适合演示/测试）
python dh_collect.py --inbox ./inbox --outbox ./outbox --once

# 收件员（常驻，每 30 秒扫一趟）
python dh_collect.py --inbox ./inbox --outbox ./outbox --interval 30

# 自检
python selftest_dh.py
```

## 上盒部署（2026-10-11 更新：现场就一条命令）

> **推荐（一键）**：把 `dh_tools/` 拷到盒子 `~/dh-lab/tools` 后，在盒子上执行：
> `bash ~/dh-lab/tools/install_on_box.sh`
> —— 自动完成：自检 → 试跑 → 容器化（无镜像则自动拉取/退化为直跑）→ 打印盒子 IP。
> 现场逐步操作见 **`现场操作单.md`**。以下是手工分解版，供排查/无脚本时使用：

> 纪律（按项目规则）：**不污染盒子**——独立目录、独立容器，删除即还原。

```bash
# ① 有网机器：构建并打包镜像
docker build -t dh-collect:0.1 .
docker save dh-collect:0.1 | gzip > dh-collect-0.1.tar.gz

# ② 搬到盒子（scp / U 盘皆可），然后：
docker load < dh-collect-0.1.tar.gz

# ③ 盒子上准备目录（只动这一个目录）
mkdir -p ~/dh-lab/inbox ~/dh-lab/outbox

# ④ 启动（收件箱只读挂载；容器名 dh- 前缀，便于识别与清理）
docker run -d --name dh-collect --restart unless-stopped \
  -v $HOME/dh-lab/inbox:/inbox:ro \
  -v $HOME/dh-lab/outbox:/outbox \
  dh-collect:0.1

# ⑤ 验收 & 清理
docker logs dh-collect          # 看运行日志
docker rm -f dh-collect && rm -rf ~/dh-lab   # 彻底清理 = 盒子还原
```

## 数据流（当前阶段）

```
东华主程序"文件导出" → txt（收件箱 inbox/）
        ↓  dh_collect 自动解析（幂等、坏文件不崩）
outbox/parsed/<文件>.json   ← 规范数据（段/参数/列/数值）
outbox/index.jsonl          ← 索引（按内容指纹去重）
outbox/摘要.md              ← 人可读摘要（给 Agent / 人看）
```

## 已知边界

- 数值保留软件导出时的有效数字（4 位左右），不做二次舍入；
- `IRand` 列是"电流量程"（非测量值）；
- 空导出（实验无数据）会被记为"跳过"并在摘要中列出，而不是报错；
- 单位按 SI 前缀换算到基本单位（Ω / A / V / Hz / s）。
