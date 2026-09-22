"""生效权限合成(OPT-41 N4):一次调用"能做什么、能看哪些库"只在这里算一次。

为什么要收口:同一个动作有两条来路 —— 程序带 `X-API-Key` 明文,人带 JWT(服务端解析
他当前挂载的档位)。OPT-41 之前判定散在六处(路由授权表、`ApiKeyService` 的两个方法、
问答/检索/知识库/会话四个路由里各自的 `if caller.api_key is not None`),换成"口径 A
全交集"后散着改必然改漏。现在路由与服务只消费 `EffectivePermission` 的结果。

四条口径(与 `docs/需求单` OPT-41 §4 对齐):
- **权限来源只有一个档位**(`permission_groups` 是唯一真相);Key 与"成员挂载"只是两
  种指向它的方式,所以两条链路算出来的东西同构,不需要两套判定;
- **口径 A**:动作 = 档位能力,数据范围 = 档位知识库。角色阶梯只做上限,由各业务服务
  的 `_require_role` 继续把关(本模块不重复判角色);
- **边界(§4.2)**:交集只作用在「双凭据强制清单」内的接口。清单外的管理接口(空间、
  成员、档位、Key、模型配置、`/admin/*`)**只看角色**,`perm` 给 None —— 否则 Admin 在
  还没有档位时会连"给自己建一档"的入口都没有,空间直接失去管理能力;
- **fail-closed**:能力清单为空 = 什么都不允许;`all_kbs=false` 且没勾库 = 一个库都不给。
  放开全部库必须是显式勾选 `all_kbs`,且**每次请求实时展开**(存快照会让新建的库永远
  进不了范围)。

错误码沿用 API Key 那一对(`API_KEY_CAPABILITY_DENIED` / `API_KEY_SCOPE_DENIED`),已接入
的第三方程序零回归;人这条链路用同一对码,靠 `details.actor = "member"` 和更具体的中文
文案区分 —— 前端据此提示"你的档位没有这个能力",而不是"你角色不够"或"请重新登录"。
"根本没有生效档位"是新情况,单独一个 `PERMISSION_GROUP_REQUIRED`(403;用 401 会被前端
踢成重新登录,而人确实登录着)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability
from app.domain.interfaces import PermissionGroupRepository, SpaceRepository
from app.domain.models import PermissionGroup

# 权限来源:程序凭据 or 成员挂载。进 details 供前端分文案,不进契约做逻辑判断
Source = Literal["api_key", "member"]
SOURCE_API_KEY: Source = "api_key"
SOURCE_MEMBER: Source = "member"

_CAPABILITY_LABELS: dict[ApiCapability, str] = {
    ApiCapability.CHAT: "对话检索",
    ApiCapability.DOCUMENTS: "文档管理",
}


@dataclass(frozen=True)
class EffectivePermission:
    """一次调用的实际权限快照(来自档位;`api_key_id` 仅用于审计/排查时回指凭据)。"""

    source: Source
    group_id: uuid.UUID
    group_name: str
    capabilities: frozenset[str]
    all_kbs: bool
    kb_ids: frozenset[uuid.UUID]
    api_key_id: uuid.UUID | None = None

    def requires_capability(self, capability: ApiCapability) -> bool:
        return str(capability) in self.capabilities

    def require_capability(self, capability: ApiCapability) -> None:
        """能力闸:档位没这个能力就 403(口径 A —— 人在网页上也一样受档位约束)。"""
        if self.requires_capability(capability):
            return
        raise AppError(
            ErrorCode.API_KEY_CAPABILITY_DENIED,
            f"{self._owner_label()}「{self.group_name}」未授予"
            f"「{_CAPABILITY_LABELS.get(capability, str(capability))}」能力",
            http_status=403,
            details=self._details(capability=str(capability)),
        )

    def resolve_kb_scope(
        self, requested: list[uuid.UUID] | None
    ) -> list[uuid.UUID] | None:
        """把请求的 KB 列表按档位范围收窄;返回 None = 不限库(`all_kbs`)。

        范围外的库被剔除;若请求**只**指向范围外的库,直接 403 —— 绝不静默降级成"全部",
        也绝不降级成"他的档位范围"(静默扩权比报错难查得多)。
        """
        if self.all_kbs:
            return None
        if not self.kb_ids:
            raise AppError(
                ErrorCode.API_KEY_SCOPE_DENIED,
                f"{self._owner_label()}「{self.group_name}」没有勾选任何知识库",
                http_status=403,
                details=self._details(reason="empty_scope"),
            )
        if not requested:
            return sorted(self.kb_ids)
        narrowed = [kb_id for kb_id in requested if kb_id in self.kb_ids]
        if not narrowed:
            raise AppError(
                ErrorCode.API_KEY_SCOPE_DENIED,
                f"{self._owner_label()}「{self.group_name}」无权访问请求的知识库",
                http_status=403,
                details=self._details(reason="out_of_scope"),
            )
        return narrowed

    def allows_kb(self, kb_id: uuid.UUID) -> bool:
        """单库判定(路径里带 kb_id 的路由用);越界由调用方按 403 处理。"""
        return self.all_kbs or kb_id in self.kb_ids

    def _owner_label(self) -> str:
        """错误文案的主语:程序链路说 Key,人链路说"你当前的档位"。"""
        return "该 API Key 的档位" if self.source == SOURCE_API_KEY else "你当前的权限档位"

    def _details(self, **extra: object) -> dict[str, object]:
        return {
            "actor": self.source,
            "group_id": str(self.group_id),
            "group_name": self.group_name,
            **extra,
        }


def permission_of_group(
    group: PermissionGroup,
    *,
    source: Source,
    api_key_id: uuid.UUID | None = None,
) -> EffectivePermission:
    """把一个档位快照成生效权限(不查库,调用方已持有档位行)。"""
    return EffectivePermission(
        source=source,
        group_id=group.id,
        group_name=group.name,
        capabilities=frozenset(str(c) for c in (group.capabilities or [])),
        all_kbs=bool(group.all_kbs),
        kb_ids=frozenset(_parse_uuid_list(group.kb_ids)),
        api_key_id=api_key_id,
    )


def permission_of_member(
    groups: PermissionGroupRepository,
    spaces: SpaceRepository,
    space_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EffectivePermission:
    """人这条链路:JWT 成员 → 他当前挂载的档位(明文永不进浏览器,N1)。

    非成员先按 404 挡掉,与业务服务同口径(防枚举:不给"这个空间存在但没给你配档"
    这种可探测的差异)。没有 current 挂载 → 403 `PERMISSION_GROUP_REQUIRED`。
    """
    if spaces.get_member(space_id, user_id) is None:
        raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
    mount = groups.get_current(space_id, user_id)
    if mount is None:
        raise AppError(
            ErrorCode.PERMISSION_GROUP_REQUIRED,
            "本空间还没有为你分配权限档位,请联系管理员",
            http_status=403,
            details={"space_id": str(space_id)},
        )
    group = groups.get(mount.group_id)
    if group is None:  # 挂载行指向已消失的档位(FK 是 CASCADE,理论上到不了这一步)
        raise AppError(
            ErrorCode.PERMISSION_GROUP_REQUIRED,
            "你当前的权限档位已失效,请联系管理员重新分配",
            http_status=403,
            details={"group_id": str(mount.group_id), "reason": "group_missing"},
        )
    return permission_of_group(group, source=SOURCE_MEMBER)


def _parse_uuid_list(raw: list | None) -> list[uuid.UUID]:
    """JSONB 里存的是字符串;解析失败的值直接丢弃(视为不在范围内)。"""
    result: list[uuid.UUID] = []
    for item in raw or []:
        try:
            result.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return result
