"""配置 fail-closed 契约:生产缺关键配置必须拒绝启动(基准 02)。

所有 Settings 构造显式 _env_file=None:测试必须对本地 server/.env 免疫,只测代码默认值。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_dev_defaults_pass() -> None:
    settings = Settings(_env_file=None)
    assert settings.env == "dev"


def test_prod_missing_config_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="fail-closed"):
        Settings(env="prod", _env_file=None)


def test_prod_with_all_required_config_passes() -> None:
    settings = Settings(
        env="prod",
        database_url="postgresql+psycopg://u:p@db:5432/wenqu",
        jwt_secret="x" * 32,
        master_key="y" * 32,
        parser_grpc_token="z" * 16,
        _env_file=None,
    )
    assert settings.env == "prod"
