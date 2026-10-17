# 东华 DH7006B 数据接入（独立贡献分支）

> 本分支**只新增目录**（`box/`、`pc/`、`tools/`、`docs/`），**不改动主干任何既有文件**；
> 是否合并、如何合并由主干开发者决定（见 [docs/独立性与合并说明.md](docs/独立性与合并说明.md)）。

## ★ box/ —— 盒子专用（全部 Python，运行在 TY1200 盒子上）

> 盒子上跑的就是这些；采集 PC 上**不需要任何我们的程序**。

| 文件 | 作用 |
|---|---|
| `dh_pull.py` | **拉取器**：SSH 只读拉取采集 PC 的数据（cron 每分钟；`scp -O` + 复制后大小核对） |
| `dh_collect.py` | **收件员**：扫描 → 解析 → 索引/摘要/逐点 JSON（容器 `dh-collect` 内运行） |
| `parse_dhsln.py` | `.dhsln`（UTF-8 XML 配方）解析器 |
| `parse_dat.py` | `.dat` 数值解码器（EIS / LSV / CV，物理自检，不依赖 txt） |
| `parse_dh_txt.py` | 软件导出 txt 解析器（自检/离线对账用） |
| `selftest_dh.py` | 自检（25 项，零依赖） |
| `Dockerfile` / `Dockerfile.localbase` | 收件员容器配方（localbase = 用盒子已有镜像离线构建） |
| `install_on_box.sh` | 一键部署（自检 → 试跑 → 容器化 → 打印盒子 IP） |
| `sync_now.sh` | 备选：盒子端"即时拉取" |
| `crontab.txt` | 定时任务配置（每分钟拉取） |

## pc/ —— 采集 PC 端（旧推送模式 · 已停用 · 仅作回退备份）

| 文件 | 原作用 |
|---|---|
| `dh_grab.ps1` / `.bat` | 抓取器（源目录只读复制到中转筐） |
| `dh_push.ps1` / `push_to_box.bat` | 推送器（送盒子；推完删副本；删除同步） |

## tools/ —— 开发与逆向工具（按需手动运行）

`parse_dh_txt.py`（见 box/）之外的探针与标定工具：`scan_dat.py`、`calib_dat.py`、`probe_dh_native.py`。

## docs/ —— 文档

`使用指南.md`（部署与日常）、`更新说明.md`（技术要点/已知限制）、`独立性与合并说明.md`（与主干关系）、`现场操作单.md`。

## 数据流（当前：盒子主动拉取）

```text
采集 PC：只开 Windows OpenSSH 服务 + 只读账号 dhbox（零自定义程序）
   ↑  SSH 只读（cron 每分钟：列目录 → 比对清单 → scp 拉取；删除 → 盒子 _trash）
盒子：box/dh_pull.py → ~/dh-lab/inbox → 容器 dh-collect（每 30 秒）
   → ~/dh-lab/outbox：摘要.md / index.jsonl / parsed/*.json → LLM / Agent
```

## 状态

| 能力 | 状态 |
|---|---|
| 同步（增/改/删） | ✅ pull 模式已上线（盒子 cron，每分钟） |
| `.dhsln` 配方解析 | ✅ 正常 |
| `.dat` 数值（EIS/LSV/CV） | ✅ 已上线（逐点 JSON + 实验归属） |
| 自检 | ✅ 25/25（本机与盒子均通过） |
