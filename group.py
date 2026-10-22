"""QGroupBind models, resolvers and commands.

QGroupBind follows the same normal-use contract as personal QBind. A group
operator can submit the real group number. QGroupBind stores that relation in
its own Bot-scoped database, which is the source used by the backend and the
binding gate. In legacy mode ``declared`` is usable for compatibility; when
QQ-mail verification is enabled, ordinary group features require ``verified``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from nonebot import get_driver, on_command
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.params import CommandArg

from .gensokyo import GensokyoMappingClient, GensokyoMappingError
from .mail import MailConfigurationError, MailDeliveryError, code_digest
from .storage import (
    BindingConflictError,
    ConfirmationRequest,
    GROUP_CONFLICT,
    GROUP_DECLARED,
    GROUP_PENDING_VERIFICATION,
    GROUP_REVOKED,
    GROUP_VERIFIED,
    GroupBinding,
    QbindStorageError,
    REQUEST_CANCELED,
    REQUEST_BACKEND_UNKNOWN,
    REQUEST_EXPIRED,
    REQUEST_FAILED,
    REQUEST_PENDING,
)


logger = logging.getLogger(__name__)
_GROUP_ID_RE = re.compile(r"^[1-9]\d{4,11}$")
_REQUEST_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_GROUP_REQUEST_TTL = 300.0


@dataclass(frozen=True)
class GroupIdentityResolution:
    """Return the usable real-group mapping plus its review status.

    ``canonical_group_id`` is populated only for a locally verified mapping.
    Group business operations additionally require the same-Bot verified user
    record and an explicit successful GSK pair synchronization.
    """

    bot_scope: str
    external_group_id: str
    canonical_group_id: Optional[str]
    verification_status: str
    reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return (
            self.verification_status == GROUP_VERIFIED
            and self.canonical_group_id is not None
        )


@dataclass(frozen=True)
class GroupVerificationContext:
    bot_scope: str
    virtual_group_id: str
    requested_real_group_id: str
    requested_by_user_id: str
    event: Any


@dataclass(frozen=True)
class GroupVerificationResult:
    status: str
    source: str
    reason: Optional[str] = None


class GroupVerificationAdapter(Protocol):
    async def verify(
        self, context: GroupVerificationContext, bot: Bot
    ) -> GroupVerificationResult:
        """Return verified, pending, conflict or rejected."""


class _UnavailableGroupVerifier:
    async def verify(
        self, context: GroupVerificationContext, bot: Bot
    ) -> GroupVerificationResult:
        return GroupVerificationResult(
            status="pending",
            source="verification_unavailable",
            reason=(
                "当前 Gensokyo/适配器没有提供真实群号归属证明；"
                "手填群号和群内确认不足以完成验证"
            ),
        )


class GensokyoGroupVerifier:
    """Use type=11 only to reject inconsistent mappings, never to verify."""

    def __init__(self, client: Optional[GensokyoMappingClient] = None):
        self.client = client or GensokyoMappingClient()

    async def declare(
        self, context: GroupVerificationContext, bot: Bot
    ) -> GroupVerificationResult:
        """Save a local declaration without changing Gensokyo's ID map."""

        return GroupVerificationResult(
            status="declared",
            source="qgroupbind:declared",
        )

    async def verify(
        self, context: GroupVerificationContext, bot: Bot
    ) -> GroupVerificationResult:
        from . import get_verified_qq

        verified_user_id = get_verified_qq(
            context.requested_by_user_id, context.bot_scope
        )
        try:
            resolved = await self.client.resolve_group_user(
                context.virtual_group_id, context.requested_by_user_id
            )
        except GensokyoMappingError as error:
            return GroupVerificationResult(
                status="pending",
                source="gensokyo:type11:consistency_only",
                reason=str(error),
            )
        if resolved.real_group_id != context.requested_real_group_id:
            return GroupVerificationResult(
                status="rejected",
                source="gensokyo:type11:consistency_only",
                reason="type=11 returned a group value inconsistent with the request",
            )
        if verified_user_id and resolved.real_user_id != verified_user_id:
            return GroupVerificationResult(
                status="rejected",
                source="gensokyo:type11:consistency_only",
                reason="type=11 returned a user value inconsistent with QBind",
            )
        return GroupVerificationResult(
            status="pending",
            source="gensokyo:type11:consistency_only",
            reason="type=11 is a consistency lookup, not ownership proof",
        )


_group_mapping_client: Any = GensokyoMappingClient()
_group_verifier: GroupVerificationAdapter = GensokyoGroupVerifier(
    _group_mapping_client
)


def set_group_verifier(verifier: GroupVerificationAdapter) -> None:
    """Register an application-owned, trusted group verification adapter."""

    global _group_verifier
    if not hasattr(verifier, "verify"):
        raise TypeError("group verifier must provide an async verify() method")
    _group_verifier = verifier


def get_group_verifier() -> GroupVerificationAdapter:
    return _group_verifier


def set_group_mapping_client(client: Any) -> None:
    """Replace the client used for verified group type=5 mapping writes."""

    if not callable(getattr(client, "set_group_mapping", None)) or not callable(
        getattr(client, "clear_group_mapping", None)
    ):
        raise TypeError(
            "group mapping client must provide set_group_mapping/clear_group_mapping"
        )
    global _group_mapping_client
    _group_mapping_client = client
    # Only a client that can read type=11 may serve the verifier; a write-only
    # fake would otherwise break the verifier with an AttributeError mid-qgconfirm.
    if isinstance(_group_verifier, GensokyoGroupVerifier) and callable(
        getattr(client, "resolve_group_user", None)
    ):
        _group_verifier.client = client


def get_group_mapping_client() -> Any:
    return _group_mapping_client


def _store():
    # Import lazily to avoid a cycle while qbind's module-level commands load.
    from . import get_store

    return get_store()


def _context(event: Any) -> Optional[tuple[str, str, str]]:
    group_id = getattr(event, "group_id", None)
    if group_id is None or str(group_id).strip() == "":
        return None
    if getattr(event, "message_type", None) == "private":
        return None
    scope = str(getattr(event, "self_id", "")).strip()
    user_id = str(event.get_user_id()).strip()
    if not scope or not user_id:
        return None
    return scope, user_id, str(group_id).strip()


def _is_superuser(user_id: str) -> bool:
    configured = getattr(get_driver().config, "superusers", set()) or set()
    if isinstance(configured, str):
        configured = {configured}
    return user_id in {str(value) for value in configured}


