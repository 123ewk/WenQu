"""OPT-21 回归:no_model_keys 夹具的契约 —— 夹具内两条密钥渠道必须是死的。

背景:4 个"模拟未配置"用例曾用 os.environ.pop 删进程变量,但 resolve_api_key
会回落读 cwd 下的 .env,本地 server/.env 有真实密钥时误打真模型(实测
dashscope 200)。CI 没有 .env,复现不了 —— 本测试把夹具契约钉在与机器无关的
断言上,防止将来退化回"只删环境变量"的写法。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.model_client import ModelNotConfigured, resolve_api_key


def test_no_model_keys_leaves_no_key_channel(no_model_keys) -> None:
    assert os.environ.get("DASHSCOPE_API_KEY", "") == ""
    assert os.environ.get("DEEPSEEK_API_KEY", "") == ""
    assert not Path(".env").exists()  # cwd 已被指到无 .env 的临时目录
    provider = SimpleNamespace(api_key_env="DASHSCOPE_API_KEY")
    with pytest.raises(ModelNotConfigured, match="DASHSCOPE_API_KEY"):
        resolve_api_key(provider, get_settings())
