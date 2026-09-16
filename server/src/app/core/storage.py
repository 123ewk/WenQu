"""对象存储抽象(基准 01 接口先行):MinIO 实现 + 测试内存替身。

约定(两个实现语义对齐,基准 03):
- get 缺失 key → FileNotFoundError;delete 缺失 key → 静默(S3 语义);
- MinIO 构造不连网,首次 put 时惰性建桶 —— 测试环境无 MinIO 也能实例化。
"""

from __future__ import annotations

import io
from typing import Protocol


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "") -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> None: ...


class MemoryStorage:
    """内存替身:dict 直存,delete_prefix 按 key 前缀过滤。"""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        self._objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._objects if k.startswith(prefix)]:
            del self._objects[key]


class MinioStorage:
    """MinIO/S3 实现;key 语义与 S3 对象路径一致。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio

        self._client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure
        )
        self._bucket = bucket
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
        self._bucket_ready = True

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        self._ensure_bucket()
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type or "application/octet-stream",
        )

    def get(self, key: str) -> bytes:
        from minio.error import S3Error

        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise FileNotFoundError(key) from exc
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        from minio.error import S3Error

        try:
            self._client.remove_object(self._bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return
            raise

    def delete_prefix(self, prefix: str) -> None:
        objects = [
            obj.object_name
            for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        ]
        if not objects:
            return
        for error in self._client.remove_objects(self._bucket, objects):
            # 迭代器只产出失败的删除项;出现即整体失败上抛
            raise RuntimeError(f"对象删除失败 {error.name}: {error.code} {error.message}")
