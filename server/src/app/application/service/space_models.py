"""空间级模型配置服务(OPT-27):成员自己在空间里配对话/重排模型,embedding 只配 Key。

与全局层(OPT-25 `/admin/*`)的四点区别:

1. **层**:所有读写锁在本空间(`space_id` 谓词),跨空间一律 404 不泄露存在性;
   空间行的默认标记互斥也只在本空间内(基类 `_clear_defaults` 已带层谓词);
2. **能力面收窄**:embedding 模型本体内置锁死(`text-embedding-v3`/1024 维),
   空间不能增删 embedding 模型 —— 能配的只有它的 Key;因此空间层 kind 只开放
   chat 与 rerank,且 kind 必须与供应商协议匹配(重排协议只挂 rerank,清单见
   `registered_rerank_protocols()`,现两种:cohere 形态与百炼原生形态);
3. **门槛**:读 = 空间成员(Viewer+,前端按角色隐藏写按钮),写 = Admin+(Role 30);
4. **地址防护**:全局层的 base_url 是运维给的可信值;空间层则不然 —— 任何注册用户都能自助
   建空间、建完就是该空间 Owner,所以空间层多一道 SSRF 闸门(prod 拒内网/回环/元数据地址,dev 放行
   以用本地 Ollama/vLLM,私有化内网网关用 `APP_ALLOW_PRIVATE_MODEL_URLS=1` 显式豁免)。

方法名与基类错开(`add_provider` 而非 `create_provider`):空间层的入参不同
(供应商类型而非协议、无 dims),同名覆盖只会让两套接口互相签名打架。

【测试连接】探活走真实上游,但只取最小响应(首个流增量 / 一条重排结果),
失败统一翻成 424 `UPSTREAM_MODEL_ERROR` + `details.reason`(auth|network|model_name),
让配置页能指错地方;缺配置(未选模型)仍是 503,不伪装成上游故障。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.service.model_admin import ModelAdminService
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import (
    BUILTIN_EMBEDDING_BASE_URL,
    BUILTIN_EMBEDDING_DIMS,
    BUILTIN_EMBEDDING_DOC_URL,
    BUILTIN_EMBEDDING_MODEL_ID,
    BUILTIN_EMBEDDING_MODEL_NAME,
    BUILTIN_EMBEDDING_PROVIDER_KEY,
    ModelCatalog,
    ModelEntry,
    ProviderEntry,
)
from app.core.model_client import resolve_api_key
from app.core.model_protocols import (
    ModelCallError,
    get_protocol,
    get_rerank_protocol,
    registered_rerank_protocols,
)
from app.core.ssrf import assert_external_http_url
from app.domain.enums import AuditAction, Role
from app.domain.interfaces import SpaceRepository
from app.domain.models import CustomModel, ModelProvider, SpaceModelSettings, User

_COHERE_RERANK_PROTOCOL = "cohere_rerank"  # 平铺入参 + POST {base}/rerank
_DASHSCOPE_RERANK_PROTOCOL = "dashscope_rerank"  # input/parameters 嵌套 + 原生服务路径
# 哪些协议是重排协议取注册表派生,新增协议不用再抄清单
_RERANK_PROTOCOLS = frozenset(registered_rerank_protocols())
_CHAT_KIND = "chat"


@dataclass(frozen=True)
class ProviderType:
    """前端【供应商名字】下拉的一项(后端只给名字与预填值,不是白名单)。

    `default_base_url` 仅用于预填输入框,用户可以改成任何 http(s) 地址。
    `model_names` 是模型名的**提示清单**(前端下拉数据源),后端不校验、自定义必须可用;
    空元组 = 没有有意义的预设(本地/自建端点),前端渲染成纯输入框。
    """

    key: str
    label: str
    doc_url: str
    default_base_url: str
    protocol: str
    kinds: tuple[str, ...]
    model_names: tuple[str, ...] = ()


PROVIDER_TYPES: tuple[ProviderType, ...] = (
    ProviderType("deepseek", "DeepSeek", "https://platform.deepseek.com/api_keys",
                 "https://api.deepseek.com/v1", "openai_compatible", (_CHAT_KIND,),
                 ("deepseek-chat", "deepseek-reasoner")),
    ProviderType("dashscope", "阿里云百炼", "https://bailian.console.aliyun.com/",
                 "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "openai_compatible", (_CHAT_KIND,),
                 ("qwen-plus", "qwen-max", "qwen-turbo")),
    ProviderType("zhipu", "智谱 GLM", "https://open.bigmodel.cn/usercenter/apikeys",
                 "https://open.bigmodel.cn/api/paas/v4", "openai_compatible", (_CHAT_KIND,),
                 ("glm-4-plus", "glm-4-flash")),
    ProviderType("moonshot", "Kimi", "https://platform.moonshot.cn/console/api-keys",
                 "https://api.moonshot.cn/v1", "openai_compatible", (_CHAT_KIND,),
                 ("kimi-k2-0711-preview", "moonshot-v1-8k")),
    # 豆包要填的是控制台自建"接入点 id"(逐账号不同),给预设等于给错值
    ProviderType("doubao", "豆包",
                 "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
                 "https://ark.cn-beijing.volces.com/api/v3", "openai_compatible",
                 (_CHAT_KIND,)),
    ProviderType("minimax", "MiniMax",
                 "https://platform.minimaxi.com/user-center/basic-information/interface-key",
                 "https://api.minimax.chat/v1", "openai_compatible", (_CHAT_KIND,),
                 ("MiniMax-Text-01",)),
    ProviderType("openai_compatible", "OpenAI 兼容自建", "",
                 "https://api.openai.com/v1", "openai_compatible", (_CHAT_KIND,),
                 ("gpt-4o-mini", "gpt-4o")),
    ProviderType("ollama", "Ollama(本地)", "https://ollama.com/library",
                 "http://127.0.0.1:11434/v1", "openai_compatible", (_CHAT_KIND,),
                 ("qwen2.5:7b", "llama3.1:8b")),
    ProviderType("vllm", "vLLM(本地)", "https://docs.vllm.ai/",
                 "http://127.0.0.1:8000/v1", "openai_compatible", (_CHAT_KIND,)),
    # base_url 只到站点根:原生端点的服务路径由协议适配器钉死,预填带 /v1 会拼出两段。
    # 旧 gte-rerank(无 -v2)2026-05-30 已下线,填它会拿到上游 model_name 类报错。
    ProviderType("dashscope_rerank", "阿里云百炼重排(gte-rerank-v2)",
                 "https://bailian.console.aliyun.com/",
                 "https://dashscope.aliyuncs.com",
                 _DASHSCOPE_RERANK_PROTOCOL, ("rerank",), ("gte-rerank-v2", "qwen3-rerank")),
    ProviderType("siliconflow_rerank", "SiliconFlow 重排",
                 "https://cloud.siliconflow.cn/account/ak",
                 "https://api.siliconflow.cn/v1", _COHERE_RERANK_PROTOCOL, ("rerank",),
                 ("BAAI/bge-reranker-v2-m3",)),
    ProviderType("jina_rerank", "Jina 重排", "https://jina.ai/api-dashboard/",
                 "https://api.jina.ai/v1", _COHERE_RERANK_PROTOCOL, ("rerank",),
                 ("jina-reranker-v2-base-multilingual",)),
    ProviderType("cohere_rerank", "Cohere 重排", "https://dashboard.cohere.com/api-keys",
                 "https://api.cohere.com/v2", _COHERE_RERANK_PROTOCOL, ("rerank",),
                 ("rerank-v3.5", "bge-reranker-v2-m3")),
)

_TYPES_BY_KEY = {t.key: t for t in PROVIDER_TYPES}

# 协议 → 该协议允许挂载的模型类别(供聚合视图给前端分块用,新增类型不用再抄清单)。
# 库里只存 protocol 不存类型 key,而类型表里 protocol→kinds 是一对多映射的确定关系。
_KINDS_BY_PROTOCOL: dict[str, tuple[str, ...]] = {}
for _t in PROVIDER_TYPES:
    _merged = set(_KINDS_BY_PROTOCOL.get(_t.protocol, ())) | set(_t.kinds)
    _KINDS_BY_PROTOCOL[_t.protocol] = tuple(sorted(_merged))


def kinds_for_protocol(protocol: str) -> tuple[str, ...]:
    """该协议允许挂载的模型类别;未知协议 → 空(前端两个区块都不列,而不是蒙一个)。"""
    return _KINDS_BY_PROTOCOL.get(protocol, ())


class SpaceModelAdminService(ModelAdminService):
    """空间层模型配置;一个实例绑定一个空间(基类只管全局层)。"""

    # 基类允许 None(= 全局层);本层构造时必然绑定空间,收窄类型让下游少一层判空
    _space_id: uuid.UUID

    def __init__(
        self,
        db: Session,
        space_id: uuid.UUID,
        spaces: SpaceRepository,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(db, space_id=space_id)
        self._spaces = spaces
        self._transport = transport

    # ---------------------------- 权限 ----------------------------

    def require_member(self, user: User) -> None:
        """只读数据的成员门槛(供不查库的端点用,如 /types)。"""
        self._require(user, Role.VIEWER)

    def _require(self, user: User, minimum: Role) -> None:
        space = self._spaces.get(self._space_id)
        membership = (
            self._spaces.get_membership(self._space_id, user.id)
            if space is not None
            else None
        )
        if space is None or membership is None:
            # 非成员/跨空间与"空间不存在"同壳:不泄露存在性(基准 04)
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if Role(membership.role) < minimum:
            raise AppError(ErrorCode.FORBIDDEN, "需要空间管理员权限", http_status=403)

    # ---------------------------- 配置页视图 ----------------------------

    def view(self, user: User) -> dict[str, Any]:
        """聚合视图:embedding 卡片 + rerank 开关 + 本空间供应商/模型(密钥只给掩码)。"""
        self._require(user, Role.VIEWER)
        catalog = ModelCatalog.load_from_db(self._db, self._space_id)
        row = self._db.get(SpaceModelSettings, self._space_id)
        key_configured = bool(row is not None and row.embedding_key_encrypted)
        counts = self.provider_model_counts()
        names = self.provider_names()
        return {
            "embedding": {
                "model_name": BUILTIN_EMBEDDING_MODEL_NAME,
                "dims": BUILTIN_EMBEDDING_DIMS,
                "doc_url": BUILTIN_EMBEDDING_DOC_URL,
                "key_configured": key_configured,
                "key_hint": "" if row is None else row.embedding_key_hint,
                # 未自配 Key 却能向量化 = 全局兜底正在生效(dev 专属;prod 恒为 False)
                "fallback_active": (not key_configured)
                and catalog.default_id("embedding") is not None,
            },
            "rerank": {
                "enabled": catalog.rerank_enabled,
                "default_model": catalog.default_id("rerank"),
            },
            "providers": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "protocol": p.protocol,
                    "base_url": p.base_url,
                    "key_hint": p.key_hint,
                    "enabled": p.enabled,
                    "model_count": counts.get(p.id, 0),
                    "kinds": list(kinds_for_protocol(p.protocol)),
                }
                for p in self.list_providers()
            ],
            "models": [
                {
                    "id": str(m.id),
                    "model_id": m.model_id,
                    "provider_id": str(m.provider_id),
                    "provider_name": names.get(m.provider_id, ""),
                    "kind": m.kind,
                    "model_name": m.model_name,
                    "context_tokens": m.context_tokens,
                    "enabled": m.enabled,
                    "is_default": m.is_default,
                }
                for m in self.list_models()
            ],
        }

    @staticmethod
    def provider_types() -> list[dict[str, Any]]:
        """供应商名字清单(前端下拉数据源;只给名字与预填值)。"""
        return [
            {
                "key": t.key,
                "label": t.label,
                "doc_url": t.doc_url,
                "default_base_url": t.default_base_url,
                "protocol": t.protocol,
                "kinds": list(t.kinds),
                "model_names": list(t.model_names),
            }
            for t in PROVIDER_TYPES
        ]

    # ---------------------------- embedding Key ----------------------------

    def set_embedding_key(self, actor: User, api_key: str, ip: str = "") -> None:
        """空串 = 清除本空间 Key(prod 清除后向量化直接不可用,dev 回落全局)。"""
        self._require(actor, Role.ADMIN)
        cleaned = self._assert_api_key(api_key)
        row = self._get_or_create_settings(actor)
        row.embedding_key_encrypted = self._encrypt(cleaned)
        row.embedding_key_hint = self._hint(cleaned)
        self._log(
            actor,
            AuditAction.MODEL_SETTING_EMBEDDING_KEY_UPDATED,
            BUILTIN_EMBEDDING_MODEL_ID,
            ip,
            detail={"configured": bool(cleaned)},
        )

    def test_embedding_key(self, actor: User, api_key: str | None, ip: str = "") -> None:
        """api_key 缺省时测已存 Key;成功无返回,失败按 reason 分诊抛错。"""
        self._require(actor, Role.ADMIN)
        row = self._db.get(SpaceModelSettings, self._space_id)
        stored = "" if row is None else row.embedding_key_encrypted
        if api_key:
            key = self._assert_api_key(api_key)
        elif stored:
            key = resolve_api_key(_builtin_provider(stored))
        else:
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED, "尚未配置向量化 Key", http_status=503
            )
        provider = _builtin_provider("")
        model = ModelEntry(
            id=BUILTIN_EMBEDDING_MODEL_ID,
            kind="embedding",
            provider_key=BUILTIN_EMBEDDING_PROVIDER_KEY,
            model=BUILTIN_EMBEDDING_MODEL_NAME,
            dims=BUILTIN_EMBEDDING_DIMS,
        )
        self._probe(
            actor,
            BUILTIN_EMBEDDING_MODEL_ID,
            ip,
            lambda: get_protocol(provider.protocol).embed(
                provider, model, key, ["ping"], transport=self._transport
            ),
        )

    # ---------------------------- rerank 开关 ----------------------------

    def set_rerank_enabled(self, actor: User, enabled: bool, ip: str = "") -> None:
        """开关与模型解耦:允许先开后配模型(检索时无默认 rerank = 等同关闭)。"""
        self._require(actor, Role.ADMIN)
        row = self._get_or_create_settings(actor)
        row.rerank_enabled = enabled
        self._log(
            actor,
            AuditAction.MODEL_SETTING_RERANK_UPDATED,
            "rerank",
            ip,
            detail={"enabled": enabled},
        )

    def _get_or_create_settings(self, actor: User) -> SpaceModelSettings:
        row = self._db.get(SpaceModelSettings, self._space_id)
        if row is None:
            row = SpaceModelSettings(space_id=self._space_id)
            self._db.add(row)
        row.updated_by = actor.id
        return row

    # ---------------------------- 供应商 / 模型(空间层规则) ----------------------------

    @classmethod
    def _allowed_kinds(cls) -> tuple[str, ...]:
        return (_CHAT_KIND, "rerank")

    def _assert_base_url(self, base_url: str) -> str:
        """空间层多一道 SSRF 闸门:这里的 base_url 是任何一个注册用户都能填的出站地址。

        dev 放行(本地 Ollama/vLLM 就是 127.0.0.1),prod 默认拒内网;
        确需内网自建网关的部署由运维显式开 `APP_ALLOW_PRIVATE_MODEL_URLS=1`。
        """
        cleaned = super()._assert_base_url(base_url)
        settings = get_settings()
        if settings.env == "prod" and not settings.allow_private_model_urls:
            assert_external_http_url(cleaned)
        return cleaned

    def _assert_extra_model_rules(
        self, *, kind: str, context_tokens: int | None
    ) -> None:
        # 上下文窗口是 OPT-29 进度条的分母,事后补不了,只能建的时候就要
        if kind == _CHAT_KIND and not context_tokens:
            raise AppError(
                ErrorCode.VALIDATION,
                "对话模型必须声明 context_tokens(上下文窗口进度条的分母)",
                http_status=422,
            )

    def add_provider(
        self,
        actor: User,
        *,
        provider_type: str,
        name: str,
        base_url: str,
        api_key: str,
        enabled: bool = True,
        ip: str = "",
    ) -> ModelProvider:
        self._require(actor, Role.ADMIN)
        type_ = _TYPES_BY_KEY.get(provider_type)
        if type_ is None:
            raise AppError(
                ErrorCode.VALIDATION,
                f"未知的供应商 {provider_type!r};可选:{', '.join(_TYPES_BY_KEY)}",
                http_status=422,
            )
        return self.create_provider(
            actor,
            name,
            base_url=base_url or type_.default_base_url,
            protocol=type_.protocol,
            api_key=api_key,
            enabled=enabled,
            ip=ip,
        )

    def edit_provider(
        self,
        actor: User,
        provider_id: uuid.UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        enabled: bool | None = None,
        ip: str = "",
    ) -> ModelProvider:
        self._require(actor, Role.ADMIN)
        return self.update_provider(
            actor,
            provider_id,
            name=name,
            base_url=base_url,
            protocol=None,  # 协议由供应商类型决定,空间层不开放改协议
            api_key=api_key,
            clear_api_key=clear_api_key,
            enabled=enabled,
            ip=ip,
        )

    def remove_provider(self, actor: User, provider_id: uuid.UUID, ip: str = "") -> None:
        self._require(actor, Role.ADMIN)
        self.delete_provider(actor, provider_id, ip=ip)

    def add_model(
        self,
        actor: User,
        *,
        provider_id: uuid.UUID,
        kind: str,
        model_name: str,
        model_id: str | None = None,
        context_tokens: int | None = None,
        enabled: bool = True,
        is_default: bool = False,
        ip: str = "",
    ) -> CustomModel:
        self._require(actor, Role.ADMIN)
        provider = self._get_provider(provider_id)
        self._assert_kind_matches_protocol(provider, kind)
        return self.create_model(
            actor,
            model_id=model_id or f"{provider.name}/{model_name}",
            provider_id=provider_id,
            kind=kind,
            model_name=model_name,
            context_tokens=context_tokens,
            enabled=enabled,
            is_default=is_default,
            ip=ip,
        )

    def edit_model(
        self,
        actor: User,
        model_pk: uuid.UUID,
        *,
        model_name: str | None = None,
        context_tokens: int | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
        ip: str = "",
    ) -> CustomModel:
        self._require(actor, Role.ADMIN)
        return self.update_model(
            actor,
            model_pk,
            model_name=model_name,
            context_tokens=context_tokens,
            enabled=enabled,
            is_default=is_default,
            ip=ip,
        )

    def remove_model(self, actor: User, model_pk: uuid.UUID, ip: str = "") -> None:
        self._require(actor, Role.ADMIN)
        self.delete_model(actor, model_pk, ip=ip)

    @staticmethod
    def _assert_kind_matches_protocol(provider: ModelProvider, kind: str) -> None:
        if provider.protocol in _RERANK_PROTOCOLS and kind != "rerank":
            raise AppError(
                ErrorCode.VALIDATION, "该供应商是重排协议,只能挂 rerank 模型", http_status=422
            )
        if provider.protocol not in _RERANK_PROTOCOLS and kind != _CHAT_KIND:
            raise AppError(
                ErrorCode.VALIDATION,
                "该供应商是对话协议,只能挂 chat 模型(embedding 已内置锁死,不可配置)",
                http_status=422,
            )

    # ---------------------------- 【测试连接】 ----------------------------

    def test_provider(
        self, actor: User, provider_id: uuid.UUID, ip: str = ""
    ) -> None:
        """用已存配置发一次最小上游请求(取该供应商下最早的一个模型)。"""
        self._require(actor, Role.ADMIN)
        provider = self._get_provider(provider_id)
        model = self._db.scalar(
            select(CustomModel)
            .where(CustomModel.provider_id == provider.id)
            .order_by(CustomModel.created_at)
            .limit(1)
        )
        if model is None:
            raise AppError(
                ErrorCode.VALIDATION,
                "该供应商下还没有模型,请先添加模型再测试连接",
                http_status=422,
            )
        self._test_model_rows(actor, model, provider, ip)

    def test_model(
        self, actor: User, model_pk: uuid.UUID, ip: str = ""
    ) -> None:
        self._require(actor, Role.ADMIN)
        model = self._get_model(model_pk)
        self._test_model_rows(actor, model, self._get_provider(model.provider_id), ip)

    def _test_model_rows(
        self,
        actor: User,
        model: CustomModel,
        provider: ModelProvider,
        ip: str,
    ) -> None:
        key = resolve_api_key(_provider_entry(provider))
        if model.kind == "rerank":
            self._probe(
                actor,
                model.model_id,
                ip,
                lambda: get_rerank_protocol(provider.protocol).rerank(
                    _provider_entry(provider),
                    _model_entry(model, provider.name),
                    key,
                    "ping",
                    ["a", "b"],
                    2,
                    transport=self._transport,
                ),
            )
            return
        self._probe(
            actor,
            model.model_id,
            ip,
            lambda: _first_chat_delta(
                provider=provider, model=model, api_key=key, transport=self._transport
            ),
        )

    def _probe(
        self, actor: User, target: str, ip: str, call: Callable[[], Any]
    ) -> None:
        """跑一次最小上游调用:成功/失败都留痕,失败翻成 424 + reason 分诊。

        失败留痕走 `_log_detached`(独立会话),因为抛错会 rollback 请求事务。
        """
        error: AppError | None = None
        cause: BaseException | None = None
        try:
            call()
        except ModelCallError as exc:
            error, cause = _upstream_error(exc), exc
        except httpx.RequestError as exc:
            error = AppError(
                ErrorCode.MODEL_UPSTREAM_ERROR,
                "网络不通或 base_url 有误",
                http_status=424,
                details={"reason": "network"},
            )
            cause = exc
        if error is None:
            self._log(actor, AuditAction.MODEL_TESTED, target, ip, detail={"ok": True})
            return
        reason = str((error.details or {}).get("reason", "unknown"))
        self._log_detached(
            actor, AuditAction.MODEL_TESTED, target, ip,
            detail={"ok": False, "reason": reason},
        )
        raise error from cause


def _builtin_provider(api_key_encrypted: str) -> ProviderEntry:
    return ProviderEntry(
        key=BUILTIN_EMBEDDING_PROVIDER_KEY,
        protocol="openai_compatible",
        base_url=BUILTIN_EMBEDDING_BASE_URL,
        api_key_encrypted=api_key_encrypted,
        enabled=True,
    )


def _provider_entry(provider: ModelProvider) -> ProviderEntry:
    return ProviderEntry(
        key=provider.name,
        protocol=provider.protocol,
        base_url=provider.base_url,
        api_key_encrypted=provider.api_key_encrypted,
        enabled=provider.enabled,
    )


def _model_entry(model: CustomModel, provider_key: str) -> ModelEntry:
    return ModelEntry(
        id=model.model_id,
        kind=model.kind,
        provider_key=provider_key,
        model=model.model_name,
        dims=model.dims,
        context_tokens=model.context_tokens,
        enabled=model.enabled,
    )


def _first_chat_delta(
    *,
    provider: ModelProvider,
    model: CustomModel,
    api_key: str,
    transport: httpx.BaseTransport | None,
) -> None:
    """只取流上的第一个增量:能取到就说明鉴权、连通性、模型名三关都过了。"""
    stream = get_protocol(provider.protocol).chat_stream(
        _provider_entry(provider),
        _model_entry(model, provider.name),
        api_key,
        [{"role": "user", "content": "ping"}],
        0.0,
        transport=transport,
    )
    try:
        next(stream, None)
    finally:
        stream.close()


def _upstream_error(exc: ModelCallError) -> AppError:
    """上游失败 → 配置页能看懂的三类原因(密钥/网络/模型名)。"""
    status = exc.status
    if status in (401, 403):
        reason, message = "auth", "Key 无效或权限不足"
    elif status in (400, 404):
        reason, message = "model_name", "模型名不存在(检查上游的模型/接入点 id)"
    else:
        reason, message = "network", f"上游返回异常{f'(status={status})' if status else ''}"
    return AppError(
        ErrorCode.MODEL_UPSTREAM_ERROR,
        message,
        http_status=424,
        details={"reason": reason},
    )
