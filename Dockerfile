# DeepSeek QQ Bot 镜像（只负责机器人本体；QQ 协议端 NapCat 由 compose 单独起）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 代码与默认配置（运行时 config/ 与 data/ 会挂载覆盖）
COPY src ./src
COPY config ./config
COPY scripts ./scripts

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "src/main.py"]
