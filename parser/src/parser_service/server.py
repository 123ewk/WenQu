"""WenQu 文档解析服务(gRPC)M0 骨架。

安全底线(基准 04):
- 解析服务处理未加密原始文档,生产强制 Token 认证(常数时间比较);
  PARSER_REQUIRE_TOKEN=1 且未配置 Token 时拒绝启动(fail-closed)。
- TLS/mTLS 与 Token 的组合校验随 M2 完整实现(启用 TLS 时缺配置拒绝明文启动)。
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
from concurrent import futures

import grpc

from parser_service.pb import parser_pb2, parser_pb2_grpc

logger = logging.getLogger("parser_service")

_TOKEN_METADATA_KEY = "x-parser-token"
_PLAIN_TEXT_FORMATS = frozenset({"txt", "md", "markdown"})


class _TokenInterceptor(grpc.ServerInterceptor):
    """全局 Token 认证:元数据缺失或不匹配一律 UNAUTHENTICATED。"""

    def __init__(self, token: str) -> None:
        self._token = token.encode()
        self._deny = grpc.unary_unary_rpc_method_handler(self._abort_unauthenticated)

    def _abort_unauthenticated(self, request: object, context: grpc.ServicerContext) -> None:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid parser token")

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata or ())
        supplied = metadata.get(_TOKEN_METADATA_KEY, "")
        # 常数时间比较,防时序侧信道(基准 04)
        if hmac.compare_digest(supplied.encode(), self._token):
            return continuation(handler_call_details)
        return self._deny


class ParserService(parser_pb2_grpc.ParserServiceServicer):
    """M0:纯文本直通解析(验证契约与链路);PDF/Office/表格解析器 M2 接入。"""

    def Parse(
        self, request: parser_pb2.ParseRequest, context: grpc.ServicerContext
    ) -> parser_pb2.ParseResponse:
        if request.format not in _PLAIN_TEXT_FORMATS:
            context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                f"format={request.format!r} 的解析器随 M2 交付",
            )
        text = request.content.decode("utf-8", errors="replace")
        blocks = [
            parser_pb2.Block(type="paragraph", text=segment.strip())
            for segment in text.split("\n\n")
            if segment.strip()
        ]
        return parser_pb2.ParseResponse(document_id=request.document_id, blocks=blocks)


def serve(listen_addr: str, token: str) -> None:
    interceptors: list[grpc.ServerInterceptor] = []
    if token:
        interceptors.append(_TokenInterceptor(token))
    else:
        logger.warning("PARSER_GRPC_TOKEN 未配置:已放行全部请求,仅限开发环境")

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4), interceptors=interceptors
    )
    parser_pb2_grpc.add_ParserServiceServicer_to_server(ParserService(), server)
    port = server.add_insecure_port(listen_addr)
    server.start()
    logger.info("parser listening on %s (port=%d)", listen_addr, port)
    server.wait_for_termination()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    arg_parser = argparse.ArgumentParser(description="WenQu 文档解析服务")
    arg_parser.add_argument("--addr", default=os.environ.get("PARSER_LISTEN_ADDR", ":50051"))
    args = arg_parser.parse_args()

    token = os.environ.get("PARSER_GRPC_TOKEN", "")
    if os.environ.get("PARSER_REQUIRE_TOKEN", "") == "1" and not token:
        raise SystemExit(
            "PARSER_REQUIRE_TOKEN=1 但未配置 PARSER_GRPC_TOKEN,拒绝启动(基准 04 fail-closed)"
        )
    serve(args.addr, token)


if __name__ == "__main__":
    main()
