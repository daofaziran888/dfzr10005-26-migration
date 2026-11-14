.PHONY: install backend frontend migrate-rehearse migrate-init migrate-run migrate-status migrate-pending migrate-rollback test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# ===== 强类型保留模型迁移（后端） =====
# 1. 隔离环境演练：临时库 + 小批次，含幂等校验，不触碰本地/线上库
migrate-rehearse:
	cd backend && .venv/bin/python -m scripts.migrate_retention rehearse --batch-size 3

# 2. 数据库结构演练（幂等建表）
migrate-init:
	cd backend && .venv/bin/python -m scripts.migrate_retention init-db

# 3. 分批回填上线（批次大小可用 BATCH_SIZE 覆盖，默认 500）
migrate-run:
	cd backend && .venv/bin/python -m scripts.migrate_retention migrate --batch-size $${BATCH_SIZE:-500}

migrate-status:
	cd backend && .venv/bin/python -m scripts.migrate_retention status

migrate-pending:
	cd backend && .venv/bin/python -m scripts.migrate_retention pending

# 回退全部迁移结果（原值快照始终保留，可重新迁移）
migrate-rollback:
	cd backend && .venv/bin/python -m scripts.migrate_retention rollback

test:
	cd backend && RETN_AUTO_BOOTSTRAP=0 .venv/bin/python -m unittest discover -s tests -v
	cd frontend && npm run build
