"""自定义模型管理服务(OPT-25,superuser 专属)。

写入口即校验(fail-closed),库里存的每个 provider/model 都保证可被调用层解析:

- `protocol` 必须在协议注册表中(`registered_protocols()`),否则 422 ——
  存进库的键永远可分发,运行时不会出现"协议未注册";
- embedding 模型必须显式声明 `dims` 且等于 1024:chunks.embedding 是
  Vector(1024) 定宽列,维度不符要到第一次入库才炸,必须在写入时拦下;
- 供应商密钥**只有这一处**接触明文:立即 AES-GCM 加密落库,列上只留密文与
  掩码提示;任何读取接口不回显明文;
- `is_default` 按 kind 唯一(服务内互斥 + 库内 partial unique 双保险);
  未启用的模型不能设默认(否则会配出"永远解析不到"的默认值)。

删除供应商 = 级联删除其下模型(ON DELETE CASCADE):被 KB/消息引用的模型
id 变不可解析,调用时报 MODEL_NOT_CONFIGURED 503,不静默兜底。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.application.repository.audit import AuditRepositoryImpl
from app.core.crypto import get_credential_cipher
from app.core.errors import AppError, ErrorCode
from app.core.model_protocols import registered_protocols
from app.domain.enums import AuditAction
from app.domain.models import AuditLog, CustomModel, ModelProvider, User

_MODEL_KINDS = ("chat", "embedding", "rerank")
# chunks.embedding 为 Vector(1024) 定宽列(ADR-1):换维度 = 全库重建,属 V1 外能力
_EMBEDDING_DIMS = 1024


class ModelAdminService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditRepositoryImpl(db)

    # ---------------------------- 供应商 ----------------------------

    def create_provider(
        self,
        actor: User,
        name: str,
        base_url: str,
        protocol: str,
        api_key: str,
        enabled: bool = True,
        ip: str = "",
    ) -> ModelProvider:
        self._assert_base_url(base_url)
        self._assert_protocol(protocol)
        self._assert_provider_name_free(name)
        provider = ModelProvider(
            name=name,
            protocol=protocol,
            base_url=base_url.rstrip("/"),
            api_key_encrypted=self._encrypt(api_key),
            key_hint=self._hint(api_key),
            enabled=enabled,
        )
        self._db.add(provider)
        self._log(actor, AuditAction.MODEL_PROVIDER_CREATED, name, ip)
        return provider

    def list_providers(self) -> list[ModelProvider]:
        return list(self._db.scalars(select(ModelProvider).order_by(ModelProvider.name)))

    def provider_names(self) -> dict[uuid.UUID, str]:
        return {p.id: p.name for p in self._db.scalars(select(ModelProvider))}

    def provider_model_counts(self) -> dict[uuid.UUID, int]:
        rows = self._db.execute(
            select(CustomModel.provider_id, func.count()).group_by(CustomModel.provider_id)
        )
        return {provider_id: int(count) for provider_id, count in rows}

    def update_provider(
        self,
        actor: User,
        provider_id: uuid.UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        protocol: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        enabled: bool | None = None,
        ip: str = "",
    ) -> ModelProvider:
        provider = self._get_provider(provider_id)
        if name is not None and name != provider.name:
            self._assert_provider_name_free(name)
            provider.name = name
        if base_url is not None:
            self._assert_base_url(base_url)
            provider.base_url = base_url.rstrip("/")
        if protocol is not None:
            self._assert_protocol(protocol)
            provider.protocol = protocol
        if clear_api_key:
            provider.api_key_encrypted = ""
            provider.key_hint = ""
        elif api_key is not None:
            provider.api_key_encrypted = self._encrypt(api_key)
            provider.key_hint = self._hint(api_key)
        if enabled is not None:
            provider.enabled = enabled
        self._log(actor, AuditAction.MODEL_PROVIDER_UPDATED, provider.name, ip)
        return provider

    def delete_provider(
        self, actor: User, provider_id: uuid.UUID, ip: str = ""
    ) -> None:
        provider = self._get_provider(provider_id)
        # 级联删其下模型(外键 ON DELETE CASCADE);默认模型标记随之消失,调用端 503 fail-closed
        self._db.execute(
            update(CustomModel)
            .where(CustomModel.provider_id == provider.id, CustomModel.is_default.is_(True))
            .values(is_default=False)
        )
        self._db.delete(provider)
        self._log(actor, AuditAction.MODEL_PROVIDER_DELETED, provider.name, ip)

    # ---------------------------- 模型 ----------------------------

    def create_model(
        self,
        actor: User,
        model_id: str,
        provider_id: uuid.UUID,
        kind: str,
        model_name: str,
        dims: int | None = None,
        context_tokens: int | None = None,
        enabled: bool = True,
        is_default: bool = False,
        ip: str = "",
    ) -> CustomModel:
        self._assert_kind(kind)
        self._assert_model_id_free(model_id)
        provider = self._get_provider(provider_id)
        dims = self._normalize_dims(kind, dims)
        if is_default:
            if not enabled:
                raise AppError(
                    ErrorCode.VALIDATION, "未启用的模型不能设为默认", http_status=422
                )
            if not provider.enabled:
                raise AppError(
                    ErrorCode.VALIDATION,
                    "所属供应商未启用,不能设为默认",
                    http_status=422,
                )
            self._clear_defaults(kind)
        model = CustomModel(
            model_id=model_id,
            provider_id=provider_id,
            kind=kind,
            model_name=model_name,
            dims=dims,
            context_tokens=context_tokens,
            enabled=enabled,
            is_default=is_default,
        )
        self._db.add(model)
        self._log(actor, AuditAction.MODEL_CREATED, model_id, ip)
        return model

    def list_models(self, kind: str | None = None) -> list[CustomModel]:
        stmt = select(CustomModel).order_by(CustomModel.model_id)
        if kind is not None:
            self._assert_kind(kind)
            stmt = stmt.where(CustomModel.kind == kind)
        return list(self._db.scalars(stmt))

    def update_model(
        self,
        actor: User,
        model_pk: uuid.UUID,
        *,
        model_name: str | None = None,
        dims: int | None = None,
        context_tokens: int | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
        ip: str = "",
    ) -> CustomModel:
        model = self._get_model(model_pk)
        if model_name is not None:
            model.model_name = model_name
        if dims is not None:
            if model.kind != "embedding":
                raise AppError(
                    ErrorCode.VALIDATION, "仅 embedding 模型需要 dims", http_status=422
                )
            self._assert_dims(dims)
            model.dims = dims
        if context_tokens is not None:
            model.context_tokens = context_tokens
        if enabled is not None:
            model.enabled = enabled
        if is_default is not None:
            if is_default:
                target_enabled = model.enabled if enabled is None else enabled
                provider = self._get_provider(model.provider_id)
                if not target_enabled or not provider.enabled:
                    raise AppError(
                        ErrorCode.VALIDATION, "启用的模型与其供应商才能设为默认",
                        http_status=422,
                    )
                self._clear_defaults(model.kind)
            model.is_default = is_default
        self._log(actor, AuditAction.MODEL_UPDATED, model.model_id, ip)
        return model

    def delete_model(self, actor: User, model_pk: uuid.UUID, ip: str = "") -> None:
        model = self._get_model(model_pk)
        self._db.delete(model)
        self._log(actor, AuditAction.MODEL_DELETED, model.model_id, ip)

    # ---------------------------- 内部 ----------------------------

    @staticmethod
    def _encrypt(plaintext: str) -> str:
        return get_credential_cipher().encrypt(plaintext) if plaintext else ""

    @staticmethod
    def _hint(plaintext: str) -> str:
        if not plaintext:
            return ""
        if len(plaintext) <= 8:
            return "****"
        return f"{plaintext[:4]}****{plaintext[-4:]}"

    @staticmethod
    def _assert_base_url(base_url: str) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise AppError(
                ErrorCode.VALIDATION, "base_url 必须是 http(s):// 开头的绝对地址",
                http_status=422,
            )

    @staticmethod
    def _assert_protocol(protocol: str) -> None:
        allowed = registered_protocols()
        if protocol not in allowed:
            raise AppError(
                ErrorCode.VALIDATION,
                f"未知的模型协议 {protocol!r};已注册:{', '.join(allowed) or '无'}",
                http_status=422,
            )

    @staticmethod
    def _assert_kind(kind: str) -> None:
        if kind not in _MODEL_KINDS:
            raise AppError(
                ErrorCode.VALIDATION,
                f"未知的模型类型 {kind!r};允许:{', '.join(_MODEL_KINDS)}",
                http_status=422,
            )

    def _normalize_dims(self, kind: str, dims: int | None) -> int | None:
        if kind != "embedding":
            return None
        if dims is None:
            raise AppError(
                ErrorCode.VALIDATION, "embedding 模型必须显式声明 dims", http_status=422
            )
        self._assert_dims(dims)
        return dims

    @staticmethod
    def _assert_dims(dims: int) -> None:
        if dims != _EMBEDDING_DIMS:
            raise AppError(
                ErrorCode.VALIDATION,
                f"当前向量列为定宽 {_EMBEDDING_DIMS} 维,dims 只能是 {_EMBEDDING_DIMS}",
                http_status=422,
            )

    def _assert_provider_name_free(self, name: str) -> None:
        exists = self._db.scalar(
            select(ModelProvider.id).where(ModelProvider.name == name)
        )
        if exists is not None:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_NAME_TAKEN, "同名供应商已存在", http_status=409
            )

    def _assert_model_id_free(self, model_id: str) -> None:
        exists = self._db.scalar(
            select(CustomModel.id).where(CustomModel.model_id == model_id)
        )
        if exists is not None:
            raise AppError(
                ErrorCode.MODEL_ID_TAKEN, "该模型 id 已存在", http_status=409
            )

    def _get_provider(self, provider_id: uuid.UUID) -> ModelProvider:
        provider = self._db.get(ModelProvider, provider_id)
        if provider is None:
            raise AppError(ErrorCode.NOT_FOUND, "供应商不存在", http_status=404)
        return provider

    def _get_model(self, model_pk: uuid.UUID) -> CustomModel:
        model = self._db.get(CustomModel, model_pk)
        if model is None:
            raise AppError(ErrorCode.NOT_FOUND, "模型不存在", http_status=404)
        return model

    def _clear_defaults(self, kind: str) -> None:
        """同 kind 的旧默认先落牌,再上新默认(partial unique 索引的双保险)。"""
        self._db.execute(
            update(CustomModel).where(CustomModel.kind == kind).values(is_default=False)
        )

    def _log(self, actor: User, action: AuditAction, target: str, ip: str) -> None:
        self._audit.add(
            AuditLog(
                actor_id=actor.id,
                space_id=None,
                action=str(action),
                target=target,
                detail={},
                ip=ip,
            )
        )
