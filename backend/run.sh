#!/usr/bin/env bash
# 本地开发启动：先校验迁移版本，再按需续跑迁移，最后起服务。
# 构建、部署、本地示例和流水线共用同一个 MIGRATION_VERSION。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

export PYTHONPATH=".venv:."
export MIGRATION_VERSION="${MIGRATION_VERSION:-2026.11.17-proxy-capacity-v2}"

python3 manage.py check
if [ "${AUTO_MIGRATE:-0}" = "1" ]; then
  python3 manage.py migrate
fi

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
