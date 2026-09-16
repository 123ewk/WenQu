"""领域常量:角色数值阶梯与审计动作类型(状态机/枚举一律用常量,禁止裸字符串,基准 01)。"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Role(IntEnum):
    """空间角色数值阶梯:数值越大权限越高,授权判断只做数值比较(基准 04)。"""

    VIEWER = 10
    EDITOR = 20
    ADMIN = 30
    OWNER = 40

    @classmethod
    def from_value(cls, value: int) -> Role:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"非法角色值: {value}") from exc


class AuditAction(StrEnum):
    """审计动作全分类;新增动作必须在此注册(40+ 分类为长期目标,见基准 04)。"""

    AUTH_REGISTER = "auth.register"
    AUTH_LOGIN_SUCCESS = "auth.login_success"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    PASSWORD_CHANGED = "user.password_changed"
    SPACE_CREATED = "space.created"
    SPACE_UPDATED = "space.updated"
    SPACE_DELETED = "space.deleted"
    MEMBER_ADDED = "space.member_added"
    MEMBER_REMOVED = "space.member_removed"
    MEMBER_ROLE_CHANGED = "space.member_role_changed"
