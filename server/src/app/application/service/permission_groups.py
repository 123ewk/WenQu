"""权限档位服务(OPT-41):一个空间内"一种权限"的定义、分配与修复。

为什么要有这一层(与 `docs/架构设计.md` §8、需求单 §4 对齐):

- **权限只有一处真相**:能力清单与知识库范围挂在档位上,`api_keys` 退化成档位的
  程序化凭据(一个档位可多把明文,轮换互不影响),成员通过挂载关系挂到档位上;
- **改一次全档生效**(N8"修复权限"):PATCH 档位即改范围,不需要重发明文 Key ——
  第三方程序手里的 Key 继续可用,这一点是"吊销重签"做不到的;
- **档位不是成员自助的**:`assign`/`unassign`/`set_current` 一律 Admin+。允许成员
  自己挂到某个档位 = 允许自己给自己提权(挑一个范围更大的档位挂上去);
- **每人每空间至多一档 current**:切换由 `clear_current` + 置位完成,库层部分唯一
  索引兜底并发(与 OPT-31 的 `api_keys.is_active` 同构,语义从"Key 上的标记"变成
  "成员指针",因为一把 Key 现在可能对多人生效);
- **范围 fail-closed**:`all_kbs = false` 且 `kb_ids = []` 是合法状态,含义是"一个库
  都不给"(与"能力清单为空 = 什么都不允许"同构)。旧数据由迁移 0017 按 `all_kbs =
  (旧 kb_ids 为空)` 回填,所以"全部库"必须显式勾选,不会因为漏填而静默放开全空间;
- **删除有门槛**:档位下还挂着凭据或成员时拒绝删除,不做"顺手级联"。级联会一次
  性抹掉一批人/一批程序的权限,而且明文 Key 不可回显,事后无法向使用方交代。

校验一律复用 API Key 那一份(`normalize_capabilities` / `assert_kbs_in_space`),
两条链路对同一个拼错的能力名、同一个跨租户知识库 id 必须给出同一个错误。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.application.service.api_keys import assert_kbs_in_space, normalize_capabilities
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
from app.domain.models import AuditLog, PermissionGroup, PermissionGroupMember, User


@dataclass(frozen=True)
class GroupMemberView:
    """档位上的一个成员:档案可能已注销(取不到),角色按本空间当前值实时取。"""

    user_id: uuid.UUID
    username: str | None
    nickname: str | None
    role: int | None  # 已被移出空间则为 None —— 挂载行留着也没有权限,由前端置灰
    is_current: bool


@dataclass(frozen=True)
class PermissionGroupView:
    """管理页视图:档位本身 + 挂在上面的人(含"谁是当前档")。"""

    group: PermissionGroup
    members: list[GroupMemberView]


@dataclass(frozen=True)
class _Scope:
    """归一化后的范围:`all_kbs=true` 时 `kb_ids` 必为空,反之按列表逐库判定。"""

    all_kbs: bool
    kb_ids: list[uuid.UUID]


class PermissionGroupService:
    def __init__(
        self,
        groups: PermissionGroupRepository,
        spaces: SpaceRepository,
        kbs: KnowledgeBaseRepository,
        audit: AuditRepository,
        api_keys: ApiKeyRepository,
        users: UserRepository,
    ) -> None:
        self._groups = groups
        self._spaces = spaces
        self._kbs = kbs
        self._audit = audit
        self._api_keys = api_keys
        self._users = users

    # ---------------------------- 档位本身 ----------------------------

    def create(
        self,
        space_id: uuid.UUID,
        user: User,
        name: str,
        description: str,
        capabilities: list[ApiCapability | str],
        kb_ids: list[uuid.UUID],
        all_kbs: bool,
        ip: str = "",
    ) -> PermissionGroup:
        self._require_role(space_id, user.id, Role.ADMIN)
        caps = normalize_capabilities(capabilities)
        scope = self._normalize_scope(space_id, kb_ids, all_kbs)
        group = self._groups.create(
            PermissionGroup(
                space_id=space_id,
                name=name,
                description=description,
                capabilities=[str(c) for c in caps],
                kb_ids=[str(kb_id) for kb_id in scope.kb_ids],
                all_kbs=scope.all_kbs,
                created_by=user.id,
            )
        )
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_CREATED,
            target=name,
            detail={
                "group_id": str(group.id),
                "capabilities": list(group.capabilities or []),
                "kb_scope": self._scope_label(group),
            },
            ip=ip,
        )
        return group

    def update(
        self,
        space_id: uuid.UUID,
        user: User,
        group_id: uuid.UUID,
        name: str,
        description: str,
        capabilities: list[ApiCapability | str],
        kb_ids: list[uuid.UUID],
        all_kbs: bool,
        ip: str = "",
    ) -> PermissionGroup:
        """N8"修复权限":整组替换能力与范围(不做半字段 PATCH 的部分更新语义)。

        审计必须记**改前/改后**,否则无法回答"谁把范围从 3 个库放成了全部库"。
        """
        self._require_role(space_id, user.id, Role.ADMIN)
        group = self._get_in_space(space_id, group_id)
        before_caps = list(group.capabilities or [])
        before_scope = self._scope_label(group)
        caps = normalize_capabilities(capabilities)
        scope = self._normalize_scope(space_id, kb_ids, all_kbs)
        group.name = name
        group.description = description
        group.capabilities = [str(c) for c in caps]
        group.kb_ids = [str(kb_id) for kb_id in scope.kb_ids]
        group.all_kbs = scope.all_kbs
        self._groups.save(group)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_UPDATED,
            target=name,
            detail={
                "group_id": str(group.id),
                "capabilities_before": before_caps,
                "capabilities_after": list(group.capabilities or []),
                "kb_scope_before": before_scope,
                "kb_scope_after": self._scope_label(group),
            },
            ip=ip,
        )
        return group

    def delete(
        self, space_id: uuid.UUID, user: User, group_id: uuid.UUID, ip: str = ""
    ) -> None:
        """只允许删"空档":还挂着凭据或成员的档位一律拒绝(见模块 docstring 的删除门槛)。"""
        self._require_role(space_id, user.id, Role.ADMIN)
        group = self._get_in_space(space_id, group_id)
        attached = [
            k for k in self._api_keys.list_for_space(space_id) if k.group_id == group.id
        ]
        if attached:
            raise AppError(
                ErrorCode.VALIDATION,
                f"该档位下还有 {len(attached)} 把 API Key,请先吊销或改指到别的档位",
                http_status=400,
                details={"group_id": str(group.id), "key_count": len(attached)},
            )
        member_count = len(self._groups.list_members(group.id))
        if member_count:
            raise AppError(
                ErrorCode.VALIDATION,
                f"该档位还挂着 {member_count} 个成员,请先解除分配",
                http_status=400,
                details={"group_id": str(group.id), "member_count": member_count},
            )
        self._groups.delete(group.id)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_DELETED,
            target=group.name,
            detail={"group_id": str(group.id)},
            ip=ip,
        )

    def get(self, space_id: uuid.UUID, user: User, group_id: uuid.UUID) -> PermissionGroupView:
        self._require_role(space_id, user.id, Role.VIEWER)
        group = self._get_in_space(space_id, group_id)
        return self._view(space_id, group)

    def list_for_space(self, space_id: uuid.UUID, user: User) -> list[PermissionGroupView]:
        self._require_role(space_id, user.id, Role.VIEWER)
        return [
            self._view(space_id, group)
            for group in self._groups.list_for_space(space_id)
        ]

    # ---------------------------- 成员分配 ----------------------------

    def assign(
        self,
        space_id: uuid.UUID,
        user: User,
        group_id: uuid.UUID,
        member_user_id: uuid.UUID,
        ip: str = "",
    ) -> PermissionGroupMember:
        """把人挂到档位上(幂等:重复分配返回既有挂载行,不报错也不产生第二行)。

        不自动置 current:分配与"用哪一档"是两个动作,前者可以一次给多人,
        后者决定他此刻的可检索范围,必须显式(见 set_current)。
        """
        self._require_role(space_id, user.id, Role.ADMIN)
        group = self._get_in_space(space_id, group_id)
        if self._spaces.get_member(space_id, member_user_id) is None:
            raise AppError(
                ErrorCode.MEMBER_NOT_FOUND, "目标用户不是该空间成员", http_status=404
            )
        existing = self._groups.get_member(group.id, member_user_id)
        if existing is not None:
            return existing
        member = self._groups.add_member(
            PermissionGroupMember(
                group_id=group.id, space_id=space_id, user_id=member_user_id
            )
        )
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_MEMBER_ASSIGNED,
            target=group.name,
            detail={"group_id": str(group.id), "member_user_id": str(member_user_id)},
            ip=ip,
        )
        return member

    def unassign(
        self,
        space_id: uuid.UUID,
        user: User,
        group_id: uuid.UUID,
        member_user_id: uuid.UUID,
        ip: str = "",
    ) -> None:
        """解除分配。若解除的正是他的当前档,他的 current 指针一并消失 ——
        这是 fail-closed(没有档位 = 没有可检索范围),不是 bug。"""
        self._require_role(space_id, user.id, Role.ADMIN)
        group = self._get_in_space(space_id, group_id)
        member = self._groups.get_member(group.id, member_user_id)
        if member is None:
            raise AppError(ErrorCode.NOT_FOUND, "该成员未挂在这个档位上", http_status=404)
        was_current = member.is_current
        self._groups.delete_member(member.id)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_MEMBER_UNASSIGNED,
            target=group.name,
            detail={
                "group_id": str(group.id),
                "member_user_id": str(member_user_id),
                "was_current": was_current,
            },
            ip=ip,
        )

    def set_current(
        self,
        space_id: uuid.UUID,
        user: User,
        group_id: uuid.UUID,
        member_user_id: uuid.UUID,
        ip: str = "",
    ) -> PermissionGroupMember:
        """换档 = 权限开关(N6/N7):只能 Admin+ 做,且目标必须已挂在该档位上。

        先清后置在同一事务里完成;并发下的双 current 由部分唯一索引兜底。
        """
        self._require_role(space_id, user.id, Role.ADMIN)
        group = self._get_in_space(space_id, group_id)
        member = self._groups.get_member(group.id, member_user_id)
        if member is None:
            raise AppError(
                ErrorCode.VALIDATION,
                "该成员尚未挂在这个档位上,请先分配",
                http_status=400,
                details={"group_id": str(group.id), "member_user_id": str(member_user_id)},
            )
        previous = self._groups.get_current(space_id, member_user_id)
        if member.is_current and (previous is None or previous.id == member.id):
            return member
        self._groups.clear_current(space_id, member_user_id)
        member.is_current = True
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.PERMISSION_GROUP_CURRENT_SET,
            target=group.name,
            detail={
                "group_id": str(group.id),
                "member_user_id": str(member_user_id),
                "previous_group_id": str(previous.group_id) if previous is not None else None,
            },
            ip=ip,
        )
        return member

    # ---------------------------- 内部 ----------------------------

    def _normalize_scope(
        self, space_id: uuid.UUID, kb_ids: list[uuid.UUID], all_kbs: bool
    ) -> _Scope:
        """两个字段表达一件事,所以要互相约束:`all_kbs` 为真时列表必须为空。

        留空列表 + `all_kbs=false` 是合法组合,含义是"一个库都不给"(fail-closed);
        与之相对,"全部库"必须显式勾选 —— 漏填不能变成放开整个空间。
        """
        if all_kbs:
            if kb_ids:
                raise AppError(
                    ErrorCode.VALIDATION,
                    "all_kbs 为真时不能再指定 kb_ids(两者表达同一件事的不同口径)",
                    http_status=422,
                )
            return _Scope(all_kbs=True, kb_ids=[])
        assert_kbs_in_space(self._kbs, space_id, kb_ids)
        return _Scope(all_kbs=False, kb_ids=list(kb_ids))

    def _get_in_space(self, space_id: uuid.UUID, group_id: uuid.UUID) -> PermissionGroup:
        """跨空间的档位一律 404:存在性本身不泄露(与 API Key 同口径)。"""
        group = self._groups.get(group_id)
        if group is None or group.space_id != space_id:
            raise AppError(ErrorCode.NOT_FOUND, "权限档位不存在", http_status=404)
        return group

    def _view(self, space_id: uuid.UUID, group: PermissionGroup) -> PermissionGroupView:
        members: list[GroupMemberView] = []
        for mount in self._groups.list_members(group.id):
            member = self._spaces.get_member(space_id, mount.user_id)
            user = self._users.get(mount.user_id)
            members.append(
                GroupMemberView(
                    user_id=mount.user_id,
                    username=user.username if user else None,
                    nickname=user.nickname if user else None,
                    role=member.role if member is not None else None,
                    is_current=mount.is_current,
                )
            )
        return PermissionGroupView(group=group, members=members)

    @staticmethod
    def _scope_label(group: PermissionGroup) -> str:
        """审计里用可读口径:`all` / `0`(空列表 = 不给任何库) / `3`(库数量)。"""
        if group.all_kbs:
            return "all"
        return str(len(group.kb_ids or []))

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
