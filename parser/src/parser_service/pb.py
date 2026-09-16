"""加载生成的 proto 模块。

grpcio-tools 平铺生成(parser_pb2.py 与 import parser_pb2 绝对导入),
生成物置于 gen/ 且不入包路径,运行时把 gen/ 加入 sys.path 是该生成方式的标准处理。
生成物入库(基准 02),改动 proto 后运行 `make parser-gen` 再生成并提交。
"""

from __future__ import annotations

import sys
from pathlib import Path

_GEN_DIR = Path(__file__).resolve().parent / "gen"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

import parser_pb2 as parser_pb2  # noqa: E402
import parser_pb2_grpc as parser_pb2_grpc  # noqa: E402

__all__ = ["parser_pb2", "parser_pb2_grpc"]
