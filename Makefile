# 迁移版本口径：Makefile、Dockerfile、docker-compose、流水线都读这一份。
# 升级字段结构时先改 backend/app/migrations.py，再同步这里，然后跑 `make check`。
MIGRATION_VERSION ?= 2026.11.17-proxy-capacity-v2
export MIGRATION_VERSION

PYTHONPATH := .venv:.
export PYTHONPATH

.PHONY: install check backend frontend build test migrate verify preview

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

check:
	cd backend && python3 -c "import app.build_check as b; raise SystemExit(b.main())"
	cd backend && python3 -m app.version_check
	cd frontend && npm run typecheck

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build
	docker compose build

# 本地示例与部署共用同一迁移版本；迁移是幂等的，重复执行不会产生第二份结果。
migrate:
	cd backend && python3 manage.py migrate --allow-failures

preview:
	cd backend && python3 manage.py preview

verify:
	cd backend && python3 manage.py verify

test:
	cd backend && python3 -m unittest discover -s tests -v

# 上线流水线本地复现：版本校验 → 构建 → 迁移 → 校验结果
release: check build migrate verify
