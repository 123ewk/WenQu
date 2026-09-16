"""系统探活与排障入口(基准 02):/health 静态探活;/system/info 排障第一入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """静态探活:不做依赖检查,供负载均衡/容器编排使用。"""
    return {"status": "ok"}


@router.get("/system/info")
async def system_info() -> dict[str, Any]:
    """版本 + 迁移状态等排障信息;M2 起接入真实迁移版本(alembic current)与构建 commit。"""
    settings = get_settings()
    return {
        "name": "wenqu-server",
        "version": settings.version,
        "env": settings.env,
        "migration_version": None,  # M2 接入
        "migration_dirty": False,
        "commit": None,  # 构建时注入,M2 起 CI 写入
    }
