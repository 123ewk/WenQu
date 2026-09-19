"""个人资料审计单测(OPT-9):昵称修改、头像上传/删除必须留下审计动作。

此前这三个动作完全绕过审计 —— 账号资料变更属于"谁改了什么"的核心审计面。
"""

from __future__ import annotations

import io

import pytest

from app.application.service.profile import ProfileService
from app.core.errors import AppError
from app.core.storage import MemoryStorage
from app.domain.enums import AuditAction, AuditResult
from tests.fakes import FakeAuditRepository, FakeUserRepository, make_user


class RecordingAudit(FakeAuditRepository):
    """带 result 默认的假审计(与 FakeAuditRepository 同形,这里只为可读)。"""


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def build_service():
    users = FakeUserRepository()
    user = users.create(make_user("profile"))
    audit = RecordingAudit()
    svc = ProfileService(users, MemoryStorage(), audit, avatar_max_mb=2)
    return svc, user, audit


def test_set_avatar_writes_audit() -> None:
    svc, user, audit = build_service()
    svc.set_avatar(user, _png_bytes(), "image/png")
    assert [(log.action, log.result) for log in audit.logs] == [
        (str(AuditAction.AVATAR_UPLOADED), str(AuditResult.SUCCESS))
    ]


def test_delete_avatar_writes_audit_only_when_exists() -> None:
    svc, user, audit = build_service()
    svc.delete_avatar(user)  # 无头像:静默,不产生审计
    assert audit.logs == []

    svc.set_avatar(user, _png_bytes(), "image/png")
    svc.delete_avatar(user)
    actions = [log.action for log in audit.logs]
    assert actions == [str(AuditAction.AVATAR_UPLOADED), str(AuditAction.AVATAR_DELETED)]


def test_update_profile_writes_audit() -> None:
    svc, user, audit = build_service()
    updated = svc.update_profile(user, "新昵称")
    assert updated.nickname == "新昵称"
    assert [log.action for log in audit.logs] == [str(AuditAction.USER_UPDATED)]
    assert audit.logs[0].detail == {"field": "nickname"}


def test_update_profile_rejects_blank() -> None:
    svc, user, _audit = build_service()
    with pytest.raises(AppError) as exc:
        svc.update_profile(user, "   ")
    assert exc.value.http_status == 422


def test_corrupt_image_syntax_error_returns_415(monkeypatch) -> None:
    """损坏图片触发 PIL SyntaxError 时也必须归一为 415,不能漏成 500(§11.2-2)。"""
    from PIL import Image

    svc, user, _audit = build_service()

    def _boom(*_args, **_kwargs):
        raise SyntaxError("corrupt image")

    monkeypatch.setattr(Image, "open", _boom)
    with pytest.raises(AppError) as exc:
        svc.set_avatar(user, _png_bytes(), "image/png")
    assert exc.value.http_status == 415
    assert exc.value.code_str == "UNSUPPORTED_FORMAT"
