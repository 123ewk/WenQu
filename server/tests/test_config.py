"""配置 fail-closed 契约:生产缺关键配置必须拒绝启动(基准 02)。"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_dev_defaults_pass() -> None:
    settings = Settings()
    assert settings.env == "dev"


def test_prod_missing_config_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="fail-closed"):
        Settings(env="prod")


def test_prod_with_all_required_config_passes() -> None:
    settings = Settings(
        env="prod",
        database_url="postgresql+psycopg://u:p@db:5432/wenqu",
        jwt_secret="x" * 32,
        master_key="y" * 32,
        parser_grpc_token="z" * 16,
    )
    assert settings.env == "prod"
