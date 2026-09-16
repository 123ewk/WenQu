"""解析服务 gRPC 网关:server → parser(独立进程,ADR-7)。

proto 生成物来自 wenqu-parser 包(monorepo 路径依赖),契约由 parser 侧
契约测试钉住;Token 元数据与 parser 服务端拦截器对应。
"""

from __future__ import annotations

import logging

import grpc
from parser_service.pb import parser_pb2, parser_pb2_grpc

logger = logging.getLogger("app.parser_client")

_TOKEN_METADATA_KEY = "x-parser-token"


class ParserUnavailableError(Exception):
    """解析服务不可达/超时(流水线按可重试错误处理)。"""


class ParserRejectedError(Exception):
    """解析服务明确拒绝(格式不支持/脏文档):不可重试。"""


class ParserClient:
    def __init__(self, addr: str, token: str, timeout_s: float = 120.0) -> None:
        self._addr = addr
        self._token = token
        self._timeout_s = timeout_s

    def parse(
        self, document_id: str, fmt: str, content: bytes
    ) -> tuple[list[parser_pb2.Block], dict[str, str]]:
        try:
            with grpc.insecure_channel(self._addr) as channel:
                stub = parser_pb2_grpc.ParserServiceStub(channel)
                response = stub.Parse(
                    parser_pb2.ParseRequest(
                        document_id=document_id, format=fmt, content=content
                    ),
                    timeout=self._timeout_s,
                    metadata=((_TOKEN_METADATA_KEY, self._token),),
                )
        except grpc.RpcError as exc:
            code = exc.code()
            if code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.DATA_LOSS):
                raise ParserRejectedError(f"{code.name}: {exc.details()}") from exc
            raise ParserUnavailableError(f"{code.name}: {exc.details()}") from exc
        blocks = list(response.blocks)
        meta = dict(response.meta)
        logger.info("parsed document_id=%s blocks=%d", document_id, len(blocks))
        return blocks, meta
