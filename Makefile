# WenQu — 自文档化 Makefile(基准 02),运行 `make help` 查看全部命令
.DEFAULT_GOAL := help

.PHONY: help version compose-dev-up compose-dev-down db-migrate \
        server-install server-run server-test server-lint \
        parser-install parser-gen parser-run parser-test parser-lint \
        web-install web-dev web-build

help: ## 显示全部命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

version: ## 显示当前版本(VERSION 单一真源)
	@cat VERSION

compose-dev-up: ## 启动开发基础设施(postgres+minio,卷带 dev 后缀)
	docker compose -f docker-compose.dev.yml up -d

compose-dev-down: ## 停止并移除开发基础设施
	docker compose -f docker-compose.dev.yml down

db-migrate: ## 执行数据库迁移(读取 .env 的 APP_DATABASE_URL)
	cd server && uv run alembic upgrade head

server-install: ## 安装主服务依赖(uv sync --locked)
	cd server && uv sync --locked

server-run: ## 启动主服务(热重载,:8000)
	cd server && uv run uvicorn app.main:app --reload --port 8000

server-test: ## 运行主服务测试
	cd server && uv run pytest

server-lint: ## 主服务静态检查(ruff + mypy)
	cd server && uv run ruff check . && uv run mypy src

parser-install: ## 安装解析服务依赖
	cd parser && uv sync --locked

parser-gen: ## 由 proto 生成 gRPC 代码(生成物入库,改动 proto 后必跑)
	cd parser && uv run python -m grpc_tools.protoc -I proto --python_out=src/parser_service/gen --grpc_python_out=src/parser_service/gen proto/parser.proto

parser-run: ## 启动解析服务(:50051,Token 见 .env)
	cd parser && uv run python -m parser_service.server

parser-test: parser-gen ## 运行解析服务测试(先重新生成 proto)
	cd parser && uv run pytest

parser-lint: ## 解析服务静态检查
	cd parser && uv run ruff check . && uv run mypy src

web-install: ## 安装前端依赖(npm ci 锁安装)
	cd web && npm ci

web-dev: ## 启动前端开发服务器(:5173)
	cd web && npm run dev

web-build: ## 前端类型检查 + 构建
	cd web && npm run build
