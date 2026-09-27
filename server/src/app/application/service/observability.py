"""OPT-49 Langfuse 观测句柄:问答链路的旁路上报(nil-safe + 失败隔离)。

设计约束(与计划文档 §3/§5 对齐):
- **nil-safe**:未配置/未启用时客户端为 None,`start_qa_trace` 返回空实现句柄,
  且**不 import langfuse**(懒加载)——未接入路径零 import 成本、零行为差异;
- **失败隔离**:客户端构造失败按"本次不可用"缓存到配置指纹变化为止;上报动作
  全部 try/except 吞掉,观测永远不进用户响应;
- **配置读取**走 `langfuse_settings`(进程内 TTL 缓存 + 写时失效,OPT-49 N1);
- **指纹缓存客户端**:配置(host/pk/sk)不变则复用同一 Langfuse 实例(自带后台
  flush 线程),配置变更自动重建;构造失败也缓存,避免每次请求都撞同一堵墙。
"""

from __future__ import annotations

import threading
import uuid

from sqlalchemy.orm import Session

from app.application.service.langfuse_config import langfuse_settings

_lock = threading.Lock()
_client = None  # Langfuse | None
_client_fingerprint: tuple[str, str, str] | None = None


def _langfuse_client(db: Session | None):
    """按配置指纹缓存的单例客户端;未配置/构造失败返回 None(懒加载 langfuse 包)。"""
    global _client, _client_fingerprint
    settings = langfuse_settings(db)
    if settings is None:
        return None
    fingerprint = (settings.host, settings.public_key, settings.secret_key)
    with _lock:
        if _client_fingerprint == fingerprint:
            return _client
        try:
            from langfuse import Langfuse  # 懒加载:未配置路径零 import

            client = Langfuse(
                host=settings.host,
                public_key=settings.public_key,
                secret_key=settings.secret_key,
                timeout=10,
            )
        except Exception:  # noqa: BLE001 — 观测可用性绝不影响主链路
            client = None
        _client, _client_fingerprint = client, fingerprint
        return client


class _NullQaTrace:
    """未接入时的空实现:所有方法零操作(不 import langfuse)。"""

    def finish(self, answer: str | None = None, error: str | None = None) -> None: ...


class _LangfuseQaTrace:
    """一条用户问答 = 一个 trace;input 在创建时写,finish 补 output。"""

    def __init__(self, client, *, user_id, space_id, question, kb_ids, model_id, agent) -> None:
        self._trace = client.trace(
            name="qa",
            input={"question": question},
            metadata={
                "user_id": str(user_id),
                "space_id": str(space_id),
                "kb_ids": [str(k) for k in kb_ids] if kb_ids else None,
                "model_id": model_id,
                "agent": agent,
            },
        )

    def finish(self, answer: str | None = None, error: str | None = None) -> None:
        try:
            metadata = {"error": error} if error else None
            self._trace.update(output=answer, metadata=metadata)
        except Exception:  # noqa: BLE001 — 上报失败静默丢弃
            pass


def start_qa_trace(
    db: Session | None,
    *,
    user_id: uuid.UUID,
    space_id: uuid.UUID,
    question: str,
    kb_ids: list[uuid.UUID] | None,
    model_id: str | None,
    agent: bool,
):
    """问答入口调用:已接入返回 Langfuse 句柄,未接入返回零操作句柄。"""
    client = _langfuse_client(db)
    if client is None:
        return _NullQaTrace()
    try:
        return _LangfuseQaTrace(
            client, user_id=user_id, space_id=space_id, question=question,
            kb_ids=kb_ids, model_id=model_id, agent=agent,
        )
    except Exception:  # noqa: BLE001
        return _NullQaTrace()
