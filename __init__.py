"""Amia QBind V2.

QBind is the only identity binding boundary for the Amia plugin ecosystem.
The public compatibility helpers remain available.  With email verification
enabled, personal and group canonical identities are usable by ordinary
business handlers only after their QQ-mail challenges reach ``verified``;
legacy declaration mode remains available for migration and tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlencode

from nonebot import get_driver, on_command, on_message, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.consts import CMD_KEY, PREFIX_KEY
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, T_State
from nonebot.plugin import PluginMetadata
from nonebot.permission import SUPERUSER

from .failure_log import install_failure_handler
from .gensokyo import (
    GensokyoMappingClient,
    GensokyoMappingError,
    GensokyoMappingExistsError,
    GensokyoMappingMissingError,
)
from .mail import (
    MailConfigurationError,
    MailDeliveryError,
    QbindEmailService,
    code_digest,
    email_digest,
    mask_email,
    qq_email,
)
from .storage import (
    GROUP_CONFLICT,
    GROUP_DECLARED,
    GROUP_PENDING_VERIFICATION,
    GROUP_REVOKED,
    GROUP_VERIFIED,
    MAPPING_GROUP,
    MAPPING_PERSONAL,
    PERSONAL_DECLARED,
    PERSONAL_REVOKED,
    PERSONAL_UNBIND_PENDING,
    PERSONAL_VERIFIED,
    REQUEST_BACKEND_UNKNOWN,
    REQUEST_CANCELED,
    REQUEST_EXPIRED,
    REQUEST_FAILED,
    REQUEST_PENDING,
    BindingConflictError,
    ConfirmationRequest,
    EmailChallenge,
    GroupUserMembership,
    PersonalBinding,
    VerifiedPair,
    QbindStorageError,
    QbindStore,
)

logger = logging.getLogger(__name__)

# Mirror every ERROR to data/qbind_log so failures survive without a terminal.
install_failure_handler(logger)

USE_MARKDOWN = os.getenv("QBIND_USE_MARKDOWN", "true").lower() not in {
    "0",
    "false",
    "no",
}

__plugin_meta__ = PluginMetadata(
    name="账号绑定",
    description="账号绑定与群组身份绑定（仅在收到绑定相关指令时响应）",
    usage=(
        "qbind <QQ号> / qunbind / qgbind <群号> / qgstatus / "
        "get_real_qq(), get_real_group(), resolve_context_identity()"
    ),
    extra={"menu_hide": True, "hide": True},
)


# ---------------------------------------------------------------------------
# Message and keyboard helpers
# ---------------------------------------------------------------------------

def _normalize_button(btn: Dict[str, Any]) -> Dict[str, Any]:
    nested: Dict[str, Any] = {}
    for key, value in btn.items():
        parts = key.split(".")
        current = nested
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return nested


def _build_button_row(buttons: List[Dict[str, Any]]) -> dict:
    btn_list = []
    for btn in buttons:
        button = _normalize_button(btn)
        render_data = button.setdefault("render_data", {})
        label = render_data.get("label", "按钮")
        render_data.setdefault("visited_label", label)
        render_data.setdefault("style", 1)
        action = button.setdefault("action", {})
        action.setdefault("type", 2)
        action.setdefault("permission", {"type": 2, "specify_user_ids": []})
        action.setdefault("data", "")
        action.setdefault("unsupport_tips", "当前客户端不支持该按钮，请使用文本指令。")
        if action["type"] == 2:
            action.setdefault("reply", False)
            action.setdefault("enter", False)
            action.setdefault("anchor", 0)
        elif action["type"] == 0:
            action["enter"] = True
            action.pop("reply", None)
        if "id" not in button:
            digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
            button["id"] = f"btn_{digest}"
        btn_list.append(button)
    return {"buttons": btn_list}


def _build_keyboard(buttons_config: List[List[Dict[str, Any]]]) -> dict:
    return {"content": {"rows": [_build_button_row(row) for row in buttons_config]}}


def _build_markdown_msg(
    content: str, buttons: Optional[List[List[Dict[str, Any]]]] = None
) -> Message:
    data: Dict[str, Any] = {"markdown": {"content": content}}
    if buttons:
        data["keyboard"] = _build_keyboard(buttons)
    return Message(MessageSegment(type="markdown", data={"data": data}))


def _reply(content: str, buttons: Optional[List[List[Dict[str, Any]]]] = None):
    return _build_markdown_msg(content, buttons) if USE_MARKDOWN else content


def _personal_action_buttons(
    user_id: str, *, include_unbind: bool = True
) -> List[List[Dict[str, Any]]]:
    """Return buttons for actions that are owned by the current user.

    The command handlers still enforce ownership.  The explicit button
    permission only prevents another member from accidentally clicking a
    personal binding action in a group message.
    """

    permission = {
        "action.permission.type": 0,
        "action.permission.specify_user_ids": [str(user_id)],
    }
    row: List[Dict[str, Any]] = [
        {
            "render_data.label": "重新发起绑定",
            "action.data": "qbind",
            "action.enter": True,
            **permission,
        }
    ]
    if include_unbind:
        row.append(
            {
                "render_data.label": "解绑个人绑定",
                "action.data": "qunbind",
                "action.enter": True,
                **permission,
            }
        )
    return [row]


def _personal_email_code_buttons(user_id: str) -> List[List[Dict[str, Any]]]:
    """Return a button that prefills the personal email-code command.

    QQ keyboard buttons cannot read the code from an email or submit a value
    that the user has not entered.  Keeping ``action.enter`` false lets the
    user click the button, type the six-digit code after the prefilled command,
    and then send it normally.
    """

    return [
        [
            {
                "render_data.label": "输入邮箱验证码",
                "action.data": "qbindcode ",
                "action.enter": False,
                "action.permission.type": 0,
                "action.permission.specify_user_ids": [str(user_id)],
            }
        ]
    ]


def _masked_for_display(value: str) -> str:
    """Render a masked address with ``∗`` so it cannot be read as a glob.

    ``mail.mask_email`` keeps ASCII ``*`` because the masking contract is
    asserted verbatim elsewhere; only the receipt text is swapped.
    """

    return str(value).replace("*", "∗")


# ---------------------------------------------------------------------------
# Store lifecycle and legacy compatibility
# ---------------------------------------------------------------------------

_BINDS_FILE = Path(__file__).with_name("binds.json")
_DEFAULT_DB_PATH = Path(
    os.getenv("QBIND_DB_PATH")
    or (Path(os.getenv("QBIND_DATA_DIR", str(Path(__file__).with_name("data")))) / "qbind.sqlite3")
)

_store: Optional[QbindStore] = None
_storage_error: Optional[Exception] = None
_email_service = QbindEmailService()

# Kept only for source compatibility with old integrations and tests.  New
# writes use SQLite and never append to this mapping.
_binds: Dict[str, str] = {}
_pending_binds: Dict[str, Dict[str, Any]] = {}
_membership_observer: Optional[
    Callable[[dict[str, object]], Awaitable[None]]
] = None


def _load_binds() -> None:
    """Load the old map for legacy one-argument helper compatibility.

    The actual migration is performed by :class:`QbindStore` and remains in
    SQLite.  Invalid JSON is retained as an explicit startup error rather than
    silently becoming an empty mapping.
    """

    _binds.clear()
    if not _BINDS_FILE.exists():
        return
    try:
        payload = json.loads(_BINDS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QbindStorageError(f"unable to read legacy binds.json: {error}") from error
    if isinstance(payload, dict) and isinstance(payload.get("bindings"), dict):
        payload = payload["bindings"]
    if not isinstance(payload, dict):
        raise QbindStorageError("legacy binds.json must contain an object mapping")
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            _binds[key] = value


def _initialise_store(
    db_path: Path = _DEFAULT_DB_PATH, legacy_json_path: Path = _BINDS_FILE
) -> None:
    global _store, _storage_error
    try:
        _store = QbindStore(db_path, legacy_json_path)
        _storage_error = None
        if legacy_json_path == _BINDS_FILE:
            _load_binds()
    except Exception as error:
        _store = None
        _storage_error = error
        logger.exception("QBind storage initialisation failed")


def configure_storage(
    db_path: os.PathLike[str] | str,
    legacy_json_path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Point QBind at an explicit database, primarily for tests/operations."""

    _initialise_store(Path(db_path), Path(legacy_json_path or _BINDS_FILE))
    if _store is None:
        raise QbindStorageError(str(_storage_error or "storage initialisation failed"))


def get_store() -> QbindStore:
    if _store is None:
        raise QbindStorageError(str(_storage_error or "QBind storage is unavailable"))
    return _store


def set_membership_observer(
    observer: Optional[Callable[[dict[str, object]], Awaitable[None]]],
) -> None:
    """Install an optional sink for verified user/group relationship changes.

    QBind remains the source of truth and does not know about the downstream
    Amia-Sync service.  The observer is an integration seam: failures are
    isolated and never turn a successful local binding into a fake failure.
    """

    if observer is not None and not callable(observer):
        raise TypeError("membership observer must be callable or None")
    global _membership_observer
    _membership_observer = observer


async def _notify_membership_observer(change: dict[str, object]) -> None:
    observer = _membership_observer
    if observer is None:
        return
    try:
        await observer(dict(change))
    except Exception:
        # The local QBind transaction has already succeeded.  Downstream
        # synchronisation is best effort and can be retried by the next
        # explicit Bot command without hiding the real binding result.
        logger.warning("QBind membership observer failed", exc_info=True)


def set_email_service(service: QbindEmailService) -> None:
    """Replace the mail service, primarily for isolated tests."""

    if not hasattr(service, "ensure_configured") or not hasattr(service, "send_code"):
        raise TypeError("email service must provide ensure_configured/send_code")
    global _email_service
    _email_service = service


def email_verification_required() -> bool:
    """Return whether new bindings must complete QQ-mail verification."""

    return bool(_email_service.required)


def personal_auth_ready(user_id: object, self_id: object) -> bool:
    """Return the personal prerequisite used by QGroupBind.

    Existing deployments can keep the legacy declaration mode while SMTP is
    being configured.  Once ``QBIND_EMAIL_REQUIRED=true`` is enabled, only a
    personally email-verified record satisfies this gate.
    """

    if email_verification_required():
        return get_verified_qq(str(user_id), str(self_id)) is not None
    return is_bound(str(user_id), str(self_id))


