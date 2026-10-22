"""QBind adapters for amia-core identity protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from nonebot import require

_core = require("amia_core")
UserIdentityKey = _core.UserIdentityKey
ResolvedIdentity = _core.ResolvedIdentity

from . import (
    get_binding_status,
    get_verified_qq,
    get_verified_pair,
)


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
    """Resolve verified QBind identities from local state without GSK writes."""

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
        scope = str(self_id)
        external_user = str(user_id)
        if group_id is None:
            user = await self.resolve_user_identity(
                UserIdentityKey(self_id=scope, user_id=external_user)
            )
            return {
                "bot_scope": scope,
                "external_user_id": external_user,
                "canonical_user_id": user.canonical_user_id,
                "user_verification_status": user.verification_status,
                "external_group_id": None,
                "canonical_group_id": None,
                "group_verification_status": None,
            }

        external_group = str(group_id)
        from .group import get_group_binding_status

        user_status = get_binding_status(external_user, scope)
        group_status = get_group_binding_status(external_group, scope)
        pair = get_verified_pair(scope, external_user, external_group)
        if pair is None:
            return {
                "bot_scope": scope,
                "external_user_id": external_user,
                "canonical_user_id": None,
                "user_verification_status": user_status,
                "external_group_id": external_group,
                "canonical_group_id": None,
                "group_verification_status": group_status,
            }
        return {
            "bot_scope": scope,
            "external_user_id": external_user,
            "canonical_user_id": pair.personal.canonical_user_id,
            "user_verification_status": pair.personal.binding_status,
            "external_group_id": external_group,
            "canonical_group_id": pair.group.real_group_id,
            "group_verification_status": pair.group.binding_status,
        }


__all__ = ["QbindIdentityResolver", "UserIdentityResolution"]
