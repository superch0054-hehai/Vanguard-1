# box/ —— 盒子专用（全部是 Python + 容器/部署文件）

这里的程序**全部运行在 TY1200 盒子上**，采集 PC 上不需要任何我们的程序。

| 文件 | 作用 |
|---|---|
| `dh_pull.py` | 拉取器：通过 SSH 只读拉取采集 PC 的数据（cron 每分钟调用 `--once`） |
| `dh_collect.py` | 收件员：扫描 inbox → 解析 → 索引/摘要/逐点 JSON（容器内运行） |
| `parse_dhsln.py` | `.dhsln`（配方 XML）解析器 |
| `parse_dat.py` | `.dat` 数值解码器（EIS / LSV / CV） |
| `selftest_dh.py` | 自检（25 项，零依赖） |
| `Dockerfile` / `Dockerfile.localbase` | 收件员容器配方（localbase = 用盒子已有镜像离线构建） |
| `install_on_box.sh` | 一键部署（自检 → 试跑 → 容器化 → 打印盒子 IP） |
| `sync_now.sh` | 备选：盒子端"即时拉取" |
| `crontab.txt` | 盒子上的定时任务配置（每分钟拉取一次） |
