# 分布式采集调度引擎 前端镜像
FROM node:20-alpine
WORKDIR /srv/web

# 与后端同一迁移版本口径，构建产物里带上版本号，便于核对前后端是否同批上线。
ARG MIGRATION_VERSION=2026.11.17-proxy-capacity-v2
ENV MIGRATION_VERSION=${MIGRATION_VERSION}
LABEL org.opencontainers.image.migration-version=${MIGRATION_VERSION}

COPY package.json ./
RUN npm install
COPY . .
RUN MIGRATION_VERSION=${MIGRATION_VERSION} npm run build

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