def _has_group_operator_access(event: Any, user_id: str) -> bool:
    if _is_superuser(user_id):
        return True
    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", "")).lower()
    return role in {"admin", "owner"}


def _has_group_owner_access(event: Any, _user_id: str) -> bool:
    """Return whether this event is from the real current group owner.

    A Bot SUPERUSER can manage a local record in legacy mode, but cannot
    prove ownership of a QQ group.  Email mode therefore requires the
    OneBot event to identify the sender as ``owner`` so the verification code
    goes to the owner's own QQ mailbox.
    """

    sender = getattr(event, "sender", None)
    return str(getattr(sender, "role", "")).lower() == "owner"


def _validate_real_group_id(value: str) -> Optional[str]:
    if not _GROUP_ID_RE.fullmatch(value):
        return "真实群号格式不正确，请输入 5-12 位非零开头数字。"
    return None


def _token(value: str) -> bool:
    return bool(_REQUEST_TOKEN_RE.fullmatch(value))


async def _query_group_name(bot: Bot, group_id: str) -> Optional[str]:
    getter = getattr(bot, "get_group_info", None)
    if not callable(getter):
        return None
    try:
        info = await getter(group_id=group_id)
    except Exception:
        logger.debug("unable to query group name", exc_info=True)
        return None
    if isinstance(info, dict):
        name = info.get("group_name")
        return str(name).strip() if name else None
    return None


def _message(content: str, buttons: Optional[list[list[dict[str, Any]]]] = None):
    from . import USE_MARKDOWN, _build_markdown_msg

    if USE_MARKDOWN:
        return _build_markdown_msg(content, buttons)
    return content


def _personal_email_ready(scope: str, user_id: str) -> bool:
    from . import email_verification_required, personal_auth_ready

    if not email_verification_required():
        return True
    return personal_auth_ready(user_id, scope)


def _personal_email_prompt(user_id: str):
    return _message(
        "### 🔒 群主需要先完成个人 QQ 邮箱认证\n\n"
        "> 群绑定验证码必须发送到群主本人 QQ 号对应的 QQ 邮箱。\n"
        "> 请先发送 `qbind <你的QQ号>`，完成邮箱验证码认证后再发起 `qgbind`。",
        [
            [
                {
                    "render_data.label": "开始个人绑定",
                    "action.data": "qbind",
                    "action.enter": True,
                    "action.permission.type": 0,
                    "action.permission.specify_user_ids": [str(user_id)],
                },
                {
                    "render_data.label": "查看群绑定状态",
                    "action.data": "qgstatus",
                    "action.enter": True,
                },
            ]
        ],
    )


def _group_action_buttons(
    *,
    include_bind: bool = False,
    include_status: bool = True,
    include_rebind: bool = False,
    include_unbind: bool = False,
) -> list[list[dict[str, Any]]]:
    """Build safe navigation buttons for QGroupBind commands.

    The buttons are convenience actions only.  The command handlers continue
    to enforce the current group owner/admin and Bot SUPERUSER checks.
    """

    actions = []
    if include_status:
        actions.append(("查看群绑定状态", "qgstatus"))
    if include_bind:
        actions.append(("发起群绑定", "qgbind"))
    if include_rebind:
        actions.append(("申请群换绑", "qgrebind"))
    if include_unbind:
        actions.append(("申请群解绑", "qgunbind"))
    if not actions:
        return []
    return [
        [
            {
                "render_data.label": label,
                "action.data": command,
                "action.enter": True,
            }
            for label, command in actions
        ]
    ]


def _group_email_code_buttons(
    user_id: str,
    *,
    include_bind: bool = False,
    include_status: bool = True,
    include_rebind: bool = False,
    include_unbind: bool = False,
) -> list[list[dict[str, Any]]]:
    """Build a group email-code input button plus optional navigation.

    The keyboard cannot read a code from the owner's mailbox.  It therefore
    pre-fills ``qgcode `` without submitting, so the owner can append the
    six-digit code and send it normally.
    """

    rows = [
        [
            {
                "render_data.label": "输入群邮箱验证码",
                "action.data": "qgcode ",
                "action.enter": False,
                "action.permission.type": 0,
                "action.permission.specify_user_ids": [str(user_id)],
            }
        ]
    ]
    rows.extend(
        _group_action_buttons(
            include_bind=include_bind,
            include_status=include_status,
            include_rebind=include_rebind,
            include_unbind=include_unbind,
        )
    )
    return rows


def _status_label(status: str) -> str:
    return {
        GROUP_DECLARED: "已声明（可使用，待可信验证）",
        GROUP_VERIFIED: "已验证",
        GROUP_PENDING_VERIFICATION: "待真实群号审核",
        GROUP_CONFLICT: "冲突，禁止自动覆盖",
        GROUP_REVOKED: "已撤销",
    }.get(status, status)


def _request_matches(
    request: ConfirmationRequest,
    *,
    kind: str,
    scope: str,
    user_id: str,
    group_id: str,
) -> bool:
    return (
        request.request_kind == kind
        and request.bot_scope == scope
        and request.actor_user_id == user_id
        and request.external_group_id == group_id
    )


def _get_group_request(
    *,
    kind: str,
    scope: str,
    user_id: str,
    group_id: str,
    token: Optional[str],
) -> Optional[ConfirmationRequest]:
    store = _store()
    if token:
        request = store.get_confirmation(token)
        if request is None:
            return None
        if not _request_matches(
            request,
            kind=kind,
            scope=scope,
            user_id=user_id,
            group_id=group_id,
        ):
            raise PermissionError("confirmation request does not belong to this Bot/user/group")
        return request
    return store.find_confirmation(
        request_kind=kind,
        bot_scope=scope,
        actor_user_id=user_id,
        external_group_id=group_id,
    )


