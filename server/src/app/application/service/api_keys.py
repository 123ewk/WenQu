"""API Key 服务(OPT-6,OPT-26 起绑定成员):签发、认证、能力级授权、KB 范围收窄、吊销。

安全语义(与 `docs/架构设计.md` §8 对齐):
- **绑定成员,Admin+ 签发**:签发 Key 必须指定空间成员 `member_user_id`,操作者须具备
  与"拉成员"同级的权限(Role.ADMIN+)——能拉人进来的人才能替这个人配程序化凭据;
  `created_by` 只作签发操作者留痕,不再参与运行时身份;
- **双重校验**:`authenticate` = ① Key 本身有效(哈希命中、未吊销)② 绑定成员身份仍在
  空间(get_member 命中);此后路由授权天然跟随**成员自己的角色**(各业务服务的
  `_require_role` 以 Caller.user = 绑定成员判定):成员被移出 → Key 立即 401;
  成员被降级 → 高权限路由被服务层 403,权限实时收敛;
- **明文只出现一次**:签发时返回完整 Key,库里只留 SHA-256 哈希与截断提示串;
- **认证 = 哈希等值查找**,不做明文比对,不遍历库;
- **能力级授权 fail-closed**:能力清单为空 = 什么都不允许;路由授权表里没有的路由
  用 Key 访问一律拒绝(表见 `app/api/api_key_auth.py`);
- **KB 范围二次收窄**:范围非空时,请求里落在范围外的 KB 被剔除;若请求只要范围外
  的 KB,直接拒绝,绝不静默降级成"全部"。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.interfaces import (
    ApiKeyRepository,
    AuditRepository,
    KnowledgeBaseRepository,
    SpaceRepository,
    UserRepository,
)
from app.domain.models import ApiKey, AuditLog, User

_TOKEN_BYTES = 32  # 256 bit 熵;sha256 哈希后 64 位十六进制
_PREFIX = "sk-live-"
_HINT_HEAD = 8  # 提示串保留的前缀长度(含 sk- 前缀)
_HINT_TAIL = 4  # 提示串保留的末位长度


@dataclass(frozen=True)
class CreatedApiKey:
    """签发结果:明文仅此一次,之后无法再取回。"""

    api_key: ApiKey
    plaintext: str


@dataclass(frozen=True)
class ApiKeyWithMember:
    """列表视图:Key + 绑定成员(可能已被删除用户,置灰展示)+ 签发者名字。"""

    api_key: ApiKey
    member: User | None
    member_role: int | None
    created_by_name: str | None


class ApiKeyService:
    def __init__(
        self,
        api_keys: ApiKeyRepository,
        spaces: SpaceRepository,
        kbs: KnowledgeBaseRepository,
        audit: AuditRepository,
        users: UserRepository | None = None,
    ) -> None:
        # users 为 OPT-26 新增依赖;允许缺省以兼容旧装配(仅绑定成员相关的接口会用到)
        self._keys = api_keys
        self._spaces = spaces
        self._kbs = kbs
        self._audit = audit
        self._users = users

    # ---------------------------- 签发与管理 ----------------------------

    def create(
        self,
        space_id: uuid.UUID,
        user: User,
        member_user_id: uuid.UUID,
        name: str,
        description: str,
        capabilities: list[ApiCapability | str],
        kb_ids: list[uuid.UUID],
        ip: str = "",
    ) -> CreatedApiKey:
        # "能拉成员的身份才能配 API":门槛与拉成员一致(Admin+),且签发留痕
        self._require_role(space_id, user.id, Role.ADMIN)
        membership = self._spaces.get_member(space_id, member_user_id)
        if membership is None:
            raise AppError(
                ErrorCode.MEMBER_NOT_FOUND, "目标用户不是该空间成员", http_status=404
            )
        caps = self._normalize_capabilities(capabilities)
        self._assert_kbs_in_space(space_id, kb_ids)

        plaintext = _PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        api_key = ApiKey(
            space_id=space_id,
            created_by=user.id,
            member_user_id=member_user_id,
            name=name,
            description=description,
            key_hash=hash_api_key(plaintext),
            key_hint=_hint(plaintext),
            capabilities=[str(c) for c in caps],
            kb_ids=[str(kb_id) for kb_id in kb_ids],
        )
        self._keys.create(api_key)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.API_KEY_CREATED,
            target=name,
            detail={
                "member_user_id": str(member_user_id),
                "capabilities": [str(c) for c in caps],
                "kb_scope": len(kb_ids),
            },
            ip=ip,
        )
        return CreatedApiKey(api_key=api_key, plaintext=plaintext)

    def list_for_space(self, space_id: uuid.UUID, user: User) -> list[ApiKeyWithMember]:
        self._require_role(space_id, user.id, Role.VIEWER)
        keys = self._keys.list_for_space(space_id)
        members = {
            u.id: (m, u) for m, u in self._spaces.list_members(space_id)
        }
        creator_names: dict[uuid.UUID, str] = {}
        if self._users is not None:
            for k in keys:
                if k.created_by is not None and k.created_by not in creator_names:
                    creator = self._users.get(k.created_by)
                    creator_names[k.created_by] = creator.username if creator else ""
        views: list[ApiKeyWithMember] = []
        for key in keys:
            bound = key.member_user_id or key.created_by
            entry = members.get(bound) if bound is not None else None
            member, role = (entry[1], entry[0].role) if entry is not None else (None, None)
            views.append(
                ApiKeyWithMember(
                    api_key=key,
                    member=member,
                    member_role=role,
                    created_by_name=(
                        creator_names.get(key.created_by)
                        if key.created_by is not None
                        else None
                    ),
                )
            )
        return views

    def revoke(
        self, space_id: uuid.UUID, user: User, key_id: uuid.UUID, ip: str = ""
    ) -> ApiKey:
        self._require_role(space_id, user.id, Role.ADMIN)
        api_key = self._keys.get(key_id)
        # 按 space_id 收窄:别的空间的 Key id 一律 404,不泄露其存在
        if api_key is None or api_key.space_id != space_id:
            raise AppError(ErrorCode.NOT_FOUND, "API Key 不存在", http_status=404)
        if api_key.revoked_at is None:
            api_key.revoked_at = datetime.now(UTC)
            self._keys.save(api_key)
            self._log(
                actor_id=user.id,
                space_id=space_id,
                action=AuditAction.API_KEY_REVOKED,
                target=api_key.name,
                ip=ip,
            )
        return api_key

    # ---------------------------- 认证(热路径) ----------------------------

    def member_of(self, api_key: ApiKey) -> tuple[User | None, int | None]:
        """绑定成员当前档案与角色(成员已注销/已移出时对应项为 None)。"""
        bound = api_key.member_user_id or api_key.created_by
        if bound is None or self._users is None:
            return None, None
        membership = self._spaces.get_member(api_key.space_id, bound)
        return self._users.get(bound), (membership.role if membership is not None else None)

    def authenticate(self, plaintext: str) -> ApiKey | None:
        """按哈希查找并校验可用性;任何不通过都返回 None(调用方归一为 401)。

        校验顺序刻意如此:格式 → 哈希命中 → 未吊销 → **绑定成员仍是成员**。
        最后一步保证"人被移出/删除但 Key 还在跑"不会发生;存量旧 Key 无
        member_user_id 时回退 created_by(迁移 0012 已回填,双保险)。
        """
        if not plaintext.startswith(_PREFIX):
            return None
        api_key = self._keys.get_by_hash(hash_api_key(plaintext))
        if api_key is None or api_key.revoked_at is not None:
            return None
        bound = api_key.member_user_id or api_key.created_by
        if bound is None:
            return None
        if self._spaces.get_membership(api_key.space_id, bound) is None:
            return None
        api_key.last_used_at = datetime.now(UTC)
        self._keys.save(api_key)
        return api_key

    # ---------------------------- 授权判定 ----------------------------

    def require_capability(self, api_key: ApiKey, capability: ApiCapability) -> None:
        """能力级授权:未声明即拒绝(空清单什么都不允许)。"""
        if str(capability) not in (api_key.capabilities or []):
            raise AppError(
                ErrorCode.API_KEY_CAPABILITY_DENIED,
                f"该 API Key 未授予「{_CAPABILITY_LABELS.get(capability, capability)}」能力",
                http_status=403,
            )

    def resolve_kb_scope(
        self, api_key: ApiKey, requested: list[uuid.UUID] | None
    ) -> list[uuid.UUID] | None:
        """把请求的 KB 列表按 Key 范围收窄。

        - 范围为空 → 返回 None(不限制,再交给空间成员身份把关);
        - 范围非空 → 与请求求交;请求为空时取全部范围;
        - 交集为空 → 403,不静默降级成"全部"。
        """
        scope = _parse_uuid_list(api_key.kb_ids)
        if not scope:
            return None
        if not requested:
            return scope
        narrowed = [kb_id for kb_id in requested if kb_id in scope]
        if not narrowed:
            raise AppError(
                ErrorCode.API_KEY_SCOPE_DENIED,
                "该 API Key 无权访问请求的知识库",
                http_status=403,
            )
        return narrowed

    # ---------------------------- 内部 ----------------------------

    def _require_role(
        self, space_id: uuid.UUID, user_id: uuid.UUID, minimum: Role
    ) -> None:
        space = self._spaces.get(space_id)
        membership = (
            self._spaces.get_membership(space_id, user_id) if space is not None else None
        )
        if space is None or membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if Role(membership.role) < minimum:
            raise AppError(ErrorCode.FORBIDDEN, "权限不足", http_status=403)

    @staticmethod
    def _normalize_capabilities(
        raw: list[ApiCapability | str],
    ) -> list[ApiCapability]:
        """拒绝未知能力名(拼错的能力名不能变成"静默失效")。"""
        try:
            return [ApiCapability(str(item)) for item in raw]
        except ValueError as exc:
            allowed = ", ".join(str(c) for c in ApiCapability)
            raise AppError(
                ErrorCode.VALIDATION,
                f"未知的能力名;允许值:{allowed}",
                http_status=422,
            ) from exc

    def _assert_kbs_in_space(
        self, space_id: uuid.UUID, kb_ids: list[uuid.UUID]
    ) -> None:
        """KB 范围必须落在本空间内,否则等于把一个租户的 Key 指向另一个租户的数据。"""
        for kb_id in kb_ids:
            kb = self._kbs.get(kb_id)
            if kb is None or kb.space_id != space_id:
                raise AppError(
                    ErrorCode.KB_NOT_FOUND, f"知识库不存在:{kb_id}", http_status=404
                )

    def _log(
        self,
        actor_id: uuid.UUID,
        space_id: uuid.UUID | None,
        action: AuditAction,
        target: str = "",
        detail: dict | None = None,
        ip: str = "",
    ) -> None:
        self._audit.add(
            AuditLog(
                actor_id=actor_id,
                space_id=space_id,
                action=str(action),
                target=target,
                detail=detail or {},
                ip=ip,
            )
        )


_CAPABILITY_LABELS: dict[ApiCapability, str] = {
    ApiCapability.CHAT: "对话检索",
    ApiCapability.DOCUMENTS: "文档管理",
}


def hash_api_key(plaintext: str) -> str:
    """认证查找键:SHA-256(明文)。Key 本身是高熵随机串,无需加盐/慢哈希。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _hint(plaintext: str) -> str:
    """展示用截断标识:保留首尾便于用户辨识,中间以 **** 遮掩。"""
    if len(plaintext) <= _HINT_HEAD + _HINT_TAIL:
        return plaintext
    return f"{plaintext[:_HINT_HEAD]}****{plaintext[-_HINT_TAIL:]}"


def _parse_uuid_list(raw: list | None) -> list[uuid.UUID]:
    """JSONB 里存的是字符串;解析失败的值直接丢弃(视为不在范围内)。"""
    result: list[uuid.UUID] = []
    for item in raw or []:
        try:
            result.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return result
