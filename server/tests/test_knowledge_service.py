"""知识库与文档服务单元测试(内存 fake + MemoryStorage,基准 03)。

重点:RBAC 门槛(EDITOR+ 增改 / ADMIN+ 删 KB)、404/403 守卫语义、
同名冲突、格式与大小校验、对象存储级联清理。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.knowledge import KnowledgeService
from app.core.errors import AppError
from app.core.storage import MemoryStorage
from app.domain.enums import DocumentStatus, Role
from app.domain.models import KnowledgeBase, Membership, Space
from tests.fakes import (
    FakeAuditRepository,
    FakeDocumentRepository,
    FakeKnowledgeBaseRepository,
    FakeSpaceRepository,
    FakeTaskRepository,
    FakeUserRepository,
    make_user,
)


def make_env(upload_max_mb: int = 50) -> SimpleNamespace:
    users = FakeUserRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    kbs = FakeKnowledgeBaseRepository()
    documents = FakeDocumentRepository(kbs_ref=kbs.kbs)
    audit = FakeAuditRepository()
    storage = MemoryStorage()
    tasks = FakeTaskRepository()
    svc = KnowledgeService(kbs, documents, spaces, audit, storage, tasks, upload_max_mb)
    owner = users.create(make_user("owner"))
    editor = users.create(make_user("editor"))
    viewer = users.create(make_user("viewer"))
    outsider = users.create(make_user("outsider"))
    space_id = spaces.create(Space(name="s", description="")).id
    for user_id, role in (
        (owner.id, Role.OWNER),
        (editor.id, Role.EDITOR),
        (viewer.id, Role.VIEWER),
    ):
        spaces.add_member(Membership(space_id=space_id, user_id=user_id, role=role))
    return SimpleNamespace(
        svc=svc,
        storage=storage,
        kbs=kbs,
        documents=documents,
        tasks=tasks,
        spaces=spaces,
        users=users,
        owner=owner,
        editor=editor,
        viewer=viewer,
        outsider=outsider,
        space_id=space_id,
    )


def test_create_kb_rbac_and_name_conflict() -> None:
    env = make_env()
    kb = env.svc.create_kb(env.owner, env.space_id, "产品手册", "desc")
    assert kb.name == "产品手册"
    # 同名 → 409
    with pytest.raises(AppError) as dup:
        env.svc.create_kb(env.editor, env.space_id, "产品手册", "")
    assert dup.value.code_str == "KB_NAME_TAKEN"
    # Viewer 只读 → 403
    with pytest.raises(AppError) as low:
        env.svc.create_kb(env.viewer, env.space_id, "x", "")
    assert low.value.code_str == "FORBIDDEN"
    # 非成员 → 404
    with pytest.raises(AppError) as out:
        env.svc.create_kb(env.outsider, env.space_id, "x", "")
    assert out.value.code_str == "SPACE_NOT_FOUND"


def test_get_kb_cross_space_is_404() -> None:
    env = make_env()
    # 别人家的 KB 挂在别的空间;本空间成员拿它来查 → KB_NOT_FOUND(不泄露存在性)
    other_kb = env.kbs.create(KnowledgeBase(space_id=uuid.uuid4(), name="别家库"))
    with pytest.raises(AppError) as not_found:
        env.svc.get_kb(env.owner, env.space_id, other_kb.id)
    assert not_found.value.code_str == "KB_NOT_FOUND"


def test_update_kb_and_rename_conflict() -> None:
    env = make_env()
    kb1 = env.svc.create_kb(env.owner, env.space_id, "库A", "d1")
    env.svc.create_kb(env.owner, env.space_id, "库B", "d2")
    updated = env.svc.update_kb(env.editor, env.space_id, kb1.id, "库A2", "d2")
    assert updated.name == "库A2"
    with pytest.raises(AppError) as dup:
        env.svc.update_kb(env.editor, env.space_id, kb1.id, "库B", "")
    assert dup.value.code_str == "KB_NAME_TAKEN"


def test_delete_kb_requires_admin_and_cleans_storage() -> None:
    env = make_env()
    kb = env.svc.create_kb(env.owner, env.space_id, "库A", "")
    env.storage.put(f"{env.space_id}/{kb.id}/doc1/file.pdf", b"data")
    # Editor 不可删 KB → 403
    with pytest.raises(AppError) as low:
        env.svc.delete_kb(env.editor, env.space_id, kb.id)
    assert low.value.code_str == "FORBIDDEN"
    # Admin 可删;对象存储前缀清理
    admin = env.users.create(make_user("admin2"))
    env.spaces.add_member(
        Membership(space_id=env.space_id, user_id=admin.id, role=Role.ADMIN)
    )
    env.svc.delete_kb(admin, env.space_id, kb.id)
    assert env.kbs.get(kb.id) is None
    with pytest.raises(FileNotFoundError):
        env.storage.get(f"{env.space_id}/{kb.id}/doc1/file.pdf")


def test_upload_document_rules() -> None:
    env = make_env()
    kb = env.svc.create_kb(env.owner, env.space_id, "库A", "")
    # 正常上传 → PENDING + 存储落位
    doc = env.svc.upload_document(
        env.editor, env.space_id, kb.id, "说 明 书.PDF", b"%PDF-1.4 fake",
        content_type="application/pdf",
    )
    assert doc.status == DocumentStatus.PENDING and doc.format == "pdf"
    assert doc.size_bytes == len(b"%PDF-1.4 fake")
    assert env.storage.get(doc.source) == b"%PDF-1.4 fake"
    assert doc.source.startswith(f"{env.space_id}/{kb.id}/")
    # Viewer 上传 → 403
    with pytest.raises(AppError) as low:
        env.svc.upload_document(env.viewer, env.space_id, kb.id, "a.md", b"x")
    assert low.value.code_str == "FORBIDDEN"
    # 旧版 office 扩展名 → 415
    with pytest.raises(AppError) as unsupported:
        env.svc.upload_document(env.editor, env.space_id, kb.id, "a.doc", b"x")
    assert unsupported.value.code_str == "UNSUPPORTED_FORMAT"
    # 超限 → 413
    tiny = KnowledgeService(
        env.kbs, env.documents, env.spaces, FakeAuditRepository(), env.storage, env.tasks, 0
    )
    with pytest.raises(AppError) as too_big:
        tiny.upload_document(env.editor, env.space_id, kb.id, "a.txt", b"x")
    assert too_big.value.code_str == "FILE_TOO_LARGE"
    # 路径穿越清洗:上层目录名不进 key
    doc2 = env.svc.upload_document(env.editor, env.space_id, kb.id, "../evil.md", b"x")
    assert "/" not in doc2.filename and doc2.filename == "evil.md"


def test_document_read_and_delete() -> None:
    env = make_env()
    kb = env.svc.create_kb(env.owner, env.space_id, "库A", "")
    doc = env.svc.upload_document(env.editor, env.space_id, kb.id, "a.txt", b"hello")
    # Viewer 可读
    assert env.svc.get_document(env.viewer, env.space_id, kb.id, doc.id).id == doc.id
    # 其它 KB 的文档互不可见
    kb2 = env.svc.create_kb(env.owner, env.space_id, "库B", "")
    with pytest.raises(AppError) as wrong_kb:
        env.svc.get_document(env.viewer, env.space_id, kb2.id, doc.id)
    assert wrong_kb.value.code_str == "DOCUMENT_NOT_FOUND"
    # 删除后存储与行都清理
    env.svc.delete_document(env.editor, env.space_id, kb.id, doc.id)
    assert env.documents.get(doc.id) is None
    with pytest.raises(FileNotFoundError):
        env.storage.get(doc.source)
    items, total = env.svc.list_documents(env.viewer, env.space_id, kb.id, 10, 0)
    assert (items, total) == ([], 0)
