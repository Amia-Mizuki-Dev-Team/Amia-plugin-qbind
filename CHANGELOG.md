# Changelog

All notable changes to QBind are documented here.

## [2026-10-22]

### Changed
- Group business gates now report the specific local blocking state (missing group binding, declared, pending verification, conflict, revoked, or unverified personal identity) instead of one generic GSK synchronization message.
- type=5 synchronization is state-aware: each successful write is recorded in SQLite (`gsk_sync_state`) and identical repeats are skipped, because Gensokyo rejects a re-write of an existing mapping instead of treating it as a no-op.

### Fixed
- Declared group bindings no longer point operators at `qgconfirm`/`qgcode`, which cannot succeed while email verification is disabled.
- Gensokyo's `已存在` (target already mapped) and `不存在` (virtual identity row missing) rejections are distinguished from generic failures: an existing target is accepted as the reached state, with the caveat that Gensokyo echoes the requested value and therefore cannot prove which virtual ID holds it; a missing row gets its own actionable message, and only a possible partial group write is compensated as a conflict.
- Completing a group binding no longer fails when the personal mapping was already written moments earlier.
- `set_group_mapping_client` keeps the group verifier on a client that can perform type=11 lookups.

## [2026-10-21]

### Added
- Emit fixed, identity-free reason codes when personal binding confirmations are rejected for conflicts.

### Fixed
- Allow identical virtual user ID and canonical QQ strings through the normal binding state machine; email-required mode still verifies only after a valid code.
- Prevent conflicting cross-Bot targets from overwriting the unscoped personal type=5 mapping; include locally resolvable pending bindings in conflict and rollback-reference checks while keeping batch eligibility to declared/verified records.
- Check all active canonical owners and report personal mapping sync failures safely without leaking identifiers or raising an unbound-variable error.
- Report personal binding conflicts explicitly instead of showing a generic failed status.
- Keep email-verification aliases fail-closed: after an exact QQ-mail challenge and explicit type=5 success, create a same-scope verified alias for the candidate key only under the current deployment assumption that the candidate key equals the post-map event key. type=5 success does not prove QQ ownership, and this key equality is not an adapter guarantee: the adapter provides neither mapping readback nor event-identity guarantees. Target conflicts, ambiguous recovery, and failed or unknown remote results never fall back to another key.
- Restrict QQ-mail sync retries to a unique verified challenge for the original source key; reject ambiguous histories, revoked targets, conflicts, and unknown outcomes without candidate-key recovery.
