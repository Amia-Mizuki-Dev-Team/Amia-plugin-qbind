"""Narrow adapter for Gensokyo's public ID-map API.

Type=5 writes one virtual-to-real numeric mapping. Type=11 is a read-only
consistency lookup and never establishes ownership or a verified QBind state.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import httpx


class GensokyoMappingError(RuntimeError):
    """A Gensokyo mapping request failed or returned an unusable response."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class GensokyoMappingExistsError(GensokyoMappingError):
    """The target type=5 value is already mapped elsewhere.

    Gensokyo answers ``<value> :已存在`` instead of overwriting, so the caller
    must decide whether the requested target state is already reached.  A retry
    cannot change this outcome.
    """

    def __init__(self, message: str, *, remote_value: str | None = None):
        super().__init__(message, retryable=False)
        self.remote_value = remote_value


class GensokyoMappingMissingError(GensokyoMappingError):
    """The virtual ID has no Gensokyo session row, so type=5 cannot apply.

    Gensokyo answers ``不存在:<virtual_id>``.  The row is recreated by ordinary
    traffic, so an immediate retry of the same request cannot succeed.
    """

    def __init__(self, message: str, *, virtual_id: str | None = None):
        super().__init__(message, retryable=False)
        self.virtual_id = virtual_id


def _decode_body(response: Any) -> str:
    """Decode a response body that may use a legacy (non-UTF-8) encoding."""

    body = getattr(response, "content", b"")
    if isinstance(body, str):
        return body
    if not isinstance(body, (bytes, bytearray)):
        return ""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return bytes(body).decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _classify_mapping_error(text: str) -> str:
    """Classify an error body as ``"exists"``, ``"missing"`` or ``""``.

    Negated English forms are checked first so ``"does not exist"`` cannot be
    mistaken for the ``"exists"`` branch.  Both branches are anchored on whole
    phrases: a bare ``exists``/``already`` also shows up in unrelated failures,
    and absorbing one of those as "target state reached" would hide a write
    that never happened.
    """

    if not text:
        return ""
    if "不存在" in text or re.search(
        r"not\s+found|does\s*n[o']?t\s+exist|not\s+exist", text, re.I
    ):
        return "missing"
    if re.search(r"已存在|already\s+exists|exists\s+already|duplicate", text, re.I):
        return "exists"
    return ""


def _remote_value_from_error(text: str) -> Optional[str]:
    """Extract the numeric value before the separator of an ``已存在`` message."""

    head = re.split(r"[:：]", text, 1)[0].strip()
    return head if head.isdigit() else None


def _virtual_id_from_error(text: str) -> Optional[str]:
    """Extract the numeric virtual ID after the ``不存在:`` marker."""

    marker = "不存在"
    index = text.find(marker)
    if index < 0:
        return None
    match = re.search(r"\d+", text[index + len(marker) :])
    return match.group(0) if match else None


def _mapping_error(
    operation: str,
    error_text: str,
    *,
    default_message: str,
    retryable: bool = False,
) -> GensokyoMappingError:
    """Build the most specific exception the error body supports."""

    kind = _classify_mapping_error(error_text)
    if kind == "exists":
        return GensokyoMappingExistsError(
            f"Gensokyo {operation} target mapping already exists",
            remote_value=_remote_value_from_error(error_text),
        )
    if kind == "missing":
        return GensokyoMappingMissingError(
            f"Gensokyo {operation} virtual identity row is missing",
            virtual_id=_virtual_id_from_error(error_text),
        )
    return GensokyoMappingError(default_message, retryable=retryable)


@dataclass(frozen=True)
class GroupUserResolution:
    """Real group/user pair returned by the read-only type=11 endpoint."""

    real_group_id: str
    real_user_id: str


def _numeric_id(value: object, field: str) -> str:
    text = str(value).strip()
    if not text or not text.isdigit():
        raise GensokyoMappingError(
            f"Gensokyo ID mapping requires numeric {field}; received a non-numeric ID",
            retryable=False,
        )
    return text


def _is_loopback_url(value: str) -> bool:
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return False
    if not host:
        return False
    normalized_host = host.lower()
    if normalized_host.endswith("."):
        normalized_host = normalized_host[:-1]
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


