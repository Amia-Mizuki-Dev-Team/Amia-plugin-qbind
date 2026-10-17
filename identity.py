"""QBind adapters for amia-core identity protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from nonebot import require

_core = require("amia_core")
UserIdentityKey = _core.UserIdentityKey
ResolvedIdentity = _core.ResolvedIdentity

from . import get_binding_status, get_verified_qq


@dataclass(frozen=True)
class UserIdentityResolution:
    """Status-rich result for callers that need more than core's opaque ID."""

    self_id: str
    external_user_id: str
    canonical_user_id: Optional[str]
    verification_status: str
    reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.verification_status == "verified" and self.canonical_user_id is not None


class QbindIdentityResolver:
    """Resolve only verified QBind identities into amia-core canonical IDs."""

    async def resolve_user_identity(self, key: UserIdentityKey) -> UserIdentityResolution:
        self_id = str(key.self_id)
        user_id = str(key.user_id)
        status = get_binding_status(user_id, self_id)
        canonical = get_verified_qq(user_id, self_id)
        reason = None if canonical else "identity is not independently verified"
        return UserIdentityResolution(
            self_id=self_id,
            external_user_id=user_id,
            canonical_user_id=canonical,
            verification_status=status,
            reason=reason,
        )

    async def resolve_identity(self, key: UserIdentityKey) -> ResolvedIdentity:
        """Implement the amia-core protocol without exposing declarations."""

        resolution = await self.resolve_user_identity(key)
        return ResolvedIdentity(
            external_key=key,
            canonical_user_id=resolution.canonical_user_id,
        )

    async def resolve_context_identity(
        self,
        *,
        self_id: object,
        user_id: object,
        group_id: Optional[object] = None,
    ):
        from .group import resolve_group_identity

        user = await self.resolve_user_identity(
            UserIdentityKey(self_id=str(self_id), user_id=str(user_id))
        )
        group = None
        if group_id is not None:
            group = await resolve_group_identity(str(self_id), str(group_id))
        return {
            "bot_scope": str(self_id),
            "external_user_id": str(user_id),
            "canonical_user_id": user.canonical_user_id,
            "user_verification_status": user.verification_status,
            "external_group_id": str(group_id) if group_id is not None else None,
            "canonical_group_id": group.canonical_group_id if group else None,
            "group_verification_status": group.verification_status if group else None,
        }


__all__ = ["QbindIdentityResolver", "UserIdentityResolution"]
