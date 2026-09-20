"""模型目录(OPT-25):数据源为库内自定义模型,内置 YAML 清单已下线。

- 目录是**一次读取的只读快照**:调用方(每请求 / 每 worker 轮)经 `load_from_db`
  构建,解析语义与旧版一致(get_model/default_model/provider_of);
- provider 密钥以 `api_key_encrypted`(enc:v1:…)随快照携带,明文只在真正调用
  模型时经 `CredentialCipher` 解密,目录与任何接口都不回显明文;
- 空目录是合法状态(上线后尚未配置模型):解析不到即 ModelNotConfigured 503,
  fail-closed,不做任何内置兜底 —— 兜底清单等于把"内置模型"换了个地方藏回来。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode

_MODEL_KINDS = ("chat", "embedding", "rerank")


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

    @classmethod
    def load_from_db(cls, session: Session) -> ModelCatalog:
        """整表快照。数据量是"管理员配置的模型数"(十级),无需缓存层。"""
        from app.domain.models import CustomModel, ModelProvider

        id_to_name = {p.id: p.name for p in session.scalars(select(ModelProvider))}
        providers = {
            p.name: ProviderEntry(
                key=p.name,
                protocol=p.protocol,
                base_url=p.base_url,
                api_key_encrypted=p.api_key_encrypted,
                enabled=p.enabled,
            )
            for p in session.scalars(select(ModelProvider))
        }
        models: dict[str, ModelEntry] = {}
        defaults: dict[str, str] = {}
        for m in session.scalars(select(CustomModel)):
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
        return cls(providers=providers, models=models, _defaults=defaults)

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
