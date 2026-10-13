# 东华 DH7006B 数据接入（独立贡献分支）

> **本分支只做"新增"**：`dh_tools/`（脚本与工具）+ `docs/`（文档）+ 本 README。
> **不改动主干任何既有文件**，两部分保持相对独立；是否合并、如何合并由主干开发者决定。

## 这是什么

把**东华 DH7006B 多通道电化学工作站**的原始数据，从采集 PC 自动同步到 **TY1200 盒子**，
并在盒子上自动解析成"人和大模型都能读"的数据（配方 + 曲线数值 + 逐点 JSON）。

数据流（全自动，无需人工干预）：

```
东华软件数据目录（采集 PC，只读）
   ↓ 抓取器 dh_grab.ps1（每 1 分钟，只读复制新增/改动文件；删除→待删清单）
中转筐 DH_export\native
   ↓ 推送器 dh_push.ps1（每 1 分钟；复制到盒子；推完删自己的副本；删除同步→盒子回收目录）
盒子 ~/dh-lab/inbox
   ↓ 收件员 dh-collect（容器，每 30 秒）
   ├─ .dhsln → 配方：实验类型 / 方法 / 每段参数 / 信号 / 通道
   └─ .dat   → 数值：EIS 阻抗谱 / LSV / CV 曲线（含逐点 JSON）
   ↓
~/dh-lab/outbox/：摘要.md（人读） + index.jsonl（索引） + parsed/*.json（逐点数值）→ Agent 可读
```

**关键纪律**：对采集 PC 的源目录**只读**（不改/不移/不删/不重命名）；盒子侧只动 `~/dh-lab` 一个目录；
不触碰盒子上其它容器（如 vLLM / Agent）。

## 目录结构

```
dh_tools/
├─ parse_dh_txt.py        导出 txt 解析器（离线校验工具；生产管道不依赖 txt）
├─ parse_dhsln.py         .dhsln（UTF-8 XML 配方）解析器
├─ parse_dat.py           .dat 二进制解码器（EIS / LSV / CV 三种结构，物理自检，不依赖 txt）
├─ dh_collect.py          收件员（扫描/解析/索引/摘要；幂等 + 指纹去重 + 同路径只留最新）
├─ selftest_dh.py         自检（25 项，含收件员端到端与幂等）
├─ dh_grab.ps1 / .bat     采集 PC 抓取器（账本记 路径+大小+修改时间；消失→待删清单；安全阀）
├─ dh_push.ps1 / push_to_box.bat  采集 PC 推送器（推完删副本；删除同步→盒子 _trash）
├─ install_on_box.sh      盒子一键部署（自检 → 试跑 → 容器化 → 打印盒子 IP）
├─ Dockerfile / Dockerfile.localbase  容器配方（localbase = 用盒子已有镜像离线构建）
├─ sync_now.sh            备选：盒子端"即时拉取"（需采集 PC 开 OpenSSH）
├─ scan_dat.py / calib_dat.py / probe_dh_native.py   逆向与标定工具（只读）
└─ 现场操作单.md            现场可照抄的操作清单
docs/
├─ 使用指南.md             部署与日常使用
├─ 更新说明.md             里程碑与技术要点 + 已知限制
└─ 独立性与合并说明.md      本分支与主干的关系说明
```

## 快速开始

- **盒子（部署接收端）**：把 `dh_tools/` 拷到盒子 → `bash ~/dh-lab/tools/install_on_box.sh`（一键：自检→试跑→容器化→打印盒子 IP）
- **采集 PC（发布数据）**：`paths.txt` 写一行软件数据目录 → 两个计划任务调用 `dh_grab.bat` / `push_to_box.bat`（每分钟）
- 详细步骤与排错：见 [docs/使用指南.md](docs/使用指南.md)

## 当前状态

| 能力 | 状态 |
|---|---|
| 全自动同步（新增 / 修改 / 删除） | ✅ 已上线（PC 侧零副本、盒子侧零冗余） |
| `.dhsln` 配方解析 | ✅ 192 份 |
| `.dat` 数值解析（EIS / LSV / CV） | ✅ 190 份，含 191 份逐点 JSON |
| 精度 | EIS：与软件显示值误差 ≤ 显示舍入（phase 4e-7、Freq 3e-7）；LSV/CV：逐列命中率 ≥99%（±0.2%） |

已知限制见 [docs/更新说明.md](docs/更新说明.md#已知限制)。
