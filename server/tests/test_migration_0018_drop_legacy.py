"""迁移 0018 删旧列 + 改名(真库 + 自建容器):把 Key 退化成"档位的凭据"这一刀的形状不变量。

0017 只加不删,0018 才是让旧代码跑不动的那一刀,风险点全在"删"和"改名"上:

1. **删干净**:`capabilities` / `kb_ids` / `is_active` 留任何一份副本,就还是两套权限口径
   (改了档位忘改副本 —— 正是 OPT-41 要消灭的东西),删漏比不删更糟;
2. **改名不是重建**:`member_user_id → owner_user_id` 必须原样带走数据、NULL 语义和 FK,
   用 drop+add 实现改名会静默清空"这把 Key 以谁的身份行事";
3. **旧索引跟着走**:`uq_api_keys_one_active_per_member` 依赖 `is_active`,不先拆就删列,
   Postgres 直接报错;拆了但约束名遗留则会让下一个读 schema 的人找错列;
4. **downgrade 权限逐字还原**:回滚后旧代码靠旧列继续跑,`all_kbs=true` 必须翻译回
   旧口径的"空 kb_ids = 不限制",而不是留个空数组让人以为"没权限";
   `is_active` 按 current 指针重新派生 —— 它只是管理页标记,不参与认证。

断言一律写**绝对期望值**而不是"和档位比一比":NOT NULL + server_default 会让
"回填一行都没跑"这种事故在相对比对里看起来完全正确。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

# 复用 0017 的存量夹具:同一批数据、同一套 UUID,回滚断言才有可比性
from tests.test_migration_0017_backfill import (
    K1,
    K2,
    K3,
    K4,
    K5,
    KB_X,
    SPACE,
    U1,
    U2,
    U3,
    _alembic_config,
    _seed_legacy,
)

SERVER_DIR = Path(__file__).resolve().parents[1]

# 0018 之后 api_keys 应该剩下的列(旧列删漏即红)
_EXPECTED_COLS = {
    "id",
    "space_id",
    "created_by",
    "owner_user_id",
    "name",
    "description",
    "key_hash",
    "key_hint",
    "group_id",
    "revoked_at",
    "last_used_at",
    "created_at",
    "updated_at",
}

# 回滚后每把存量 Key 的旧列期望值(绝对值,见模块 docstring 第 4 点)
_LEGACY_EXPECTED: dict[uuid.UUID, tuple[list[str], list[str], bool]] = {
    K1: (["chat"], [], False),  # 曾是 U1 的使用中,但 current 已让位给更晚的 K4
    K2: (["chat", "documents"], [KB_X], False),
    K3: ([], [], False),  # 已吊销:旧口径下也不该有使用中语义
    K4: (["chat"], [KB_X], True),  # U1 的 current 档位
    K5: (["chat"], [], False),  # U3 没有任何 current
}


def _schema(url: str) -> dict[str, set[str]]:
    """采集 api_keys 的表/列/索引/FK 名;引擎用完即释,不留悬空连接。"""
    engine = sa.create_engine(url)
    try:
        insp = sa.inspect(engine)
        return {
            "tables": set(insp.get_table_names()),
            "cols": {c["name"] for c in insp.get_columns("api_keys")},
            "idx": {i["name"] for i in insp.get_indexes("api_keys")},
            "fks": {f["name"] for f in insp.get_foreign_keys("api_keys")},
            "notnull": {
                c["name"]: c["nullable"] is False for c in insp.get_columns("api_keys")
            },
        }
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def at_0018() -> Iterator[sa.Engine]:
    """升到 0016 塞存量 → 升到 0018,用例看到的即"删列完成后的生产库"。"""
    if shutil.which("docker") is None:
        pytest.skip("docker 不可用,跳过迁移 0018 测试")
    try:
        try:  # testcontainers 4.9+ 将社区模块迁移到 community 命名空间
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
        container.start()
    except Exception as exc:  # noqa: BLE001 — 环境不可用即跳过
        pytest.skip(f"无法启动 postgres 测试容器: {exc}")

    url = container.get_connection_url()
    # alembic/env.py 只认 APP_DATABASE_URL;本夹具不碰 app 的引擎缓存,用完原值归还
    prior = os.environ.get("APP_DATABASE_URL")
    os.environ["APP_DATABASE_URL"] = url
    try:
        from alembic import command

        command.upgrade(_alembic_config(), "0016")
        engine = sa.create_engine(url)
        try:
            with engine.begin() as conn:
                _seed_legacy(conn)
            # 钉死 0018 而不是 head:本模块断言的是这一刀的刀口形状
            command.upgrade(_alembic_config(), "0018")
            yield engine
        finally:
            engine.dispose()
    finally:
        if prior is None:
            os.environ.pop("APP_DATABASE_URL", None)
        else:
            os.environ["APP_DATABASE_URL"] = prior
        container.stop()


def test_legacy_permission_columns_are_gone(at_0018: sa.Engine) -> None:
    """三列必须彻底消失,且 api_keys 的列集合与模型一字不差。"""
    schema = _schema(os.environ["APP_DATABASE_URL"])
    assert "permission_groups" in schema["tables"], "0017 的表没被保留"
    leftover = {"capabilities", "kb_ids", "is_active", "member_user_id"} & schema["cols"]
    assert not leftover, f"旧列删漏,权限副本仍在: {leftover}"
    assert schema["cols"] == _EXPECTED_COLS, (
        f"api_keys 列集合漂移: 多 {schema['cols'] - _EXPECTED_COLS},"
        f" 少 {_EXPECTED_COLS - schema['cols']}"
    )


def test_rename_kept_data_and_null_semantics(at_0018: sa.Engine) -> None:
    """改名必须原样带走数据:owner_user_id 有值的照旧,该为 NULL 的照旧为 NULL。"""
    with at_0018.connect() as conn:
        rows = {
            r.id: (r.owner_user_id, r.group_id)
            for r in conn.execute(
                sa.text("SELECT id, owner_user_id, group_id FROM api_keys")
            ).all()
        }
    assert set(rows) == {K1, K2, K3, K4, K5}, "改名过程中丢了行"
    assert rows[K1] == (U1, K1), "K1 的归属人应随改名保留"
    assert rows[K2][0] == U2 and rows[K3][0] == U3
    # member_user_id 为 NULL 的两把:回退 created_by 的语义在 owner_user_id 上仍是 NULL
    assert rows[K4][0] is None and rows[K5][0] is None, "改名不该顺手把 NULL 填成 created_by"
    assert rows[K4][1] == K4 and rows[K1][1] == K1, "回填的 group_id 必须跟着保留"


def test_old_partial_unique_index_dropped_and_fk_renamed(at_0018: sa.Engine) -> None:
    """依赖 is_active 的旧唯一索引必须消失;FK 名要跟新列一致,否则下一个读 schema 的人找错列。"""
    schema = _schema(os.environ["APP_DATABASE_URL"])
    assert "uq_api_keys_one_active_per_member" not in schema["idx"], (
        f"旧索引仍在(依赖已删的 is_active): {schema['idx']}"
    )
    # 约束名沿用 0012 的显式命名(fk_api_keys_member_user),不是 Postgres 默认名
    assert "fk_api_keys_owner_user" in schema["fks"], f"FK 未改名: {schema['fks']}"
    assert "fk_api_keys_member_user" not in schema["fks"], f"留下了幽灵约束名: {schema['fks']}"


def test_group_id_still_required_after_drop(at_0018: sa.Engine) -> None:
    """删列不能顺手把 0017 的 NOT NULL 松掉 —— 漏写 group_id 要在库层就炸。"""
    url_schema = _schema(os.environ["APP_DATABASE_URL"])
    assert url_schema["notnull"]["group_id"] is True, "group_id 变成了可空"
    with pytest.raises(sa.exc.IntegrityError):
        with at_0018.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO api_keys"
                    " (id, space_id, created_by, owner_user_id, name, description,"
                    "  key_hash, key_hint, revoked_at, created_at, updated_at)"
                    " VALUES (:id, :s, :u, :u, '无档位', '', :h, 'sk-****0000',"
                    "  NULL, now(), now())"
                ),
                {"id": uuid.uuid4(), "s": SPACE, "u": U1, "h": "x" * 64},
            )


# 放在最后:这条会改 schema 再改回来,别影响上面的形状断言
def test_downgrade_restores_legacy_columns_verbatim(at_0018: sa.Engine) -> None:
    """回滚到 0017:权限逐字还原、is_active 按 current 派生、旧索引重建,旧代码可继续跑。"""
    from alembic import command

    url = os.environ["APP_DATABASE_URL"]
    try:
        command.downgrade(_alembic_config(), "0017")
        schema = _schema(url)
        assert {"capabilities", "kb_ids", "is_active", "member_user_id"} <= schema["cols"], (
            f"旧列没还原: {schema['cols']}"
        )
        assert "fk_api_keys_member_user" in schema["fks"], "FK 名没跟着改回去"
        assert "uq_api_keys_one_active_per_member" in schema["idx"], "旧唯一索引没重建"

        with at_0018.connect() as conn:
            rows = {
                r.id: (json.loads(r.capabilities), json.loads(r.kb_ids), r.is_active)
                for r in conn.execute(
                    sa.text(
                        "SELECT id, capabilities::text, kb_ids::text, is_active FROM api_keys"
                    )
                ).all()
            }
            # 档位上的"全部库"标记必须翻译回旧口径的空列表,而不是照搬 true
            all_kbs_left = conn.execute(
                sa.text(
                    "SELECT count(*) FROM api_keys k"
                    " JOIN permission_groups g ON g.id = k.group_id"
                    " WHERE g.all_kbs AND k.kb_ids <> '[]'::jsonb"
                )
            ).scalar_one()
        assert all_kbs_left == 0, "all_kbs 档位回滚后 kb_ids 没写成空列表"
        for key, expected in _LEGACY_EXPECTED.items():
            assert rows[key] == expected, f"{key} 回滚后与期望不符: {rows[key]} != {expected}"
    finally:
        command.upgrade(_alembic_config(), "0018")
    assert "is_active" not in _schema(url)["cols"], "再次升级没成立"
