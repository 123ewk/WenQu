"""模型目录(OPT-25;OPT-27 空间分层):库内模型 = 全局层 + 当前空间层两份合并快照。

- 目录是**一次读取的只读快照**:调用方(每请求 / 每 worker 轮)经 `load_from_db(session,
  space_id)` 构建;space_id=None 只含全局层(superuser /admin/* 管的),非空则"全局 ∪ 空间",
  同名/同 id **空间行遮蔽全局行**,默认解析"空间默认 → 全局默认";
- embedding 特殊(OPT-27 用户裁决):模型本体内置锁死(换模型 = 已入库向量作废),
  空间配了自己的 Key 就在快照里**合成**内置条目并顶为默认;未配 Key 时
  **dev 回落全局层 embedding(WARNING 留痕),prod 直接 503**(全局 embedding 行不进快照);
- provider 密钥以 `api_key_encrypted`(enc:v1:…)随快照携带,明文只在真正调用
  模型时经 `CredentialCipher` 解密,目录与任何接口都不回显明文;
- 空目录是合法状态:解析不到即 ModelNotConfigured 503,fail-closed,不做任何内置兜底
  —— 兜底清单等于把"内置模型"换了个地方藏回来(embedding 内置是用户点名的例外)。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger("app.model_catalog")

_MODEL_KINDS = ("chat", "embedding", "rerank")

# 内置 embedding(OPT-27):模型本体进代码常量,空间只提供它的 Key
BUILTIN_EMBEDDING_PROVIDER_KEY = "builtin-dashscope"
BUILTIN_EMBEDDING_MODEL_ID = "builtin/text-embedding-v3"
BUILTIN_EMBEDDING_MODEL_NAME = "text-embedding-v3"
BUILTIN_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BUILTIN_EMBEDDING_DIMS = 1024
BUILTIN_EMBEDDING_DOC_URL = "https://bailian.console.aliyun.com/"


@dataclass(frozen=True)
class ProviderEntry:
    key: str  # 供应商名(唯一),兼作契约里的 provider_key 分组键
    protocol: str  # 协议注册表键(app.core.model_protocols),调用时分发依据
    base_url: str
    api_key_encrypted: str  # enc:v1:… 密文;空串 = 本地免密钥端点(ollama/vllm)
    enabled: bool


@dataclass(frozen=True)
class ModelEntry:
    id: str  # 对外模型 id(custom_models.model_id,进契约/落 messages/kb 配置)
    kind: str  # chat | embedding | rerank
    provider_key: str
    model: str  # 上游真实模型名/接入点 id
    dims: int | None = None
    context_tokens: int | None = None
    enabled: bool = True


class ModelNotConfigured(AppError):
    """模型/供应商未配置或密钥缺失。

    属**服务端配置问题**(用户无法自救),故 503 而非 4xx;继承 AppError 是为了走
    统一错误壳 —— 此前它是裸 Exception,直接落到 500 INTERNAL_ERROR,前端无法按
    code 分支(标准 §5.4)。
    """

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.MODEL_NOT_CONFIGURED, message, http_status=503)


@dataclass
class ModelCatalog:
    providers: dict[str, ProviderEntry]
    models: dict[str, ModelEntry]
    _defaults: dict[str, str]
    space_id: uuid.UUID | None = None
    # OPT-27:该空间是否启用重排(快照随带,检索挂点零额外查询)
    rerank_enabled: bool = False

    @classmethod
    def load_from_db(cls, session: Session, space_id: uuid.UUID | None = None) -> ModelCatalog:
        """整表快照(OPT-27):space_id=None 仅全局层;否则"全局 ∪ 空间",空间行遮蔽全局行。

        数据量是"管理员配置的模型数"(十级),无需缓存层。
        """
        from app.domain.models import CustomModel, ModelProvider, SpaceModelSettings

        settings = get_settings()
        providers: dict[str, ProviderEntry] = {}
        id_to_name: dict[uuid.UUID, str] = {}
        models: dict[str, ModelEntry] = {}
        defaults: dict[str, str] = {}

        def _layer(scope: uuid.UUID | None) -> None:
            nonlocal providers, id_to_name
            rows = list(
                session.scalars(
                    select(ModelProvider).where(
                        ModelProvider.space_id.is_(None)
                        if scope is None
                        else ModelProvider.space_id == scope
                    )
                )
            )
            for p in rows:
                providers[p.name] = ProviderEntry(
                    key=p.name,
                    protocol=p.protocol,
                    base_url=p.base_url,
                    api_key_encrypted=p.api_key_encrypted,
                    enabled=p.enabled,
                )
                id_to_name[p.id] = p.name
            for m in session.scalars(
                select(CustomModel).where(
                    CustomModel.space_id.is_(None)
                    if scope is None
                    else CustomModel.space_id == scope
                )
            ):
                # prod 空间快照不收编全局 embedding:空间必须自配 Key(用户裁决 2026-09-20)
                if (
                    scope is None
                    and space_id is not None
                    and settings.env == "prod"
                    and m.kind == "embedding"
                ):
                    continue
                provider_key = id_to_name.get(m.provider_id, "")
                models[m.model_id] = ModelEntry(
                    id=m.model_id,
                    kind=m.kind,
                    provider_key=provider_key,
                    model=m.model_name,
                    dims=m.dims,
                    context_tokens=m.context_tokens,
                    enabled=m.enabled,
                )
                if m.is_default:
                    provider = providers.get(provider_key)
                    if m.enabled and provider is not None and provider.enabled:
                        defaults[m.kind] = m.model_id

        _layer(None)  # 先全局
        rerank_enabled = False
        if space_id is not None:
            space_settings = session.get(SpaceModelSettings, space_id)
            rerank_enabled = bool(
                space_settings is not None and space_settings.rerank_enabled
            )
            _layer(space_id)  # 空间行遮蔽全局同名行
            if space_settings is not None and space_settings.embedding_key_encrypted:
                # 空间自配 Key → 合成内置 embedding 并顶为默认(遮蔽全局默认)
                providers[BUILTIN_EMBEDDING_PROVIDER_KEY] = ProviderEntry(
                    key=BUILTIN_EMBEDDING_PROVIDER_KEY,
                    protocol="openai_compatible",
                    base_url=BUILTIN_EMBEDDING_BASE_URL,
                    api_key_encrypted=space_settings.embedding_key_encrypted,
                    enabled=True,
                )
                models[BUILTIN_EMBEDDING_MODEL_ID] = ModelEntry(
                    id=BUILTIN_EMBEDDING_MODEL_ID,
                    kind="embedding",
                    provider_key=BUILTIN_EMBEDDING_PROVIDER_KEY,
                    model=BUILTIN_EMBEDDING_MODEL_NAME,
                    dims=BUILTIN_EMBEDDING_DIMS,
                    enabled=True,
                )
                defaults["embedding"] = BUILTIN_EMBEDDING_MODEL_ID
            elif defaults.get("embedding"):
                # 仅剩全局 embedding 默认 = dev 回落(留痕;prod 上面已被排除)
                logger.warning(
                    "space %s 未配置向量化 Key,回落全局 embedding 默认(dev 专属路径)",
                    space_id,
                )
        return cls(
            providers=providers,
            models=models,
            _defaults=defaults,
            space_id=space_id,
            rerank_enabled=rerank_enabled,
        )

    def enabled_models_by_kind(self) -> dict[str, list[ModelEntry]]:
        """按用途列出**可用**模型(模型启用且其 provider 启用)—— 供前端选择器。"""
        result: dict[str, list[ModelEntry]] = {kind: [] for kind in _MODEL_KINDS}
        for model in self.models.values():
            if not model.enabled:
                continue
            provider = self.providers.get(model.provider_key)
            if provider is None or not provider.enabled:
                continue
            result.setdefault(model.kind, []).append(model)
        for entries in result.values():
            entries.sort(key=lambda item: item.id)
        return result

    def provider_display_name(self, provider_key: str) -> str:
        """供应商显示名(用于前端下拉分组);库内 name 即显示名,缺失时回退 key。"""
        return provider_key

    def defaults(self) -> dict[str, str]:
        return dict(self._defaults)

    def default_id(self, kind: str) -> str | None:
        """该 kind 的默认模型 id;未配置返回 None(不抛错,供"可空落库"场景使用)。"""
        return self._defaults.get(kind)

    def default_model(self, kind: str) -> ModelEntry:
        model_id = self._defaults.get(kind)
        if not model_id:
            if kind == "embedding" and self.space_id is not None:
                raise ModelNotConfigured(
                    "该空间未配置向量化 Key(内置模型 text-embedding-v3),"
                    "请到 空间设置 › 模型配置 填入 API Key"
                )
            raise ModelNotConfigured(f"未配置默认的 {kind} 模型(管理端勾选 is_default)")
        return self.get_model(model_id, kind)

    def get_model(self, model_id: str, kind: str | None = None) -> ModelEntry:
        model = self.models.get(model_id)
        if model is None:
            raise ModelNotConfigured(f"模型 {model_id} 不在清单中")
        if kind is not None and model.kind != kind:
            raise ModelNotConfigured(f"模型 {model_id} 不是 {kind} 类型")
        if not model.enabled:
            raise ModelNotConfigured(f"模型 {model_id} 未启用")
        provider = self.providers.get(model.provider_key)
        if provider is None or not provider.enabled:
            raise ModelNotConfigured(f"模型 {model_id} 的供应商不可用")
        return model

    def get_provider(self, provider_key: str) -> ProviderEntry:
        """按 key 取供应商(含未启用者):密钥解析与运行时可用性是两件事。"""
        provider = self.providers.get(provider_key)
        if provider is None:
            raise ModelNotConfigured(f"供应商 {provider_key} 未注册")
        return provider

    def provider_of(self, model: ModelEntry) -> ProviderEntry:
        provider = self.providers.get(model.provider_key)
        if provider is None:
            raise ModelNotConfigured(f"模型 {model.id} 的供应商不存在")
        if not provider.enabled:
            raise ModelNotConfigured(f"供应商 {provider.key} 未启用")
        return provider
