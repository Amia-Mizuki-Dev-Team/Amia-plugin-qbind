from nonebot import require

_core = require("amia_core")
UserIdentityKey = _core.UserIdentityKey
ResolvedIdentity = _core.ResolvedIdentity

from . import get_real_qq

class QbindIdentityResolver:
    async def resolve_identity(self, key: UserIdentityKey) -> ResolvedIdentity:
        real_qq = get_real_qq(key.user_id)
        return ResolvedIdentity(external_key=key, canonical_user_id=real_qq)
