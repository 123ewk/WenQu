"""配置 fail-closed 契约:生产缺关键配置必须拒绝启动(基准 02)。

所有 Settings 构造显式 _env_file=None:测试必须对本地 server/.env 免疫,只测代码默认值。
例外要补一刀:`env="prod"` 那条还得分外清掉进程环境里的同名变量 —— `_env_file=None` 挡不住
`os.environ`(本机 source 过 .env 时它会盖过默认值)。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_dev_defaults_pass() -> None:
    settings = Settings(_env_file=None)
    assert settings.env == "dev"


def test_prod_missing_config_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_env_file=None` 只挡得住 .env **文件**,挡不住**进程环境**里的同名变量。

    本机 `set -a && . server/.env` 后跑门禁时,四个必填项已从环境读进来 → "缺配置"根本
    缺不出来,这条会假绿/假红取决于 shell 状态。所以显式删掉它们,让断言测的是代码默认值。
    """
    for key in (
        "APP_DATABASE_URL",
        "APP_JWT_SECRET",
        "APP_MASTER_KEY",
        "PARSER_GRPC_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
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