def _request_result_message(request: ConfirmationRequest) -> str:
    if request.result_code == "email_pending":
        return (
            "📩 群主 QQ 邮箱验证码已发送。\n"
            "请发送 `qgcode <6位验证码>` 完成群绑定。"
        )
    if request.result_code == "declared":
        return (
            "✅ 真实群号已保存到当前 Bot 的 QGroupBind 本地声明。\n"
            "⚠️ 声明不会写入 GSK，也不会放行群业务；需要可信验证并完成同 Bot 双 verified 映射同步。"
        )
    if request.result_code == "verified":
        return "✅ QBind 本地群绑定已验证；GSK 映射仍须等待同 Bot 个人绑定 verified 后成对同步。"
    if request.result_code == "verified_synced":
        return "✅ 群绑定已验证，个人与群 GSK type=5 映射均已明确同步。"
    if request.result_code == "pending_verification":
        return "⏳ 请求已记录，但真实群号仍待可信来源审核；当前不会向其他插件提供该群号。"
    if request.result_code == "conflict":
        return "⚠️ 该请求已进入冲突状态，没有覆盖已有群映射。"
    if request.result_code == "revoked":
        return "✅ QBind 本地群绑定已撤销；GSK type=5 不提供可靠删除，远端映射需显式对账。"
    if request.result_code == "failed":
        return "❌ QGroupBind 本地状态更新失败，未报告为成功；请稍后重试或联系管理员检查数据库。"
    if request.result_code == "backend_applied_unrecorded":
        return "⚠️ 外部状态更新了，但 QGroupBind 本地记录未完整保存；请联系管理员恢复，暂不报告为完全成功。"
    if request.status == REQUEST_EXPIRED:
        return "⏰ 群绑定确认已过期，请重新发起。"
    if request.status == REQUEST_CANCELED:
        return "✅ 群绑定确认请求已取消。"
    if request.status == REQUEST_BACKEND_UNKNOWN:
        return "⚠️ 外部验证结果未知，群绑定保留为可恢复请求；请稍后再次点击确认。"
    return f"请求状态：{request.status}。"


def _resolve_group(scope: str, group_id: str) -> GroupIdentityResolution:
    record = _store().get_group(scope, group_id)
    if record is None:
        return GroupIdentityResolution(scope, group_id, None, "unbound", "no binding")
    canonical = (
        record.real_group_id
        if record.binding_status == GROUP_VERIFIED
        else None
    )
    return GroupIdentityResolution(
        scope,
        group_id,
        canonical,
        record.binding_status,
        record.last_error,
    )


def get_real_group(
    virtual_group_id: object,
    self_id: Optional[object] = None,
    *,
    verified_only: bool = True,
) -> Optional[str]:
    """Return a verified group number for one Bot namespace.

    ``verified_only`` remains for call compatibility; declarations are never
    returned. Group operations still require paired identity through
    :func:`qbind.sync_verified_pair`.
    """

    if self_id is None:
        return None
    record = _store().get_group(str(self_id), str(virtual_group_id))
    if record is None:
        return None
    # Declarations are never exposed as group identities, even to legacy callers.
    if record.binding_status != GROUP_VERIFIED:
        return None
    return record.real_group_id


def get_group_name(
    virtual_group_id: object, self_id: Optional[object] = None
) -> Optional[str]:
    """Return the group name captured for one Bot-scoped mapping."""

    if self_id is None:
        return None
    record = _store().get_group(str(self_id), str(virtual_group_id))
    if record is None or not record.group_name:
        return None
    return str(record.group_name).strip() or None


def is_group_bound(
    virtual_group_id: object,
    self_id: Optional[object] = None,
    *,
    verified_only: bool = True,
) -> bool:
    return get_real_group(
        virtual_group_id, self_id, verified_only=verified_only
    ) is not None


def get_group_binding_status(
    virtual_group_id: object, self_id: Optional[object] = None
) -> str:
    if self_id is None:
        return "unbound"
    return _resolve_group(str(self_id), str(virtual_group_id)).verification_status


async def resolve_group_identity(
    self_id: object, virtual_group_id: object
) -> GroupIdentityResolution:
    return _resolve_group(str(self_id), str(virtual_group_id))


class GroupIdentityResolver:
    async def resolve_group_identity(
        self, self_id: object, virtual_group_id: object
    ) -> GroupIdentityResolution:
        return await resolve_group_identity(self_id, virtual_group_id)

    async def get_real_group(
        self, self_id: object, virtual_group_id: object
    ) -> Optional[str]:
        return get_real_group(virtual_group_id, self_id, verified_only=True)


async def ensure_group_bound(event: Any, bot: Bot) -> bool:
    context = _context(event)
    if context is None:
        return False
    scope, user_id, group_id = context
    from . import sync_verified_pair

    try:
        await sync_verified_pair(scope, user_id, group_id)
    except Exception:
        logger.warning("verified personal/group GSK pair is unavailable")
        await bot.send(
            event,
            _message(
                "### 🔒 当前群身份暂不可用\n\n"
                "> 当前 Bot 下个人与群绑定必须都为 verified，且两项 GSK 映射均明确同步成功；本次业务请求未执行。"
            ),
        )
        return False
    return True

async def approve_group_binding(
    *,
    self_id: object,
    virtual_group_id: object,
    verified_by: object,
    verification_source: str,
    operation_id: Optional[str] = None,
) -> GroupBinding:
    """Apply a trusted external verification result atomically.

    This is an integration boundary for a real verification service or a
    separately audited administrator workflow.  Calling it is intentionally
    not part of the normal qgbind command path.
    """

    return _store().verify_group(
        bot_scope=str(self_id),
        virtual_group_id=str(virtual_group_id),
        verified_by=str(verified_by),
        verification_source=verification_source,
        operation_id=operation_id,
    )


def approve_group_binding_request(
    request_id: object,
    *,
    verified_by: object,
    verification_source: str,
):
    """Approve a still-pending qgbind/qgrebind request from a trusted review flow."""

    store = _store()
    request = store.get_confirmation(request_id)
    if request is None or request.request_kind not in {"group_bind", "group_rebind"}:
        raise QbindStorageError("group verification request was not found")
    if request.status != REQUEST_PENDING:
        return request
    if not request.external_group_id or not request.candidate_id:
        raise QbindStorageError("group verification request has no group candidate")
    store.apply_verified_group(
        bot_scope=request.bot_scope,
        virtual_group_id=request.external_group_id,
        real_group_id=request.candidate_id,
        group_name=request.group_name,
        bound_by_user_id=request.actor_user_id,
        verified_by=str(verified_by),
        verification_source=verification_source,
        operation_id=request.request_id,
        allow_replace=request.request_kind == "group_rebind",
        expected_version=request.expected_version,
    )
    return store.complete_confirmation(request.request_id, result_code="verified")


