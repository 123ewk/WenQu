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
    # 空间自配模型 base_url 的 SSRF 闸门(OPT-27):默认拦截内网/本机/元数据地址。
    # 只有"部署确需内网自建网关"时由运维显式开 1(此时内网边界交给网络策略);dev 环境恒放行。
    allow_private_model_urls: bool = Field(
        False, validation_alias="APP_ALLOW_PRIVATE_MODEL_URLS"
    )

    # ---- 检索 ----
    retrieval_min_score: float = Field(0.3, validation_alias="APP_RETRIEVAL_MIN_SCORE")

    # ---- 流式问答(OPT-3:断线宽限 —— 期间重连继续生成到完整,无人回来才中止) ----
    stream_grace_seconds: float = Field(5.0, validation_alias="APP_STREAM_GRACE_SECONDS")
    # 事件日志空闲 TTL:已完成且空闲超过该时长的流会被清道夫回收
    stream_log_ttl_seconds: float = Field(3600.0, validation_alias="APP_STREAM_LOG_TTL")
    # 问答流水线阶段序(OPT-5):逗号分隔,未知名/空值启动即失败(fail-closed)
    # OPT-45 P2 起在 compose 前插 recall_memory(记忆召回);删掉该阶段即退回"无记忆注入"旧链
    qa_pipeline: str = Field(
        "retrieve,cite,fallback,recall_memory,compose,generate,persist",
        validation_alias="APP_QA_PIPELINE",
    )
    # Agent 工具循环轮数上限(OPT-10);复读/预算护栏另在循环内固定
    agent_max_rounds: int = Field(20, validation_alias="APP_AGENT_MAX_ITERATIONS")

    # ---- 外部事实记忆与上下文压缩(OPT-45) ----
    memory_enabled: bool = Field(True, validation_alias="APP_MEMORY_ENABLED")
    memory_recall_top_k: int = Field(5, validation_alias="APP_MEMORY_RECALL_TOP_K")
    # 召回相似度阈值:低于此值的记忆不注入,防噪声记忆污染上下文
    memory_recall_min_sim: float = Field(0.30, validation_alias="APP_MEMORY_RECALL_MIN_SIM")
    # 单次 Agent 提问最多弹几条记忆提议(护栏,超出丢弃并记日志)
    memory_proposal_max_per_turn: int = Field(
        3, validation_alias="APP_MEMORY_PROPOSAL_MAX_PER_TURN"
    )
    # "忽略"留痕的去重窗口(天):命中未过期 dismissed 内容则不再弹同一提议
    memory_proposal_dismiss_ttl_days: int = Field(
        7, validation_alias="APP_MEMORY_PROPOSAL_DISMISS_TTL_DAYS"
    )

    # ---- 历史滚动压缩(OPT-45 P3) ----
    compact_enabled: bool = Field(True, validation_alias="APP_COMPACT_ENABLED")
    # 生成前自动兜底阈值:上一轮上下文占用 > 窗口×此比例时,先同步压缩再作答
    compact_trigger_ratio: float = Field(0.8, validation_alias="APP_COMPACT_TRIGGER_RATIO")
    # 压缩后仍原样保留的最近消息条数(无摘要时回退"最近 10 条"旧行为)
    compact_keep_recent: int = Field(6, validation_alias="APP_COMPACT_KEEP_RECENT")
    # 低于此条数不压缩(太短没意义,且省一次摘要调用)
    compact_min_messages: int = Field(8, validation_alias="APP_COMPACT_MIN_MESSAGES")

    # ---- 演示模式(OPT-46):一键体验用的开关 + 种子账号/空间身份 ----
    # 默认 0 = 完全关闭(状态接口只回 enabled=false,前端演示入口不出现)。
    # 打开后 /api/v1/demo/status 会公开"演示账号叫什么、种子数据是否就绪";
    # 密码不在其中 —— 只有 scripts/seed_demo.py 与自动演示脚本从 APP_DEMO_PASSWORD 读。
    demo_mode: bool = Field(False, validation_alias="APP_DEMO_MODE")
    demo_username: str = Field("demo", validation_alias="APP_DEMO_USERNAME")
    demo_password: str = Field("", validation_alias="APP_DEMO_PASSWORD")
    # 种子脚本与状态接口共用同一套名字:改名即换一套演示数据(旧数据不会被误删)
    demo_space_name: str = Field("演示空间", validation_alias="APP_DEMO_SPACE_NAME")
    demo_kb_name: str = Field("大模型应用工程知识库", validation_alias="APP_DEMO_KB_NAME")

    # ---- 上传 ----
    upload_max_mb: int = Field(50, validation_alias="APP_UPLOAD_MAX_MB")
    avatar_max_mb: int = Field(2, validation_alias="APP_AVATAR_MAX_MB")

    # ---- 认证与加密 ----
    jwt_secret: str = Field("", validation_alias="APP_JWT_SECRET")
    master_key: str = Field("", validation_alias="APP_MASTER_KEY")
    access_token_minutes: int = Field(24 * 60, validation_alias="APP_ACCESS_TOKEN_MINUTES")
    refresh_token_days: int = Field(7, validation_alias="APP_REFRESH_TOKEN_DAYS")
    audit_retention_days: int = Field(180, validation_alias="APP_AUDIT_RETENTION_DAYS")

    # ---- OPT-41 N1 灰度开关:核心接口(问答/检索/文档增删)是否强制"人也要有生效档位" ----
    # 默认 0 = 人链路只看角色(与今天一致)。上线前必须先给每个真人成员配好档位并设为
    # 当前档,否则打开那一刻所有人都会掉进禁用态(需求单 Q6 选的"无档位即全禁")。
    # 只影响人(JWT)链路;程序(X-API-Key)链路一直都按档位收口,不受此开关影响。
    require_permission_group_for_core: bool = Field(
        False, validation_alias="APP_REQUIRE_PERMISSION_GROUP_FOR_CORE"
    )
    # OPT-48:上述开关落库后,DB 值的进程内缓存 TTL(秒)。本进程写后立即失效;
    # 其它 worker 最多 TTL 秒后跟上——开关不是权限本身,晚跟上只是晚开始拒绝,不会越权。
    permission_enforcement_cache_ttl_seconds: int = Field(
        60, validation_alias="APP_PERMISSION_ENFORCEMENT_CACHE_TTL_SECONDS"
    )

    # ---- 可观测(留空 = 关闭,nil-safe 零开销) ----
    otlp_endpoint: str = Field("", validation_alias="APP_OTLP_ENDPOINT")

    # ---- 网络身份(OPT-12/翻转项 #8):可信代理 CIDR 逗号分隔,如 "127.0.0.1,10.0.0.0/8" ----
    # 留空 = 不信任任何代理,XFF 完全不参与取值(fail-closed);非法网段在启动/测试期即报错
    trusted_proxies: str = Field("", validation_alias="APP_TRUSTED_PROXIES")

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
