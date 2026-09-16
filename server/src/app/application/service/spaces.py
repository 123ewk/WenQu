"""空间服务:CRUD 与成员管理(数值角色阶梯 RBAC,基准 04)。

守卫语义:
- 非成员访问空间资源 → 404(与"不存在"不可区分,防枚举);
- 是成员但角色不足 → 403;
- 授权规则:授予/操作的目标角色必须**严格低于**操作者角色,唯一例外是 Owner 转让
  (转给他人 Owner 后自己降为 Admin,避免"最后一个 Owner"死锁)。
"""

from __future__ import annotations

import uuid

from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction, Role
from app.domain.interfaces import AuditRepository, SpaceRepository, UserRepository
from app.domain.models import AuditLog, Membership, Space, User


class SpaceService:
    def __init__(
        self,
        spaces: SpaceRepository,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        self._spaces = spaces
        self._users = users
        self._audit = audit

    # ---------------------------- 空间 CRUD ----------------------------

    def create(self, user: User, name: str, description: str, ip: str = "") -> tuple[Space, int]:
        space = Space(name=name, description=description, created_by=user.id)
        self._spaces.create(space)
        self._spaces.add_member(
            Membership(space_id=space.id, user_id=user.id, role=Role.OWNER)
        )
        self._log(
            actor_id=user.id,
            space_id=space.id,
            action=AuditAction.SPACE_CREATED,
            target=name,
            ip=ip,
        )
        return space, Role.OWNER

    def list_for_user(self, user: User) -> list[tuple[Space, int]]:
        return self._spaces.list_for_user(user.id)

    def get_with_role(self, space_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Space, int]:
        space = self._spaces.get(space_id)
        membership = (
            self._spaces.get_membership(space_id, user_id) if space is not None else None
        )
        if space is None or membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        return space, membership.role

    def update(
        self, space_id: uuid.UUID, user: User, name: str, description: str, ip: str = ""
    ) -> Space:
        space, _role = self._require_role(space_id, user.id, Role.ADMIN)
        space.name = name
        space.description = description
        self._spaces.save(space)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.SPACE_UPDATED,
            target=name,
            ip=ip,
        )
        return space

    def delete(self, space_id: uuid.UUID, user: User, ip: str = "") -> None:
        space, _role = self._require_role(space_id, user.id, Role.OWNER)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.SPACE_DELETED,
            target=space.name,
            detail={"name": space.name},
            ip=ip,
        )
        self._spaces.delete(space)  # 成员级联由 FK CASCADE;知识库级联随 M2 扩展

    # ---------------------------- 成员管理 ----------------------------

    def list_members(self, space_id: uuid.UUID, user: User) -> list[tuple[Membership, User]]:
        self.get_with_role(space_id, user.id)  # 任意成员可查看成员列表
        return self._spaces.list_members(space_id)

    def add_member(
        self, space_id: uuid.UUID, actor: User, username: str, role_value: int, ip: str = ""
    ) -> tuple[Membership, User]:
        _space, actor_role = self._require_role(space_id, actor.id, Role.ADMIN)
        target_role = Role.from_value(role_value)
        if actor_role <= target_role:
            raise AppError(
                ErrorCode.FORBIDDEN, "不能授予不低于自己角色的成员身份", http_status=403
            )
        target = self._users.get_by_username(username)
        if target is None:
            raise AppError(ErrorCode.USER_NOT_FOUND, "该用户尚未注册", http_status=404)
        if self._spaces.get_membership(space_id, target.id) is not None:
            raise AppError(ErrorCode.MEMBER_ALREADY, "该用户已是空间成员", http_status=409)
        membership = self._spaces.add_member(
            Membership(space_id=space_id, user_id=target.id, role=target_role)
        )
        self._log(
            actor_id=actor.id,
            space_id=space_id,
            action=AuditAction.MEMBER_ADDED,
            target=username,
            detail={"role": int(target_role), "user_id": str(target.id)},
            ip=ip,
        )
        return membership, target

    def change_role(
        self,
        space_id: uuid.UUID,
        actor: User,
        target_user_id: uuid.UUID,
        role_value: int,
        ip: str = "",
    ) -> tuple[Membership, User]:
        _space, actor_membership = self._require_role_with_membership(
            space_id, actor.id, Role.ADMIN
        )
        actor_role = Role(actor_membership.role)
        target = self._spaces.get_member(space_id, target_user_id)
        if target is None:
            raise AppError(ErrorCode.MEMBER_NOT_FOUND, "成员不存在", http_status=404)
        target_user = self._users.get(target.user_id)
        if target_user is None:
            raise AppError(ErrorCode.MEMBER_NOT_FOUND, "成员不存在", http_status=404)
        if target.user_id == actor.id:
            raise AppError(ErrorCode.FORBIDDEN, "不能修改自己的角色", http_status=403)
        target_role_value = Role.from_value(role_value)

        if target_role_value == Role.OWNER:
            # 转让所有权:仅 Owner 可发起;原 Owner 降级 Admin,保证有主且不死锁
            if actor_role != Role.OWNER:
                raise AppError(ErrorCode.FORBIDDEN, "仅空间所有者可以转让所有权", http_status=403)
            if Role(target.role) == Role.OWNER:
                raise AppError(ErrorCode.FORBIDDEN, "该成员已是所有者", http_status=403)
            target.role = Role.OWNER
            actor_membership.role = Role.ADMIN
            self._spaces.update_member(actor_membership)
            detail = {"role": int(Role.OWNER), "transfer": True}
        else:
            if actor_role <= target_role_value:
                raise AppError(
                    ErrorCode.FORBIDDEN, "不能授予不低于自己角色的成员身份", http_status=403
                )
            if actor_role <= Role(target.role):
                raise AppError(ErrorCode.FORBIDDEN, "不能操作不低于自己的成员", http_status=403)
            target.role = target_role_value
            detail = {"role": int(target_role_value)}

        self._spaces.update_member(target)
        self._log(
            actor_id=actor.id,
            space_id=space_id,
            action=AuditAction.MEMBER_ROLE_CHANGED,
            target=target_user.username,
            detail=detail,
            ip=ip,
        )
        return target, target_user

    def remove_member(
        self, space_id: uuid.UUID, actor: User, target_user_id: uuid.UUID, ip: str = ""
    ) -> None:
        _space, actor_role = self._require_role(space_id, actor.id, Role.ADMIN)
        target = self._spaces.get_member(space_id, target_user_id)
        if target is None:
            raise AppError(ErrorCode.MEMBER_NOT_FOUND, "成员不存在", http_status=404)
        if target.user_id == actor.id:
            raise AppError(ErrorCode.FORBIDDEN, "不能移除自己,请使用退出空间", http_status=403)
        if Role(target.role) >= actor_role:
            raise AppError(ErrorCode.FORBIDDEN, "不能移除不低于自己的成员", http_status=403)
        target_user = self._users.get(target.user_id)
        self._spaces.remove_member(target)
        self._log(
            actor_id=actor.id,
            space_id=space_id,
            action=AuditAction.MEMBER_REMOVED,
            target=target_user.username if target_user else str(target.user_id),
            ip=ip,
        )

    def leave(self, space_id: uuid.UUID, user: User, ip: str = "") -> None:
        membership = self._spaces.get_membership(space_id, user.id)
        if membership is None or self._spaces.get(space_id) is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if Role(membership.role) == Role.OWNER:
            raise AppError(
                ErrorCode.FORBIDDEN, "所有者不能退出空间,请先转让所有权或删除空间", http_status=403
            )
        self._spaces.remove_member(membership)
        self._log(
            actor_id=user.id,
            space_id=space_id,
            action=AuditAction.MEMBER_REMOVED,
            target=user.username,
            detail={"self_leave": True},
            ip=ip,
        )

    def list_audit(
        self, space_id: uuid.UUID, user: User, limit: int, offset: int
    ) -> tuple[list[AuditLog], int]:
        self._require_role(space_id, user.id, Role.ADMIN)
        return self._audit.list_for_space(space_id, limit, offset)

    # ---------------------------- 守卫 ----------------------------

    def _require_role(
        self, space_id: uuid.UUID, user_id: uuid.UUID, minimum: Role
    ) -> tuple[Space, int]:
        space, role = self.get_with_role(space_id, user_id)
        if role < minimum:
            raise AppError(ErrorCode.FORBIDDEN, "权限不足", http_status=403)
        return space, role

    def _require_role_with_membership(
        self, space_id: uuid.UUID, user_id: uuid.UUID, minimum: Role
    ) -> tuple[Space, Membership]:
        space, _role = self._require_role(space_id, user_id, minimum)
        membership = self._spaces.get_membership(space_id, user_id)
        assert membership is not None  # _require_role 已保证成员存在
        return space, membership

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
