# WenQu parser 生产镜像(gRPC 解析服务;上下文 = parser/ 目录)。
# 构建:docker build -f parser/Dockerfile parser
# 注意:parser 生产强制 PARSER_REQUIRE_TOKEN=1 由 compose 注入,镜像不内置默认(基准 04 fail-closed)。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /repo
COPY . /repo

RUN uv sync --locked

RUN useradd --uid 10001 --create-home appuser
USER appuser

EXPOSE 50051
CMD ["uv", "run", "--no-sync", "python", "-m", "parser_service.server"]