async def _confirm_group_bind(
    event: Any,
    bot: Bot,
    request: ConfirmationRequest,
) -> str:
    scope, _, group_id = _context(event) or ("", "", "")
    user_id = str(event.get_user_id()).strip()
    store = _store()
    if request.status != REQUEST_PENDING:
        return _request_result_message(request)
    if request.expires_at <= time.time():
        expired = store.complete_confirmation(
            request.request_id, result_code="expired", status=REQUEST_EXPIRED
        )
        return _request_result_message(expired or request)
    from . import email_verification_required

    if email_verification_required():
        if not _has_group_owner_access(event, user_id):
            raise PermissionError(
                "邮箱认证模式下必须由当前群主确认，验证码将发送到群主 QQ 邮箱"
            )
    elif not _has_group_operator_access(event, user_id):
        raise PermissionError("only the current group owner/admin or Bot SUPERUSER may confirm")
    if not _personal_email_ready(scope, user_id):
        raise PermissionError(
            "群主必须先完成个人 QQ 邮箱认证，才能认证这个群的绑定"
        )

    current = store.get_group(scope, group_id)
    if request.expected_version is not None:
        if current is None or current.version != request.expected_version:
            store.complete_confirmation(
                request.request_id,
                result_code="conflict",
                status=REQUEST_FAILED,
                error="group binding changed while the request was pending",
            )
            return "⚠️ 当前群绑定已发生变化，请重新查询状态后再操作。"
    elif current is not None and current.binding_status == GROUP_VERIFIED:
        store.complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="a verified mapping already exists",
        )
        return "⚠️ 当前群已经有已验证映射；请使用 qgrebind 申请换绑。"
    elif (
        request.expected_version is None
        and current is not None
        and (
            current.binding_status != GROUP_PENDING_VERIFICATION
            or current.real_group_id != request.candidate_id
        )
    ):
        store.complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="another group binding request changed the target",
        )
        return "⚠️ 当前群绑定申请已发生变化，未覆盖现有记录。"

    candidate = request.candidate_id or ""
    verification_context = GroupVerificationContext(
        bot_scope=scope,
        virtual_group_id=group_id,
        requested_real_group_id=candidate,
        requested_by_user_id=user_id,
        event=event,
    )
    verifier = get_group_verifier()
    try:
        # The built-in adapter stores a local declaration. Custom adapters may
        # still perform a trusted ownership check and promote it to verified.
        declare = getattr(verifier, "declare", None)
        client = getattr(verifier, "client", None)
        if callable(declare) and callable(
            getattr(client, "set_group_mapping", None)
        ):
            result = await declare(verification_context, bot)
        else:
            result = await verifier.verify(verification_context, bot)
    except GensokyoMappingError as error:
        logger.warning("Gensokyo group mapping declaration failed: %s", error)
        failed = store.update_confirmation_error(
            request.request_id, str(error), backend_unknown=error.retryable
        )
        return _request_result_message(failed or request)
    except Exception as error:
        logger.exception("group binding adapter failed")
        failed = store.update_confirmation_error(
            request.request_id,
            f"group binding adapter failed: {error}",
            backend_unknown=True,
        )
        return _request_result_message(failed or request)

    from . import email_verification_required, get_verified_qq, start_email_challenge

    if email_verification_required() and result.status in {"declared", "verified"}:
        owner_qq = get_verified_qq(user_id, scope)
        if not owner_qq:
            raise PermissionError(
                "群主必须先完成个人 QQ 邮箱认证，才能认证这个群的绑定"
            )
        try:
            pending = store.set_group_pending(
                bot_scope=scope,
                virtual_group_id=group_id,
                real_group_id=candidate,
                group_name=request.group_name,
                bound_by_user_id=user_id,
                verification_source=f"{result.source}:qq_email_pending",
                operation_id=request.request_id,
                allow_replace=request.request_kind == "group_rebind",
            )
            _, masked = await start_email_challenge(
                purpose=request.request_kind,
                bot_scope=scope,
                actor_user_id=user_id,
                external_user_id=user_id,
                external_group_id=group_id,
                candidate_id=candidate,
                group_name=request.group_name,
                expected_version=pending.version,
                qq_number=owner_qq,
            )
        except (BindingConflictError, QbindStorageError) as error:
            logger.exception("group email challenge could not enter pending state")
            failed = store.complete_confirmation(
                request.request_id,
                result_code="conflict" if isinstance(error, BindingConflictError) else "failed",
                status=REQUEST_FAILED,
                error=str(error),
            )
            return _request_result_message(failed or request)
        except (MailConfigurationError, MailDeliveryError) as error:
            logger.warning("group email challenge could not be delivered: %s", error)
            failed = store.complete_confirmation(
                request.request_id,
                result_code="failed",
                status=REQUEST_FAILED,
                error=str(error),
            )
            return (
                "❌ 群主 QQ 邮箱验证码发送失败。\n"
                f"> {error}\n"
                "> 当前没有把群绑定报告为成功，请修复邮箱配置后重新发起。"
            ) if failed is not None else str(error)
        store.complete_confirmation(request.request_id, result_code="email_pending")
        return (
            "📩 群主 QQ 邮箱验证已发起。\n"
            f"> 验证码已发送至：`{masked}`\n"
            "> 请群主发送 `qgcode <6位验证码>` 完成群绑定。\n"
            "> 验证码有效期为 10 分钟，最多可尝试 5 次。"
        )

    try:
        if result.status == "declared":
            store.declare_group(
                bot_scope=scope,
                virtual_group_id=group_id,
                real_group_id=candidate,
                group_name=request.group_name,
                bound_by_user_id=user_id,
                verification_source=result.source,
                operation_id=request.request_id,
                allow_replace=request.request_kind == "group_rebind",
            )
            completed = store.complete_confirmation(
                request.request_id, result_code="declared"
            )
            return _request_result_message(completed or request)

        if result.status == "verified":
            store.apply_verified_group(
                bot_scope=scope,
                virtual_group_id=group_id,
                real_group_id=candidate,
                group_name=request.group_name,
                bound_by_user_id=user_id,
                verified_by=user_id,
                verification_source=result.source,
                operation_id=request.request_id,
                allow_replace=request.request_kind == "group_rebind",
                expected_version=request.expected_version,
            )
            from . import sync_verified_pair

            try:
                await sync_verified_pair(scope, user_id, group_id)
            except Exception:
                logger.exception("verified group mapping could not be synchronized")
                failed = store.complete_confirmation(
                    request.request_id,
                    result_code="failed",
                    status=REQUEST_FAILED,
                    error="verified GSK pair synchronization failed or is unknown",
                )
                return _request_result_message(failed or request)
            completed = store.complete_confirmation(
                request.request_id, result_code="verified_synced"
            )
            return _request_result_message(completed or request)

        if result.status == "pending":
            if current is None or current.binding_status not in {
                GROUP_VERIFIED,
                GROUP_DECLARED,
            }:
                try:
                    store.set_group_pending(
                        bot_scope=scope,
                        virtual_group_id=group_id,
                        real_group_id=candidate,
                        group_name=request.group_name,
                        bound_by_user_id=user_id,
                        verification_source=result.source,
                        operation_id=request.request_id,
                    )
                except BindingConflictError as error:
                    store.mark_group_conflict(
                        bot_scope=scope,
                        virtual_group_id=group_id,
                        error=str(error),
                        operation_id=request.request_id,
                    )
                    completed = store.complete_confirmation(
                        request.request_id,
                        result_code="conflict",
                        status=REQUEST_FAILED,
                        error=str(error),
                    )
                    return _request_result_message(completed or request)
            pending_request = store.complete_confirmation(
                request.request_id,
                result_code="pending_verification",
                status=REQUEST_PENDING,
                error=result.reason,
            )
            return _request_result_message(pending_request or request)

        if result.status in {"conflict", "rejected"}:
            if current is None or current.binding_status not in {
                GROUP_VERIFIED,
                GROUP_DECLARED,
            }:
                store.mark_group_conflict_from_verification(
                    bot_scope=scope,
                    virtual_group_id=group_id,
                    real_group_id=candidate,
                    group_name=request.group_name,
                    bound_by_user_id=user_id,
                    verification_source=result.source,
                    error=result.reason or result.status,
                    operation_id=request.request_id,
                    expected_version=current.version if current is not None else None,
                    expected_absent=current is None,
                )
            completed = store.complete_confirmation(
                request.request_id,
                result_code="conflict",
                status=REQUEST_FAILED,
                error=result.reason or result.status,
            )
            return _request_result_message(completed or request)

        raise ValueError(f"unsupported group verification status: {result.status}")
    except (BindingConflictError, QbindStorageError) as error:
        logger.exception("group binding state transition failed")
        completed = store.complete_confirmation(
            request.request_id,
            result_code="conflict" if isinstance(error, BindingConflictError) else "failed",
            status=REQUEST_FAILED,
            error=str(error),
        )
        return _request_result_message(completed or request)


