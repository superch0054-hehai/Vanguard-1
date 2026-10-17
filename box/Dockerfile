# 东华数据"收件员"容器 —— 上盒就绪物（2026-10-11）
#
# 说明：盒子（TY1200）不通外网，所以镜像在有网的机器上构建好后
#       docker save → 搬过去 → docker load（见 README.md）。
#
# 构建（在有 Docker 的机器上）：
#   docker build -t dh-collect:0.1 .
#
# 运行（盒子上，只读挂载收件箱、可写挂载输出目录）：
#   docker run -d --name dh-collect --restart unless-stopped \
#     -v /home/admin/dh-lab/inbox:/inbox:ro \
#     -v /home/admin/dh-lab/outbox:/outbox \
#     dh-collect:0.1

FROM python:3.11-slim

WORKDIR /app
COPY parse_dh_txt.py parse_dhsln.py parse_dat.py dh_collect.py selftest_dh.py /app/

# 自检不过就不启动（免得带着坏代码上班）
RUN python selftest_dh.py

ENTRYPOINT ["python", "dh_collect.py"]
CMD ["--inbox", "/inbox", "--outbox", "/outbox", "--interval", "30"]
