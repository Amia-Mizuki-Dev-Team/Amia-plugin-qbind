# Changelog

## QBind / QGroupBind ID routing correction — 2026-10-17

- Personal and group declarations now persist only in QBind's Bot-scoped SQLite mapping; normal bind/unbind flows no longer treat Gensokyo type=5 acceptance as identity evidence or mutate Gensokyo's isolated ID map.
- PJSK relay now enforces QBind's personal and group binding gate, resolves both IDs with the current Bot `self_id`, and sends the declared QQ/group IDs to Haruki/Sakura.
- Backend replies addressed to those declared IDs are routed back through the original event's virtual IDs for OneBot delivery.
- Unverified declarations remain marked `declared`; they are usable by same-Bot business features but are not an ownership proof.

## QBind V2 local development update — 2026-10-16

### Acceptance matrix follow-up

- Added `tests/test_acceptance_matrix.py` for the executable local portions of attachment section 10.1–10.3.
- Covered personal invalid input, duplicate binding, timeout, cross-user confirmation, canonical conflict, backend failure, restart persistence and recoverable unbind.
- Covered group operator permissions, invalid group IDs, cross-user/cross-group tokens, concurrent admin requests, verifier rejection, timeout, reuse, unbind and restart persistence.
- Covered two virtual user IDs sharing one canonical identity only after trusted verification, distinct group identities, and independent group/personal revocation.
- Fixed the personal canonical uniqueness rule so unverified claims remain exclusive while trusted verification may add a second virtual alias for the same real QQ.
- Added the normal QGroupBind declaration path: Gensokyo `type=5` writes the virtual-group to real-group mapping just like personal QBind, and a successful request is immediately available to backend group consumers as `declared`. A later trusted or email review can promote it to `verified`.
- Added a global pre-use access gate: ordinary private-chat business messages require the user's personal binding, while group business messages require both the user's personal binding and the current group's `declared` or `verified` mapping. Binding, status and help commands remain available for recovery.

### Added

- SQLite-backed personal and group binding storage with Bot `self_id` namespaces.
- Binding states for declared, verified, pending verification, conflict, revoked and recoverable unbind operations.
- Atomic audit records with digested identity references rather than plaintext IDs in audit subjects.
- One-time, expiring confirmation requests bound to Bot, user and group context.
- Safe import of legacy `binds.json`, rollback backup creation and conflict-preserving migration.
- `QGroupBind` commands: `qgbind`, `qgconfirm`, `qgcancel`, `qgstatus`, `qgunbind` and `qgrebind`.
- `GroupIdentityResolver`, `resolve_context_identity()` and trusted verification adapter boundaries.
- Gensokyo type=5 client for personal and group idmap updates.
- Optional Gensokyo `type=11` group/user pair client and `GensokyoGroupVerifier` for later trusted review; type=5 remains a usable declaration, not an ownership-proof status.

### Security behavior changes

- A successful Gensokyo type=5 response is stored as `declared`, immediately exposed to backend group consumers, and kept distinct from the later `verified` security-review status.
- `QbindIdentityResolver` and `get_verified_qq()` never expose an unverified manual QQ number as canonical identity.
- Personal unbind verifies that the target belongs to the current Bot/user record and retains `unbind_pending` recovery state when the backend is partial or unavailable.
- Group commands use type=5 for the normal declaration path and never treat it as independent ownership proof; later email/trusted review is represented separately by `verified`.
- Existing verified group mappings are not automatically overwritten by a new request; rebinds preserve history and require the same operator confirmation flow.

### Verification boundary

The local unittest suite covers storage and resolver behavior. Gensokyo Release015/current production behavior, `use_uin` variants, `idmap_isolation` runtime behavior, trusted QQ/group ownership evidence, Haruki integration and real QQ group acceptance remain `NOT_VERIFIED`/`NOT_RUN` until performed in the target environment.