async def start_email_challenge(
    *,
    purpose: str,
    bot_scope: str,
    actor_user_id: str,
    qq_number: str,
    external_user_id: Optional[str] = None,
    external_group_id: Optional[str] = None,
    candidate_id: Optional[str] = None,
    group_name: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> tuple[EmailChallenge, str]:
    """Create and deliver one local QQ-mail challenge.

    The caller supplies a canonical QQ number already obtained from the
    binding flow.  The recipient is derived here as ``<qq>@qq.com``; callers
    cannot substitute another email address.
    """

    _email_service.ensure_configured()
    address = qq_email(qq_number)
    challenge_id = uuid.uuid4().hex
    code, digest = _email_service.new_code(challenge_id)
    challenge = get_store().create_email_challenge(
        challenge_id=challenge_id,
        purpose=purpose,
        bot_scope=bot_scope,
        actor_user_id=actor_user_id,
        external_user_id=external_user_id,
        external_group_id=external_group_id,
        candidate_id=candidate_id,
        group_name=group_name,
        expected_version=expected_version,
        target_email_digest=email_digest(bot_scope, address),
        code_digest=digest,
        ttl_seconds=_email_service.config.challenge_ttl_seconds,
        max_attempts=_email_service.config.max_attempts,
    )
    try:
        await _email_service.send_code(
            recipient=address,
            code=code,
            purpose=purpose,
        )
    except Exception:
        get_store().cancel_email_challenge(challenge.challenge_id)
        raise
    return challenge, mask_email(address)


def storage_health() -> dict[str, Optional[str]]:
    if _store is None:
        return {"database": None, "legacy_json": str(_BINDS_FILE), "migration_error": str(_storage_error)}
    return _store.storage_health()


def _save_binds() -> None:
    """Compatibility-only atomic writer for callers still using ``_binds``."""

    temporary = _BINDS_FILE.with_name(f"{_BINDS_FILE.name}.tmp-{uuid.uuid4().hex}")
    try:
        _BINDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(_binds, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, _BINDS_FILE)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise QbindStorageError(f"unable to write legacy binds.json: {error}") from error


_initialise_store()


def _legacy_get(session_id: str, *, verified_only: bool) -> Optional[str]:
    if verified_only:
        return None
    if session_id in _binds:
        return _binds[session_id]
    if session_id in _binds.values():
        return session_id
    if _store is not None:
        record = _store.get_personal("legacy", session_id)
        if record and record.binding_status in {
            PERSONAL_DECLARED,
            PERSONAL_VERIFIED,
            PERSONAL_UNBIND_PENDING,
        }:
            return record.canonical_user_id
        record = _store.get_personal_by_canonical("legacy", session_id)
        if record and record.binding_status in {
            PERSONAL_DECLARED,
            PERSONAL_VERIFIED,
            PERSONAL_UNBIND_PENDING,
        }:
            return record.canonical_user_id
    return None


def get_binding_status(session_id: object, self_id: Optional[object] = None) -> str:
    user_id = str(session_id)
    if self_id is None:
        if _legacy_get(user_id, verified_only=False) is not None:
            return PERSONAL_DECLARED
        return "unbound"
    record = get_store().get_personal(str(self_id), user_id)
    return record.binding_status if record else "unbound"


def get_real_qq(
    session_id: object,
    self_id: Optional[object] = None,
    *,
    verified_only: bool = False,
) -> Optional[str]:
    """Return the declared/verified QQ for compatibility.

    Pass ``verified_only=True`` or use :func:`get_verified_qq` for a trusted
    canonical identity.  Omitting ``self_id`` is a legacy compatibility mode;
    it never scans all Bot namespaces.
    """

    user_id = str(session_id)
    if self_id is None:
        return _legacy_get(user_id, verified_only=verified_only)
    record = get_store().get_personal(str(self_id), user_id)
    if record is None:
        return None
    if verified_only and record.binding_status != PERSONAL_VERIFIED:
        return None
    if record.binding_status not in {
        PERSONAL_DECLARED,
        PERSONAL_VERIFIED,
        PERSONAL_UNBIND_PENDING,
    }:
        return None
    return record.canonical_user_id


def get_verified_qq(session_id: object, self_id: Optional[object] = None) -> Optional[str]:
    return get_real_qq(session_id, self_id, verified_only=True)


def get_verified_pair(
    self_id: object, user_id: object, group_id: object
) -> Optional[VerifiedPair]:
    """Return same-Bot personal/group rows only when both are verified."""

    return get_store().get_verified_pair(self_id, user_id, group_id)


def record_verified_membership(
    *,
    self_id: object,
    group_id: object,
    user_id: object,
    group_name: Optional[object] = None,
    source: str = "qbind.command_gate",
) -> Optional[GroupUserMembership]:
    """Record a relationship only after both QBind sides are verified."""

    return get_store().record_verified_membership(
        bot_scope=str(self_id),
        virtual_group_id=str(group_id),
        virtual_user_id=str(user_id),
        group_name=group_name,
        source=source,
    )


def get_group_members(
    group_id: object,
    self_id: object,
    *,
    limit: int = 1000,
) -> list[GroupUserMembership]:
    """Return currently verified users observed in a real group."""

    return get_store().get_group_members(str(self_id), str(group_id), limit=limit)


def get_user_groups(
    user_id: object,
    self_id: object,
    *,
    limit: int = 1000,
) -> list[GroupUserMembership]:
    """Return currently verified groups observed for a real user."""

    return get_store().get_user_groups(str(self_id), str(user_id), limit=limit)


def _membership_change(membership: GroupUserMembership) -> dict[str, object]:
    return {
        "operation": "upsert",
        "bot_scope": membership.bot_scope,
        "virtual_group_id": membership.virtual_group_id,
        "real_group_id": membership.real_group_id,
        "group_name": membership.group_name,
        "virtual_user_id": membership.virtual_user_id,
        "real_user_id": membership.real_user_id,
        "membership_status": membership.membership_status,
        "source": membership.source,
        "first_seen_at": membership.first_seen_at,
        "last_seen_at": membership.last_seen_at,
    }


async def _publish_verified_membership(
    *,
    self_id: object,
    group_id: object,
    user_id: object,
    group_name: Optional[object] = None,
    source: str = "qbind.command_gate",
) -> Optional[GroupUserMembership]:
    membership = record_verified_membership(
        self_id=self_id,
        group_id=group_id,
        user_id=user_id,
        group_name=group_name,
        source=source,
    )
    if membership is not None:
        await _notify_membership_observer(_membership_change(membership))
    return membership


def is_bound(
    session_id: object,
    self_id: Optional[object] = None,
    *,
    verified_only: bool = False,
) -> bool:
    return get_real_qq(session_id, self_id, verified_only=verified_only) is not None


def approve_personal_binding(
    *,
    self_id: object,
    user_id: object,
    verified_by: object,
    verification_source: str,
    operation_id: Optional[str] = None,
):
    """Promote a declaration only after an external trusted verifier acted."""

    return get_store().verify_personal(
        bot_scope=str(self_id),
        external_user_id=str(user_id),
        verified_by=str(verified_by),
        verification_source=verification_source,
        operation_id=operation_id,
    )


def approve_personal_alias(
    *,
    self_id: object,
    user_id: object,
    canonical_user_id: object,
    verified_by: object,
    verification_source: str,
    operation_id: Optional[str] = None,
):
    """Link another virtual user ID after trusted proof of the same QQ identity."""

    return get_store().link_verified_personal(
        bot_scope=str(self_id),
        external_user_id=str(user_id),
        canonical_user_id=str(canonical_user_id),
        bound_by_user_id=str(verified_by),
        verified_by=str(verified_by),
        verification_source=verification_source,
        operation_id=operation_id,
    )


async def ensure_bound(event: MessageEvent, bot: Bot) -> bool:
    """Require a completed personal binding for an explicit command.

    This compatibility gate deliberately checks that a QBind record exists,
    including the ``declared`` state produced by the normal qbind flow.  APIs
    that need a trusted canonical QQ must continue to call
    :func:`get_verified_qq` or the Core resolver instead.
    """

    self_id = str(getattr(event, "self_id", ""))
    user_id = str(event.get_user_id())
    if self_id and is_bound(user_id, self_id):
        return True
    await _send_bind_prompt_once(event, bot)
    return False


# ---------------------------------------------------------------------------
# Prompt/rate-limit helpers
# ---------------------------------------------------------------------------

_BIND_PROMPT_TTL_SECONDS = 30
_PROMPT_CACHE_KEY = "_amia_qbind_bind_prompt_cache"
_RATE_LIMIT_SECONDS = 5
_last_cmd_time: Dict[str, float] = {}
_PENDING_TIMEOUT = 120


def _bind_prompt_key(event: MessageEvent) -> str:
    return ":".join(
        (
            str(getattr(event, "self_id", "")),
            str(getattr(event, "message_id", "")),
            event.get_user_id(),
        )
    )


async def _send_bind_prompt_once(event: MessageEvent, bot: Bot) -> bool:
    driver = get_driver()
    now = time.monotonic()
    cache = getattr(driver, _PROMPT_CACHE_KEY, None)
    if cache is None:
        cache = {}
        setattr(driver, _PROMPT_CACHE_KEY, cache)
    for key, expires_at in tuple(cache.items()):
        if expires_at <= now:
            cache.pop(key, None)
    key = _bind_prompt_key(event)
    if key in cache:
        return False
    cache[key] = now + _BIND_PROMPT_TTL_SECONDS
    if email_verification_required():
        content = (
            "### 🔒 请先完成 QQ 邮箱认证\n\n"
            "> 当前 Bot 下还没有你的已验证个人 QQ 绑定。\n"
            "> 请发送 `qbind <你的QQ号>`，再按邮件提示发送 "
            "`qbindcode <6位验证码>`；认证完成后才能使用业务指令。"
        )
    else:
        content = (
            "### 🔒 请先完成 QQ 号绑定\n\n"
            "> 当前 Bot 下还没有你的个人 QQ 号绑定。\n"
            "> 请发送 `qbind <你的QQ号>` 并确认；绑定后业务后台会读取此声明值，后续安全审核另行完成。"
        )
    await bot.send(
        event,
        _reply(
            content,
            _personal_action_buttons(event.get_user_id(), include_unbind=False),
        ),
    )
    return True


def _check_rate_limit(key: str) -> Optional[int]:
    now = time.time()
    last = _last_cmd_time.get(key)
    if last and now - last < _RATE_LIMIT_SECONDS:
        return int(_RATE_LIMIT_SECONDS - (now - last)) + 1
    _last_cmd_time[key] = now
    return None


async def _cleanup_expired_pending() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            get_store().expire_confirmations()
        except QbindStorageError:
            logger.exception("unable to expire QBind confirmation requests")
        now = time.time()
        for key, value in tuple(_pending_binds.items()):
            if now - float(value.get("timestamp", now)) > _PENDING_TIMEOUT:
                _pending_binds.pop(key, None)


def _scope_user(event: MessageEvent) -> tuple[str, str]:
    return str(getattr(event, "self_id", "")).strip(), str(event.get_user_id()).strip()


_BINDING_COMMANDS = frozenset(
    {
        "qbind",
        "confirm_bind",
        "cancel_bind",
        "qbindcode",
        "qunbind",
        "qgbind",
        "qgconfirm",
        "qgcancel",
        "qgcode",
        "qgstatus",
        "qgunbind",
        "qgrebind",
    }
)
_INFORMATION_COMMANDS = frozenset({"help", "menu", "qhelp", "帮助", "菜单"})
_GUARD_PROMPT_TTL_SECONDS = 30
_GUARD_PROMPT_CACHE_KEY = "_amia_qbind_guard_prompt_cache"


def _parsed_command_name(state: T_State) -> Optional[str]:
    """Return the command that NoneBot parsed for this message, if any.

    NoneBot runs ``TrieRule.get_value`` before checking matchers.  Reading that
    state is important here: checking the raw text would treat ordinary group
    conversation as a business command, especially when ``command_start``
    contains the empty string.
    """

    prefix = state.get(PREFIX_KEY)
    if not isinstance(prefix, dict):
        return None
    command = prefix.get(CMD_KEY)
    if not command:
        return None
    if isinstance(command, (tuple, list)):
        return ".".join(str(part) for part in command).casefold()
    return str(command).casefold()


def _is_binding_or_information_command(state: T_State) -> bool:
    command = _parsed_command_name(state)
    return command in _BINDING_COMMANDS or command in _INFORMATION_COMMANDS


def _guard_prompt_key(event: MessageEvent, kind: str) -> str:
    return ":".join(
        (
            kind,
            str(getattr(event, "self_id", "")),
            str(getattr(event, "message_id", "")),
            str(getattr(event, "group_id", "")),
            event.get_user_id(),
        )
    )


def _guard_prompt_is_new(event: MessageEvent, kind: str) -> bool:
    driver = get_driver()
    now = time.monotonic()
    cache = getattr(driver, _GUARD_PROMPT_CACHE_KEY, None)
    if cache is None:
        cache = {}
        setattr(driver, _GUARD_PROMPT_CACHE_KEY, cache)
    for key, expires_at in tuple(cache.items()):
        if expires_at <= now:
            cache.pop(key, None)
    key = _guard_prompt_key(event, kind)
    if key in cache:
        return False
    cache[key] = now + _GUARD_PROMPT_TTL_SECONDS
    return True


async def _send_group_bind_prompt_once(event: MessageEvent, bot: Bot) -> bool:
    if not _guard_prompt_is_new(event, "group"):
        return False
    await bot.send(
        event,
        _reply(
            "### 🔒 当前群尚未完成 QGroupBind\n\n"
            "> 当前 Bot 指令需要先完成本群身份绑定。\n"
            "> 请由群主/管理员在本群发送 `qgbind <真实群号>` 完成群身份绑定。\n"
            "> 绑定完成前，群业务指令不会执行；普通聊天不会触发此提示。",
            [
                [
                    {
                        "render_data.label": "发起群绑定",
                        "action.data": "qgbind",
                        "action.enter": True,
                    },
                    {
                        "render_data.label": "查看群绑定状态",
                        "action.data": "qgstatus",
                        "action.enter": True,
                    },
                ]
            ],
        ),
    )
    return True


def _group_status_buttons(*, include_bind: bool = False) -> List[List[Dict[str, Any]]]:
    row: List[Dict[str, Any]] = []
    if include_bind:
        row.append(
            {
                "render_data.label": "发起群绑定",
                "action.data": "qgbind ",
                "action.enter": False,
            }
        )
    row.append(
        {
            "render_data.label": "查看群绑定状态",
            "action.data": "qgstatus",
            "action.enter": True,
        }
    )
    return [row]


def _group_block_prompt(
    status: str,
) -> tuple[str, str, List[List[Dict[str, Any]]]]:
    """Explain a locally blocking group state.

    Returns the prompt-dedupe kind, the markdown body and its buttons.  Every
    text is derived from local state only: none of them carries a QQ number, a
    group number or a virtual ID, and none of them mentions GSK, because no
    remote write has happened yet in these branches.
    """

    if status == GROUP_DECLARED:
        return (
            "group_declared",
            # Declared rows only exist while email verification is disabled
            # (group.py promotes to pending_verification otherwise), so qgcode
            # and qgconfirm cannot help here: say what actually unblocks it.
            "### 🔒 本群绑定仍是声明状态\n\n"
            "> 当前部署为本地声明模式，群绑定声明不会放行群业务指令。\n"
            "> 需要启用 QQ 邮箱认证，或由管理员完成可信审核后，才能放行。\n"
            "> 可用 `qgstatus` 查看当前群绑定状态。",
            _group_status_buttons(),
        )
    if status == GROUP_PENDING_VERIFICATION:
        return (
            "group_pending_verification",
            "### 🔒 本群绑定等待邮箱验证码\n\n"
            "> 本群绑定正在等待邮箱验证码，验证完成前群业务指令不会执行。\n"
            "> 请发起人（群主/管理员）发送 `qgcode <6位验证码>` 完成验证。\n"
            "> 可用 `qgstatus` 查看当前群绑定状态。",
            _group_status_buttons(),
        )
    if status == GROUP_CONFLICT:
        return (
            "group_conflict",
            "### 🔒 本群绑定存在映射冲突\n\n"
            "> 本群绑定存在映射冲突，需要管理员对账，本次业务请求未执行。\n"
            "> 可用 `qgstatus` 查看当前群绑定状态。",
            _group_status_buttons(),
        )
    if status == GROUP_REVOKED:
        return (
            "group_revoked",
            "### 🔒 本群绑定已撤销\n\n"
            "> 本群绑定已撤销，群业务指令不会执行。\n"
            "> 如需恢复，请由群主/管理员重新发送 `qgbind <真实群号>`。\n"
            "> 可用 `qgstatus` 查看当前群绑定状态。",
            _group_status_buttons(include_bind=True),
        )
    return (
        "group_unknown",
        "### 🔒 本群身份暂不可用\n\n"
        "> 本群绑定状态未知，群业务请求未执行。\n"
        "> 可用 `qgstatus` 查看当前群绑定状态。",
        _group_status_buttons(),
    )


async def _send_group_state_prompt(event: MessageEvent, bot: Bot, status: str) -> bool:
    kind, content, buttons = _group_block_prompt(status)
    if not _guard_prompt_is_new(event, kind):
        return False
    await bot.send(event, _reply(content, buttons))
    return True


def _personal_block_prompt(
    status: Optional[str], user_id: str
) -> tuple[str, str, List[List[Dict[str, Any]]]]:
    """Explain a locally blocking personal state.

    Like the group prompts these carry no QQ number, group number or virtual
    ID, and they never mention GSK: a locally unverified personal row blocks
    before any remote write is attempted, so retrying cannot help.
    """

    if status == PERSONAL_DECLARED:
        return (
            "personal_declared",
            "### 🔒 个人绑定仍是声明状态\n\n"
            "> 当前部署为本地声明模式，个人声明不会放行群业务指令。\n"
            "> 需要启用 QQ 邮箱认证，或由管理员完成可信审核后，才能放行。\n"
            "> 如需重新确认，可发送 `qbind <你的QQ号>`；持续不可用请联系管理员。",
            _personal_action_buttons(user_id, include_unbind=False),
        )
    if status == PERSONAL_UNBIND_PENDING:
        return (
            "personal_unbind_pending",
            "### ⏳ 个人绑定正在解绑\n\n"
            "> 解绑处理完成前业务指令不会执行；如需重新绑定请稍后再试。",
            _personal_action_buttons(user_id, include_unbind=False),
        )
    if status is None or status == PERSONAL_REVOKED:
        return (
            "personal_unavailable",
            "### 🔒 个人绑定不可用\n\n"
            "> 请先发送 `qbind <你的QQ号>` 完成个人绑定。",
            _personal_action_buttons(user_id, include_unbind=False),
        )
    return (
        "personal_unknown",
        "### 🔒 个人身份暂不可用\n\n"
        "> 个人绑定状态未知，业务请求未执行；请稍后重试或联系管理员。",
        _personal_action_buttons(user_id, include_unbind=False),
    )


async def _send_personal_state_prompt(
    event: MessageEvent, bot: Bot, status: Optional[str], user_id: str
) -> bool:
    kind, content, buttons = _personal_block_prompt(status, user_id)
    if not _guard_prompt_is_new(event, kind):
        return False
    await bot.send(event, _reply(content, buttons))
    return True


async def ensure_context_bound(event: MessageEvent, bot: Bot) -> bool:
    """Require verified identity and successful GSK sync before business work."""

    scope, user_id = _scope_user(event)
    if not scope or not user_id:
        await _send_bind_prompt_once(event, bot)
        return False

    group_id = getattr(event, "group_id", None)
    if group_id is None or str(group_id).strip() == "":
        try:
            verified_only = email_verification_required()
            bound = is_bound(user_id, scope, verified_only=verified_only)
            personal = get_store().get_personal(scope, user_id) if bound else None
            if personal is not None and personal.binding_status == PERSONAL_VERIFIED:
                await sync_verified_personal_mapping(scope, user_id)
        except Exception:
            logger.exception("QBind personal identity validation failed; blocking business request")
            await bot.send(
                event,
                _reply("### ⚠️ 个人身份暂不可用\n\n> QBind 状态校验或 GSK 映射同步失败，业务请求未执行。"),
            )
            return False
        if not bound:
            await _send_bind_prompt_once(event, bot)
            return False
        return True

    group_key = str(group_id).strip()
    if not is_bound(
        user_id, scope, verified_only=email_verification_required()
    ):
        await _send_bind_prompt_once(event, bot)
        return False
    try:
        group = get_store().get_group(scope, group_key)
    except Exception:
        logger.exception("QBind group binding lookup failed; blocking business request")
        await bot.send(
            event,
            _reply(
                "### ⚠️ 当前群身份暂不可用\n\n> QBind 本地状态校验失败，业务请求未执行。"
            ),
        )
        return False
    if group is None:
        await _send_group_bind_prompt_once(event, bot)
        return False
    if group.binding_status != GROUP_VERIFIED:
        # Local state alone already blocks: say which one, and never blame GSK.
        await _send_group_state_prompt(event, bot, group.binding_status)
        return False

    try:
        personal = get_store().get_personal(scope, user_id)
    except Exception:
        logger.exception("QBind personal binding lookup failed; blocking business request")
        await bot.send(
            event,
            _reply(
                "### ⚠️ 当前群身份暂不可用\n\n> QBind 本地状态校验失败，业务请求未执行。"
            ),
        )
        return False
    if personal is None or personal.binding_status != PERSONAL_VERIFIED:
        # Reported before the sync: get_verified_pair() needs a verified personal
        # row, so retrying GSK would never succeed either.
        await _send_personal_state_prompt(
            event,
            bot,
            None if personal is None else personal.binding_status,
            user_id,
        )
        return False

    try:
        pair = await sync_verified_pair(scope, user_id, group_key)
    except Exception:
        logger.exception("verified pair GSK sync is unavailable; blocking business request")
        await bot.send(
            event,
            _reply(
                "### 🔒 GSK 映射同步失败\n\n"
                "> 外部（GSK）身份映射未能明确同步成功，本次业务请求未执行；"
                "可稍后重试，持续失败请用 `qgstatus` 并提供日志给管理员。",
                _group_status_buttons(),
            ),
        )
        return False

    group_name = pair.group.group_name
    if not group_name:
        getter = getattr(bot, "get_group_info", None)
        if callable(getter):
            try:
                info = await getter(group_id=group_key)
            except Exception:
                logger.debug("unable to query group name for membership archive", exc_info=True)
            else:
                if isinstance(info, dict) and info.get("group_name"):
                    group_name = str(info["group_name"]).strip()
    try:
        await _publish_verified_membership(
            self_id=scope,
            group_id=group_key,
            user_id=user_id,
            group_name=group_name,
        )
    except Exception:
        logger.exception("verified membership could not be persisted; blocking business request")
        await bot.send(
            event,
            _reply("### ⚠️ 当前群身份暂不可用\n\n> QBind 本地状态校验失败，业务请求未执行。"),
        )
        return False
    return True

access_guard = on_message(priority=-1, block=False)


@access_guard.handle()
async def access_guard_handler(
    event: MessageEvent, bot: Bot, matcher: Matcher, state: T_State
) -> None:
    """Stop business messages until their required identity bindings exist."""

    # Only messages already recognized by NoneBot's command trie are in scope.
    # In particular, this must not turn ordinary group conversation into a
    # QBind/QGroupBind prompt.
    if _parsed_command_name(state) is None:
        return
    if _is_binding_or_information_command(state):
        return
    try:
        if await ensure_context_bound(event, bot):
            return
    except Exception:
        logger.exception("QBind access validation failed; stopping business propagation")
    matcher.stop_propagation()


def _validate_qq(value: str) -> Optional[str]:
    if not re.fullmatch(r"[1-9]\d{4,10}", value):
        return "QQ 号格式不正确！请输入 5-11 位非零开头数字。"
    return None


def _build_api_url(session_id: str, real_qq: str) -> str:
    """Compatibility URL builder for one numeric type=5 mapping."""

    base = (os.getenv("QBIND_GENSOKYO_API_URL") or "http://127.0.0.1:15630").rstrip("/")
    return f"{base}/getid?{urlencode({'oldRowValue': session_id, 'newRowValue': real_qq, 'type': '5'})}"


_mapping_client: Any = GensokyoMappingClient()


def set_mapping_client(client: Any) -> None:
    if not hasattr(client, "set_personal_mapping") or not hasattr(
        client, "clear_personal_mapping"
    ):
        raise TypeError("mapping client must provide personal set/clear methods")
    global _mapping_client
    _mapping_client = client
    # A client that also owns the group mapping methods becomes the single
    # seam for both type=5 writes; a personal-only client leaves the group
    # client untouched, exactly as before.
    if callable(getattr(client, "set_group_mapping", None)) and callable(
        getattr(client, "clear_group_mapping", None)
    ):
        from .group import set_group_mapping_client

        set_group_mapping_client(client)


def _personal_request(
    *,
    kind: str,
    scope: str,
    user_id: str,
    token: Optional[str],
) -> Optional[ConfirmationRequest]:
    store = get_store()
    if token:
        request = store.get_confirmation(token)
        if request is None:
            return None
        if (
            request.request_kind != kind
            or request.bot_scope != scope
            or request.actor_user_id != user_id
            or request.external_user_id != user_id
        ):
            raise PermissionError("confirmation request does not belong to this Bot/user")
        return request
    return store.find_confirmation(
        request_kind=kind,
        bot_scope=scope,
        actor_user_id=user_id,
        external_user_id=user_id,
    )


def _request_message(request: ConfirmationRequest) -> str:
    if request.result_code == "conflict":
        return "❌ QQ 号绑定冲突：本次个人绑定未完成。请先检查已有绑定或联系管理员处理。"
    if request.result_code == "email_pending":
        return (
            "📨 QQ 邮箱验证码已发送。\n"
            "请在有效期内发送 `qbindcode <6位验证码>` 完成个人绑定。"
        )
    if request.result_code == "declared":
        return (
            "✅ QQ 号已保存到当前 Bot 的 QBind 本地声明。\n"
            "⚠️ 尚未完成独立所有权核验，因此不会写入 GSK；只有 verified 状态才会同步映射。"
        )
    if request.result_code == "unbound":
        return "✅ QBind 本地个人绑定已撤销，历史审计记录已保留；GSK type=5 不提供可靠删除，本地撤销不代表远端映射已清除，需显式对账。"
    if request.result_code == "failed":
        return "❌ QBind 本地状态更新失败，绑定未报告为成功；请稍后重试或联系管理员检查数据库。"
    if request.result_code == "backend_applied_unrecorded":
        return "⚠️ 后端状态已变化，但本地持久化未完成；未报告为完全成功，请联系管理员恢复。"
    if request.status == REQUEST_EXPIRED:
        return "⏰ 确认请求已过期，请重新发起。"
    if request.status == REQUEST_CANCELED:
        return "✅ 确认请求已取消。"
    if request.status == REQUEST_BACKEND_UNKNOWN:
        return "⚠️ 后端结果未知，保留了可恢复状态；请稍后重试或由管理员核对。"
    return f"请求状态：{request.status}。"


# ---------------------------------------------------------------------------
# Personal qbind
# ---------------------------------------------------------------------------

_PERSONAL_MAPPING_CONFLICT = "personal mapping conflicts with active bindings"
_PERSONAL_MAPPING_WRITE_LOCK = asyncio.Lock()


class _PostMappingAliasError(QbindStorageError):
    """A confirmed remote mapping could not be recorded as a local alias."""


def _personal_mapping_conflicts(records: List[Any]) -> Dict[str, set[str]]:
    targets: Dict[str, set[str]] = {}
    for record in records:
        if record.canonical_user_id:
            targets.setdefault(record.external_user_id, set()).add(
                record.canonical_user_id
            )
    return {
        external_user_id: canonical_ids
        for external_user_id, canonical_ids in targets.items()
        if len(canonical_ids) > 1
    }


def _assert_personal_mapping_consistent(
    external_user_id: str, canonical_user_id: str
) -> None:
    records = get_store().list_locally_resolvable_personal_bindings()
    if any(
        record.external_user_id == external_user_id
        and (
            not record.canonical_user_id
            or record.canonical_user_id != canonical_user_id
        )
        for record in records
    ):
        raise BindingConflictError(_PERSONAL_MAPPING_CONFLICT)


async def _set_personal_mapping(
    external_user_id: str, canonical_user_id: str, *, lock_held: bool = False
) -> None:
    async def write_mapping() -> None:
        _assert_personal_mapping_consistent(external_user_id, canonical_user_id)
        await _mapping_client.set_personal_mapping(
            external_user_id, canonical_user_id
        )

    if lock_held:
        await write_mapping()
    else:
        async with _PERSONAL_MAPPING_WRITE_LOCK:
            await write_mapping()


def _assert_group_mapping_consistent(
    external_group_id: str, canonical_group_id: str
) -> None:
    records = get_store().list_locally_resolvable_group_bindings()
    if any(
        record.virtual_group_id == external_group_id
        and record.real_group_id != canonical_group_id
        for record in records
    ):
        raise BindingConflictError("group mapping conflicts with active Bot bindings")


async def sync_verified_personal_mapping(
    self_id: object, user_id: object
) -> PersonalBinding:
    """Synchronize one locally verified personal mapping or fail closed.

    The type=5 write is skipped when SQLite already records the identical
    write for the live row version: Gensokyo is not idempotent and answers a
    repeat write with ``已存在``.
    """

    scope, external_id = str(self_id).strip(), str(user_id).strip()
    async with _PERSONAL_MAPPING_WRITE_LOCK:
        store = get_store()
        record = store.get_personal(scope, external_id)
        if (
            record is None
            or record.binding_status != PERSONAL_VERIFIED
            or not record.canonical_user_id
        ):
            raise QbindStorageError("personal mapping is not locally verified")
        state = store.get_gsk_sync(scope, external_id, MAPPING_PERSONAL)
        if (
            state is not None
            and state.canonical_id == record.canonical_user_id
            and state.local_version == record.version
        ):
            return record
        _assert_personal_mapping_consistent(external_id, record.canonical_user_id)
        try:
            await _mapping_client.set_personal_mapping(
                external_id, record.canonical_user_id
            )
        except GensokyoMappingExistsError as error:
            # Gensokyo echoes the value we asked to write, so this proves only
            # that the target is mapped, not which virtual ID holds it.
            # Absorbing it is an explicit trade-off: the alternative is failing
            # every command after our own previous write.  A non-standard body
            # that reports a different value still fails closed.
            if (
                error.remote_value is not None
                and error.remote_value != record.canonical_user_id
            ):
                raise
            logger.warning(
                "GSK %s mapping already exists; treating the target state as "
                "reached (local_version=%s)",
                MAPPING_PERSONAL,
                record.version,
            )
        except GensokyoMappingError:
            raise
        except Exception as error:
            raise GensokyoMappingError(
                "personal GSK mapping result is unknown", retryable=True
            ) from error
        store.record_gsk_sync(
            bot_scope=scope,
            external_id=external_id,
            mapping_kind=MAPPING_PERSONAL,
            canonical_id=record.canonical_user_id,
            local_version=record.version,
        )
        current = store.get_personal(scope, external_id)
        if (
            current is None
            or current.binding_status != PERSONAL_VERIFIED
            or current.canonical_user_id != record.canonical_user_id
            or current.version != record.version
        ):
            raise QbindStorageError("personal binding changed during GSK synchronization")
        return current


async def sync_verified_pair(
    self_id: object, user_id: object, group_id: object
) -> VerifiedPair:
    """Sync both same-Bot verified mappings in order and fail closed on partial writes.

    Each step is skipped when SQLite already records the identical write for
    the live row version, because a repeated type=5 write of an existing value
    is rejected by Gensokyo rather than being a no-op.
    """

    scope = str(self_id).strip()
    external_user = str(user_id).strip()
    external_group = str(group_id).strip()
    async with _PERSONAL_MAPPING_WRITE_LOCK:
        store = get_store()
        pair = store.get_verified_pair(scope, external_user, external_group)
        if pair is None:
            raise QbindStorageError(
                "same-Bot personal and group bindings must both be verified"
            )
        real_user = pair.personal.canonical_user_id or ""
        real_group = pair.group.real_group_id or ""
        _assert_personal_mapping_consistent(external_user, real_user)
        _assert_group_mapping_consistent(external_group, real_group)
        from .group import get_group_mapping_client

        group_client = get_group_mapping_client()
        personal_state = store.get_gsk_sync(scope, external_user, MAPPING_PERSONAL)
        group_state = store.get_gsk_sync(scope, external_group, MAPPING_GROUP)
        # Only the group step can leave a remote write partially applied after
        # this function returns, so compensation keys off that step alone.  A
        # personal-step failure is a deterministic refusal or a local check and
        # never warrants pinning the group row to a permanent conflict.
        partial_risk = False
        try:
            if not (
                personal_state is not None
                and personal_state.canonical_id == real_user
                and personal_state.local_version == pair.personal.version
            ):
                try:
                    await _mapping_client.set_personal_mapping(
                        external_user, real_user
                    )
                except GensokyoMappingExistsError as error:
                    if (
                        error.remote_value is not None
                        and error.remote_value != real_user
                    ):
                        raise
                    logger.warning(
                        "GSK %s mapping already exists; treating the target "
                        "state as reached (local_version=%s)",
                        MAPPING_PERSONAL,
                        pair.personal.version,
                    )
                store.record_gsk_sync(
                    bot_scope=scope,
                    external_id=external_user,
                    mapping_kind=MAPPING_PERSONAL,
                    canonical_id=real_user,
                    local_version=pair.personal.version,
                )
                current = store.get_verified_pair(
                    scope, external_user, external_group
                )
                if (
                    current is None
                    or current.personal.version != pair.personal.version
                    or current.group.version != pair.group.version
                    or current.personal.canonical_user_id != real_user
                    or current.group.real_group_id != real_group
                ):
                    raise QbindStorageError(
                        "verified pair changed during GSK synchronization"
                    )
            if not (
                group_state is not None
                and group_state.canonical_id == real_group
                and group_state.local_version == pair.group.version
            ):
                # From this line on the remote group mapping may be partially
                # applied, so any later failure must be pinned as a conflict.
                partial_risk = True
                try:
                    await group_client.set_group_mapping(
                        external_group, real_group
                    )
                except GensokyoMappingExistsError as error:
                    if (
                        error.remote_value is not None
                        and error.remote_value != real_group
                    ):
                        raise
                    logger.warning(
                        "GSK %s mapping already exists; treating the target "
                        "state as reached (local_version=%s)",
                        MAPPING_GROUP,
                        pair.group.version,
                    )
                store.record_gsk_sync(
                    bot_scope=scope,
                    external_id=external_group,
                    mapping_kind=MAPPING_GROUP,
                    canonical_id=real_group,
                    local_version=pair.group.version,
                )
            current = store.get_verified_pair(scope, external_user, external_group)
            if (
                current is None
                or current.personal.version != pair.personal.version
                or current.group.version != pair.group.version
            ):
                raise QbindStorageError("verified pair changed during GSK synchronization")
            return current
        except Exception as error:
            if partial_risk:
                # No safe compensation exists without type=5 readback or a reliable delete contract.
                try:
                    get_store().mark_group_conflict(
                        bot_scope=scope,
                        virtual_group_id=external_group,
                        error=(
                            "GSK pair synchronization failed or is unknown; "
                            "explicit reconciliation is required"
                        ),
                        expected_version=pair.group.version,
                    )
                except Exception:
                    logger.exception("partial GSK pair failure could not be persisted locally")
            if isinstance(
                error, (BindingConflictError, GensokyoMappingError, QbindStorageError)
            ):
                raise
            raise GensokyoMappingError(
                "GSK verified-pair mapping result is unknown; explicit reconciliation is required",
                retryable=True,
            ) from error


bind = on_command("qbind", priority=5, block=False)


@bind.handle()
async def bind_handler(
    event: MessageEvent, bot: Bot, arg: Message = CommandArg()
):
    scope, user_id = _scope_user(event)
    if not scope or not user_id:
        await bind.finish(_reply("### ❌ 无法发起绑定\n\n> 无法确定当前 Bot 身份，未创建绑定请求。"))
    wait = _check_rate_limit(f"{scope}:{user_id}")
    if wait:
        await bind.finish(_reply(f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。"))
    candidate = arg.extract_plain_text().strip()
    if not candidate:
        await bind.finish(
            _reply(
                "### ❌ 缺少 QQ 号\n\n> 格式：`qbind <你的QQ号>`\n"
                "> 手动输入和确认只会产生声明，不代表独立验证所有权。"
            )
        )
    error = _validate_qq(candidate)
    if error:
        await bind.finish(
            _reply(
                f"### ❌ 绑定失败\n\n> {error}",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    try:
        current = get_store().get_personal(scope, user_id)
        if current and current.binding_status in {
            PERSONAL_DECLARED,
            PERSONAL_VERIFIED,
            PERSONAL_UNBIND_PENDING,
        }:
            if (
                current.canonical_user_id == candidate
                and current.binding_status == PERSONAL_VERIFIED
            ):
                try:
                    await sync_verified_personal_mapping(scope, user_id)
                except GensokyoMappingMissingError:
                    logger.exception(
                        "verified personal mapping sync found no Gensokyo virtual identity record"
                    )
                    await bind.finish(
                        _reply(
                            "### ⚠️ 远端缺少会话身份记录\n\n"
                            "> Gensokyo 中找不到当前会话对应的虚拟身份记录，可能已随会话或服务重启回收，本次无法建立映射。\n"
                            "> 请在群内或私聊中正常发送一条消息后再试；持续失败请联系管理员。",
                            _personal_action_buttons(user_id),
                        )
                    )
                except BindingConflictError:
                    logger.exception(
                        "verified personal mapping sync hit a cross-bot mapping conflict"
                    )
                    await bind.finish(
                        _reply(
                            "### ❌ 个人映射冲突\n\n"
                            "> 检测到与当前请求不一致的个人映射，未更新远端或本地身份，请联系管理员对账。",
                            _personal_action_buttons(user_id),
                        )
                    )
                except (GensokyoMappingError, QbindStorageError):
                    logger.exception("verified personal mapping sync failed")
                    await bind.finish(
                        _reply(
                            "### ❌ GSK 用户映射同步失败\n\n"
                            "> 当前个人身份未报告同步成功，请稍后重试或联系管理员对账。",
                            _personal_action_buttons(user_id),
                        )
                    )
                await bind.finish(
                    _reply(
                        "### ✅ GSK 用户映射已同步\n\n"
                        "> 当前个人绑定状态未更改。",
                        _personal_action_buttons(user_id),
                    )
                )
            if current.binding_status == PERSONAL_VERIFIED:
                await bind.finish(
                    _reply(
                        "### ℹ️ 当前已有个人绑定\n\n"
                        "> 当前 Bot 下已经存在已验证个人身份。\n"
                        "> 如需更换或撤销，请使用渲染的按钮或管理员对账流程。",
                        _personal_action_buttons(user_id),
                    )
                )
            if (
                email_verification_required()
                and current.binding_status == PERSONAL_DECLARED
                and current.canonical_user_id == candidate
            ):
                try:
                    _assert_personal_mapping_consistent(user_id, candidate)
                except BindingConflictError:
                    logger.exception(
                        "declared personal mapping is inconsistent with another bot"
                    )
                    await bind.finish(
                        _reply(
                            "### ❌ 个人映射冲突\n\n"
                            "> 检测到其他 Bot 的有效个人绑定与当前请求不一致，未更新映射或本地身份。",
                            _personal_action_buttons(user_id),
                        )
                    )
                try:
                    _, masked = await start_email_challenge(
                        purpose="personal_bind",
                        bot_scope=scope,
                        actor_user_id=user_id,
                        external_user_id=user_id,
                        candidate_id=candidate,
                        qq_number=candidate,
                    )
                except (MailConfigurationError, MailDeliveryError) as error:
                    logger.exception("personal QQ email verification code delivery failed")
                    await bind.finish(
                        _reply(
                            f"### ❌ 邮箱验证码发送失败\n\n> {error}",
                            _personal_action_buttons(user_id),
                        )
                    )
                await bind.finish(
                    _reply(
                        "### 📩 重新验证 QQ 邮箱\n\n"
                        f"> 验证码已发送至：`{_masked_for_display(masked)}`\n"
                        "> 请发送 `qbindcode <6位验证码>` 完成个人绑定。\n"
                        "> 验证码有效期为 10 分钟。",
                        _personal_email_code_buttons(user_id),
                    )
                )
            if (
                current.binding_status == PERSONAL_DECLARED
                and current.canonical_user_id == candidate
            ):
                await bind.finish(
                    _reply(
                        "### ℹ️ 当前个人身份仍为声明状态\n\n"
                        "> 声明不会写入 GSK；需要完成邮箱或可信审核并进入 verified 后才可同步。",
                        _personal_action_buttons(user_id),
                    )
                )
            if current.binding_status == PERSONAL_UNBIND_PENDING:
                await bind.finish(
                    _reply(
                        "### ⏳ 个人绑定正在解绑\n\n"
                        "> 解绑流程完成前不会创建新的绑定请求。\n"
                        "> 如需重新绑定，请稍后重试或联系管理员。",
                        _personal_action_buttons(user_id),
                    )
                )
            await bind.finish(
                _reply(
                    "### ⏳ 当前已有待审核声明\n\n"
                    "> 当前 Bot 下已有一条待验证的个人声明。\n"
                    "> 完成可信审核前不会自动合并档案；如需撤销请使用 `qunbind`。",
                    _personal_action_buttons(user_id),
                )
            )
        request = get_store().create_confirmation(
            request_kind="personal_bind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_user_id=user_id,
            candidate_id=candidate,
            expected_version=current.version if current else None,
            ttl_seconds=120,
        )
    except QbindStorageError as error:
        logger.exception("personal binding request could not be persisted")
        await bind.finish(
            _reply(
                f"### ❌ 绑定请求未保存\n\n> {error}",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    await bind.finish(
        _reply(
            "### ⚠️ 确认个人身份声明\n\n"
            f"> 待声明 QQ：`{candidate}`\n\n"
            "> 确认后会将此 QQ 号保存到当前 Bot 的 QBind 本地映射，供后台业务使用。"
            "这属于本人声明，不代表已完成所有权核验；后续可通过安全审核提升状态。\n"
            "> ⏰ 请求两分钟内有效。",
            [
                [
                    {
                        "render_data.label": "✅ 确认声明",
                        "action.data": f"confirm_bind {request.request_id}",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [user_id],
                    },
                    {
                        "render_data.label": "❌ 取消",
                        "action.data": f"cancel_bind {request.request_id}",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [user_id],
                    },
                ]
            ],
        )
    )


confirm = on_command("confirm_bind", priority=5, block=False)


@confirm.handle()
async def confirm_handler(
    event: MessageEvent, bot: Bot, arg: Message = CommandArg()
):
    scope, user_id = _scope_user(event)
    token = arg.extract_plain_text().strip() or None
    if token and not re.fullmatch(r"[0-9a-f]{32}", token):
        await confirm.finish(
            _reply(
                "### ❌ 确认失败\n\n> 确认令牌格式不正确，请使用原消息中的按钮。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    try:
        request = _personal_request(
            kind="personal_bind", scope=scope, user_id=user_id, token=token
        )
    except PermissionError as error:
        logger.exception("confirm_bind request ownership check failed")
        await confirm.finish(
            _reply(
                f"### ❌ 确认失败\n\n> {error}",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if request is None:
        await confirm.finish(
            _reply(
                "### ❌ 没有待确认请求\n\n> 没有属于当前 Bot/用户的待确认请求。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if request.status != REQUEST_PENDING:
        await confirm.finish(
            _reply(_request_message(request), _personal_action_buttons(user_id))
        )
    if request.expires_at <= time.time():
        expired = get_store().complete_confirmation(
            request.request_id, result_code="expired", status=REQUEST_EXPIRED
        )
        await confirm.finish(
            _reply(_request_message(expired or request), _personal_action_buttons(user_id, include_unbind=False))
        )

    current = get_store().get_personal(scope, user_id)
    if request.expected_version is not None:
        if current is None or current.version != request.expected_version:
            logger.warning(
                "QBind confirmation rejected; "
                "reason_code=confirm_bind.expected_version_mismatch"
            )
            failed = get_store().complete_confirmation(
                request.request_id,
                result_code="conflict",
                status=REQUEST_FAILED,
                error="personal binding changed while confirmation was pending",
            )
            await confirm.finish(
                _reply(_request_message(failed or request), _personal_action_buttons(user_id))
            )
    elif current is not None and current.binding_status in {
        PERSONAL_DECLARED,
        PERSONAL_VERIFIED,
        PERSONAL_UNBIND_PENDING,
    }:
        logger.warning(
            "QBind confirmation rejected; "
            "reason_code=confirm_bind.active_binding_exists"
        )
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="personal binding already exists",
        )
        await confirm.finish(
            _reply(_request_message(failed or request), _personal_action_buttons(user_id))
        )

    candidate = request.candidate_id or ""
    existing_owners = get_store().list_active_personal_by_canonical(scope, candidate)
    foreign_owners = [
        owner
        for owner in existing_owners
        if owner.external_user_id != user_id
    ]
    if foreign_owners:
        logger.warning(
            "QBind confirmation hit an existing canonical owner; "
            "reason_code=confirm_bind.canonical_owner_exists"
        )
        if any(
            owner.binding_status != PERSONAL_VERIFIED for owner in foreign_owners
        ):
            # declare_personal(allow_verified_canonical_reuse=True) only reuses
            # a canonical whose other owners are all verified, so a declared or
            # unbind_pending owner cannot be taken over even with mailbox proof.
            get_store().complete_confirmation(
                request.request_id,
                result_code="conflict",
                status=REQUEST_FAILED,
                error="canonical identity is already owned by another user",
            )
            await confirm.finish(
                _reply(
                    "### ❌ QQ 号绑定冲突：该 QQ 号正被其他会话身份占用\n\n"
                    "> 占用方尚未完成验证，当前不能接管；"
                    "请等待对方完成验证或联系管理员对账。",
                    _personal_action_buttons(user_id),
                )
            )
        if not email_verification_required():
            failed = get_store().complete_confirmation(
                request.request_id,
                result_code="conflict",
                status=REQUEST_FAILED,
                error="canonical identity is already owned by another user",
            )
            await confirm.finish(
                _reply(
                    "### ❌ QQ 号绑定冲突：该 QQ 号已被其他会话身份绑定\n\n"
                    "> 当前部署为本地声明模式，需要启用 QQ 邮箱认证后，"
                    "才能通过邮箱验证证明归属并转移绑定。\n"
                    "> 如急需处理，请联系管理员对账。",
                    _personal_action_buttons(user_id),
                )
            )
        # Another virtual identity already owns this canonical QQ.  The mailbox
        # is the same ownership proof required for a first bind, so passing it
        # transfers the canonical to this identity; the previous row stays
        # verified as history and the takeover creates no event alias.
        try:
            _, masked = await start_email_challenge(
                purpose="personal_bind",
                bot_scope=scope,
                actor_user_id=user_id,
                external_user_id=user_id,
                candidate_id=candidate,
                qq_number=candidate,
            )
        except (MailConfigurationError, MailDeliveryError) as error:
            logger.exception(
                "personal QQ email verification code delivery failed during takeover"
            )
            failed = get_store().complete_confirmation(
                request.request_id,
                result_code="failed",
                status=REQUEST_FAILED,
                error=str(error),
            )
            await confirm.finish(
                _reply(
                    f"### ❌ 邮箱验证码发送失败\n\n> {error}",
                    _personal_action_buttons(user_id, include_unbind=False),
                )
            )
        get_store().complete_confirmation(
            request.request_id, result_code="email_pending"
        )
        await confirm.finish(
            _reply(
                "### 📩 该 QQ 号已有绑定记录，需验证归属\n\n"
                f"> 验证码已发送至：`{_masked_for_display(masked)}`\n"
                "> 该 QQ 号当前绑定在另一个会话身份上；"
                "完成邮箱验证后会自动转移到当前身份。\n"
                "> 请发送 `qbindcode <6位验证码>` 完成验证，有效期 10 分钟。",
                _personal_email_code_buttons(user_id),
            )
        )
    try:
        _assert_personal_mapping_consistent(user_id, candidate)
    except BindingConflictError:
        logger.warning(
            "QBind confirmation rejected; "
            "reason_code=confirm_bind.cross_bot_mapping_inconsistent"
        )
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error=_PERSONAL_MAPPING_CONFLICT,
        )
        await confirm.finish(
            _reply(
                "### ❌ QQ 号绑定冲突（个人映射冲突）：本次个人绑定未完成\n\n"
                "> 检测到其他 Bot 的有效个人绑定与当前请求不一致，未更新映射或本地身份。",
                _personal_action_buttons(user_id),
            )
        )
    if email_verification_required():
        try:
            _, masked = await start_email_challenge(
                purpose="personal_bind",
                bot_scope=scope,
                actor_user_id=user_id,
                external_user_id=user_id,
                candidate_id=candidate,
                qq_number=candidate,
            )
        except (MailConfigurationError, MailDeliveryError) as error:
            logger.exception(
                "personal QQ email verification code delivery failed during confirmation"
            )
            failed = get_store().complete_confirmation(
                request.request_id,
                result_code="failed",
                status=REQUEST_FAILED,
                error=str(error),
            )
            await confirm.finish(
                _reply(
                    f"### ❌ 邮箱验证码发送失败\n\n> {error}",
                    _personal_action_buttons(user_id, include_unbind=False),
                )
            )
        completed = get_store().complete_confirmation(
            request.request_id, result_code="email_pending"
        )
        await confirm.finish(
            _reply(
                "### 📩 QQ 邮箱验证已发起\n\n"
                f"> 验证码已发送至：`{_masked_for_display(masked)}`\n"
                "> 请发送 `qbindcode <6位验证码>` 完成个人绑定。\n"
                "> 验证码有效期为 10 分钟，最多可尝试 5 次。",
                _personal_email_code_buttons(user_id),
            )
        )
    try:
        get_store().declare_personal(
            bot_scope=scope,
            external_user_id=user_id,
            canonical_user_id=candidate,
            bound_by_user_id=user_id,
            verification_source="qbind:declared",
            operation_id=request.request_id,
        )
        completed = get_store().complete_confirmation(
            request.request_id, result_code="declared"
        )
    except (BindingConflictError, QbindStorageError) as error:
        if isinstance(error, BindingConflictError):
            logger.warning(
                "QBind confirmation rejected; "
                "reason_code=confirm_bind.local_binding_conflict"
            )
        logger.exception("QBind local mapping save failed")
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict" if isinstance(error, BindingConflictError) else "failed",
            status=REQUEST_FAILED,
            error=str(error),
        )
        await confirm.finish(
            _reply(_request_message(failed or request), _personal_action_buttons(user_id))
        )
    await confirm.finish(
        _reply(_request_message(completed or request), _personal_action_buttons(user_id))
    )


personal_mapping_resync = on_command(
    "批量重同步用户映射", permission=SUPERUSER, priority=1, block=True
)


@personal_mapping_resync.handle()
async def handle_personal_mapping_resync(
    bot: Bot, matcher: Matcher, args: Message = CommandArg()
) -> None:
    """Preview by default; append 执行 to perform one explicit serial batch."""
    execute = str(args).strip() == "执行"
    try:
        records = get_store().list_active_personal_bindings()
        guard_records = get_store().list_locally_resolvable_personal_bindings()
    except Exception:
        logger.error("Batch personal mapping resync could not read local records")
        await matcher.finish("批量重同步未执行：无法读取本地绑定记录。")

    # type=5 has no Bot scope: conflicting targets for a virtual ID are unsafe.
    conflicts = _personal_mapping_conflicts(guard_records)
    if conflicts:
        await matcher.finish(
            f"批量重同步已中止：发现 {len(conflicts)} 个跨 Bot 映射冲突；未调用远端接口。"
        )

    eligible_records = [
        record
        for record in records
        if record.canonical_user_id and record.binding_status == PERSONAL_VERIFIED
    ]
    unique_pairs = list(dict.fromkeys(
        (record.bot_scope, record.external_user_id)
        for record in eligible_records
    ))
    skipped = len(eligible_records) - len(unique_pairs)
    if not execute:
        await matcher.finish(
            f"预览：待同步 {len(unique_pairs)} 条个人映射，重复跳过 {skipped} 条。"
            "确认后发送：批量重同步用户映射 执行。群组映射不受影响。"
        )

    # Re-read immediately before the first write so preview-time state is never reused.
    try:
        records = get_store().list_active_personal_bindings()
        guard_records = get_store().list_locally_resolvable_personal_bindings()
    except Exception:
        logger.error("Batch personal mapping resync revalidation failed")
        await matcher.finish("批量重同步未执行：重新校验本地记录失败。")
    conflicts = _personal_mapping_conflicts(guard_records)
    if conflicts:
        await matcher.finish(
            f"批量重同步已中止：重新校验发现 {len(conflicts)} 个映射冲突；未调用远端接口。"
        )
    eligible_records = [
        record
        for record in records
        if record.canonical_user_id and record.binding_status == PERSONAL_VERIFIED
    ]
    unique_pairs = list(dict.fromkeys(
        (record.bot_scope, record.external_user_id)
        for record in eligible_records
    ))
    skipped = len(eligible_records) - len(unique_pairs)
    succeeded = failed = 0
    for scope, virtual_user_id in unique_pairs:
        try:
            await sync_verified_personal_mapping(scope, virtual_user_id)
            succeeded += 1
        except Exception:
            failed += 1
            logger.error("Batch personal mapping resync record failed")
    await matcher.finish(
        f"批量重同步完成：成功 {succeeded} 条，失败 {failed} 条，重复跳过 {skipped} 条。"
        + ("失败项未自动重试，请管理员评估后再决定。" if failed else "")
    )


async def _finalize_personal_email_binding(
    scope: str,
    user_id: str,
    candidate: str,
    challenge_id: str,
    *,
    verified_retry: bool,
):
    async with _PERSONAL_MAPPING_WRITE_LOCK:
        current = get_store().get_personal(scope, user_id)
        if verified_retry:
            if (
                current is None
                or current.binding_status != PERSONAL_VERIFIED
                or current.canonical_user_id != candidate
            ):
                raise QbindStorageError(
                    "verified email retry source is no longer a matching verified binding"
                )
        else:
            if current is None or current.binding_status == PERSONAL_REVOKED:
                current = get_store().declare_personal(
                    bot_scope=scope,
                    external_user_id=user_id,
                    canonical_user_id=candidate,
                    bound_by_user_id=user_id,
                    verification_source="qbind:qq_email",
                    operation_id=challenge_id,
                    allow_verified_canonical_reuse=True,
                )
            elif current.canonical_user_id != candidate:
                raise BindingConflictError(
                    "current personal QQ differs from email challenge"
                )
            if current.binding_status not in {PERSONAL_DECLARED, PERSONAL_VERIFIED}:
                raise QbindStorageError(
                    f"personal binding cannot be email-verified from {current.binding_status}"
                )

        if current is None or current.canonical_user_id != candidate:
            raise QbindStorageError(
                "personal binding differs from the email challenge candidate"
            )

        # The exact QQ-mail challenge is the ownership proof; type=5 success is not.
        await _set_personal_mapping(user_id, candidate, lock_held=True)
        if current.binding_status == PERSONAL_DECLARED:
            verified = get_store().verify_personal(
                bot_scope=scope,
                external_user_id=user_id,
                verified_by=user_id,
                verification_source="qbind:qq_email",
                operation_id=challenge_id,
            )
        else:
            verified = current

        other_owners = [
            owner
            for owner in get_store().list_active_personal_by_canonical(scope, candidate)
            if owner.external_user_id != user_id
        ]
        if other_owners:
            # Email proof is the same evidence as a first bind, so a canonical
            # already held by another identity moves here.  The previous row
            # stays verified for history and no event alias is created.
            logger.info(
                "personal canonical taken over by email proof (source=%s)",
                verified.verification_source,
            )
            return verified
        source_expected_version = verified.version
        get_store().assert_verified_personal_alias_available(
            bot_scope=scope,
            external_user_id=candidate,
            canonical_user_id=candidate,
            source_external_user_id=user_id,
            email_challenge_id=challenge_id,
            source_expected_version=source_expected_version,
            require_unique_challenge=verified_retry,
            reject_revoked_target=True,
        )
        try:
            return get_store().link_verified_personal(
                bot_scope=scope,
                external_user_id=candidate,
                canonical_user_id=candidate,
                bound_by_user_id=user_id,
                verified_by=user_id,
                verification_source="qbind:qq_email_alias",
                operation_id=challenge_id,
                source_external_user_id=user_id,
                email_challenge_id=challenge_id,
                source_expected_version=source_expected_version,
                require_unique_challenge=verified_retry,
                reject_revoked_target=True,
            )
        except Exception as error:
            raise _PostMappingAliasError(
                "Gensokyo mapping succeeded but local alias persistence failed"
            ) from error


qbindcode = on_command("qbindcode", priority=5, block=False)


@qbindcode.handle()
async def qbindcode_handler(
    event: MessageEvent, arg: Message = CommandArg()
):
    scope, user_id = _scope_user(event)
    if not email_verification_required():
        await qbindcode.finish(
            _reply(
                "### ℹ️ QQ 邮箱认证未启用\n\n"
                "> 当前部署仍使用本地声明模式；管理员可配置 `QBIND_EMAIL_REQUIRED=true` 后启用邮箱认证。"
            )
        )
    code = arg.extract_plain_text().strip()
    if not re.fullmatch(r"\d{6}", code):
        await qbindcode.finish(
            _reply(
                "### ❌ 验证码格式不正确\n\n> 请发送邮件中的 6 位数字验证码，例如：`qbindcode 123456`。",
                _personal_email_code_buttons(user_id),
            )
        )
    current = get_store().get_personal(scope, user_id)
    challenge = get_store().find_email_challenge(
        purpose="personal_bind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_user_id=user_id,
    )
    if (
        challenge is None
        and current is not None
        and current.binding_status == PERSONAL_VERIFIED
        and current.canonical_user_id
    ):
        retry_challenge = get_store().find_verified_email_challenge(
            purpose="personal_bind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_user_id=user_id,
            candidate_id=current.canonical_user_id,
        )
        if (
            retry_challenge is not None
            and retry_challenge.candidate_id == current.canonical_user_id
        ):
            challenge = retry_challenge
    if challenge is None:
        await qbindcode.finish(
            _reply(
                "### ❌ 没有待验证的 QQ 邮箱请求\n\n"
                "> 请先发送 `qbind <你的QQ号>` 发起个人绑定。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    candidate = challenge.candidate_id
    if not candidate or _validate_qq(candidate) is not None:
        await qbindcode.finish(
            _reply(
                "### ❌ QQ 邮箱认证请求无效\n\n"
                "> 认证请求缺少可验证的 QQ 候选，未更新绑定或映射。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    verified_retry = (
        challenge.status == "verified"
        and current is not None
        and current.binding_status == PERSONAL_VERIFIED
        and current.canonical_user_id == candidate
    )
    if challenge.status == "verified" and not verified_retry:
        await qbindcode.finish(
            _reply(
                "### ❌ 已验证请求不能用于当前身份\n\n"
                "> 请先发送 `qbind <你的QQ号>` 获取新的邮箱验证码；历史请求不会重建事件别名。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if not verified_retry:
        wait = _check_rate_limit(f"email:{scope}:{user_id}")
        if wait:
            await qbindcode.finish(
                _reply(
                    f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。",
                    _personal_email_code_buttons(user_id),
                )
            )
    if verified_retry:
        attempt = None
    else:
        attempt = get_store().verify_email_challenge(
            challenge.challenge_id,
            code_digest(challenge.challenge_id, code, _email_service.config.code_secret),
        )
    if attempt is not None and not attempt.accepted:
        message = {
            "invalid_code": "验证码不正确，请检查邮件后重试。",
            "max_attempts": "验证码尝试次数已用完，请重新发起绑定。",
            "expired": "验证码已过期，请重新发起绑定。",
            "canceled": "验证码请求已失效，请重新发起绑定。",
        }.get(attempt.reason, "当前验证码请求不可用，请重新发起绑定。")
        await qbindcode.finish(
            _reply(
                f"### ❌ QQ 邮箱验证失败\n\n> {message}",
                _personal_email_code_buttons(user_id),
            )
        )


    try:
        verified = await _finalize_personal_email_binding(
            scope, user_id, candidate, challenge.challenge_id,
            verified_retry=verified_retry,
        )
    except _PostMappingAliasError:
        logger.exception("Personal email mapping succeeded but alias persistence failed")
        await qbindcode.finish(
            _reply(
                "### ⚠️ GSK 映射已确认，但本地事件别名未保存\n\n"
                "> 为保持安全，当前事件身份尚未放行；请联系管理员恢复，系统不会自动回退或清除映射。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    except BindingConflictError:
        logger.exception("QQ email verification hit a personal mapping conflict")
        await qbindcode.finish(
            _reply(
                "### ❌ 个人映射冲突\n\n"
                "> 检测到其他 Bot 的有效个人绑定与当前请求不一致，未保存或同步当前绑定。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    except QbindStorageError as error:
        logger.exception("QQ email verification could not finalize personal binding")
        await qbindcode.finish(
            _reply(
                "### ❌ 邮箱验证未完成\n\n> 本地绑定尚未完成验证；请稍后重试或联系管理员。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    except GensokyoMappingError:
        logger.error("Personal mapping sync after email verification failed")
        await qbindcode.finish(
            _reply(
                "### ⚠️ GSK 同步失败\n\n"
                "> 本次绑定未报告成功；尚未完成本地 verified 状态转换，远端结果可能未知，请稍后重试或联系管理员对账。",
                _personal_action_buttons(user_id),
            )
        )
    except Exception:
        logger.exception("Personal email binding finalization failed unexpectedly")
        await qbindcode.finish(
            _reply(
                "### ⚠️ 绑定状态未知\n\n"
                "> 本次操作未报告成功；请联系管理员核对 QBind 与 GSK 状态后再重试。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    await qbindcode.finish(
        _reply(
            "### ✅ 个人 QQ 绑定已完成邮箱认证并同步至 GSK\n\n"
            f"> 已验证 QQ：`{verified.canonical_user_id}`\n"
            "> 后续群绑定必须由这个 QQ 对应的群主/管理员邮箱完成。",
            _personal_action_buttons(user_id),
        )
    )


cancel_cmd = on_command("cancel_bind", priority=5, block=False)


@cancel_cmd.handle()
async def cancel_handler(event: MessageEvent, arg: Message = CommandArg()):
    scope, user_id = _scope_user(event)
    token = arg.extract_plain_text().strip() or None
    try:
        request = _personal_request(
            kind="personal_bind", scope=scope, user_id=user_id, token=token
        )
    except PermissionError as error:
        logger.exception("cancel_bind request ownership check failed")
        await cancel_cmd.finish(
            _reply(
                f"### ❌ 取消失败\n\n> {error}",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if request is None:
        await cancel_cmd.finish(
            _reply(
                "### ❌ 没有可取消的绑定请求\n\n> 当前 Bot/用户没有可取消的绑定请求。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    canceled = get_store().cancel_confirmation(request.request_id)
    await cancel_cmd.finish(
        _reply(
            _request_message(canceled or request),
            _personal_action_buttons(user_id, include_unbind=False),
        )
    )


# ---------------------------------------------------------------------------
# Personal qunbind with ownership and recoverable backend state
# ---------------------------------------------------------------------------

unbind = on_command("qunbind", priority=5, block=False)


def _unbind_parts(value: str) -> tuple[Optional[str], Optional[str], bool]:
    parts = value.split()
    confirmed = bool(parts and parts[-1] == "确认")
    if confirmed:
        parts.pop()
    token = next((part for part in parts if re.fullmatch(r"[0-9a-f]{32}", part)), None)
    parts = [part for part in parts if part != token]
    requested = parts[0] if parts else None
    return requested, token, confirmed


async def _execute_personal_unbind(
    event: MessageEvent, request: ConfirmationRequest
) -> str:
    scope, user_id = _scope_user(event)
    if request.status != REQUEST_PENDING:
        return _request_message(request)
    current = get_store().get_personal(scope, user_id)
    if current is None or not current.canonical_user_id:
        completed = get_store().complete_confirmation(
            request.request_id, result_code="unbound"
        )
        return _request_message(completed or request)
    if (
        request.expected_version is not None
        and current.version != request.expected_version
        and current.binding_status != PERSONAL_UNBIND_PENDING
    ):
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="personal binding changed while unbind was pending",
        )
        return _request_message(failed or request)
    if request.candidate_id != current.canonical_user_id:
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="unbind target does not belong to the current user",
        )
        return _request_message(failed or request)
    try:
        get_store().mark_personal_unbind_pending(
            bot_scope=scope,
            external_user_id=user_id,
            operation_id=request.request_id,
        )
    except Exception as error:
        logger.exception("personal unbind could not enter local pending state")
        failed = get_store().update_confirmation_error(
            request.request_id, str(error), backend_unknown=False
        )
        return _request_message(failed or request)

    try:
        get_store().complete_personal_unbind(
            bot_scope=scope,
            external_user_id=user_id,
            operation_id=request.request_id,
        )
        await _notify_membership_observer(
            {
                "operation": "revoke_user",
                "bot_scope": scope,
                "virtual_user_id": user_id,
                "real_user_id": current.canonical_user_id,
            }
        )
        completed = get_store().complete_confirmation(
            request.request_id, result_code="unbound"
        )
        return _request_message(completed or request)
    except QbindStorageError as error:
        logger.exception("QBind local unbind completed but request finalization failed")
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="failed",
            status=REQUEST_FAILED,
            error=str(error),
        )
        return _request_message(failed or request)


@unbind.handle()
async def unbind_handler(
    event: MessageEvent, bot: Bot, arg: Message = CommandArg()
):
    scope, user_id = _scope_user(event)
    wait = _check_rate_limit(f"{scope}:{user_id}")
    if wait:
        await unbind.finish(_reply(f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。"))
    requested, token, confirmed = _unbind_parts(arg.extract_plain_text().strip())
    current = get_store().get_personal(scope, user_id)
    if current is None or not current.canonical_user_id:
        await unbind.finish(
            _reply(
                "### ❌ 没有个人绑定\n\n> 当前 Bot 下没有可解绑的个人绑定记录。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if current.binding_status == PERSONAL_REVOKED:
        await unbind.finish(
            _reply(
                "### ℹ️ 个人绑定已撤销\n\n> 当前个人绑定已经是已撤销状态。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if requested and requested != current.canonical_user_id:
        await unbind.finish(
            _reply(
                "### ❌ 解绑目标不匹配\n\n"
                "> 解绑目标与当前用户自己的绑定记录不一致。\n"
                "> 不能通过输入他人的 QQ 号修改他人身份。",
                _personal_action_buttons(user_id),
            )
        )

    if not confirmed and not token:
        request = get_store().create_confirmation(
            request_kind="personal_unbind",
            bot_scope=scope,
            actor_user_id=user_id,
            external_user_id=user_id,
            candidate_id=current.canonical_user_id,
            expected_version=current.version,
            ttl_seconds=120,
        )
        await unbind.finish(
            _reply(
                "### ⚠️ 确认解绑\n\n"
                f"> 当前个人 QQ：`{current.canonical_user_id}`\n\n"
                "> 解绑只撤销当前用户自己的身份映射，不会删除跨群档案资产。\n"
                "> ⏰ 请求两分钟内有效。",
                [
                    [
                        {
                            "render_data.label": "⚠️ 确认解绑",
                            "action.data": f"qunbind {request.request_id} 确认",
                            "action.enter": True,
                            "action.permission.type": 0,
                            "action.permission.specify_user_ids": [user_id],
                        }
                    ]
                ],
            )
        )

    try:
        request = _personal_request(
            kind="personal_unbind", scope=scope, user_id=user_id, token=token
        )
    except PermissionError as error:
        logger.exception("unbind request ownership check failed")
        await unbind.finish(
            _reply(f"### ❌ 解绑失败\n\n> {error}", _personal_action_buttons(user_id))
        )
    if request is None:
        await unbind.finish(
            _reply(
                "### ❌ 没有待确认解绑请求\n\n> 当前没有属于你的待确认解绑请求，请先发送 `qunbind`。",
                _personal_action_buttons(user_id),
            )
        )
    if request.status != REQUEST_PENDING:
        await unbind.finish(_reply(_request_message(request)))
    if request.expires_at <= time.time():
        expired = get_store().complete_confirmation(
            request.request_id, result_code="expired", status=REQUEST_EXPIRED
        )
        await unbind.finish(_reply(_request_message(expired or request)))
    result = await _execute_personal_unbind(event, request)
    await unbind.finish(
        _reply(result, _personal_action_buttons(user_id, include_unbind=False))
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

driver = get_driver()


@driver.on_startup
async def _start_cleanup() -> None:
    asyncio.create_task(_cleanup_expired_pending())


_core = require("amia_core")
registry = _core.registry
from .identity import QbindIdentityResolver


@driver.on_startup
async def _register_identity_resolver() -> None:
    registry.register_identity_resolver(QbindIdentityResolver(), replace=True)


# Importing the module registers QGroupBind commands and public resolvers.
from .group import (  # noqa: E402
    GensokyoGroupVerifier,
    GroupIdentityResolution,
    GroupIdentityResolver,
    GroupVerificationAdapter,
    GroupVerificationContext,
    GroupVerificationResult,
    approve_group_binding,
    approve_group_binding_request,
    ensure_group_bound,
    get_group_binding_status,
    get_group_name,
    get_group_mapping_client,
    get_real_group,
    is_group_bound,
    resolve_group_identity,
    set_group_verifier,
    set_group_mapping_client,
)


async def resolve_context_identity(
    *,
    self_id: object,
    user_id: object,
    group_id: Optional[object] = None,
) -> dict[str, Optional[str]]:
    """Resolve user and optional group IDs in one call.

    The personal canonical field is ``None`` unless independently verified.
    In group context, both same-Bot personal and group bindings must be
    verified before either canonical ID is returned; a declaration alone is
    not sufficient.  Unknown IDs are never inferred from names, number ranges,
    group names, or values in another Bot namespace.
    """

    resolver = QbindIdentityResolver()
    return await resolver.resolve_context_identity(
        self_id=self_id, user_id=user_id, group_id=group_id
    )


def get_group_verifier():
    from .group import get_group_verifier as _get_group_verifier

    return _get_group_verifier()


__all__ = [
    "GroupIdentityResolution",
    "GroupIdentityResolver",
    "GroupVerificationAdapter",
    "GroupVerificationContext",
    "GroupVerificationResult",
    "GensokyoGroupVerifier",
    "QbindIdentityResolver",
    "approve_group_binding",
    "approve_group_binding_request",
    "approve_personal_binding",
    "approve_personal_alias",
    "configure_storage",
    "ensure_bound",
    "ensure_context_bound",
    "ensure_group_bound",
    "get_binding_status",
    "get_group_binding_status",
    "get_group_name",
    "get_group_mapping_client",
    "get_group_verifier",
    "get_group_members",
    "get_real_group",
    "get_real_qq",
    "get_store",
    "get_user_groups",
    "get_verified_pair",
    "get_verified_qq",
    "sync_verified_pair",
    "sync_verified_personal_mapping",
    "is_bound",
    "is_group_bound",
    "record_verified_membership",
    "resolve_context_identity",
    "resolve_group_identity",
    "set_group_verifier",
    "set_group_mapping_client",
    "set_membership_observer",
    "set_mapping_client",
    "storage_health",
]
