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

from .gensokyo import GensokyoMappingClient
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
    QbindStorageError,
    QbindStore,
)

logger = logging.getLogger(__name__)

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
_SHARE_NOTICE_TTL_SECONDS = 3600
_SHARE_NOTICE_CACHE_KEY = "_amia_qbind_share_notice_cache"


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


def _share_notice_is_new(
    event: MessageEvent,
    *,
    user_id: str,
    group_id: str,
    real_group_id: str,
) -> bool:
    """Throttle the successful sharing notice without suppressing new mappings."""

    driver = get_driver()
    now = time.monotonic()
    cache = getattr(driver, _SHARE_NOTICE_CACHE_KEY, None)
    if cache is None:
        cache = {}
        setattr(driver, _SHARE_NOTICE_CACHE_KEY, cache)
    for key, expires_at in tuple(cache.items()):
        if expires_at <= now:
            cache.pop(key, None)
    # The store identity is included so isolated tests/reloads do not inherit
    # a notice from an older database with the same Bot/user/group IDs.
    key = ":".join(
        (
            str(id(get_store())),
            str(getattr(event, "self_id", "")),
            str(user_id),
            str(group_id),
            str(real_group_id),
        )
    )
    if key in cache:
        return False
    cache[key] = now + _SHARE_NOTICE_TTL_SECONDS
    return True


async def _send_profile_share_notice_once(
    event: MessageEvent,
    bot: Bot,
    *,
    user_id: str,
    group_id: str,
    group_name: str,
    real_group_id: str,
) -> bool:
    if not _share_notice_is_new(
        event,
        user_id=user_id,
        group_id=group_id,
        real_group_id=real_group_id,
    ):
        return False
    display_name = group_name.strip().replace("`", "'") or "当前群（群名未记录）"
    await bot.send(
        event,
        _reply(
            "### ✅ 档案访问已启用\n\n"
            f"> 你的档案已分享至群：**{display_name}**。\n\n"
            "> 当前个人 QBind 与本群 QGroupBind 均已完成本地绑定声明。\n"
            "> 只有这个已绑定群可以按当前 Bot 业务读取你的档案；普通聊天不会触发绑定检查。\n"
            "> 这表示当前 Bot 的群内访问范围已生效，不等于 QQ/群归属的独立所有权核验。",
            [
                [
                    {
                        "render_data.label": "查看群绑定状态",
                        "action.data": "qgstatus",
                        "action.enter": True,
                    },
                    {
                        "render_data.label": "撤回个人共享",
                        "action.data": "qunbind",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [str(user_id)],
                    },
                ]
            ],
        ),
    )
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


async def ensure_context_bound(event: MessageEvent, bot: Bot) -> bool:
    """Require the personal binding and, for group events, group binding.

    This is the shared pre-use gate for business handlers.  In legacy mode,
    local declaration states remain usable for compatibility.  When
    ``QBIND_EMAIL_REQUIRED=true`` is enabled, both personal and group checks
    require the corresponding ``verified`` state.
    Binding/status/help commands are intentionally outside this helper so a
    user can recover from an unbound state.
    """

    scope, user_id = _scope_user(event)
    verified_only = email_verification_required()
    if not scope or not user_id or not is_bound(
        user_id, scope, verified_only=verified_only
    ):
        await _send_bind_prompt_once(event, bot)
        return False

    group_id = getattr(event, "group_id", None)
    if group_id is None or str(group_id).strip() == "":
        return True

    from .group import get_group_name, get_real_group, is_group_bound

    if is_group_bound(str(group_id), scope, verified_only=verified_only):
        real_group_id = get_real_group(str(group_id), scope)
        if real_group_id:
            group_name = get_group_name(str(group_id), scope)
            if not group_name:
                getter = getattr(bot, "get_group_info", None)
                if callable(getter):
                    try:
                        info = await getter(group_id=str(group_id))
                    except Exception:
                        logger.debug("unable to query group name for share notice", exc_info=True)
                    else:
                        if isinstance(info, dict) and info.get("group_name"):
                            group_name = str(info["group_name"]).strip()
            await _send_profile_share_notice_once(
                event,
                bot,
                user_id=user_id,
                group_id=str(group_id),
                group_name=group_name or "当前群（群名未记录）",
                real_group_id=real_group_id,
            )
            # The archive is populated only after an explicit Bot command has
            # passed both gates.  Normal group messages never reach this
            # helper, and the storage method independently requires both
            # binding rows to be email/trust verified.
            await _publish_verified_membership(
                self_id=scope,
                group_id=str(group_id),
                user_id=user_id,
                group_name=group_name,
            )
        return True
    await _send_group_bind_prompt_once(event, bot)
    return False


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
    if await ensure_context_bound(event, bot):
        return
    matcher.stop_propagation()


