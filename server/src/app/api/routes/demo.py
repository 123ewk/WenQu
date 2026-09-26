"""演示模式状态(OPT-46):登录页用的只读入口,不需要凭据。

为什么单独一个接口:自动演示要在"新用户还没登录"时就知道演示入口在不在、
种子数据(demo 账号 + 演示空间 + 知识库文档)是否已就绪,好决定是显示"一键体验"
还是提示先跑 make demo-seed。这三件事用已登录接口都问不到。

纪律:
- APP_DEMO_MODE 关时只回 enabled=false,其余字段一律留空(不给探测者任何线索);
- 密码**永不**出现在响应里 —— 接口只给用户名,密码由 seed 脚本与演示脚本读环境变量;
- ready 用最保守的判据:演示空间里 0 篇失败、0 篇仍在入库、completed > 0。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_kb_stats_repository, get_space_repository, get_user_repository
from app.core.config import get_settings
from app.domain.enums import DocumentStatus
from app.domain.interfaces import KbStatsRepository, SpaceRepository, UserRepository

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])


class DemoStatusOut(BaseModel):
    enabled: bool
    username: str = ""
    space_name: str = ""
    kb_name: str = ""
    document_total: int = 0
    document_ready: int = 0
    ready: bool = False


@router.get("/status", response_model=DemoStatusOut)
def demo_status(
    users: UserRepository = Depends(get_user_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    stats: KbStatsRepository = Depends(get_kb_stats_repository),
) -> DemoStatusOut:
    """未登录可读:演示入口 + 种子就绪状态(身份全部来自配置,不新增表)。"""
    settings = get_settings()
    if not settings.demo_mode:
        return DemoStatusOut(enabled=False)

    disabled_hint = DemoStatusOut(
        enabled=True,
        username=settings.demo_username,
        space_name=settings.demo_space_name,
        kb_name=settings.demo_kb_name,
    )
    demo_user = users.get_by_username(settings.demo_username)
    if demo_user is None:
        return disabled_hint
    space = next(
        (
            space
            for space, _role in spaces.list_for_user(demo_user.id)
            if space.name == settings.demo_space_name
        ),
        None,
    )
    if space is None:
        return disabled_hint

    counts = stats.counts_by_status(space.id)
    total = sum(counts.values())
    completed = counts.get(DocumentStatus.COMPLETED.value, 0)
    failed = counts.get(DocumentStatus.FAILED.value, 0)
    return DemoStatusOut(
        enabled=True,
        username=settings.demo_username,
        space_name=settings.demo_space_name,
        kb_name=settings.demo_kb_name,
        document_total=total,
        document_ready=completed,
        ready=completed > 0 and completed + failed == total,
    )
