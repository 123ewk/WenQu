# WenQu server 生产镜像(Go-Live 翻转项 #1 基础镜像钉 tag / 非 root)。
# 构建上下文 = 仓库根(server 依赖同仓 wenqu-parser 路径包):
#   docker build -f server/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /repo
COPY . /repo
# uv 项目根在 server/(pyproject+uv.lock),路径包 ../parser 同上下文可用
WORKDIR /repo/server

# 依赖按 uv.lock 全量安装进系统 Python(不加 --no-dev:mypy/ruff 随镜像保留,供 exec 排障)
RUN uv sync --locked

# 非 root 运行(翻转项:全部容器非 root;数据全在外置 pg/minio,无写本地卷需求)
RUN useradd --uid 10001 --create-home appuser
USER appuser

EXPOSE 8000
# 单 worker 是硬约束非调参:OPT-3 事件日志为 API 进程内单例,多进程会破坏续流
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