def _validate_qq(value: str) -> Optional[str]:
    if not re.fullmatch(r"[1-9]\d{4,10}", value):
        return "QQ 号格式不正确！请输入 5-11 位非零开头数字。"
    return None


def _build_api_url(session_id: str, real_qq: str) -> str:
    """Compatibility URL builder for the personal-only type=5 endpoint."""

    base = (os.getenv("QBIND_GENSOKYO_API_URL") or "http://127.0.0.1:15630").rstrip("/")
    return f"{base}/getid?{urlencode({'oldRowValue': session_id, 'newRowValue': real_qq, 'type': '5'})}"


def _build_unbind_api_url(session_id: str) -> str:
    base = (os.getenv("QBIND_GENSOKYO_API_URL") or "http://127.0.0.1:15630").rstrip("/")
    return f"{base}/getid?{urlencode({'oldRowValue': session_id, 'newRowValue': '0', 'type': '5'})}"


_mapping_client: Any = GensokyoMappingClient()


def set_mapping_client(client: Any) -> None:
    if not hasattr(client, "set_personal_mapping") or not hasattr(
        client, "clear_personal_mapping"
    ):
        raise TypeError("mapping client must provide personal set/clear methods")
    global _mapping_client
    _mapping_client = client


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
    if request.result_code == "email_pending":
        return (
            "📨 QQ 邮箱验证码已发送。\n"
            "请在有效期内发送 `qbindcode <6位验证码>` 完成个人绑定。"
        )
    if request.result_code == "declared":
        return (
            "✅ QQ 号已保存到当前 Bot 的 QBind 映射并标记为‘已声明’，绑定功能现在可以使用。\n"
            "⚠️ 这是本人声明，尚未完成所有权核验；后续安全审核可再将状态提升为‘已验证’。"
        )
    if request.result_code == "unbound":
        return "✅ 个人绑定已解绑，历史审计记录已保留。"
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
    if candidate == user_id:
        await bind.finish(
            _reply(
                "### ❌ 绑定失败\n\n> 虚拟用户 ID 与 QQ 号不能因为字符串相同而被视为已验证身份。",
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
            if current.binding_status == PERSONAL_VERIFIED:
                await bind.finish(
                    _reply(
                        "### ℹ️ 当前已有个人绑定\n\n"
                        "> 当前 Bot 下已经存在已验证个人身份。",
                        _personal_action_buttons(user_id),
                    )
                )
            if (
                email_verification_required()
                and current.binding_status == PERSONAL_DECLARED
                and current.canonical_user_id == candidate
            ):
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
                    await bind.finish(
                        _reply(
                            f"### ❌ 邮箱验证码发送失败\n\n> {error}",
                            _personal_action_buttons(user_id),
                        )
                    )
                await bind.finish(
                    _reply(
                        "### 📩 重新验证 QQ 邮箱\n\n"
                        f"> 验证码已发送至：`{masked}`\n"
                        "> 请发送 `qbindcode <6位验证码>` 完成个人绑定。\n"
                        "> 验证码有效期为 10 分钟。",
                        _personal_email_code_buttons(user_id),
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
    existing_owner = get_store().get_personal_by_canonical(scope, candidate)
    if (
        existing_owner is not None
        and existing_owner.external_user_id != user_id
        and existing_owner.binding_status
        in {PERSONAL_DECLARED, PERSONAL_VERIFIED, PERSONAL_UNBIND_PENDING}
    ):
        failed = get_store().complete_confirmation(
            request.request_id,
            result_code="conflict",
            status=REQUEST_FAILED,
            error="canonical identity is already owned by another user",
        )
        await confirm.finish(
            _reply(_request_message(failed or request), _personal_action_buttons(user_id))
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
                f"> 验证码已发送至：`{masked}`\n"
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
    wait = _check_rate_limit(f"email:{scope}:{user_id}")
    if wait:
        await qbindcode.finish(
            _reply(
                f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。",
                _personal_email_code_buttons(user_id),
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
    challenge = get_store().find_email_challenge(
        purpose="personal_bind",
        bot_scope=scope,
        actor_user_id=user_id,
        external_user_id=user_id,
    )
    if challenge is None:
        await qbindcode.finish(
            _reply(
                "### ❌ 没有待验证的 QQ 邮箱请求\n\n"
                "> 请先发送 `qbind <你的QQ号>` 发起个人绑定。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    attempt = get_store().verify_email_challenge(
        challenge.challenge_id,
        code_digest(challenge.challenge_id, code, _email_service.config.code_secret),
    )
    if not attempt.accepted:
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

    candidate = challenge.candidate_id or ""
    current = get_store().get_personal(scope, user_id)
    existing_owner = get_store().get_personal_by_canonical(scope, candidate)
    if (
        existing_owner is not None
        and existing_owner.external_user_id != user_id
        and existing_owner.binding_status
        in {PERSONAL_DECLARED, PERSONAL_VERIFIED, PERSONAL_UNBIND_PENDING}
    ):
        await qbindcode.finish(
            _reply(
                "### ❌ QQ 号绑定冲突\n\n> 这个 QQ 号已经属于其他用户，未覆盖原有身份。",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    if current and current.binding_status == PERSONAL_VERIFIED:
        if current.canonical_user_id != candidate:
            await qbindcode.finish(
                _reply(
                    "### ❌ 个人绑定已发生变化\n\n> 当前用户已经有其他已验证 QQ 号，未覆盖原有身份。",
                    _personal_action_buttons(user_id),
                )
            )
        await qbindcode.finish(
            _reply(
                "### ✅ QQ 邮箱验证已完成\n\n> 当前个人绑定已经是已验证状态。",
                _personal_action_buttons(user_id),
            )
        )
    try:
        if current is None or current.binding_status == PERSONAL_REVOKED:
            get_store().declare_personal(
                bot_scope=scope,
                external_user_id=user_id,
                canonical_user_id=candidate,
                bound_by_user_id=user_id,
                verification_source="qbind:qq_email",
                operation_id=challenge.challenge_id,
            )
        elif current.binding_status == PERSONAL_DECLARED:
            if current.canonical_user_id != candidate:
                raise BindingConflictError("current personal QQ differs from email challenge")
        else:
            raise QbindStorageError(
                f"personal binding cannot be email-verified from {current.binding_status}"
            )
        verified = get_store().verify_personal(
            bot_scope=scope,
            external_user_id=user_id,
            verified_by=user_id,
            verification_source="qbind:qq_email",
            operation_id=challenge.challenge_id,
        )
    except (BindingConflictError, QbindStorageError) as error:
        logger.exception("QQ email verification could not finalize personal binding")
        await qbindcode.finish(
            _reply(
                f"### ❌ QQ 邮箱已验证，但本地绑定保存失败\n\n> {error}",
                _personal_action_buttons(user_id, include_unbind=False),
            )
        )
    await qbindcode.finish(
        _reply(
            "### ✅ 个人 QQ 绑定已完成邮箱认证\n\n"
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
    A group canonical mapping is returned after a successful ``qgconfirm``
    declaration so backend group features can behave like personal QBind;
    ``group_verification_status`` still distinguishes ``declared`` from the
    later ``verified`` security-review state.  Unknown IDs are never inferred
    from names, number ranges, group names, or values in another Bot namespace.
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
    "get_verified_qq",
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
