"""API Key 服务(OPT-6;OPT-26 起绑成员;OPT-41 起权限只读档位):签发、认证、授权、吊销。

安全语义(与 `docs/架构设计.md` §8、`docs/需求单` OPT-41 对齐):
- **权限只有一处真相**:能力清单与知识库范围都从 Key 的 `group_id` 读(见
  `PermissionGroupService`);Key 本身只剩"凭据 + 归因"。改一次档位,其下全部凭据
  即时生效或收窄,不需要重发明文;
- **签发 = 给一个档位配凭据**,Admin+ 才能做(与"拉成员"同闸);`owner_user_id` 只是
  这把凭据以谁的身份行事,不再决定他能做什么;
- **双重校验**:`authenticate` = ① Key 本身有效(哈希命中、未吊销)② 归因成员身份仍在
  空间(get_membership 命中);此后路由授权天然跟随**该成员自己的角色**(各业务服务的
  `_require_role` 以 Caller.user 判定):人被移出 → Key 立即 401,降级 → 高权限路由被
  服务层 403,权限实时收敛;
- **明文只出现一次**:签发时返回完整 Key,库里只留 SHA-256 哈希与截断提示串;
- **认证 = 哈希等值查找**,不做明文比对,不遍历库;
- **授权判定不在本服务里**:`effective_permission(api_key)` 只负责把 Key 的档位交给
  生效权限合成器(见 `effective_permission.py`),能力闸与 KB 范围收窄由它统一实现 ——
  人(JWT)链路走同一个合成器,两条链路口径必然一致,不会再各改各的。

档位读取按请求缓存(`_group_cache`):一次请求内 Key → 档位最多查一次库,列表接口
不为每把 Key 各查一遍。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.service.effective_permission import (
    SOURCE_API_KEY,
    EffectivePermission,
    permission_of_group,
)
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.interfaces import (
    ApiKeyRepository,
    AuditRepository,
    KnowledgeBaseRepository,
    PermissionGroupRepository,
    SpaceRepository,
    UserRepository,
)
from app.domain.models import ApiKey, AuditLog, PermissionGroup, User

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
class ApiKeyWithOwner:
    """列表/详情视图:凭据 + 所属档位 + 归因成员(可能已被移出)+ 签发者名字。

    `group` 是这张视图里唯一的权限来源 —— 响应 DTO 直接照它出字段,旧列已无。
    """

    api_key: ApiKey
    group: PermissionGroup
    owner: User | None
    owner_role: int | None
    created_by_name: str | None


class ApiKeyService:
    def __init__(
        self,
        api_keys: ApiKeyRepository,
        spaces: SpaceRepository,
        kbs: KnowledgeBaseRepository,
        audit: AuditRepository,
        users: UserRepository | None = None,
        *,
        groups: PermissionGroupRepository,
    ) -> None:
        # users 为 OPT-26 新增依赖;groups 为 OPT-41 权限档位依赖(权限的唯一来源)
        self._keys = api_keys
        self._spaces = spaces
        self._kbs = kbs
        self._audit = audit
        self._users = users
        self._groups = groups
        # 服务实例按请求组装,所以这份缓存的作用域就是单个请求
        self._group_cache: dict[uuid.UUID, PermissionGroup] = {}

    # ---------------------------- 签发与管理 ----------------------------

    def create(
        self,
        space_id: uuid.UUID,
        user: User,
        owner_user_id: uuid.UUID,
        group_id: uuid.UUID,
        name: str,
        description: str = "",
        ip: str = "",
    ) -> CreatedApiKey:
        """给一个档位配一把凭据。

        这里**不**把 owner 挂到档位的成员列表上:成员列表表达的是"哪些人可以把这档
        设成自己的当前档"(Admin 显式分配),而 Key 是程序化凭据。签发时顺手加人,
        会让"给共享档位再签一把 Key"这个动作静默扩掉一档的成员集合。
        """
        # "能拉成员的身份才能配 API":门槛与拉成员一致(Admin+),且签发留痕
        self._require_role(space_id, user.id, Role.ADMIN)
        if self._spaces.get_member(space_id, owner_user_id) is None:
            raise AppError(
                ErrorCode.MEMBER_NOT_FOUND, "目标用户不是该空间成员", http_status=404
            )
        group = self._groups.get(group_id)
        # 跨空间的档位一律 404:把 A 空间的凭据指向 B 空间的权限就是串租户
        if group is None or group.space_id != space_id:
            raise AppError(ErrorCode.NOT_FOUND, "权限档位不存在", http_status=404)

        plaintext = _PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        api_key = ApiKey(
            space_id=space_id,
            created_by=user.id,
            owner_user_id=owner_user_id,
            group_id=group.id,
            name=name,
            description=description,
            key_hash=hash_api_key(plaintext),
            key_hint=_hint(plaintext),
        )
        self._group_cache[group.id] = group
        self._keys.create(api_key)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.API_KEY_CREATED,
            target=name,
            detail={
                "owner_user_id": str(owner_user_id),
                "group_id": str(group.id),
                "capabilities": list(group.capabilities or []),
                "kb_scope": "all" if group.all_kbs else str(len(group.kb_ids or [])),
            },
            ip=ip,
        )
        return CreatedApiKey(api_key=api_key, plaintext=plaintext)

    def list_for_space(self, space_id: uuid.UUID, user: User) -> list[ApiKeyWithOwner]:
        self._require_role(space_id, user.id, Role.VIEWER)
        keys = self._keys.list_for_space(space_id)
        self._prefetch_groups(keys)
        members = {u.id: (m, u) for m, u in self._spaces.list_members(space_id)}
        return [self._view(key, members) for key in keys]

    def describe(self, api_key: ApiKey) -> ApiKeyWithOwner:
        """单条视图:签发响应与详情页共用(响应字段与列表完全一致,前端不用两套映射)。"""
        members = {
            u.id: (m, u) for m, u in self._spaces.list_members(api_key.space_id)
        }
        return self._view(api_key, members)

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

    def owner_of(self, api_key: ApiKey) -> tuple[User | None, int | None]:
        """归因成员当前档案与角色(已被移出时对应项为 None)。"""
        bound = api_key.owner_user_id or api_key.created_by
        if bound is None or self._users is None:
            return None, None
        membership = self._spaces.get_member(api_key.space_id, bound)
        return self._users.get(bound), (membership.role if membership is not None else None)

    def authenticate(self, plaintext: str) -> ApiKey | None:
        """按哈希查找并校验可用性;任何不通过都返回 None(调用方归一为 401)。

        校验顺序刻意如此:格式 → 哈希命中 → 未吊销 → **归因成员仍是成员**。
        最后一步保证"人被移出/删除但 Key 还在跑"不会发生;存量旧 Key 无
        owner_user_id 时回退 created_by(迁移 0012 已回填,双保险)。
        """
        if not plaintext.startswith(_PREFIX):
            return None
        api_key = self._keys.get_by_hash(hash_api_key(plaintext))
        if api_key is None or api_key.revoked_at is not None:
            return None
        bound = api_key.owner_user_id or api_key.created_by
        if bound is None:
            return None
        if self._spaces.get_membership(api_key.space_id, bound) is None:
            return None
        api_key.last_used_at = datetime.now(UTC)
        self._keys.save(api_key)
        return api_key

    # ---------------------------- 授权判定 ----------------------------

    def effective_permission(self, api_key: ApiKey) -> EffectivePermission:
        """Key 的生效权限快照:权限来自它所属的档位(判定全在合成器里,N4)。"""
        return permission_of_group(
            self._group_of(api_key), source=SOURCE_API_KEY, api_key_id=api_key.id
        )

    # ---------------------------- 内部 ----------------------------

    def _prefetch_groups(self, keys: list[ApiKey]) -> None:
        """列表接口按 id 批量取档位,避免每把 Key 各查一次(N+1)。"""
        missing = {k.group_id for k in keys if k.group_id not in self._group_cache}
        if not missing:
            return
        for group in self._groups.list_by_ids(list(missing)):
            self._group_cache[group.id] = group

    def _group_of(self, api_key: ApiKey) -> PermissionGroup:
        """Key 的权限来源。FK 是 RESTRICT,档位不可能先于引用它的 Key 消失。"""
        group = self._group_cache.get(api_key.group_id)
        if group is None:
            group = self._groups.get(api_key.group_id)
            if group is None:
                raise AppError(
                    ErrorCode.API_KEY_CAPABILITY_DENIED,
                    "该 API Key 的权限档位不存在",
                    http_status=403,
                )
            self._group_cache[group.id] = group
        return group

    def _view(self, api_key: ApiKey, members: dict) -> ApiKeyWithOwner:
        bound = api_key.owner_user_id or api_key.created_by
        entry = members.get(bound) if bound is not None else None
        owner, role = (entry[1], entry[0].role) if entry is not None else (None, None)
        return ApiKeyWithOwner(
            api_key=api_key,
            group=self._group_of(api_key),
            owner=owner,
            owner_role=role,
            created_by_name=self._creator_name(api_key.created_by),
        )

    def _creator_name(self, created_by: uuid.UUID | None) -> str | None:
        if created_by is None or self._users is None:
            return None
        creator = self._users.get(created_by)
        return creator.username if creator else ""

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


def hash_api_key(plaintext: str) -> str:
    """认证查找键:SHA-256(明文)。Key 本身是高熵随机串,无需加盐/慢哈希。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def normalize_capabilities(raw: list[ApiCapability | str]) -> list[ApiCapability]:
    """拒绝未知能力名(拼错的能力名不能变成"静默失效")。Key 与权限档位共用这一份。"""
    try:
        return [ApiCapability(str(item)) for item in raw]
    except ValueError as exc:
        allowed = ", ".join(str(c) for c in ApiCapability)
        raise AppError(
            ErrorCode.VALIDATION,
            f"未知的能力名;允许值:{allowed}",
            http_status=422,
        ) from exc


def assert_kbs_in_space(
    kbs: KnowledgeBaseRepository, space_id: uuid.UUID, kb_ids: list[uuid.UUID]
) -> None:
    """范围里的库必须落在本空间内,否则等于把一个租户的凭据指向另一个租户的数据。"""
    for kb_id in kb_ids:
        kb = kbs.get(kb_id)
        if kb is None or kb.space_id != space_id:
            raise AppError(ErrorCode.KB_NOT_FOUND, f"知识库不存在:{kb_id}", http_status=404)


def _hint(plaintext: str) -> str:
    """展示用截断标识:保留首尾便于用户辨识,中间以 **** 遮掩。"""
    if len(plaintext) <= _HINT_HEAD + _HINT_TAIL:
        return plaintext
    return f"{plaintext[:_HINT_HEAD]}****{plaintext[-_HINT_TAIL:]}"
