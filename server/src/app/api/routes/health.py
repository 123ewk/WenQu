"""系统探活与排障入口(基准 02):/health 静态探活;/system/info 排障第一入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.migrations import migration_status

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """静态探活:不做依赖检查,供负载均衡/容器编排使用。"""
    return {"status": "ok"}


@router.get("/system/info")
async def system_info() -> dict[str, Any]:
    """版本 + 真实迁移状态(上线七步:迁移后确认非 dirty);DB 不可达时 current=null 且 dirty=true。"""
    settings = get_settings()
    status = migration_status()
    return {
        "name": "wenqu-server",
        "version": settings.version,
        "env": settings.env,
        "migration_version": status.current,
        "migration_head": status.head,
        "migration_dirty": status.dirty,
        "commit": None,  # 构建时注入(WEB_COMMIT 构建参数),镜像未注入则为 null
    }
