"""应用配置(基准 02):优先级 环境变量 > .env > 代码默认值,注释即文档。

fail-closed:APP_ENV=prod 时关键配置缺失直接拒绝启动。
密钥只经环境变量/.env 注入,禁止进 YAML;
安全开关的"未配置"与"显式关闭"语义不同,后续新增三态开关时在此标注。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version() -> str:
    """从仓库根 VERSION 单一真源读取;读不到时回退占位(仅发生在非源码运行)。"""
    try:
        return (Path(__file__).resolve().parents[4] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    env: Literal["dev", "prod"] = Field("dev", validation_alias="APP_ENV")
    version: str = Field(default_factory=_read_version)

    # ---- 数据存储 ----
    database_url: str = Field("", validation_alias="APP_DATABASE_URL")
    minio_endpoint: str = Field("localhost:9000", validation_alias="MINIO_ENDPOINT")
    minio_access_key: str = Field("", validation_alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field("", validation_alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field("wenqu-docs", validation_alias="MINIO_BUCKET")
    minio_secure: bool = Field(False, validation_alias="MINIO_SECURE")

    # ---- 解析服务 ----
    parser_grpc_addr: str = Field("localhost:50051", validation_alias="PARSER_GRPC_ADDR")
    parser_grpc_token: str = Field("", validation_alias="PARSER_GRPC_TOKEN")

    # ---- 模型(ADR-4:后台任务并发预算信号量,供应商限速的保守起配) ----
    model_bg_concurrency: int = Field(2, validation_alias="APP_MODEL_BG_CONCURRENCY")

    # ---- 检索 ----
    retrieval_min_score: float = Field(0.3, validation_alias="APP_RETRIEVAL_MIN_SCORE")

    # ---- 流式问答(OPT-3:断线宽限 —— 期间重连继续生成到完整,无人回来才中止) ----
    stream_grace_seconds: float = Field(5.0, validation_alias="APP_STREAM_GRACE_SECONDS")
    # 事件日志空闲 TTL:已完成且空闲超过该时长的流会被清道夫回收
    stream_log_ttl_seconds: float = Field(3600.0, validation_alias="APP_STREAM_LOG_TTL")
    # 问答流水线阶段序(OPT-5):逗号分隔,未知名/空值启动即失败(fail-closed)
    qa_pipeline: str = Field(
        "retrieve,cite,fallback,compose,generate,persist",
        validation_alias="APP_QA_PIPELINE",
    )

    # ---- 上传 ----
    upload_max_mb: int = Field(50, validation_alias="APP_UPLOAD_MAX_MB")
    avatar_max_mb: int = Field(2, validation_alias="APP_AVATAR_MAX_MB")

    # ---- 认证与加密 ----
    jwt_secret: str = Field("", validation_alias="APP_JWT_SECRET")
    master_key: str = Field("", validation_alias="APP_MASTER_KEY")
    access_token_minutes: int = Field(24 * 60, validation_alias="APP_ACCESS_TOKEN_MINUTES")
    refresh_token_days: int = Field(7, validation_alias="APP_REFRESH_TOKEN_DAYS")
    audit_retention_days: int = Field(180, validation_alias="APP_AUDIT_RETENTION_DAYS")

    # ---- 可观测(留空 = 关闭,nil-safe 零开销) ----
    otlp_endpoint: str = Field("", validation_alias="APP_OTLP_ENDPOINT")

    @model_validator(mode="after")
    def _prod_must_be_explicit(self) -> Settings:
        if self.env == "prod":
            missing = [
                name
                for name, value in (
                    ("APP_DATABASE_URL", self.database_url),
                    ("APP_JWT_SECRET", self.jwt_secret),
                    ("APP_MASTER_KEY", self.master_key),
                    ("PARSER_GRPC_TOKEN", self.parser_grpc_token),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"APP_ENV=prod 缺少必填配置,拒绝启动(fail-closed):{', '.join(missing)}"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
