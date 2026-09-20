"""对象存储测试:内存替身全语义 + MinIO 实现用假 client 验证错误翻译与批量删除。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.storage import MemoryStorage, MinioStorage


class TestMemoryStorage:
    def test_roundtrip_and_missing(self) -> None:
        storage = MemoryStorage()
        storage.put("a/b/c.txt", b"hello", content_type="text/plain")
        assert storage.get("a/b/c.txt") == b"hello"
        with pytest.raises(FileNotFoundError):
            storage.get("a/b/missing.txt")

    def test_delete_is_idempotent(self) -> None:
        storage = MemoryStorage()
        storage.put("k", b"v")
        storage.delete("k")
        storage.delete("k")  # 不存在也静默
        with pytest.raises(FileNotFoundError):
            storage.get("k")

    def test_delete_prefix_only_hits_prefix(self) -> None:
        storage = MemoryStorage()
        storage.put("space1/kb1/doc1/f1", b"1")
        storage.put("space1/kb1/doc1/f2", b"2")
        storage.put("space1/kb1/doc12/f3", b"3")  # 前缀 doc1 的兄弟,不能误删
        storage.put("space2/other", b"4")

        storage.delete_prefix("space1/kb1/doc1/")

        with pytest.raises(FileNotFoundError):
            storage.get("space1/kb1/doc1/f1")
        assert storage.get("space1/kb1/doc12/f3") == b"3"
        assert storage.get("space2/other") == b"4"


def _s3_error(code: str) -> Exception:
    from minio.error import S3Error

    return S3Error(
        code=code, message=code, resource="r", request_id="i", host_id="h", response=MagicMock()
    )


def test_minio_get_translates_nosuchkey(monkeypatch) -> None:
    storage = MinioStorage("ep", "ak", "sk", "bucket")
    client = MagicMock()
    client.get_object.side_effect = _s3_error("NoSuchKey")
    monkeypatch.setattr(storage, "_client", client)
    with pytest.raises(FileNotFoundError):
        storage.get("missing")

    client.get_object.side_effect = _s3_error("AccessDenied")
    with pytest.raises(Exception):  # noqa: B017 — 非 NoSuchKey 原样抛出
        storage.get("forbidden")


def test_minio_put_is_lazy_and_delete_prefix_drains(monkeypatch) -> None:
    storage = MinioStorage("ep", "ak", "sk", "bucket")
    client = MagicMock()
    client.bucket_exists.return_value = True
    client.list_objects.return_value = [
        SimpleNamespace(object_name="sp/kb/doc/f1"),
        SimpleNamespace(object_name="sp/kb/doc/f2"),
    ]
    client.remove_objects.return_value = iter([])  # 无删除错误
    monkeypatch.setattr(storage, "_client", client)

    storage.put("sp/kb/doc/f1", b"data")

    client.bucket_exists.assert_called_once_with("bucket")
    client.put_object.assert_called_once()

    storage.delete_prefix("sp/kb/doc/")
    # minio.remove_objects 只接受 DeleteObject(真实客户端会做 XML 序列化;mock 传字符串也能过,
    # 正是这里放过过 delete_prefix 线上 500)——必须钉住元素类型
    from minio.deleteobjects import DeleteObject

    sent = client.remove_objects.call_args.args[1]
    assert all(isinstance(o, DeleteObject) for o in sent)
    assert [o.name for o in sent] == ["sp/kb/doc/f1", "sp/kb/doc/f2"]


def test_minio_delete_swallows_nosuchkey(monkeypatch) -> None:
    storage = MinioStorage("ep", "ak", "sk", "bucket")
    client = MagicMock()
    client.remove_object.side_effect = _s3_error("NoSuchKey")
    monkeypatch.setattr(storage, "_client", client)
    storage.delete("gone")  # 静默,不抛
