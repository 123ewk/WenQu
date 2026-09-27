# WenQu parser 生产镜像(gRPC 解析服务;上下文 = parser/ 目录)。
# 构建:docker build -f parser/Dockerfile parser
# 注意:parser 生产强制 PARSER_REQUIRE_TOKEN=1 由 compose 注入,镜像不内置默认(基准 04 fail-closed)。
# 依赖层与源码层分离 + uv 下载缓存挂 cache mount(理由同 server/Dockerfile)。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /repo
COPY pyproject.toml uv.lock /repo/
# --no-install-project:此刻 parser 源码还没拷进来,先只装第三方依赖
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project

COPY . /repo
# 补装 wenqu-parser 自身:依赖已在环境里,零下载
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked

RUN useradd --uid 10001 --create-home appuser
USER appuser

EXPOSE 50051
CMD ["uv", "run", "--no-sync", "python", "-m", "parser_service.server"]