qgbind = on_command("qgbind", priority=5, block=False)


@qgbind.handle()
async def qgbind_handler(
    event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()
):
    context = _context(event)
    if context is None:
        await qgbind.finish(_message("### ❌ 无法发起群绑定\n\n> `qgbind` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    from . import email_verification_required

    if email_verification_required() and not _has_group_owner_access(event, user_id):
        await qgbind.finish(
            _message(
                "### ❌ 只有当前群主可以完成邮箱认证\n\n"
                "> 邮箱模式下验证码必须发送到群主本人 QQ 邮箱，管理员不能代替群主绑定。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _has_group_operator_access(event, user_id):
        await qgbind.finish(
            _message(
                "### ❌ 没有群绑定权限\n\n"
                "> 只有当前群主/管理员或 Bot SUPERUSER 可以发起群绑定。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _personal_email_ready(scope, user_id):
        await qgbind.finish(_personal_email_prompt(user_id))
    candidate = arg.extract_plain_text().strip()
    error = _validate_real_group_id(candidate)
    if error:
        await qgbind.finish(
            _message(
                f"### ❌ 群绑定失败\n\n> {error}\n> 格式：`qgbind <真实群号>`",
                _group_action_buttons(include_status=True),
            )
        )
    current = _store().get_group(scope, group_id)
    if current and current.binding_status == GROUP_VERIFIED:
        message = (
            f"### ⚠️ 当前群已有已验证映射\n\n"
            f"> 当前虚拟群：`{group_id}`\n"
            "> 如需纠错，请使用 `qgrebind <真实群号>`，不会直接覆盖现有记录。"
        )
        await qgbind.finish(
            _message(
                message,
                _group_action_buttons(include_status=True, include_rebind=True),
            )
        )
    if current and current.binding_status == GROUP_DECLARED:
        message = (
            f"### ℹ️ 当前群已有已声明绑定\n\n"
            f"> 当前虚拟群：`{group_id}`\n"
            f"> 当前真实群号：`{current.real_group_id}`\n"
            "> 如需纠错，请使用 `qgrebind <真实群号>`，不会直接覆盖现有记录。"
        )
        await qgbind.finish(
            _message(
                message,
                _group_action_buttons(include_status=True, include_rebind=True),
            )
        )
    if (
        current
        and current.binding_status == GROUP_PENDING_VERIFICATION
        and current.real_group_id == candidate
    ):
        # A previous type=11/manual-review request can leave the group row in
        # pending_verification after its confirmation request expires.  Only
        # block a new request when the corresponding confirmation is still
        # live; an expired request must be replaceable by qgbind.
        active_request = _store().find_confirmation(
            request_kind="group_bind",
            bot_scope=scope,
            actor_user_id=current.bound_by_user_id,
            external_group_id=group_id,
            candidate_id=candidate,
        ) or _store().find_confirmation(
            request_kind="group_rebind",
            bot_scope=scope,
            actor_user_id=current.bound_by_user_id,
            external_group_id=group_id,
            candidate_id=candidate,
        )
        if active_request is not None:
            if active_request.actor_user_id == user_id:
                await qgbind.finish(
                    _message(
                        "### ⏳ 当前群已有待处理请求\n\n"
                        "> 当前群已有相同的待处理绑定请求，请点击原消息的‘确认群绑定’按钮，"
                        "或发送 `qgconfirm` 继续。",
                        _group_action_buttons(include_status=True),
                    )
                )
            await qgbind.finish(
                _message(
                    "### ⏳ 当前群已有其他管理员的待处理请求\n\n"
                    "> 请等待对方确认或取消后再重新申请。",
                    _group_action_buttons(include_status=True),
                )
            )
    group_name = await _query_group_name(bot, group_id)
    request = _store().create_confirmation(
        request_kind="group_bind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_group_id=group_id,
        candidate_id=candidate,
        group_name=group_name,
        expected_version=current.version if current else None,
        ttl_seconds=_GROUP_REQUEST_TTL,
    )
    await qgbind.finish(
        _message(
            "### ⚠️ 确认当前群绑定\n\n"
            f"> 当前虚拟群：`{group_id}`\n"
            f"> 当前群名称：`{group_name or '未读取到'}`\n"
            f"> 待绑定真实群号：`{candidate}`\n\n"
            "> 确认后只会保存 QBind 本地声明；声明不会写入 GSK 或放行群业务。"
            "群业务需要当前 Bot 下个人与群都 verified，并完成两次 type=5 同步。\n"
            "> ⏰ 请求五分钟内有效。",
            [
                [
                    {
                        "render_data.label": "✅ 确认群绑定",
                        "action.data": f"qgconfirm {request.request_id}",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [user_id],
                    },
                    {
                        "render_data.label": "❌ 取消",
                        "action.data": f"qgcancel {request.request_id}",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [user_id],
                    },
                ]
            ],
        )
    )


qgrebind = on_command("qgrebind", priority=5, block=False)


@qgrebind.handle()
async def qgrebind_handler(
    event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()
):
    context = _context(event)
    if context is None:
        await qgrebind.finish(_message("### ❌ 无法申请换绑\n\n> `qgrebind` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    from . import email_verification_required

    if email_verification_required() and not _has_group_owner_access(event, user_id):
        await qgrebind.finish(
            _message(
                "### ❌ 只有当前群主可以完成邮箱认证\n\n"
                "> 邮箱模式下验证码必须发送到群主本人 QQ 邮箱，管理员不能代替群主换绑。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _has_group_operator_access(event, user_id):
        await qgrebind.finish(
            _message(
                "### ❌ 没有群换绑权限\n\n"
                "> 只有当前群主/管理员或 Bot SUPERUSER 可以申请换绑。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _personal_email_ready(scope, user_id):
        await qgrebind.finish(_personal_email_prompt(user_id))
    candidate = arg.extract_plain_text().strip()
    error = _validate_real_group_id(candidate)
    if error:
        await qgrebind.finish(
            _message(
                f"### ❌ 群换绑失败\n\n> {error}\n> 格式：`qgrebind <真实群号>`",
                _group_action_buttons(include_status=True),
            )
        )
    current = _store().get_group(scope, group_id)
    if current is None or current.binding_status not in {
        GROUP_VERIFIED,
        GROUP_DECLARED,
    }:
        await qgrebind.finish(
            _message(
                "### ❌ 当前群没有可换绑的映射\n\n"
                "> 请先使用 `qgbind <真实群号>` 发起首次绑定。",
                _group_action_buttons(include_bind=True),
            )
        )
    if current.real_group_id == candidate:
        await qgrebind.finish(
            _message(
                "### ℹ️ 无需重复换绑\n\n> 当前群已经绑定这个真实群号，无需重复提交。",
                _group_action_buttons(include_status=True, include_unbind=True),
            )
        )
    group_name = await _query_group_name(bot, group_id)
    request = _store().create_confirmation(
        request_kind="group_rebind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_group_id=group_id,
        candidate_id=candidate,
        group_name=group_name or current.group_name,
        expected_version=current.version,
        ttl_seconds=_GROUP_REQUEST_TTL,
    )
    await qgrebind.finish(
        _message(
            "### ⚠️ 确认申请换绑\n\n"
            f"> 当前真实群号：`{current.real_group_id}`\n"
            f"> 待绑定真实群号：`{candidate}`\n\n"
            "> 换绑不会删除历史记录；确认后会更新群映射并保留为已声明状态。",
            [
                [
                    {
                        "render_data.label": "✅ 确认换绑",
                        "action.data": f"qgconfirm {request.request_id}",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [user_id],
                    }
                ]
            ],
        )
    )


qgconfirm = on_command("qgconfirm", priority=5, block=False)


@qgconfirm.handle()
async def qgconfirm_handler(
    event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()
):
    context = _context(event)
    if context is None:
        await qgconfirm.finish(_message("### ❌ 无法确认群绑定\n\n> `qgconfirm` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    if not _personal_email_ready(scope, user_id):
        await qgconfirm.finish(_personal_email_prompt(user_id))
    token = arg.extract_plain_text().strip() or None
    if token and not _token(token):
        await qgconfirm.finish(
            _message(
                "### ❌ 确认失败\n\n> 确认令牌格式不正确，请使用原消息中的按钮。",
                _group_action_buttons(include_status=True),
            )
        )
    try:
        if token:
            request = _store().get_confirmation(token)
            if request is None or request.request_kind not in {
                "group_bind",
                "group_rebind",
            }:
                request = None
            elif not _request_matches(
                request,
                kind=request.request_kind,
                scope=scope,
                user_id=user_id,
                group_id=group_id,
            ):
                raise PermissionError(
                    "confirmation request does not belong to this Bot/user/group"
                )
        else:
            request = _get_group_request(
                kind="group_bind",
                scope=scope,
                user_id=user_id,
                group_id=group_id,
                token=None,
            ) or _get_group_request(
                kind="group_rebind",
                scope=scope,
                user_id=user_id,
                group_id=group_id,
                token=None,
            )
    except PermissionError as error:
        await qgconfirm.finish(
            _message(
                f"### ❌ 确认失败\n\n> {error}",
                _group_action_buttons(include_status=True),
            )
        )
    if request is None:
        await qgconfirm.finish(
            _message(
                "### ❌ 没有待确认请求\n\n> 当前群没有属于你的待确认请求，或请求已经过期。",
                _group_action_buttons(include_bind=True),
            )
        )
    try:
        message = await _confirm_group_bind(event, bot, request)
    except PermissionError as error:
        await qgconfirm.finish(
            _message(
                f"### ❌ 确认失败\n\n> {error}",
                _group_action_buttons(include_status=True),
            )
        )
    await qgconfirm.finish(
        _message(
            message,
            (
                _group_email_code_buttons(
                    user_id,
                    include_status=True,
                    include_rebind=True,
                    include_unbind=True,
                )
                if "`qgcode <6位验证码>`" in message
                else _group_action_buttons(
                    include_status=True,
                    include_rebind=True,
                    include_unbind=True,
                )
            ),
        )
    )


qgcode = on_command("qgcode", priority=5, block=False)


@qgcode.handle()
async def qgcode_handler(
    event: GroupMessageEvent, arg: Message = CommandArg()
):
    context = _context(event)
    if context is None:
        await qgcode.finish(_message("### ❌ 无法验证群邮箱\n\n> `qgcode` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    from . import email_verification_required

    if not email_verification_required():
        await qgcode.finish(
            _message(
                "### ℹ️ QQ 邮箱认证未启用\n\n"
                "> 当前部署仍使用本地声明模式。"
            )
        )
    if email_verification_required() and not _has_group_owner_access(event, user_id):
        await qgcode.finish(
            _message(
                "### ❌ 只有当前群主可以提交验证码\n\n"
                "> 这封验证码发送到了群主 QQ 邮箱，管理员不能代替群主完成认证。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _has_group_operator_access(event, user_id):
        await qgcode.finish(
            _message(
                "### ❌ 没有群邮箱认证权限\n\n"
                "> 只有当前群主/管理员或 Bot SUPERUSER 可以提交验证码。",
                _group_action_buttons(include_status=True),
            )
        )
    if not _personal_email_ready(scope, user_id):
        await qgcode.finish(_personal_email_prompt(user_id))
    code = arg.extract_plain_text().strip()
    if not re.fullmatch(r"\d{6}", code):
        await qgcode.finish(
            _message(
                "### ❌ 验证码格式不正确\n\n> 请发送邮件中的 6 位数字验证码，例如：`qgcode 123456`。",
                _group_email_code_buttons(user_id, include_status=True),
            )
        )
    challenge = _store().find_email_challenge(
        purpose="group_rebind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_user_id=user_id,
        external_group_id=group_id,
    ) or _store().find_email_challenge(
        purpose="group_bind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_user_id=user_id,
        external_group_id=group_id,
    )
    if challenge is None:
        await qgcode.finish(
            _message(
                "### ❌ 没有待验证的群主邮箱请求\n\n"
                "> 请先在本群发送 `qgbind <真实群号>` 发起群绑定。",
                _group_action_buttons(include_bind=True),
            )
        )
    from . import _email_service

    attempt = _store().verify_email_challenge(
        challenge.challenge_id,
        code_digest(challenge.challenge_id, code, _email_service.config.code_secret),
    )
    if not attempt.accepted:
        message = {
            "invalid_code": "验证码不正确，请检查群主 QQ 邮箱后重试。",
            "max_attempts": "验证码尝试次数已用完，请重新发起群绑定。",
            "expired": "验证码已过期，请重新发起群绑定。",
            "canceled": "验证码请求已失效，请重新发起群绑定。",
        }.get(attempt.reason, "当前验证码请求不可用，请重新发起群绑定。")
        await qgcode.finish(
            _message(
                f"### ❌ 群主 QQ 邮箱验证失败\n\n> {message}",
                _group_email_code_buttons(
                    user_id,
                    include_bind=True,
                    include_status=True,
                ),
            )
        )
    current = _store().get_group(scope, group_id)
    if (
        current is None
        or current.binding_status != GROUP_PENDING_VERIFICATION
        or current.real_group_id != challenge.candidate_id
        or (
            challenge.expected_version is not None
            and current.version != challenge.expected_version
        )
    ):
        await qgcode.finish(
            _message(
                "### ❌ 群绑定状态已变化\n\n> 邮箱已验证，但当前群映射已经变化，未覆盖现有记录。",
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )
    try:
        verified = _store().verify_group(
            bot_scope=scope,
            virtual_group_id=group_id,
            verified_by=user_id,
            verification_source="qgroupbind:qq_email",
            operation_id=challenge.challenge_id,
            expected_version=challenge.expected_version,
        )
    except (BindingConflictError, QbindStorageError) as error:
        logger.exception("group QQ email verification could not finalize mapping")
        await qgcode.finish(
            _message(
                f"### ❌ 群邮箱已验证，但本地群绑定保存失败\n\n> {error}",
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )
    from . import _publish_verified_membership, sync_verified_pair

    try:
        await sync_verified_pair(scope, user_id, group_id)
    except Exception:
        logger.exception("group QQ verification succeeded but GSK pair sync failed")
        await qgcode.finish(
            _message(
                "### ⚠️ 群邮箱已验证，但 GSK 成对映射同步失败\n\n"
                "> 群业务已阻断，远端结果可能未知；请管理员显式对账后再恢复。",
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )

    await _publish_verified_membership(
        self_id=scope,
        group_id=group_id,
        user_id=user_id,
        group_name=verified.group_name,
        source="qbind.group_verified",
    )
    await qgcode.finish(
        _message(
            "### ✅ 群主 QQ 邮箱认证完成\n\n"
            f"> 当前群：**{verified.group_name or '当前群'}**\n"
            "> 当前 Bot 下个人与群 verified 映射已同步；其他用户仍须完成个人 verified 绑定。",
            _group_action_buttons(include_status=True, include_rebind=True, include_unbind=True),
        )
    )


qgcancel = on_command("qgcancel", priority=5, block=False)


@qgcancel.handle()
async def qgcancel_handler(event: GroupMessageEvent, arg: Message = CommandArg()):
    context = _context(event)
    if context is None:
        await qgcancel.finish(_message("### ❌ 无法取消群绑定\n\n> `qgcancel` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    token = arg.extract_plain_text().strip() or None
    if token and not _token(token):
        await qgcancel.finish(
            _message(
                "### ❌ 取消失败\n\n> 取消令牌格式不正确，请使用原消息中的按钮。",
                _group_action_buttons(include_status=True),
            )
        )
    request = None
    if token:
        request = _store().get_confirmation(token)
        if request is None or not _request_matches(
            request,
            kind=request.request_kind,
            scope=scope,
            user_id=user_id,
            group_id=group_id,
        ) or request.request_kind not in {"group_bind", "group_rebind"}:
            await qgcancel.finish(
                _message(
                    "### ❌ 取消失败\n\n> 该取消请求不属于当前 Bot、用户或群。",
                    _group_action_buttons(include_status=True),
                )
            )
    else:
        request = _store().find_confirmation(
            request_kind="group_bind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_group_id=group_id,
        ) or _store().find_confirmation(
            request_kind="group_rebind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_group_id=group_id,
        )
    if request is None:
        await qgcancel.finish(
            _message(
                "### ❌ 没有待取消请求\n\n> 当前群没有属于你的待取消请求。",
                _group_action_buttons(include_bind=True),
            )
        )
    canceled = _store().cancel_confirmation(request.request_id)
    await qgcancel.finish(
        _message(
            _request_result_message(canceled or request),
            _group_action_buttons(include_status=True, include_bind=True),
        )
    )


qgstatus = on_command("qgstatus", priority=5, block=False)


@qgstatus.handle()
async def qgstatus_handler(event: GroupMessageEvent):
    context = _context(event)
    if context is None:
        await qgstatus.finish(_message("### ❌ 无法查看群状态\n\n> `qgstatus` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    record = _store().get_group(scope, group_id)
    if record is None:
        await qgstatus.finish(
            _message(
                "### ℹ️ 当前群尚未建立 QGroupBind 记录",
                _group_action_buttons(include_bind=True),
            )
        )
    can_view_candidate = _has_group_operator_access(event, user_id)
    real_id = record.real_group_id if can_view_candidate else None
    details = [
        f"当前虚拟群：`{group_id}`",
        f"状态：`{_status_label(record.binding_status)}`",
        f"群名称：`{record.group_name or '未记录'}`",
    ]
    if real_id and record.binding_status in {GROUP_VERIFIED, GROUP_DECLARED}:
        details.append(f"当前真实群号：`{real_id}`")
    elif can_view_candidate and real_id:
        details.append(f"待审核群号：`{real_id}`（未验证，不对外提供）")
    if record.last_error:
        details.append(f"原因：`{record.last_error}`")
    await qgstatus.finish(
        _message(
            "### QGroupBind 状态\n\n" + "\n".join(details),
            _group_action_buttons(
                include_status=True,
                include_rebind=can_view_candidate
                and record.binding_status in {GROUP_VERIFIED, GROUP_DECLARED},
                include_unbind=can_view_candidate
                and record.binding_status in {GROUP_VERIFIED, GROUP_DECLARED},
            ),
        )
    )


qgunbind = on_command("qgunbind", priority=5, block=False)


@qgunbind.handle()
async def qgunbind_handler(event: GroupMessageEvent, arg: Message = CommandArg()):
    context = _context(event)
    if context is None:
        await qgunbind.finish(_message("### ❌ 无法申请群解绑\n\n> `qgunbind` 只能在群聊中使用。"))
    scope, user_id, group_id = context
    if not _has_group_operator_access(event, user_id):
        await qgunbind.finish(
            _message(
                "### ❌ 没有群解绑权限\n\n"
                "> 只有当前群主/管理员或 Bot SUPERUSER 可以申请群解绑。",
                _group_action_buttons(include_status=True),
            )
        )
    value = arg.extract_plain_text().strip()
    token = value if value else None
    if token and not _token(token):
        await qgunbind.finish(
            _message(
                "### ❌ 解绑失败\n\n> 解绑令牌格式不正确，请使用原消息中的按钮。",
                _group_action_buttons(include_status=True),
            )
        )
    request = None
    if token:
        request = _store().get_confirmation(token)
        if request is None or not _request_matches(
            request,
            kind="group_unbind",
            scope=scope,
            user_id=user_id,
            group_id=group_id,
        ):
            await qgunbind.finish(
                _message(
                    "### ❌ 解绑失败\n\n> 该解绑请求不属于当前 Bot、用户或群。",
                    _group_action_buttons(include_status=True),
                )
            )
    else:
        current = _store().get_group(scope, group_id)
        if current is None or current.binding_status not in {
            GROUP_VERIFIED,
            GROUP_DECLARED,
        }:
            await qgunbind.finish(
                _message(
                    "### ❌ 当前群没有可解绑的映射",
                    _group_action_buttons(include_bind=True),
                )
            )
        request = _store().create_confirmation(
            request_kind="group_unbind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_group_id=group_id,
            expected_version=current.version,
            ttl_seconds=_GROUP_REQUEST_TTL,
        )
        await qgunbind.finish(
            _message(
                "### ⚠️ 确认群解绑\n\n"
                f"> 当前真实群号：`{current.real_group_id}`\n"
                "> 解绑只撤销群身份映射，不会删除成员个人档案或经济资产。",
                [
                    [
                        {
                            "render_data.label": "⚠️ 确认解绑",
                            "action.data": f"qgunbind {request.request_id}",
                            "action.enter": True,
                            "action.permission.type": 0,
                            "action.permission.specify_user_ids": [user_id],
                        }
                    ]
                ],
            )
        )

    if request.status != REQUEST_PENDING:
        await qgunbind.finish(
            _message(
                _request_result_message(request),
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )
    if request.expires_at <= time.time():
        expired = _store().complete_confirmation(
            request.request_id, result_code="expired", status=REQUEST_EXPIRED
        )
        await qgunbind.finish(
            _message(
                _request_result_message(expired or request),
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )
    current = _store().get_group(scope, group_id)
    if current is None or current.binding_status not in {
        GROUP_VERIFIED,
        GROUP_DECLARED,
    }:
        await qgunbind.finish(
            _message(
                "### ❌ 群映射已变化\n\n> 当前群映射已变化，未执行解绑。",
                _group_action_buttons(include_status=True),
            )
        )
    try:
        revoked = _store().revoke_group(
            bot_scope=scope,
            virtual_group_id=group_id,
            actor_user_id=user_id,
            expected_version=request.expected_version,
            operation_id=request.request_id,
        )
    except (BindingConflictError, QbindStorageError) as error:
        failed = _store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error=str(error),
        )
        await qgunbind.finish(
            _message(
                _request_result_message(failed or request),
                _group_action_buttons(include_status=True, include_bind=True),
            )
        )
    from . import _notify_membership_observer

    await _notify_membership_observer(
        {
            "operation": "revoke_group",
            "bot_scope": scope,
            "virtual_group_id": group_id,
            "real_group_id": current.real_group_id or revoked.real_group_id,
        }
    )
    completed = _store().complete_confirmation(request.request_id, result_code="revoked")
    await qgunbind.finish(
        _message(
            f"✅ QBind 本地群绑定已撤销（版本 {revoked.version}）。\n"
            "GSK type=5 不提供可靠删除，远端映射需显式对账；群绑定历史已保留，成员个人身份未受影响。"
            + (f"\n{_request_result_message(completed)}" if completed else ""),
            _group_action_buttons(include_status=True, include_bind=True),
        )
    )


__all__ = [
    "GroupIdentityResolution",
    "GroupIdentityResolver",
    "GroupVerificationAdapter",
    "GroupVerificationContext",
    "GroupVerificationResult",
    "GensokyoGroupVerifier",
    "approve_group_binding",
    "approve_group_binding_request",
    "ensure_group_bound",
    "get_group_binding_status",
    "get_group_name",
    "get_group_mapping_client",
    "get_group_verifier",
    "get_real_group",
    "is_group_bound",
    "resolve_group_identity",
    "set_group_verifier",
    "set_group_mapping_client",
]
