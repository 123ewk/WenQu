"""模型清单契约(drift guard 雏形,基准 03 元测试思想):

defaults 引用的模型必须真实存在、模型引用的 provider 必须已注册、
embedding 必须声明维度 —— 改坏 YAML 会在 CI 直接红,而不是运行时才发现。
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "config" / "models.yaml").read_text(encoding="utf-8")
)


def _model_ids(kind: str) -> set[str]:
    return {m["id"] for m in CONFIG["models"].get(kind, [])}


def test_defaults_reference_existing_models() -> None:
    for kind in ("chat", "embedding", "rerank"):
        assert kind in CONFIG["defaults"], f"defaults 缺少 {kind}"
        assert CONFIG["defaults"][kind] in _model_ids(kind), (
            f"defaults.{kind}={CONFIG['defaults'][kind]} 不在模型清单中"
        )


def test_every_model_provider_is_registered() -> None:
    providers = CONFIG["providers"]
    for kind, items in CONFIG["models"].items():
        for model in items:
            assert model["provider"] in providers, (
                f"{kind}/{model['id']} 引用了未注册 provider {model['provider']}"
            )
            entry = providers[model["provider"]]
            assert "base_url" in entry and "api_key_env" in entry


def test_embedding_models_declare_dims() -> None:
    for model in CONFIG["models"]["embedding"]:
        assert isinstance(model.get("dims"), int) and model["dims"] > 0, (
            f"embedding 模型 {model['id']} 未声明 dims"
        )


def test_enabled_providers_have_unique_api_key_env() -> None:
    seen: dict[str, str] = {}
    for name, entry in CONFIG["providers"].items():
        if not entry.get("enabled", False):
            continue
        key_env = entry["api_key_env"]
        if key_env:  # 本地 provider 允许为空
            assert key_env not in seen, (
                f"api_key_env {key_env} 被 {seen.get(key_env)} 与 {name} 重复"
            )
            seen[key_env] = name
