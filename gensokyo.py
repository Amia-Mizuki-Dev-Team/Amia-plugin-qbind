"""Narrow adapter for the Gensokyo type=5 identity-map API.

Gensokyo's endpoint changes a virtual numeric row value to another numeric
row value.  It does not prove that a user owns the number they typed, so this
adapter reports only whether the backend update succeeded.  QBind stores that
result as ``declared`` and never as ``verified``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


class GensokyoMappingError(RuntimeError):
    """A type=5 request failed or returned an unusable response."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class GroupUserResolution:
    """Real group/user pair returned by Gensokyo's type=11 endpoint."""

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


class GensokyoMappingClient:
    """Call Gensokyo type=5 personal and type=11 pair endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (base_url or os.getenv("QBIND_GENSOKYO_API_URL") or "http://127.0.0.1:15630").rstrip("/")
        configured_timeout = os.getenv("QBIND_GENSOKYO_TIMEOUT", "5")
        try:
            self.timeout = float(timeout if timeout is not None else configured_timeout)
        except (TypeError, ValueError):
            self.timeout = 5.0
        if self.timeout <= 0:
            self.timeout = 5.0

    async def _request_json(self, params: dict[str, str], operation: str) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/getid", params=params)
        except httpx.TimeoutException as error:
            raise GensokyoMappingError(f"{operation} request timed out", retryable=True) from error
        except httpx.RequestError as error:
            raise GensokyoMappingError(
                f"unable to reach Gensokyo {operation} endpoint: {error}",
                retryable=True,
            ) from error

        try:
            data = response.json()
        except ValueError as error:
            raise GensokyoMappingError(
                f"Gensokyo {operation} returned invalid JSON (HTTP {response.status_code})",
                retryable=response.status_code >= 500,
            ) from error
        if response.status_code != 200:
            detail = data.get("error") if isinstance(data, dict) else None
            raise GensokyoMappingError(
                f"Gensokyo {operation} rejected the request (HTTP {response.status_code}: {detail or 'unknown error'})",
                retryable=response.status_code >= 500,
            )
        if isinstance(data, dict) and data.get("error"):
            raise GensokyoMappingError(
                f"Gensokyo {operation} rejected the request: {data['error']}",
                retryable=False,
            )
        return data

    async def _update(self, old_row_value: object, new_row_value: object) -> None:
        old_value = _numeric_id(old_row_value, "oldRowValue")
        new_value = _numeric_id(new_row_value, "newRowValue")
        data = await self._request_json(
            {
                "oldRowValue": old_value,
                "newRowValue": new_value,
                "type": "5",
            },
            "type=5",
        )
        if not isinstance(data, dict) or data.get("status") != "success":
            raise GensokyoMappingError(
                "Gensokyo type=5 returned no explicit success status",
                retryable=False,
            )

    async def resolve_group_user(
        self, virtual_group_id: object, virtual_user_id: object
    ) -> GroupUserResolution:
        """Resolve a virtual group/user pair through Gensokyo type=11."""

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

    async def set_personal_mapping(self, virtual_user_id: object, real_qq: object) -> None:
        await self._update(virtual_user_id, real_qq)

    async def clear_personal_mapping(self, virtual_user_id: object) -> None:
        await self._update(virtual_user_id, "0")

    async def set_group_mapping(
        self, virtual_group_id: object, real_group_id: object
    ) -> None:
        """Declare a virtual-group to real-group mapping through type=5.

        This has the same semantics as the personal qbind mapping: a
        successful backend update is a declaration, not independent proof of
        ownership.  QGroupBind keeps that distinction in its local state.
        """

        await self._update(virtual_group_id, real_group_id)

    async def clear_group_mapping(self, virtual_group_id: object) -> None:
        """Clear the virtual-group row created by ``set_group_mapping``."""

        await self._update(virtual_group_id, "0")


__all__ = ["GensokyoMappingClient", "GensokyoMappingError", "GroupUserResolution"]
