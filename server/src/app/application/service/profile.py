"""个人资料服务:头像上传/读取/删除(缺口 #6)。

安全要点:
- **内容校验而非扩展名**:用 Pillow 真正解码一次(verify),伪装成 .png 的任意字节
  会被拒 —— 头像会被回传给浏览器,不可信内容不能只凭文件名放行;
- 大小限制先于解码(避免超大文件打爆内存),解码后再限制像素尺寸(防解压炸弹);
- 对象存储 key 按用户维度隔离(avatars/{user_id}),换头像覆盖旧对象,不留孤儿。
"""

from __future__ import annotations

import io
import uuid

from app.core.errors import AppError, ErrorCode
from app.core.storage import ObjectStorage
from app.domain.enums import AuditAction, AuditResult
from app.domain.interfaces import AuditRepository, UserRepository
from app.domain.models import AuditLog, User

# Pillow 识别的格式 → 回传 content-type
_ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_MAX_PIXELS = 4096  # 单边像素上限(解压炸弹防护)


class ProfileService:
    def __init__(
        self,
        users: UserRepository,
        storage: ObjectStorage,
        audit: AuditRepository,
        avatar_max_mb: int = 2,
    ) -> None:
        self._users = users
        self._storage = storage
        self._audit = audit
        self._max_bytes = avatar_max_mb * 1024 * 1024

    def update_profile(self, user: User, nickname: str) -> User:
        """修改昵称(PATCH /users/me):此前绕过审计,账号资料变更必须留痕。"""
        nickname = nickname.strip()
        if not nickname:
            raise AppError(ErrorCode.VALIDATION, "昵称不能为空", http_status=422)
        old = user.nickname
        user.nickname = nickname
        self._users.save(user)
        if old != nickname:
            self._log(user.id, AuditAction.USER_UPDATED, detail={"field": "nickname"})
        return user

    def set_avatar(self, user: User, content: bytes, content_type: str = "") -> str:
        """校验并保存头像,返回内容类型(供读取接口回传)。"""
        if not content:
            raise AppError(ErrorCode.VALIDATION, "文件为空", http_status=400)
        if len(content) > self._max_bytes:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE,
                f"头像不得超过 {self._max_bytes // (1024 * 1024)}MB",
                http_status=413,
            )
        fmt = self._detect_format(content)
        key = f"avatars/{user.id}"
        self._storage.put(key, content, _ALLOWED_FORMATS[fmt])
        user.avatar_key = key
        self._users.save(user)
        self._log(user.id, AuditAction.AVATAR_UPLOADED)
        return _ALLOWED_FORMATS[fmt]

    def get_avatar(self, user: User) -> tuple[bytes, str]:
        if not user.avatar_key:
            raise AppError(ErrorCode.NOT_FOUND, "尚未设置头像", http_status=404)
        try:
            data = self._storage.get(user.avatar_key)
        except FileNotFoundError as exc:
            # 库里有记录但对象已丢(存储异常):按未设置处理,不留 500
            raise AppError(ErrorCode.NOT_FOUND, "头像文件不存在", http_status=404) from exc
        return data, _content_type_of(data)

    def delete_avatar(self, user: User) -> None:
        if user.avatar_key:
            self._storage.delete(user.avatar_key)
            user.avatar_key = None
            self._users.save(user)
            self._log(user.id, AuditAction.AVATAR_DELETED)

    def _log(
        self,
        actor_id: uuid.UUID,
        action: AuditAction,
        detail: dict | None = None,
        ip: str = "",
    ) -> None:
        self._audit.add(
            AuditLog(
                actor_id=actor_id,
                space_id=None,  # 个人资料不属于任何空间
                action=str(action),
                target=str(actor_id),
                result=str(AuditResult.SUCCESS),
                detail=detail or {},
                ip=ip,
            )
        )

    def _detect_format(self, content: bytes) -> str:
        """真正解码校验:非图片/截断/超像素一律 415。"""
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()  # 结构校验(不解码像素)
                detected = (image.format or "").upper()
                width, height = image.size
        # SyntaxError:PIL 个别损坏图片会抛它(前端缺口台账 §11.2-2 实测),不接就漏成 500
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise AppError(
                ErrorCode.UNSUPPORTED_FORMAT, "仅支持 PNG/JPEG/WEBP 图片", http_status=415
            ) from exc
        if detected not in _ALLOWED_FORMATS:
            raise AppError(
                ErrorCode.UNSUPPORTED_FORMAT, "仅支持 PNG/JPEG/WEBP 图片", http_status=415
            )
        if max(width, height) > _MAX_PIXELS:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE,
                f"图片单边不得超过 {_MAX_PIXELS} 像素",
                http_status=413,
            )
        return detected


def _content_type_of(data: bytes) -> str:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            return _ALLOWED_FORMATS.get((image.format or "").upper(), "application/octet-stream")
    except Exception:  # noqa: BLE001 — 读不出就按二进制回传,不阻断读取
        return "application/octet-stream"