class GensokyoMappingClient:
    """Call Gensokyo type=5 writes and type=11 consistency lookups."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (
            base_url or os.getenv("QBIND_GENSOKYO_API_URL") or "http://127.0.0.1:15630"
        ).rstrip("/")
        self._lotus_password = os.getenv("QBIND_GENSOKYO_LOTUS_PASSWORD", "")
        configured_timeout = os.getenv("QBIND_GENSOKYO_TIMEOUT", "5")
        try:
            self.timeout = float(timeout if timeout is not None else configured_timeout)
        except (TypeError, ValueError):
            self.timeout = 5.0
        if self.timeout <= 0:
            self.timeout = 5.0

    async def _request_json(self, params: dict[str, str], operation: str) -> Any:
        try:
            parsed_base_url = urlsplit(self.base_url)
            scheme = parsed_base_url.scheme.lower()
            has_host = parsed_base_url.hostname is not None
        except ValueError:
            scheme = ""
            has_host = False
        is_loopback = _is_loopback_url(self.base_url)
        if scheme not in {"http", "https"} or not has_host:
            raise GensokyoMappingError(
                "Gensokyo API URL must be a valid HTTP(S) URL", retryable=False
            )
        if scheme == "http" and not is_loopback:
            raise GensokyoMappingError(
                "Gensokyo API requests to non-loopback hosts require HTTPS",
                retryable=False,
            )
        if not self._lotus_password and not is_loopback:
            raise GensokyoMappingError(
                "Gensokyo API requests require loopback; authenticated remote access is not configured",
                retryable=False,
            )

        endpoint = f"{self.base_url}/getid"
        request_url = endpoint
        request_params: Optional[dict[str, str]] = params
        headers: dict[str, str] = {}
        if self._lotus_password:
            timestamp = str(int(time.time()))
            # Reuse the signed query verbatim so the URL and HMAC cannot diverge.
            query = urlencode(sorted(params.items()))
            signed_query = f"{query}&timestamp={timestamp}"
            path = urlsplit(endpoint).path
            payload = f"{path}?{signed_query}"
            token = hmac.new(
                self._lotus_password.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            request_url = f"{endpoint}?{signed_query}"
            request_params = None
            headers = {"X-Token": token}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    request_url, params=request_params, headers=headers
                )
        except httpx.TimeoutException:
            raise GensokyoMappingError(
                f"Gensokyo {operation} request timed out", retryable=True
            ) from None
        except Exception:
            raise GensokyoMappingError(
                f"unable to reach Gensokyo {operation} endpoint", retryable=True
            ) from None

        body_text = _decode_body(response)
        try:
            data = response.json()
        except ValueError:
            # Gensokyo may answer with GBK bytes, so re-decode the body before
            # declaring the payload unusable: the error text is what tells an
            # idempotent "已存在" apart from a genuine failure.
            try:
                data = json.loads(body_text) if body_text.strip() else None
            except ValueError:
                data = None
        error_text = ""
        if isinstance(data, dict) and data.get("error"):
            error_text = str(data["error"]).strip()
        elif data is None:
            error_text = body_text.strip()
        if response.status_code != 200 or (
            isinstance(data, dict) and data.get("error")
        ):
            raise _mapping_error(
                operation,
                error_text,
                default_message=(
                    f"Gensokyo {operation} returned invalid JSON "
                    f"(HTTP {response.status_code})"
                    if data is None
                    else f"Gensokyo {operation} rejected the request "
                    f"(HTTP {response.status_code})"
                ),
                retryable=response.status_code >= 500,
            )
        return data

    async def _update(self, old_row_value: object, new_row_value: object) -> None:
        old_value = _numeric_id(old_row_value, "oldRowValue")
        new_value = _numeric_id(new_row_value, "newRowValue")
        if new_value == "0":
            raise GensokyoMappingError(
                "Gensokyo type=5 zero-value deletion is not a supported mapping",
                retryable=False,
            )
        data = await self._request_json(
            {
                "type": "5",
                "oldRowValue": old_value,
                "newRowValue": new_value,
            },
            "type=5",
        )
        if not isinstance(data, dict) or data.get("status") != "success":
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error") or "").strip()
            raise _mapping_error(
                "type=5",
                detail,
                default_message=(
                    "Gensokyo type=5 returned no explicit success status"
                ),
            )

    async def resolve_group_user(
        self, virtual_group_id: object, virtual_user_id: object
    ) -> GroupUserResolution:
        """Read type=11 values only for consistency checks, never verification."""

        virtual_group = _numeric_id(virtual_group_id, "id")
        virtual_user = _numeric_id(virtual_user_id, "subid")
        data = await self._request_json(
            {"id": virtual_group, "subid": virtual_user, "type": "11"},
            "type=11",
        )
        if not isinstance(data, dict):
            raise GensokyoMappingError(
                "Gensokyo type=11 returned an invalid object", retryable=False
            )
        return GroupUserResolution(
            real_group_id=_numeric_id(data.get("firstRealValue"), "firstRealValue"),
            real_user_id=_numeric_id(data.get("secondRealValue"), "secondRealValue"),
        )

    async def set_personal_mapping(
        self, virtual_user_id: object, real_user_id: object
    ) -> None:
        """Map one verified virtual user ID to its bound real user ID via type=5."""

        await self._update(virtual_user_id, real_user_id)

    async def set_group_mapping(
        self, virtual_group_id: object, real_group_id: object
    ) -> None:
        """Map one verified virtual group ID to its bound real group ID via type=5."""

        await self._update(virtual_group_id, real_group_id)

    async def clear_personal_mapping(self, virtual_user_id: object) -> None:
        """Refuse unreliable type=5 zero-value deletion; require reconciliation."""

        _numeric_id(virtual_user_id, "oldRowValue")
        raise GensokyoMappingError(
            "Gensokyo type=5 zero-value deletion is not reliable; manual reconciliation is required",
            retryable=False,
        )

    async def clear_group_mapping(self, virtual_group_id: object) -> None:
        """Refuse unreliable type=5 zero-value deletion; require reconciliation."""

        _numeric_id(virtual_group_id, "oldRowValue")
        raise GensokyoMappingError(
            "Gensokyo type=5 zero-value deletion is not reliable; manual reconciliation is required",
            retryable=False,
        )


__all__ = [
    "GensokyoMappingClient",
    "GensokyoMappingError",
    "GensokyoMappingExistsError",
    "GensokyoMappingMissingError",
    "GroupUserResolution",
]
