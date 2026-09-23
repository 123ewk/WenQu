"""迁移 0017 回填不变量(真库 + 自建容器):存量"一 Key 一成员"翻译成"一档一凭据"必须逐行等价。

回填只跑一次、跑在生产数据上,而旧代码此后仍读旧列 —— 回填写错不会立刻暴露,所以把风险点
逐个钉死:

1. **不丢权限**:每把 Key 的档位能力/范围与旧列逐字相同,`all_kbs` 正确表达旧的"空 = 全部";
2. **不漏人不串人**:绑定成员(含 `member_user_id` 为 NULL 时回退 `created_by`)都有挂载行,
   且挂载行的 `space_id` 与 Key 同空间;
3. **current 指针口径**:只从**未吊销且旧 `is_active`** 的行派生;同一人按 COALESCE 归并后只留
   `created_at` 最新的一档 —— 旧的部分唯一索引只约束 `member_user_id` 列本身,回退口径下同一
   物理人可能出现两条 current,回填必须自己收敛;
4. **新约束真的生效**:每人至多一档 current、`api_keys.group_id` 非空、档位被 Key 引用时不可删;
5. **可回滚**:downgrade 到 0016 后新表与新列彻底消失,旧列原样保留(旧代码要靠它跑)。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

SERVER_DIR = Path(__file__).resolve().parents[1]

_USER_COLS = "(id, username, nickname, password_hash)"
_KEY_COLS = """(
    id, space_id, created_by, member_user_id, name, description,
    key_hash, key_hint, capabilities, kb_ids, is_active, revoked_at,
    created_at, updated_at
)"""

SPACE = uuid.uuid4()
U1 = uuid.uuid4()
U2 = uuid.uuid4()
U3 = uuid.uuid4()
# 五把存量 Key 覆盖回填的全部分支
K1 = uuid.uuid4()  # 成员 U1,空范围,使用中 → 档位 all_kbs=true;曾是 U1 的 current
K2 = uuid.uuid4()  # 成员 U2,限定一个库,未使用 → 不产 current
K3 = uuid.uuid4()  # 成员 U3,使用中但已吊销 → 不产 current
K4 = uuid.uuid4()  # member_user_id NULL + created_by U1,使用中且更晚 → U1 的 current 归它
K5 = uuid.uuid4()  # member_user_id NULL + created_by U3,未使用 → U3 无 current
KB_X = str(uuid.uuid4())


def _alembic_config() -> object:
    from alembic.config import Config

    cfg = Config(SERVER_DIR.joinpath("alembic.ini").as_posix())
    cfg.set_main_option("script_location", SERVER_DIR.joinpath("alembic").as_posix())
    return cfg


def _ts(day: int) -> datetime:
    return datetime(2026, 9, day, 8, 0, 0, tzinfo=UTC)


def _inspect(url: str) -> tuple[set[str], set[str]]:
    """返回 (库内表名, api_keys 列名);引擎用完即释,不留悬空连接。"""
    engine = sa.create_engine(url)
    try:
        insp = sa.inspect(engine)
        return (
            set(insp.get_table_names()),
            {c["name"] for c in insp.get_columns("api_keys")},
        )
    finally:
        engine.dispose()


def _insert_key(
    conn: sa.Connection,
    *,
    key_id: uuid.UUID,
    member_user_id: uuid.UUID | None,
    created_by: uuid.UUID,
    name: str,
    capabilities: list[str],
    kb_ids: list[str],
    is_active: bool,
    revoked_at: datetime | None,
    created_at: datetime,
) -> None:
    conn.execute(
        sa.text(
            f"""
            INSERT INTO api_keys {_KEY_COLS}
            VALUES (:id, :space_id, :created_by, :member_user_id, :name, '',
                    :key_hash, :key_hint, CAST(:caps AS jsonb), CAST(:kb_ids AS jsonb),
                    :is_active, :revoked_at, :created_at, :created_at)
            """
        ),
        {
            "id": key_id,
            "space_id": SPACE,
            "created_by": created_by,
            "member_user_id": member_user_id,
            "name": name,
            "key_hash": f"{key_id.hex}h",
            "key_hint": f"sk-****{str(key_id)[:4]}",
            "caps": json.dumps(capabilities),
            "kb_ids": json.dumps(kb_ids),
            "is_active": is_active,
            "revoked_at": revoked_at,
            "created_at": created_at,
        },
    )


def _seed_legacy(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(f"INSERT INTO users {_USER_COLS} VALUES (:id, :u, :n, 'x')"),
        [
            {"id": U1, "u": "u1", "n": "一号"},
            {"id": U2, "u": "u2", "n": "二号"},
            {"id": U3, "u": "u3", "n": "三号"},
        ],
    )
    conn.execute(
        sa.text("INSERT INTO spaces (id, name, created_by) VALUES (:id, '老空间', :by)"),
        {"id": SPACE, "by": U1},
    )
    _insert_key(
        conn,
        key_id=K1,
        member_user_id=U1,
        created_by=U1,
        name="旧口径全部库",
        capabilities=["chat"],
        kb_ids=[],
        is_active=True,
        revoked_at=None,
        created_at=_ts(1),
    )
    _insert_key(
        conn,
        key_id=K2,
        member_user_id=U2,
        created_by=U1,
        name="旧口径限定库",
        capabilities=["chat", "documents"],
        kb_ids=[KB_X],
        is_active=False,
        revoked_at=None,
        created_at=_ts(2),
    )
    _insert_key(
        conn,
        key_id=K3,
        member_user_id=U3,
        created_by=U1,
        name="旧口径已吊销",
        capabilities=[],
        kb_ids=[],
        is_active=True,
        revoked_at=_ts(9),
        created_at=_ts(3),
    )
    _insert_key(
        conn,
        key_id=K4,
        member_user_id=None,
        created_by=U1,
        name="旧口径回退签发人",
        capabilities=["chat"],
        kb_ids=[KB_X],
        is_active=True,
        revoked_at=None,
        created_at=_ts(5),
    )
    _insert_key(
        conn,
        key_id=K5,
        member_user_id=None,
        created_by=U3,
        name="旧口径无标记",
        capabilities=["chat"],
        kb_ids=[],
        is_active=False,
        revoked_at=None,
        created_at=_ts(4),
    )


@pytest.fixture(scope="module")
def backfilled() -> Iterator[sa.Engine]:
    """升到 0016 塞存量数据,再升到 head —— 用例看到的即是"回填完成后的生产库"。"""
    if shutil.which("docker") is None:
        pytest.skip("docker 不可用,跳过迁移回填测试")
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
            # 钉死在 0017 而不是 head:0018 会删掉旧列,而本模块的断言正是要拿
            # api_keys.capabilities/kb_ids/is_active 与档位逐行比对(0018 之后旧列已不存在)。
            command.upgrade(_alembic_config(), "0017")
            yield engine
        finally:
            engine.dispose()
    finally:
        if prior is None:
            os.environ.pop("APP_DATABASE_URL", None)
        else:
            os.environ["APP_DATABASE_URL"] = prior
        container.stop()


def test_backfill_preserves_every_key_permission_and_scope(backfilled: sa.Engine) -> None:
    """权限逐字等价:档位能力/范围 = 旧列,`all_kbs` 精确翻译"空范围 = 全部"。"""
    with backfilled.connect() as conn:
        rows = conn.execute(
            sa.text(
                """
                SELECT k.id,
                       g.id IS NULL                       AS missing,
                       g.capabilities <> k.capabilities   AS caps_diff,
                       g.kb_ids <> k.kb_ids               AS kb_diff,
                       g.all_kbs <> (k.kb_ids = '[]'::jsonb) AS all_kbs_diff,
                       g.space_id <> k.space_id           AS space_diff,
                       g.name <> k.name                   AS name_diff
                FROM api_keys k
                LEFT JOIN permission_groups g ON g.id = k.group_id
                """
            )
        ).all()
    assert rows, "存量 Key 应非空"
    for r in rows:
        assert not r.missing, f"Key {r[0]} 未派生档位"
        assert not r.caps_diff, f"Key {r[0]} 能力清单与档位不一致"
        assert not r.kb_diff, f"Key {r[0]} 知识库范围与档位不一致"
        assert not r.all_kbs_diff, f"Key {r[0]} 的 all_kbs 翻译错误"
        assert not r.space_diff, f"Key {r[0]} 的档位串到了别的空间"
        assert not r.name_diff, f"Key {r[0]} 的档位名与 Key 不一致"


def test_backfill_mounts_coalesce_bound_member_and_keeps_one_current(
    backfilled: sa.Engine,
) -> None:
    """挂载覆盖 COALESCE 口径的绑定人;current 只从未吊销且 is_active 的行派生,同人只留最新。"""
    with backfilled.connect() as conn:
        mount_count = conn.execute(
            sa.text("SELECT count(*) FROM permission_group_members")
        ).scalar_one()
        current_users = set(
            conn.execute(
                sa.text(
                    "SELECT user_id FROM permission_group_members WHERE is_current"
                )
            )
            .scalars()
            .all()
        )
        k1_current = conn.execute(
            sa.text("SELECT is_current FROM permission_group_members WHERE group_id = :g"),
            {"g": K1},
        ).scalar_one()
        mount_spaces = set(
            conn.execute(
                sa.text("SELECT DISTINCT space_id FROM permission_group_members")
            )
            .scalars()
            .all()
        )

    assert mount_count == 5, "五把 Key 的绑定人都非 NULL,应各有一行挂载"
    # K3 已吊销、K5 未标记都不产 current;K1 与 K4 同人,K4 更晚 → 只有 U1 一条 current
    assert current_users == {U1}
    assert k1_current is False, "同一人的旧 current 必须让位给 created_at 最新的那一档"
    assert mount_spaces == {SPACE}, "挂载行冗余 space_id 必须与 Key 所属空间一致"


def test_one_current_per_member_index_is_enforced(backfilled: sa.Engine) -> None:
    """部分唯一索引(每人每空间至多一档 current)必须真挡得住第二条。"""
    with pytest.raises(sa.exc.IntegrityError):
        with backfilled.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO permission_group_members"
                    " (id, group_id, space_id, user_id, is_current)"
                    " VALUES (:id, :g, :s, :u, true)"
                ),
                {"id": uuid.uuid4(), "g": K2, "s": SPACE, "u": U1},
            )


def test_group_id_is_not_null(backfilled: sa.Engine) -> None:
    """新 Key 必须带档位 —— 漏写 group_id 要在库层就炸,不能静默留下"无权限凭据"。"""
    with pytest.raises(sa.exc.IntegrityError):
        with backfilled.begin() as conn:
            conn.execute(
                sa.text(
                    f"""
                    INSERT INTO api_keys {_KEY_COLS}
                    VALUES (:id, :s, :u, :u, '无档位', '', :h, 'sk-****0000',
                            '[]'::jsonb, '[]'::jsonb, false, NULL, now(), now())
                    """
                ),
                {"id": uuid.uuid4(), "s": SPACE, "u": U1, "h": "x" * 64},
            )


def test_group_with_keys_cannot_be_deleted(backfilled: sa.Engine) -> None:
    """档位下还挂着 Key 时不可删(RESTRICT)—— 删档位等于悄悄抹掉一批凭据的权限。"""
    with pytest.raises(sa.exc.IntegrityError):
        with backfilled.begin() as conn:
            conn.execute(sa.text("DELETE FROM permission_groups WHERE id = :g"), {"g": K1})


def test_downgrade_is_clean_and_re_upgradable(backfilled: sa.Engine) -> None:
    """回滚到 0016 要把新表与新列拆干净;再次升级仍要成立(生产可能反复)。"""
    from alembic import command

    url = os.environ["APP_DATABASE_URL"]
    try:
        command.downgrade(_alembic_config(), "0016")
        tables, key_cols = _inspect(url)
        assert "permission_groups" not in tables
        assert "permission_group_members" not in tables
        assert "group_id" not in key_cols
    finally:
        command.upgrade(_alembic_config(), "0017")
    assert "permission_groups" in _inspect(url)[0]
