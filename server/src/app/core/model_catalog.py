"""模型目录加载(ADR-4):providers/models/defaults 声明式 YAML,密钥永不进本文件。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ProviderEntry:
    key: str
    base_url: str
    api_key_env: str
    enabled: bool


@dataclass(frozen=True)
class ModelEntry:
    id: str
    kind: str  # chat | embedding | rerank
    provider_key: str
    model: str
    dims: int | None = None
    context_tokens: int | None = None
    enabled: bool = True


class ModelNotConfigured(Exception):
    """模型/供应商未配置或密钥缺失(6xxx 域,面向调用方 503 语义)。"""


def _default_config_path() -> Path:
    # src/app/core/model_catalog.py → parents[3] = server/,config 固定随包走
    return Path(__file__).resolve().parents[3] / "config" / "models.yaml"


class ModelCatalog:
    def __init__(self, data: dict[str, Any]) -> None:
        self._providers: dict[str, ProviderEntry] = {}
        for key, entry in data.get("providers", {}).items():
            self._providers[key] = ProviderEntry(
                key=key,
                base_url=entry["base_url"],
                api_key_env=entry.get("api_key_env", ""),
                enabled=bool(entry.get("enabled", False)),
            )
        self._models: dict[str, ModelEntry] = {}
        for kind, items in data.get("models", {}).items():
            for item in items:
                self._models[item["id"]] = ModelEntry(
                    id=item["id"],
                    kind=kind,
                    provider_key=item["provider"],
                    model=item["model"],
                    dims=item.get("dims"),
                    context_tokens=item.get("context_tokens"),
                    enabled=bool(item.get("enabled", True)),
                )
        defaults = data.get("defaults", {})
        self._defaults: dict[str, str] = {
            kind: defaults[kind] for kind in ("chat", "embedding", "rerank") if kind in defaults
        }

    @classmethod
    def load(cls, path: Path | None = None) -> ModelCatalog:
        config_path = path or _default_config_path()
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return cls(data)

    def default_model(self, kind: str) -> ModelEntry:
        model_id = self._defaults.get(kind)
        if not model_id:
            raise ModelNotConfigured(f"defaults.{kind} 未配置")
        return self.get_model(model_id, kind)

    def get_model(self, model_id: str, kind: str | None = None) -> ModelEntry:
        model = self._models.get(model_id)
        if model is None:
            raise ModelNotConfigured(f"模型 {model_id} 不在清单中")
        if kind is not None and model.kind != kind:
            raise ModelNotConfigured(f"模型 {model_id} 不是 {kind} 类型")
        if not model.enabled:
            raise ModelNotConfigured(f"模型 {model_id} 未启用")
        provider = self._providers.get(model.provider_key)
        if provider is None or not provider.enabled:
            raise ModelNotConfigured(f"模型 {model_id} 的供应商不可用")
        return model

    def get_provider(self, provider_key: str) -> ProviderEntry:
        """按 key 取供应商(含未启用者):密钥解析与运行时可用性是两件事。"""
        provider = self._providers.get(provider_key)
        if provider is None:
            raise ModelNotConfigured(f"供应商 {provider_key} 未注册")
        return provider

    def provider_of(self, model: ModelEntry) -> ProviderEntry:
        provider = self._providers[model.provider_key]
        if not provider.enabled:
            raise ModelNotConfigured(f"供应商 {provider.key} 未启用")
        return provider
