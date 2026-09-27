# WenQu server 生产镜像(Go-Live 翻转项 #1 基础镜像钉 tag / 非 root)。
# 构建上下文 = 仓库根(server 依赖同仓 wenqu-parser 路径包):
#   docker build -f server/Dockerfile .
# 依赖层与源码层分离:先只拷 pyproject/uv.lock 装依赖、再拷源码 —— lock 不变的发版
# 整层命中缓存,不再重下全部包(之前 COPY . 排在 uv sync 前,每次发版重下 ~40min);
# uv 下载缓存挂 BuildKit cache mount,依赖真变了也只下增量。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# ── 依赖层:wenqu-parser 是 editable 路径依赖(../parser),sync 要读其元数据、
#    运行期按路径加载源码,故 parser/ 整体随依赖层进镜像(它极少改动)。
WORKDIR /repo
COPY server/pyproject.toml server/uv.lock /repo/server/
COPY parser/ /repo/parser/

WORKDIR /repo/server
# 依赖按 uv.lock 全量安装进系统 Python(不加 --no-dev:mypy/ruff 随镜像保留,供 exec 排障);
# --no-install-project:此刻 server 源码还没拷进来,先只装第三方依赖
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project

# ── 源码层(每次发版必变):server 源码 + 仓库根 VERSION(config.py 按相对层级读它做版本单一真源)
COPY server/ /repo/server/
COPY VERSION /repo/
# 补装 wenqu-server 自身:依赖已在环境里,零下载
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked

# 非 root 运行(翻转项:全部容器非 root;数据全在外置 pg/minio,无写本地卷需求)
RUN useradd --uid 10001 --create-home appuser
USER appuser

EXPOSE 8000
# 单 worker 是硬约束非调参:OPT-3 事件日志为 API 进程内单例,多进程会破坏续流
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
