# 分布式采集调度引擎 后端镜像
FROM python:3.12-slim
WORKDIR /srv/app

# 迁移版本口径：构建时固定，部署和本地必须传同一个值，否则容器起不来。
ARG MIGRATION_VERSION=2026.11.17-proxy-capacity-v2
ENV MIGRATION_VERSION=${MIGRATION_VERSION}
LABEL org.opencontainers.image.migration-version=${MIGRATION_VERSION}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 构建期就把版本和代码对齐，问题留在构建阶段而不是上线后。
RUN python -m app.build_check

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
