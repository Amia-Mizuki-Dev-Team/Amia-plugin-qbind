"""Durable storage and state transitions for QBind V2.

The plugin deliberately keeps this storage local to QBind.  Other plugins
should use the public resolver APIs instead of reading this database.

The legacy ``binds.json`` file is imported as *declared* data only.  A legacy
row is never promoted to ``verified`` merely because it was present in the
old file or because Gensokyo accepted a type=5 mapping update.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


PERSONAL_DECLARED = "declared"
PERSONAL_VERIFIED = "verified"
PERSONAL_UNBIND_PENDING = "unbind_pending"
PERSONAL_REVOKED = "revoked"

GROUP_DECLARED = "declared"
GROUP_PENDING_VERIFICATION = "pending_verification"
GROUP_VERIFIED = "verified"
GROUP_CONFLICT = "conflict"
GROUP_REVOKED = "revoked"

MEMBERSHIP_VERIFIED = "verified"
MEMBERSHIP_REVOKED = "revoked"

REQUEST_PENDING = "pending"
REQUEST_COMPLETED = "completed"
REQUEST_CANCELED = "canceled"
REQUEST_EXPIRED = "expired"
REQUEST_FAILED = "failed"
REQUEST_BACKEND_UNKNOWN = "backend_unknown"

EMAIL_CHALLENGE_PENDING = "pending"
EMAIL_CHALLENGE_VERIFIED = "verified"
EMAIL_CHALLENGE_EXPIRED = "expired"
EMAIL_CHALLENGE_CANCELED = "canceled"
EMAIL_CHALLENGE_FAILED = "failed"


class QbindStorageError(RuntimeError):
    """Base class for storage and state-transition failures."""


class BindingConflictError(QbindStorageError):
    """Raised when a requested identity is already owned by another record."""


class LegacyMigrationError(QbindStorageError):
    """Raised when the legacy JSON cannot be safely migrated."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _digest(scope: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return hashlib.sha256(f"{scope}\x00{value}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PersonalBinding:
    bot_scope: str
    external_user_id: str
    canonical_user_id: Optional[str]
    binding_status: str
    verification_source: Optional[str]
    bound_by_user_id: Optional[str]
    verified_by: Optional[str]
    created_at: str
    verified_at: Optional[str]
    updated_at: str
    version: int
    last_error: Optional[str]


@dataclass(frozen=True)
class GroupBinding:
    bot_scope: str
    virtual_group_id: str
    real_group_id: Optional[str]
    group_name: Optional[str]
    binding_status: str
    verification_source: Optional[str]
    bound_by_user_id: Optional[str]
    verified_by: Optional[str]
    created_at: str
    verified_at: Optional[str]
    updated_at: str
    version: int
    last_error: Optional[str]


@dataclass(frozen=True)
class GroupUserMembership:
    """A verified user/group relationship observed by an explicit command.

    The row is intentionally keyed by the Bot namespace and virtual IDs.  The
    real IDs are the current QBind projection and are refreshed when a
    verified mapping is replaced.  Read methods still join the live binding
    tables, so revoked identities stop being returned without deleting the
    historical observation.
    """

    bot_scope: str
    virtual_group_id: str
    real_group_id: str
    group_name: Optional[str]
    virtual_user_id: str
    real_user_id: str
    membership_status: str
    source: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class ConfirmationRequest:
    request_id: str
    request_kind: str
    bot_scope: str
    actor_user_id: str
    external_user_id: Optional[str]
    external_group_id: Optional[str]
    candidate_id: Optional[str]
    group_name: Optional[str]
    expected_version: Optional[int]
    status: str
    result_code: Optional[str]
    last_error: Optional[str]
    created_at: str
    expires_at: float
    updated_at: str


@dataclass(frozen=True)
class EmailChallenge:
    challenge_id: str
    purpose: str
    bot_scope: str
    actor_user_id: str
    external_user_id: Optional[str]
    external_group_id: Optional[str]
    candidate_id: Optional[str]
    group_name: Optional[str]
    expected_version: Optional[int]
    target_email_digest: str
    code_digest: str
    attempts: int
    max_attempts: int
    status: str
    created_at: str
    expires_at: float
    updated_at: str


@dataclass(frozen=True)
class EmailChallengeAttempt:
    accepted: bool
    reason: str
    challenge: Optional[EmailChallenge]


@dataclass(frozen=True)
class MigrationResult:
    imported: int
    skipped: int
    backup_path: Optional[str]


class QbindStore:
    """Small SQLite store with explicit, auditable state transitions."""

    def __init__(self, db_path: os.PathLike[str] | str, legacy_json_path: os.PathLike[str] | str):
        self.db_path = Path(db_path)
        self.legacy_json_path = Path(legacy_json_path)
        self._lock = threading.RLock()
        self.migration_error: Optional[str] = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialise(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS personal_bindings (
                        bot_scope TEXT NOT NULL,
                        external_user_id TEXT NOT NULL,
                        canonical_user_id TEXT,
                        binding_status TEXT NOT NULL,
                        verification_source TEXT,
                        bound_by_user_id TEXT,
                        verified_by TEXT,
                        created_at TEXT NOT NULL,
                        verified_at TEXT,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        last_error TEXT,
                        PRIMARY KEY (bot_scope, external_user_id)
                    );

                    CREATE INDEX IF NOT EXISTS ix_personal_canonical
                    ON personal_bindings (bot_scope, canonical_user_id);

                    CREATE TABLE IF NOT EXISTS group_bindings (
                        bot_scope TEXT NOT NULL,
                        virtual_group_id TEXT NOT NULL,
                        real_group_id TEXT,
                        group_name TEXT,
                        binding_status TEXT NOT NULL,
                        verification_source TEXT,
                        bound_by_user_id TEXT,
                        verified_by TEXT,
                        created_at TEXT NOT NULL,
                        verified_at TEXT,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        last_error TEXT,
                        PRIMARY KEY (bot_scope, virtual_group_id)
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS uq_group_active_real
                    ON group_bindings (bot_scope, real_group_id)
                    WHERE real_group_id IS NOT NULL
                      AND binding_status IN ('verified', 'pending_verification');

                    CREATE UNIQUE INDEX IF NOT EXISTS uq_group_active_real_declared
                    ON group_bindings (bot_scope, real_group_id)
                    WHERE real_group_id IS NOT NULL
                      AND binding_status IN ('verified', 'pending_verification', 'declared');

                    CREATE INDEX IF NOT EXISTS ix_group_real
                    ON group_bindings (bot_scope, real_group_id);

                    CREATE TABLE IF NOT EXISTS group_user_memberships (
                        bot_scope TEXT NOT NULL,
                        virtual_group_id TEXT NOT NULL,
                        virtual_user_id TEXT NOT NULL,
                        real_group_id TEXT NOT NULL,
                        real_user_id TEXT NOT NULL,
                        group_name TEXT,
                        membership_status TEXT NOT NULL,
                        source TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (bot_scope, virtual_group_id, virtual_user_id)
                    );

                    CREATE INDEX IF NOT EXISTS ix_membership_real_group
                    ON group_user_memberships (
                        bot_scope, real_group_id, membership_status
                    );

                    CREATE INDEX IF NOT EXISTS ix_membership_real_user
                    ON group_user_memberships (
                        bot_scope, real_user_id, membership_status
                    );

                    CREATE TABLE IF NOT EXISTS group_binding_history (
                        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        bot_scope TEXT NOT NULL,
                        virtual_group_id TEXT NOT NULL,
                        real_group_id TEXT,
                        binding_status TEXT NOT NULL,
                        operation_id TEXT,
                        actor_user_id TEXT,
                        verification_source TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS confirmation_requests (
                        request_id TEXT PRIMARY KEY,
                        request_kind TEXT NOT NULL,
                        bot_scope TEXT NOT NULL,
                        actor_user_id TEXT NOT NULL,
                        external_user_id TEXT,
                        external_group_id TEXT,
                        candidate_id TEXT,
                        group_name TEXT,
                        expected_version INTEGER,
                        status TEXT NOT NULL,
                        result_code TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS email_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        purpose TEXT NOT NULL,
                        bot_scope TEXT NOT NULL,
                        actor_user_id TEXT NOT NULL,
                        external_user_id TEXT,
                        external_group_id TEXT,
                        candidate_id TEXT,
                        group_name TEXT,
                        expected_version INTEGER,
                        target_email_digest TEXT NOT NULL,
                        code_digest TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 5,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS ix_email_challenge_lookup
                    ON email_challenges (
                        purpose, bot_scope, actor_user_id, status, created_at
                    );

                    CREATE INDEX IF NOT EXISTS ix_confirmation_lookup
                    ON confirmation_requests (
                        request_kind, bot_scope, actor_user_id, status, created_at
                    );

                    CREATE TABLE IF NOT EXISTS audit_log (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        bot_scope TEXT NOT NULL,
                        subject_type TEXT NOT NULL,
                        subject_digest TEXT,
                        actor_digest TEXT,
                        from_status TEXT,
                        to_status TEXT,
                        operation_id TEXT,
                        details_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS storage_meta (
                        meta_key TEXT PRIMARY KEY,
                        meta_value TEXT NOT NULL
                    );
                    """
                )
                # A trusted real identity may legitimately have more than one
                # virtual user row (for example, group-isolated virtual IDs).
                # Unverified declarations must still be unique so that two
                # users cannot claim the same canonical ID before review.
                connection.execute("DROP INDEX IF EXISTS uq_personal_active_canonical")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_personal_active_canonical
                    ON personal_bindings (bot_scope, canonical_user_id)
                    WHERE canonical_user_id IS NOT NULL
                      AND binding_status IN ('declared', 'unbind_pending');
                    """
                )
                connection.commit()
            finally:
                connection.close()

        try:
            self.migrate_legacy_json()
        except LegacyMigrationError as error:
            # Do not make an invalid legacy file disappear or silently turn it
            # into an empty identity database.  Commands will surface this
            # error through ``storage_health`` and write operations remain
            # blocked until the operator resolves it.
            self.migration_error = str(error)

    @staticmethod
    def _personal_from_row(row: Optional[sqlite3.Row]) -> Optional[PersonalBinding]:
        if row is None:
            return None
        return PersonalBinding(
            bot_scope=row["bot_scope"],
            external_user_id=row["external_user_id"],
            canonical_user_id=row["canonical_user_id"],
            binding_status=row["binding_status"],
            verification_source=row["verification_source"],
            bound_by_user_id=row["bound_by_user_id"],
            verified_by=row["verified_by"],
            created_at=row["created_at"],
            verified_at=row["verified_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _group_from_row(row: Optional[sqlite3.Row]) -> Optional[GroupBinding]:
        if row is None:
            return None
        return GroupBinding(
            bot_scope=row["bot_scope"],
            virtual_group_id=row["virtual_group_id"],
            real_group_id=row["real_group_id"],
            group_name=row["group_name"],
            binding_status=row["binding_status"],
            verification_source=row["verification_source"],
            bound_by_user_id=row["bound_by_user_id"],
            verified_by=row["verified_by"],
            created_at=row["created_at"],
            verified_at=row["verified_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _membership_from_row(
        row: Optional[sqlite3.Row],
    ) -> Optional[GroupUserMembership]:
        if row is None:
            return None
        return GroupUserMembership(
            bot_scope=row["bot_scope"],
            virtual_group_id=row["virtual_group_id"],
            real_group_id=row["real_group_id"],
            group_name=row["group_name"],
            virtual_user_id=row["virtual_user_id"],
            real_user_id=row["real_user_id"],
            membership_status=row["membership_status"],
            source=row["source"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )

    @staticmethod
    def _request_from_row(row: Optional[sqlite3.Row]) -> Optional[ConfirmationRequest]:
        if row is None:
            return None
        return ConfirmationRequest(
            request_id=row["request_id"],
            request_kind=row["request_kind"],
            bot_scope=row["bot_scope"],
            actor_user_id=row["actor_user_id"],
            external_user_id=row["external_user_id"],
            external_group_id=row["external_group_id"],
            candidate_id=row["candidate_id"],
            group_name=row["group_name"],
            expected_version=(
                int(row["expected_version"])
                if row["expected_version"] is not None
                else None
            ),
            status=row["status"],
            result_code=row["result_code"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            expires_at=float(row["expires_at"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _email_challenge_from_row(
        row: Optional[sqlite3.Row],
    ) -> Optional[EmailChallenge]:
        if row is None:
            return None
        return EmailChallenge(
            challenge_id=row["challenge_id"],
            purpose=row["purpose"],
            bot_scope=row["bot_scope"],
            actor_user_id=row["actor_user_id"],
            external_user_id=row["external_user_id"],
            external_group_id=row["external_group_id"],
            candidate_id=row["candidate_id"],
            group_name=row["group_name"],
            expected_version=(
                int(row["expected_version"])
                if row["expected_version"] is not None
                else None
            ),
            target_email_digest=row["target_email_digest"],
            code_digest=row["code_digest"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            status=row["status"],
            created_at=row["created_at"],
            expires_at=float(row["expires_at"]),
            updated_at=row["updated_at"],
        )

    def _audit_tx(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        bot_scope: str,
        subject_type: str,
        subject_value: Optional[str],
        actor_user_id: Optional[str],
        from_status: Optional[str],
        to_status: Optional[str],
        operation_id: Optional[str],
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log (
                event_type, bot_scope, subject_type, subject_digest,
                actor_digest, from_status, to_status, operation_id,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                bot_scope,
                subject_type,
                _digest(bot_scope, subject_value),
                _digest(bot_scope, actor_user_id),
                from_status,
                to_status,
                operation_id,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def storage_health(self) -> dict[str, Optional[str]]:
        return {
            "database": str(self.db_path),
            "legacy_json": str(self.legacy_json_path),
            "migration_error": self.migration_error,
        }

    def get_personal(self, bot_scope: object, external_user_id: object) -> Optional[PersonalBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT * FROM personal_bindings
                    WHERE bot_scope = ? AND external_user_id = ?
                    """,
                    (scope, user_id),
                ).fetchone()
                return self._personal_from_row(row)
            finally:
                connection.close()

    def get_personal_by_canonical(
        self, bot_scope: object, canonical_user_id: object
    ) -> Optional[PersonalBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        canonical = _normalise(canonical_user_id, "canonical_user_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT * FROM personal_bindings
                    WHERE bot_scope = ? AND canonical_user_id = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (scope, canonical),
                ).fetchone()
                return self._personal_from_row(row)
            finally:
                connection.close()

    def declare_personal(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        canonical_user_id: object,
        bound_by_user_id: object,
        verification_source: str,
        operation_id: Optional[str] = None,
        allow_verified_canonical_reuse: bool = False,
    ) -> PersonalBinding:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        canonical = _normalise(canonical_user_id, "canonical_user_id")
        actor = _normalise(bound_by_user_id, "bound_by_user_id")
        source = _normalise(verification_source, "verification_source")
        if user_id == canonical:
            raise BindingConflictError("virtual user ID and canonical QQ must differ")

        try:
            with self._transaction() as connection:
                current_row = connection.execute(
                    """
                    SELECT * FROM personal_bindings
                    WHERE bot_scope = ? AND external_user_id = ?
                    """,
                    (scope, user_id),
                ).fetchone()
                current = self._personal_from_row(current_row)
                if current and current.binding_status in {
                    PERSONAL_DECLARED,
                    PERSONAL_VERIFIED,
                    PERSONAL_UNBIND_PENDING,
                }:
                    if (
                        current.canonical_user_id == canonical
                        and current.binding_status == PERSONAL_DECLARED
                    ):
                        return current
                    raise BindingConflictError(
                        "the user already has an active binding or unbind recovery"
                    )

                other_rows = connection.execute(
                    """
                    SELECT * FROM personal_bindings
                    WHERE bot_scope = ? AND canonical_user_id = ?
                      AND external_user_id <> ?
                      AND binding_status IN ('declared', 'verified', 'unbind_pending')
                    """,
                    (scope, canonical, user_id),
                ).fetchall()
                if other_rows and (
                    not allow_verified_canonical_reuse
                    or any(row["binding_status"] != PERSONAL_VERIFIED for row in other_rows)
                ):
                    raise BindingConflictError(
                        "the canonical QQ is already claimed in this Bot namespace"
                    )

                now = _now()
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO personal_bindings (
                            bot_scope, external_user_id, canonical_user_id,
                            binding_status, verification_source,
                            bound_by_user_id, verified_by, created_at,
                            verified_at, updated_at, version, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, 1, NULL)
                        """,
                        (
                            scope,
                            user_id,
                            canonical,
                            PERSONAL_DECLARED,
                            source,
                            actor,
                            now,
                            now,
                        ),
                    )
                    old_status = None
                    version = 1
                else:
                    connection.execute(
                        """
                        UPDATE personal_bindings
                        SET canonical_user_id = ?, binding_status = ?,
                            verification_source = ?, bound_by_user_id = ?,
                            verified_by = NULL, verified_at = NULL,
                            updated_at = ?, version = version + 1,
                            last_error = NULL
                        WHERE bot_scope = ? AND external_user_id = ?
                        """,
                        (
                            canonical,
                            PERSONAL_DECLARED,
                            source,
                            actor,
                            now,
                            scope,
                            user_id,
                        ),
                    )
                    old_status = current.binding_status
                    version = current.version + 1

                self._audit_tx(
                    connection,
                    event_type="personal.declared",
                    bot_scope=scope,
                    subject_type="user",
                    subject_value=user_id,
                    actor_user_id=actor,
                    from_status=old_status,
                    to_status=PERSONAL_DECLARED,
                    operation_id=operation_id,
                    details={
                        "canonical_digest": _digest(scope, canonical),
                        "verification_source": source,
                        "version": version,
                    },
                )
                row = connection.execute(
                    """
                    SELECT * FROM personal_bindings
                    WHERE bot_scope = ? AND external_user_id = ?
                    """,
                    (scope, user_id),
                ).fetchone()
                result = self._personal_from_row(row)
                if result is None:
                    raise QbindStorageError("personal binding disappeared after write")
                return result
        except sqlite3.IntegrityError as error:
            raise BindingConflictError("personal binding conflicts with an existing identity") from error

    def link_verified_personal(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        canonical_user_id: object,
        bound_by_user_id: object,
        verified_by: object,
        verification_source: str,
        operation_id: Optional[str] = None,
    ) -> PersonalBinding:
        """Add a virtual alias only after trusted proof of the real identity."""

        current = self.get_personal(bot_scope, external_user_id)
        if (
            current is not None
            and current.binding_status == PERSONAL_VERIFIED
            and current.canonical_user_id == str(canonical_user_id).strip()
        ):
            return current
        self.declare_personal(
            bot_scope=bot_scope,
            external_user_id=external_user_id,
            canonical_user_id=canonical_user_id,
            bound_by_user_id=bound_by_user_id,
            verification_source=verification_source,
            operation_id=operation_id,
            allow_verified_canonical_reuse=True,
        )
        return self.verify_personal(
            bot_scope=bot_scope,
            external_user_id=external_user_id,
            verified_by=verified_by,
            verification_source=verification_source,
            operation_id=operation_id,
        )

    def verify_personal(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        verified_by: object,
        verification_source: str,
        operation_id: Optional[str] = None,
    ) -> PersonalBinding:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        verifier = _normalise(verified_by, "verified_by")
        source = _normalise(verification_source, "verification_source")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            current = self._personal_from_row(row)
            if current is None or not current.canonical_user_id:
                raise QbindStorageError("no declared personal binding is available to verify")
            if current.binding_status == PERSONAL_VERIFIED:
                return current
            if current.binding_status != PERSONAL_DECLARED:
                raise QbindStorageError(
                    f"personal binding cannot be verified from status {current.binding_status}"
                )
            now = _now()
            connection.execute(
                """
                UPDATE personal_bindings
                SET binding_status = ?, verification_source = ?, verified_by = ?,
                    verified_at = ?, updated_at = ?, version = version + 1,
                    last_error = NULL
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (
                    PERSONAL_VERIFIED,
                    source,
                    verifier,
                    now,
                    now,
                    scope,
                    user_id,
                ),
            )
            self._audit_tx(
                connection,
                event_type="personal.verified",
                bot_scope=scope,
                subject_type="user",
                subject_value=user_id,
                actor_user_id=verifier,
                from_status=PERSONAL_DECLARED,
                to_status=PERSONAL_VERIFIED,
                operation_id=operation_id,
                details={
                    "canonical_digest": _digest(scope, current.canonical_user_id),
                    "verification_source": source,
                },
            )
            verified = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            result = self._personal_from_row(verified)
            if result is None:
                raise QbindStorageError("personal verification disappeared after write")
            return result

    def mark_personal_unbind_pending(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        operation_id: Optional[str] = None,
    ) -> PersonalBinding:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            current = self._personal_from_row(row)
            if current is None or not current.canonical_user_id:
                raise QbindStorageError("no personal binding exists for unbind")
            if current.binding_status == PERSONAL_REVOKED:
                return current
            if current.binding_status not in {
                PERSONAL_DECLARED,
                PERSONAL_VERIFIED,
                PERSONAL_UNBIND_PENDING,
            }:
                raise QbindStorageError(
                    f"personal binding cannot be unbound from status {current.binding_status}"
                )
            if current.binding_status != PERSONAL_UNBIND_PENDING:
                now = _now()
                connection.execute(
                    """
                    UPDATE personal_bindings
                    SET binding_status = ?, updated_at = ?, version = version + 1,
                        last_error = NULL
                    WHERE bot_scope = ? AND external_user_id = ?
                    """,
                    (PERSONAL_UNBIND_PENDING, now, scope, user_id),
                )
                self._audit_tx(
                    connection,
                    event_type="personal.unbind_started",
                    bot_scope=scope,
                    subject_type="user",
                    subject_value=user_id,
                    actor_user_id=user_id,
                    from_status=current.binding_status,
                    to_status=PERSONAL_UNBIND_PENDING,
                    operation_id=operation_id,
                    details={
                        "canonical_digest": _digest(scope, current.canonical_user_id)
                    },
                )
            updated = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            result = self._personal_from_row(updated)
            if result is None:
                raise QbindStorageError("personal unbind state disappeared after write")
            return result

    def set_personal_error(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        error: str,
        operation_id: Optional[str] = None,
    ) -> Optional[PersonalBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        with self._transaction() as connection:
            now = _now()
            connection.execute(
                """
                UPDATE personal_bindings
                SET last_error = ?, updated_at = ?, version = version + 1
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (str(error)[:1000], now, scope, user_id),
            )
            self._audit_tx(
                connection,
                event_type="personal.operation_failed",
                bot_scope=scope,
                subject_type="user",
                subject_value=user_id,
                actor_user_id=user_id,
                from_status=None,
                to_status=None,
                operation_id=operation_id,
                details={"error": str(error)[:300]},
            )
            row = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            return self._personal_from_row(row)

    def complete_personal_unbind(
        self,
        *,
        bot_scope: object,
        external_user_id: object,
        operation_id: Optional[str] = None,
    ) -> PersonalBinding:
        scope = _normalise(bot_scope, "bot_scope")
        user_id = _normalise(external_user_id, "external_user_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            current = self._personal_from_row(row)
            if current is None:
                raise QbindStorageError("no personal binding exists for unbind")
            if current.binding_status == PERSONAL_REVOKED:
                return current
            if current.binding_status != PERSONAL_UNBIND_PENDING:
                raise QbindStorageError(
                    f"personal unbind must recover from {PERSONAL_UNBIND_PENDING}, "
                    f"not {current.binding_status}"
                )
            now = _now()
            connection.execute(
                """
                UPDATE personal_bindings
                SET binding_status = ?, updated_at = ?, version = version + 1,
                    last_error = NULL
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (PERSONAL_REVOKED, now, scope, user_id),
            )
            self._audit_tx(
                connection,
                event_type="personal.unbound",
                bot_scope=scope,
                subject_type="user",
                subject_value=user_id,
                actor_user_id=user_id,
                from_status=PERSONAL_UNBIND_PENDING,
                to_status=PERSONAL_REVOKED,
                operation_id=operation_id,
                details={
                    "canonical_digest": _digest(scope, current.canonical_user_id)
                },
            )
            updated = connection.execute(
                """
                SELECT * FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, user_id),
            ).fetchone()
            result = self._personal_from_row(updated)
            if result is None:
                raise QbindStorageError("personal unbind disappeared after write")
            return result

    def get_group(self, bot_scope: object, virtual_group_id: object) -> Optional[GroupBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        group_id = _normalise(virtual_group_id, "virtual_group_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND virtual_group_id = ?
                    """,
                    (scope, group_id),
                ).fetchone()
                return self._group_from_row(row)
            finally:
                connection.close()

    def get_group_by_real(
        self, bot_scope: object, real_group_id: object
    ) -> Optional[GroupBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        real_id = _normalise(real_group_id, "real_group_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND real_group_id = ?
                      AND binding_status IN ('verified', 'pending_verification', 'declared')
                    ORDER BY version DESC LIMIT 1
                    """,
                    (scope, real_id),
                ).fetchone()
                return self._group_from_row(row)
            finally:
                connection.close()

    def record_verified_membership(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        virtual_user_id: object,
        group_name: Optional[object],
        source: str = "qbind.command_gate",
    ) -> Optional[GroupUserMembership]:
        """Record one relationship only when both live bindings are verified.

        This method derives both real IDs from the current binding rows.  A
        caller cannot submit a different QQ or group number and have it
        recorded as a trusted relationship.  ``None`` means that either
        binding is absent or is still only declared/pending.
        """

        scope = _normalise(bot_scope, "bot_scope")
        virtual_group = _normalise(virtual_group_id, "virtual_group_id")
        virtual_user = _normalise(virtual_user_id, "virtual_user_id")
        source_value = _normalise(source, "source")[:120]
        if group_name is None:
            group_label = None
        else:
            group_label = " ".join(
                str(group_name).replace("\x00", " ").split()
            ).strip()[:160] or None

        with self._transaction() as connection:
            personal_row = connection.execute(
                """
                SELECT canonical_user_id, binding_status
                FROM personal_bindings
                WHERE bot_scope = ? AND external_user_id = ?
                """,
                (scope, virtual_user),
            ).fetchone()
            group_row = connection.execute(
                """
                SELECT real_group_id, group_name, binding_status
                FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_group),
            ).fetchone()
            if (
                personal_row is None
                or personal_row["binding_status"] != PERSONAL_VERIFIED
                or not personal_row["canonical_user_id"]
                or group_row is None
                or group_row["binding_status"] != GROUP_VERIFIED
                or not group_row["real_group_id"]
            ):
                return None

            real_user = str(personal_row["canonical_user_id"])
            real_group = str(group_row["real_group_id"])
            if group_label is None:
                group_label = group_row["group_name"]
            now = _now()
            connection.execute(
                """
                INSERT INTO group_user_memberships (
                    bot_scope, virtual_group_id, virtual_user_id,
                    real_group_id, real_user_id, group_name,
                    membership_status, source, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (bot_scope, virtual_group_id, virtual_user_id)
                DO UPDATE SET
                    real_group_id = excluded.real_group_id,
                    real_user_id = excluded.real_user_id,
                    group_name = excluded.group_name,
                    membership_status = excluded.membership_status,
                    source = excluded.source,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    scope,
                    virtual_group,
                    virtual_user,
                    real_group,
                    real_user,
                    group_label,
                    MEMBERSHIP_VERIFIED,
                    source_value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM group_user_memberships
                WHERE bot_scope = ? AND virtual_group_id = ?
                  AND virtual_user_id = ?
                """,
                (scope, virtual_group, virtual_user),
            ).fetchone()
            return self._membership_from_row(row)

    def get_group_members(
        self,
        bot_scope: object,
        real_group_id: object,
        *,
        limit: int = 1000,
    ) -> list[GroupUserMembership]:
        """Return currently verified users observed in one real group."""

        scope = _normalise(bot_scope, "bot_scope")
        real_group = _normalise(real_group_id, "real_group_id")
        bounded_limit = max(1, min(int(limit), 5000))
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT m.*
                    FROM group_user_memberships AS m
                    JOIN personal_bindings AS p
                      ON p.bot_scope = m.bot_scope
                     AND p.external_user_id = m.virtual_user_id
                    JOIN group_bindings AS g
                      ON g.bot_scope = m.bot_scope
                     AND g.virtual_group_id = m.virtual_group_id
                    WHERE m.bot_scope = ?
                      AND m.real_group_id = ?
                      AND m.membership_status = ?
                      AND p.binding_status = ?
                      AND g.binding_status = ?
                    ORDER BY m.last_seen_at DESC, m.virtual_user_id ASC
                    LIMIT ?
                    """,
                    (
                        scope,
                        real_group,
                        MEMBERSHIP_VERIFIED,
                        PERSONAL_VERIFIED,
                        GROUP_VERIFIED,
                        bounded_limit,
                    ),
                ).fetchall()
                return [
                    membership
                    for row in rows
                    if (membership := self._membership_from_row(row)) is not None
                ]
            finally:
                connection.close()

    def get_user_groups(
        self,
        bot_scope: object,
        real_user_id: object,
        *,
        limit: int = 1000,
    ) -> list[GroupUserMembership]:
        """Return currently verified groups observed for one real user."""

        scope = _normalise(bot_scope, "bot_scope")
        real_user = _normalise(real_user_id, "real_user_id")
        bounded_limit = max(1, min(int(limit), 5000))
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT m.*
                    FROM group_user_memberships AS m
                    JOIN personal_bindings AS p
                      ON p.bot_scope = m.bot_scope
                     AND p.external_user_id = m.virtual_user_id
                    JOIN group_bindings AS g
                      ON g.bot_scope = m.bot_scope
                     AND g.virtual_group_id = m.virtual_group_id
                    WHERE m.bot_scope = ?
                      AND m.real_user_id = ?
                      AND m.membership_status = ?
                      AND p.binding_status = ?
                      AND g.binding_status = ?
                    ORDER BY m.last_seen_at DESC, m.real_group_id ASC
                    LIMIT ?
                    """,
                    (
                        scope,
                        real_user,
                        MEMBERSHIP_VERIFIED,
                        PERSONAL_VERIFIED,
                        GROUP_VERIFIED,
                        bounded_limit,
                    ),
                ).fetchall()
                return [
                    membership
                    for row in rows
                    if (membership := self._membership_from_row(row)) is not None
                ]
            finally:
                connection.close()

    def set_group_pending(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        real_group_id: object,
        group_name: Optional[str],
        bound_by_user_id: object,
        verification_source: str,
        operation_id: Optional[str] = None,
        allow_replace: bool = False,
        binding_status: str = GROUP_PENDING_VERIFICATION,
    ) -> GroupBinding:
        scope = _normalise(bot_scope, "bot_scope")
        virtual_id = _normalise(virtual_group_id, "virtual_group_id")
        real_id = _normalise(real_group_id, "real_group_id")
        actor = _normalise(bound_by_user_id, "bound_by_user_id")
        source = _normalise(verification_source, "verification_source")
        if binding_status not in {GROUP_PENDING_VERIFICATION, GROUP_DECLARED}:
            raise ValueError("group binding status must be declared or pending_verification")
        try:
            with self._transaction() as connection:
                current_row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND virtual_group_id = ?
                    """,
                    (scope, virtual_id),
                ).fetchone()
                current = self._group_from_row(current_row)
                if current and current.binding_status == GROUP_VERIFIED and not allow_replace:
                    raise BindingConflictError("the virtual group already has a verified binding")
                if (
                    current
                    and current.binding_status == binding_status
                    and current.real_group_id == real_id
                ):
                    return current

                other_row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND real_group_id = ?
                      AND virtual_group_id <> ?
                      AND binding_status IN ('verified', 'pending_verification', 'declared')
                    LIMIT 1
                    """,
                    (scope, real_id, virtual_id),
                ).fetchone()
                if other_row is not None:
                    raise BindingConflictError(
                        "the real group ID is already claimed in this Bot namespace"
                    )

                now = _now()
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO group_bindings (
                            bot_scope, virtual_group_id, real_group_id,
                            group_name, binding_status, verification_source,
                            bound_by_user_id, verified_by, created_at,
                            verified_at, updated_at, version, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, 1, NULL)
                        """,
                        (
                            scope,
                            virtual_id,
                            real_id,
                            group_name,
                            binding_status,
                            source,
                            actor,
                            now,
                            now,
                        ),
                    )
                    old_status = None
                    version = 1
                else:
                    connection.execute(
                        """
                        INSERT INTO group_binding_history (
                            bot_scope, virtual_group_id, real_group_id,
                            binding_status, operation_id, actor_user_id,
                            verification_source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scope,
                            virtual_id,
                            current.real_group_id,
                            current.binding_status,
                            operation_id,
                            actor,
                            current.verification_source,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE group_bindings
                        SET real_group_id = ?, group_name = ?,
                            binding_status = ?, verification_source = ?,
                            bound_by_user_id = ?, verified_by = NULL,
                            verified_at = NULL, updated_at = ?,
                            version = version + 1, last_error = NULL
                        WHERE bot_scope = ? AND virtual_group_id = ?
                        """,
                        (
                            real_id,
                            group_name,
                            binding_status,
                            source,
                            actor,
                            now,
                            scope,
                            virtual_id,
                        ),
                    )
                    old_status = current.binding_status
                    version = current.version + 1

                self._audit_tx(
                    connection,
                    event_type=(
                        "group.declared"
                        if binding_status == GROUP_DECLARED
                        else "group.verification_pending"
                    ),
                    bot_scope=scope,
                    subject_type="group",
                    subject_value=virtual_id,
                    actor_user_id=actor,
                    from_status=old_status,
                    to_status=binding_status,
                    operation_id=operation_id,
                    details={
                        "real_group_digest": _digest(scope, real_id),
                        "verification_source": source,
                        "version": version,
                    },
                )
                row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND virtual_group_id = ?
                    """,
                    (scope, virtual_id),
                ).fetchone()
                result = self._group_from_row(row)
                if result is None:
                    raise QbindStorageError("group binding disappeared after write")
                return result
        except sqlite3.IntegrityError as error:
            raise BindingConflictError("group binding conflicts with an existing identity") from error

    def declare_group(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        real_group_id: object,
        group_name: Optional[str],
        bound_by_user_id: object,
        verification_source: str,
        operation_id: Optional[str] = None,
        allow_replace: bool = False,
    ) -> GroupBinding:
        """Persist a usable mapping while retaining the declared review state."""

        return self.set_group_pending(
            bot_scope=bot_scope,
            virtual_group_id=virtual_group_id,
            real_group_id=real_group_id,
            group_name=group_name,
            bound_by_user_id=bound_by_user_id,
            verification_source=verification_source,
            operation_id=operation_id,
            allow_replace=allow_replace,
            binding_status=GROUP_DECLARED,
        )

    def verify_group(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        verified_by: object,
        verification_source: str,
        operation_id: Optional[str] = None,
        allow_replace: bool = False,
        expected_version: Optional[int] = None,
    ) -> GroupBinding:
        scope = _normalise(bot_scope, "bot_scope")
        virtual_id = _normalise(virtual_group_id, "virtual_group_id")
        verifier = _normalise(verified_by, "verified_by")
        source = _normalise(verification_source, "verification_source")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            current = self._group_from_row(row)
            if current is None or not current.real_group_id:
                raise QbindStorageError("no group binding request is available to verify")
            if current.binding_status == GROUP_VERIFIED:
                return current
            if current.binding_status not in {
                GROUP_PENDING_VERIFICATION,
                GROUP_DECLARED,
            }:
                raise QbindStorageError(
                    f"group binding cannot be verified from status {current.binding_status}"
                )
            other_row = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND real_group_id = ?
                  AND virtual_group_id <> ?
                  AND binding_status IN ('verified', 'pending_verification', 'declared')
                LIMIT 1
                """,
                (scope, current.real_group_id, virtual_id),
            ).fetchone()
            if other_row is not None:
                raise BindingConflictError(
                    "the real group ID is already verified for another virtual group"
                )
            now = _now()
            connection.execute(
                """
                UPDATE group_bindings
                SET binding_status = ?, verification_source = ?, verified_by = ?,
                    verified_at = ?, updated_at = ?, version = version + 1,
                    last_error = NULL
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (
                    GROUP_VERIFIED,
                    source,
                    verifier,
                    now,
                    now,
                    scope,
                    virtual_id,
                ),
            )
            self._audit_tx(
                connection,
                event_type="group.verified",
                bot_scope=scope,
                subject_type="group",
                subject_value=virtual_id,
                actor_user_id=verifier,
                from_status=current.binding_status,
                to_status=GROUP_VERIFIED,
                operation_id=operation_id,
                details={
                    "real_group_digest": _digest(scope, current.real_group_id),
                    "verification_source": source,
                },
            )
            verified = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            result = self._group_from_row(verified)
            if result is None:
                raise QbindStorageError("group verification disappeared after write")
            return result

    def apply_verified_group(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        real_group_id: object,
        group_name: Optional[str],
        bound_by_user_id: object,
        verified_by: object,
        verification_source: str,
        operation_id: Optional[str] = None,
        allow_replace: bool = False,
        expected_version: Optional[int] = None,
    ) -> GroupBinding:
        """Atomically create or replace a verified group mapping.

        The external proof is collected before this method is called; the
        database transition itself is still one transaction, so a rebind never
        exposes a half-written group identity.
        """

        scope = _normalise(bot_scope, "bot_scope")
        virtual_id = _normalise(virtual_group_id, "virtual_group_id")
        real_id = _normalise(real_group_id, "real_group_id")
        actor = _normalise(bound_by_user_id, "bound_by_user_id")
        verifier = _normalise(verified_by, "verified_by")
        source = _normalise(verification_source, "verification_source")
        try:
            with self._transaction() as connection:
                current_row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND virtual_group_id = ?
                    """,
                    (scope, virtual_id),
                ).fetchone()
                current = self._group_from_row(current_row)
                if expected_version is not None and (
                    current is None or current.version != expected_version
                ):
                    raise BindingConflictError(
                        "group binding changed while verification was pending"
                    )
                if current and current.binding_status == GROUP_VERIFIED:
                    if current.real_group_id == real_id:
                        return current
                    if not allow_replace:
                        raise BindingConflictError(
                            "the virtual group already has a verified binding"
                        )

                other_row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND real_group_id = ?
                      AND virtual_group_id <> ?
                      AND binding_status IN ('verified', 'pending_verification', 'declared')
                    LIMIT 1
                    """,
                    (scope, real_id, virtual_id),
                ).fetchone()
                if other_row is not None:
                    raise BindingConflictError(
                        "the real group ID is already claimed in this Bot namespace"
                    )

                now = _now()
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO group_bindings (
                            bot_scope, virtual_group_id, real_group_id,
                            group_name, binding_status, verification_source,
                            bound_by_user_id, verified_by, created_at,
                            verified_at, updated_at, version, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
                        """,
                        (
                            scope,
                            virtual_id,
                            real_id,
                            group_name,
                            GROUP_VERIFIED,
                            source,
                            actor,
                            verifier,
                            now,
                            now,
                            now,
                        ),
                    )
                    old_status = None
                    version = 1
                else:
                    connection.execute(
                        """
                        INSERT INTO group_binding_history (
                            bot_scope, virtual_group_id, real_group_id,
                            binding_status, operation_id, actor_user_id,
                            verification_source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scope,
                            virtual_id,
                            current.real_group_id,
                            current.binding_status,
                            operation_id,
                            actor,
                            current.verification_source,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE group_bindings
                        SET real_group_id = ?, group_name = ?,
                            binding_status = ?, verification_source = ?,
                            bound_by_user_id = ?, verified_by = ?,
                            verified_at = ?, updated_at = ?,
                            version = version + 1, last_error = NULL
                        WHERE bot_scope = ? AND virtual_group_id = ?
                        """,
                        (
                            real_id,
                            group_name,
                            GROUP_VERIFIED,
                            source,
                            actor,
                            verifier,
                            now,
                            now,
                            scope,
                            virtual_id,
                        ),
                    )
                    old_status = current.binding_status
                    version = current.version + 1

                self._audit_tx(
                    connection,
                    event_type="group.verified",
                    bot_scope=scope,
                    subject_type="group",
                    subject_value=virtual_id,
                    actor_user_id=verifier,
                    from_status=old_status,
                    to_status=GROUP_VERIFIED,
                    operation_id=operation_id,
                    details={
                        "real_group_digest": _digest(scope, real_id),
                        "verification_source": source,
                        "version": version,
                    },
                )
                row = connection.execute(
                    """
                    SELECT * FROM group_bindings
                    WHERE bot_scope = ? AND virtual_group_id = ?
                    """,
                    (scope, virtual_id),
                ).fetchone()
                result = self._group_from_row(row)
                if result is None:
                    raise QbindStorageError("group verification disappeared after write")
                return result
        except sqlite3.IntegrityError as error:
            raise BindingConflictError("group binding conflicts with an existing identity") from error

    def mark_group_conflict(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        error: str,
        operation_id: Optional[str] = None,
    ) -> Optional[GroupBinding]:
        scope = _normalise(bot_scope, "bot_scope")
        virtual_id = _normalise(virtual_group_id, "virtual_group_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            current = self._group_from_row(row)
            if current is None:
                return None
            now = _now()
            connection.execute(
                """
                UPDATE group_bindings
                SET binding_status = ?, last_error = ?, updated_at = ?,
                    version = version + 1
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (
                    GROUP_CONFLICT,
                    str(error)[:1000],
                    now,
                    scope,
                    virtual_id,
                ),
            )
            self._audit_tx(
                connection,
                event_type="group.conflict",
                bot_scope=scope,
                subject_type="group",
                subject_value=virtual_id,
                actor_user_id=current.bound_by_user_id,
                from_status=current.binding_status,
                to_status=GROUP_CONFLICT,
                operation_id=operation_id,
                details={"error": str(error)[:300]},
            )
            updated = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            return self._group_from_row(updated)

    def revoke_group(
        self,
        *,
        bot_scope: object,
        virtual_group_id: object,
        actor_user_id: object,
        expected_version: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> GroupBinding:
        scope = _normalise(bot_scope, "bot_scope")
        virtual_id = _normalise(virtual_group_id, "virtual_group_id")
        actor = _normalise(actor_user_id, "actor_user_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            current = self._group_from_row(row)
            if current is None:
                raise QbindStorageError("no group binding exists")
            if expected_version is not None and current.version != expected_version:
                raise BindingConflictError("group binding changed while confirmation was pending")
            if current.binding_status == GROUP_REVOKED:
                return current
            if current.binding_status not in {GROUP_VERIFIED, GROUP_DECLARED}:
                raise QbindStorageError(
                    f"group binding cannot be revoked from status {current.binding_status}"
                )
            now = _now()
            connection.execute(
                """
                INSERT INTO group_binding_history (
                    bot_scope, virtual_group_id, real_group_id,
                    binding_status, operation_id, actor_user_id,
                    verification_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    virtual_id,
                    current.real_group_id,
                    current.binding_status,
                    operation_id,
                    actor,
                    current.verification_source,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE group_bindings
                SET binding_status = ?, updated_at = ?, version = version + 1,
                    last_error = NULL
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (GROUP_REVOKED, now, scope, virtual_id),
            )
            self._audit_tx(
                connection,
                event_type="group.revoked",
                bot_scope=scope,
                subject_type="group",
                subject_value=virtual_id,
                actor_user_id=actor,
                from_status=current.binding_status,
                to_status=GROUP_REVOKED,
                operation_id=operation_id,
                details={
                    "real_group_digest": _digest(scope, current.real_group_id)
                },
            )
            updated = connection.execute(
                """
                SELECT * FROM group_bindings
                WHERE bot_scope = ? AND virtual_group_id = ?
                """,
                (scope, virtual_id),
            ).fetchone()
            result = self._group_from_row(updated)
            if result is None:
                raise QbindStorageError("group revoke disappeared after write")
            return result

    def create_email_challenge(
        self,
        *,
        challenge_id: Optional[str] = None,
        purpose: str,
        bot_scope: object,
        actor_user_id: object,
        target_email_digest: str,
        code_digest: str,
        external_user_id: Optional[object] = None,
        external_group_id: Optional[object] = None,
        candidate_id: Optional[object] = None,
        group_name: Optional[str] = None,
        expected_version: Optional[int] = None,
        ttl_seconds: float = 600.0,
        max_attempts: int = 5,
    ) -> EmailChallenge:
        """Create a one-time email challenge and invalidate older ones.

        The clear-text address and OTP never enter SQLite.  Only their scoped
        digest/code digest are retained, so a database inspection does not
        disclose the target mailbox or the code sent to it.
        """

        challenge_purpose = _normalise(purpose, "purpose")
        scope = _normalise(bot_scope, "bot_scope")
        actor = _normalise(actor_user_id, "actor_user_id")
        email_digest = _normalise(target_email_digest, "target_email_digest")
        otp_digest = _normalise(code_digest, "code_digest")
        user_id = str(external_user_id) if external_user_id is not None else None
        group_id = str(external_group_id) if external_group_id is not None else None
        candidate = str(candidate_id) if candidate_id is not None else None
        challenge_id = _normalise(challenge_id or uuid.uuid4().hex, "challenge_id")
        now = _now()
        expires_at = time.time() + max(30.0, float(ttl_seconds))
        attempts_limit = max(1, int(max_attempts))

        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE email_challenges
                SET status = ?, updated_at = ?
                WHERE purpose = ? AND bot_scope = ? AND actor_user_id = ?
                  AND status = ?
                  AND (external_user_id IS ?)
                  AND (external_group_id IS ?)
                """,
                (
                    EMAIL_CHALLENGE_CANCELED,
                    now,
                    challenge_purpose,
                    scope,
                    actor,
                    EMAIL_CHALLENGE_PENDING,
                    user_id,
                    group_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO email_challenges (
                    challenge_id, purpose, bot_scope, actor_user_id,
                    external_user_id, external_group_id, candidate_id,
                    group_name, expected_version, target_email_digest,
                    code_digest, attempts, max_attempts, status, created_at,
                    expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    challenge_id,
                    challenge_purpose,
                    scope,
                    actor,
                    user_id,
                    group_id,
                    candidate,
                    group_name,
                    expected_version,
                    email_digest,
                    otp_digest,
                    attempts_limit,
                    EMAIL_CHALLENGE_PENDING,
                    now,
                    expires_at,
                    now,
                ),
            )
            self._audit_tx(
                connection,
                event_type="email.challenge_created",
                bot_scope=scope,
                subject_type=challenge_purpose,
                subject_value=group_id or user_id or actor,
                actor_user_id=actor,
                from_status=None,
                to_status=EMAIL_CHALLENGE_PENDING,
                operation_id=challenge_id,
                details={
                    "target_email_digest": email_digest,
                    "expires_at": expires_at,
                    "max_attempts": attempts_limit,
                },
            )
            row = connection.execute(
                "SELECT * FROM email_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            result = self._email_challenge_from_row(row)
            if result is None:
                raise QbindStorageError("email challenge disappeared after write")
            return result

    def cancel_email_challenge(
        self, challenge_id: object, *, reason: str = "canceled"
    ) -> Optional[EmailChallenge]:
        token = _normalise(challenge_id, "challenge_id")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE email_challenges
                SET status = ?, updated_at = ?
                WHERE challenge_id = ? AND status = ?
                """,
                (
                    EMAIL_CHALLENGE_CANCELED,
                    _now(),
                    token,
                    EMAIL_CHALLENGE_PENDING,
                ),
            )
            row = connection.execute(
                "SELECT * FROM email_challenges WHERE challenge_id = ?",
                (token,),
            ).fetchone()
            return self._email_challenge_from_row(row)

    def get_email_challenge(self, challenge_id: object) -> Optional[EmailChallenge]:
        token = _normalise(challenge_id, "challenge_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM email_challenges WHERE challenge_id = ?",
                    (token,),
                ).fetchone()
                return self._email_challenge_from_row(row)
            finally:
                connection.close()

    def find_email_challenge(
        self,
        *,
        purpose: str,
        bot_scope: object,
        actor_user_id: object,
        external_user_id: Optional[object] = None,
        external_group_id: Optional[object] = None,
    ) -> Optional[EmailChallenge]:
        challenge_purpose = _normalise(purpose, "purpose")
        scope = _normalise(bot_scope, "bot_scope")
        actor = _normalise(actor_user_id, "actor_user_id")
        user_id = str(external_user_id) if external_user_id is not None else None
        group_id = str(external_group_id) if external_group_id is not None else None
        self.expire_email_challenges()
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT * FROM email_challenges
                    WHERE purpose = ? AND bot_scope = ? AND actor_user_id = ?
                      AND status = ?
                      AND (external_user_id IS ?)
                      AND (external_group_id IS ?)
                      AND expires_at > ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        challenge_purpose,
                        scope,
                        actor,
                        EMAIL_CHALLENGE_PENDING,
                        user_id,
                        group_id,
                        time.time(),
                    ),
                ).fetchone()
                return self._email_challenge_from_row(row)
            finally:
                connection.close()

    def verify_email_challenge(
        self, challenge_id: object, code_digest: str
    ) -> EmailChallengeAttempt:
        token = _normalise(challenge_id, "challenge_id")
        digest = _normalise(code_digest, "code_digest")
        now_timestamp = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM email_challenges WHERE challenge_id = ?",
                (token,),
            ).fetchone()
            current = self._email_challenge_from_row(row)
            if current is None:
                return EmailChallengeAttempt(False, "not_found", None)
            if current.status != EMAIL_CHALLENGE_PENDING:
                return EmailChallengeAttempt(False, current.status, current)
            if current.expires_at <= now_timestamp:
                connection.execute(
                    """
                    UPDATE email_challenges
                    SET status = ?, updated_at = ?
                    WHERE challenge_id = ? AND status = ?
                    """,
                    (
                        EMAIL_CHALLENGE_EXPIRED,
                        _now(),
                        token,
                        EMAIL_CHALLENGE_PENDING,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM email_challenges WHERE challenge_id = ?",
                    (token,),
                ).fetchone()
                return EmailChallengeAttempt(
                    False,
                    EMAIL_CHALLENGE_EXPIRED,
                    self._email_challenge_from_row(updated),
                )
            if current.attempts >= current.max_attempts:
                connection.execute(
                    "UPDATE email_challenges SET status = ?, updated_at = ? WHERE challenge_id = ?",
                    (EMAIL_CHALLENGE_FAILED, _now(), token),
                )
                updated = connection.execute(
                    "SELECT * FROM email_challenges WHERE challenge_id = ?",
                    (token,),
                ).fetchone()
                return EmailChallengeAttempt(
                    False,
                    "max_attempts",
                    self._email_challenge_from_row(updated),
                )

            accepted = hmac.compare_digest(current.code_digest, digest)
            attempts = current.attempts + 1
            status = (
                EMAIL_CHALLENGE_VERIFIED
                if accepted
                else (
                    EMAIL_CHALLENGE_FAILED
                    if attempts >= current.max_attempts
                    else EMAIL_CHALLENGE_PENDING
                )
            )
            connection.execute(
                """
                UPDATE email_challenges
                SET attempts = ?, status = ?, updated_at = ?
                WHERE challenge_id = ? AND status = ?
                """,
                (
                    attempts,
                    status,
                    _now(),
                    token,
                    EMAIL_CHALLENGE_PENDING,
                ),
            )
            self._audit_tx(
                connection,
                event_type=(
                    "email.challenge_verified"
                    if accepted
                    else "email.challenge_attempt_failed"
                ),
                bot_scope=current.bot_scope,
                subject_type=current.purpose,
                subject_value=(
                    current.external_group_id
                    or current.external_user_id
                    or current.actor_user_id
                ),
                actor_user_id=current.actor_user_id,
                from_status=current.status,
                to_status=status,
                operation_id=token,
                details={"attempts": attempts},
            )
            updated = connection.execute(
                "SELECT * FROM email_challenges WHERE challenge_id = ?",
                (token,),
            ).fetchone()
            return EmailChallengeAttempt(
                accepted,
                "verified" if accepted else ("max_attempts" if status == EMAIL_CHALLENGE_FAILED else "invalid_code"),
                self._email_challenge_from_row(updated),
            )

    def expire_email_challenges(self) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE email_challenges
                SET status = ?, updated_at = ?
                WHERE status = ? AND expires_at <= ?
                """,
                (
                    EMAIL_CHALLENGE_EXPIRED,
                    _now(),
                    EMAIL_CHALLENGE_PENDING,
                    time.time(),
                ),
            )
            return int(cursor.rowcount)

    def create_confirmation(
        self,
        *,
        request_kind: str,
        bot_scope: object,
        actor_user_id: object,
        external_user_id: Optional[object] = None,
        external_group_id: Optional[object] = None,
        candidate_id: Optional[object] = None,
        group_name: Optional[str] = None,
        expected_version: Optional[int] = None,
        ttl_seconds: float = 120.0,
    ) -> ConfirmationRequest:
        scope = _normalise(bot_scope, "bot_scope")
        actor = _normalise(actor_user_id, "actor_user_id")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        request_id = uuid.uuid4().hex
        now = _now()
        import time

        expires_at = time.time() + ttl_seconds
        external_user = (
            _normalise(external_user_id, "external_user_id")
            if external_user_id is not None
            else None
        )
        external_group = (
            _normalise(external_group_id, "external_group_id")
            if external_group_id is not None
            else None
        )
        candidate = (
            _normalise(candidate_id, "candidate_id") if candidate_id is not None else None
        )
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE confirmation_requests
                SET status = ?, result_code = 'replaced', updated_at = ?
                WHERE request_kind = ? AND bot_scope = ? AND actor_user_id = ?
                  AND status = 'pending'
                  AND (external_user_id IS ?)
                  AND (external_group_id IS ?)
                """,
                (
                    REQUEST_CANCELED,
                    now,
                    request_kind,
                    scope,
                    actor,
                    external_user,
                    external_group,
                ),
            )
            connection.execute(
                """
                INSERT INTO confirmation_requests (
                    request_id, request_kind, bot_scope, actor_user_id,
                    external_user_id, external_group_id, candidate_id,
                    group_name, expected_version, status, result_code,
                    last_error, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, ?)
                """,
                (
                    request_id,
                    request_kind,
                    scope,
                    actor,
                    external_user,
                    external_group,
                    candidate,
                    group_name,
                    expected_version,
                    now,
                    expires_at,
                    now,
                ),
            )
            self._audit_tx(
                connection,
                event_type="confirmation.created",
                bot_scope=scope,
                subject_type=request_kind,
                subject_value=external_group or external_user or actor,
                actor_user_id=actor,
                from_status=None,
                to_status=REQUEST_PENDING,
                operation_id=request_id,
                details={
                    "candidate_digest": _digest(scope, candidate),
                    "expires_at": expires_at,
                },
            )
            row = connection.execute(
                "SELECT * FROM confirmation_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            result = self._request_from_row(row)
            if result is None:
                raise QbindStorageError("confirmation request disappeared after write")
            return result

    def get_confirmation(self, request_id: object) -> Optional[ConfirmationRequest]:
        token = _normalise(request_id, "request_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM confirmation_requests WHERE request_id = ?",
                    (token,),
                ).fetchone()
                return self._request_from_row(row)
            finally:
                connection.close()

    def find_confirmation(
        self,
        *,
        request_kind: str,
        bot_scope: object,
        actor_user_id: object,
        external_user_id: Optional[object] = None,
        external_group_id: Optional[object] = None,
        candidate_id: Optional[object] = None,
    ) -> Optional[ConfirmationRequest]:
        scope = _normalise(bot_scope, "bot_scope")
        actor = _normalise(actor_user_id, "actor_user_id")
        import time

        candidate = (
            _normalise(candidate_id, "candidate_id")
            if candidate_id is not None
            else None
        )
        candidate_clause = ""
        query_params: list[object] = [
            request_kind,
            scope,
            actor,
            str(external_user_id) if external_user_id is not None else None,
            str(external_group_id) if external_group_id is not None else None,
        ]
        if candidate is not None:
            candidate_clause = " AND candidate_id = ?"
            query_params.append(candidate)
        query_params.append(time.time())

        self.expire_confirmations()
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    f"""
                    SELECT * FROM confirmation_requests
                    WHERE request_kind = ? AND bot_scope = ? AND actor_user_id = ?
                      AND status = 'pending'
                      AND (external_user_id IS ?)
                      AND (external_group_id IS ?)
                      {candidate_clause}
                      AND expires_at > ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    tuple(query_params),
                ).fetchone()
                return self._request_from_row(row)
            finally:
                connection.close()

    def update_confirmation_error(
        self, request_id: object, error: str, *, backend_unknown: bool = False
    ) -> Optional[ConfirmationRequest]:
        token = _normalise(request_id, "request_id")
        status = REQUEST_BACKEND_UNKNOWN if backend_unknown else REQUEST_PENDING
        with self._transaction() as connection:
            now = _now()
            connection.execute(
                """
                UPDATE confirmation_requests
                SET status = ?, last_error = ?, updated_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (status, str(error)[:1000], now, token),
            )
            row = connection.execute(
                "SELECT * FROM confirmation_requests WHERE request_id = ?",
                (token,),
            ).fetchone()
            return self._request_from_row(row)

    def complete_confirmation(
        self,
        request_id: object,
        *,
        result_code: str,
        status: str = REQUEST_COMPLETED,
        error: Optional[str] = None,
    ) -> Optional[ConfirmationRequest]:
        token = _normalise(request_id, "request_id")
        with self._transaction() as connection:
            now = _now()
            connection.execute(
                """
                UPDATE confirmation_requests
                SET status = ?, result_code = ?, last_error = ?, updated_at = ?
                WHERE request_id = ? AND status IN ('pending', 'backend_unknown')
                """,
                (status, result_code, error, now, token),
            )
            row = connection.execute(
                "SELECT * FROM confirmation_requests WHERE request_id = ?",
                (token,),
            ).fetchone()
            return self._request_from_row(row)

    def cancel_confirmation(self, request_id: object, *, result_code: str = "canceled") -> Optional[ConfirmationRequest]:
        token = _normalise(request_id, "request_id")
        with self._transaction() as connection:
            now = _now()
            connection.execute(
                """
                UPDATE confirmation_requests
                SET status = ?, result_code = ?, updated_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (REQUEST_CANCELED, result_code, now, token),
            )
            row = connection.execute(
                "SELECT * FROM confirmation_requests WHERE request_id = ?",
                (token,),
            ).fetchone()
            return self._request_from_row(row)

    def expire_confirmations(self) -> int:
        import time

        with self._transaction() as connection:
            now = _now()
            cursor = connection.execute(
                """
                UPDATE confirmation_requests
                SET status = ?, result_code = 'expired', updated_at = ?
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (REQUEST_EXPIRED, now, time.time()),
            )
            return int(cursor.rowcount)

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT audit_id, event_type, bot_scope, subject_type,
                           subject_digest, actor_digest, from_status,
                           to_status, operation_id, details_json, created_at
                    FROM audit_log ORDER BY audit_id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

    def migrate_legacy_json(self) -> MigrationResult:
        path = self.legacy_json_path
        if not path.exists():
            return MigrationResult(imported=0, skipped=0, backup_path=None)

        marker = f"legacy_json_migration:{path.resolve()}"
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT meta_value FROM storage_meta WHERE meta_key = ?",
                    (marker,),
                ).fetchone()
                if row is not None:
                    return MigrationResult(
                        imported=0,
                        skipped=0,
                        backup_path=row["meta_value"] or None,
                    )
            finally:
                connection.close()

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LegacyMigrationError(f"unable to read legacy binds.json: {error}") from error

        if isinstance(payload, dict) and isinstance(payload.get("bindings"), dict):
            payload = payload["bindings"]
        if not isinstance(payload, dict):
            raise LegacyMigrationError("legacy binds.json must contain an object mapping")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.migration-{timestamp}.bak")
        try:
            shutil.copy2(path, backup)
        except OSError as error:
            raise LegacyMigrationError(f"unable to create legacy rollback backup: {error}") from error

        imported = 0
        skipped = 0
        legacy_scope = "legacy"
        try:
            with self._transaction() as connection:
                for external, canonical in payload.items():
                    if not isinstance(external, str) or not isinstance(canonical, str):
                        skipped += 1
                        continue
                    external = external.strip()
                    canonical = canonical.strip()
                    if not external or not canonical or external == canonical:
                        # The old implementation stored a reverse canonical ->
                        # canonical entry as well.  It is not a separate
                        # external identity and must not create a self-bind.
                        skipped += 1
                        continue
                    current = connection.execute(
                        """
                        SELECT 1 FROM personal_bindings
                        WHERE bot_scope = ? AND external_user_id = ?
                        """,
                        (legacy_scope, external),
                    ).fetchone()
                    if current is not None:
                        skipped += 1
                        continue
                    canonical_claim = connection.execute(
                        """
                        SELECT 1 FROM personal_bindings
                        WHERE bot_scope = ? AND canonical_user_id = ?
                          AND binding_status IN ('declared', 'verified', 'unbind_pending')
                        LIMIT 1
                        """,
                        (legacy_scope, canonical),
                    ).fetchone()
                    if canonical_claim is not None:
                        skipped += 1
                        self._audit_tx(
                            connection,
                            event_type="legacy.migration_conflict",
                            bot_scope=legacy_scope,
                            subject_type="user",
                            subject_value=external,
                            actor_user_id=None,
                            from_status=None,
                            to_status=None,
                            operation_id=marker,
                            details={"canonical_digest": _digest(legacy_scope, canonical)},
                        )
                        continue
                    now = _now()
                    connection.execute(
                        """
                        INSERT INTO personal_bindings (
                            bot_scope, external_user_id, canonical_user_id,
                            binding_status, verification_source,
                            bound_by_user_id, verified_by, created_at,
                            verified_at, updated_at, version, last_error
                        ) VALUES (?, ?, ?, ?, 'legacy_json', ?, NULL, ?, NULL, ?, 1, NULL)
                        """,
                        (
                            legacy_scope,
                            external,
                            canonical,
                            PERSONAL_DECLARED,
                            external,
                            now,
                            now,
                        ),
                    )
                    imported += 1
                    self._audit_tx(
                        connection,
                        event_type="legacy.migrated",
                        bot_scope=legacy_scope,
                        subject_type="user",
                        subject_value=external,
                        actor_user_id=None,
                        from_status=None,
                        to_status=PERSONAL_DECLARED,
                        operation_id=marker,
                        details={"canonical_digest": _digest(legacy_scope, canonical)},
                    )
                connection.execute(
                    """
                    INSERT INTO storage_meta (meta_key, meta_value)
                    VALUES (?, ?)
                    """,
                    (marker, str(backup)),
                )
        except sqlite3.IntegrityError as error:
            raise LegacyMigrationError(f"legacy migration encountered a storage conflict: {error}") from error

        return MigrationResult(imported=imported, skipped=skipped, backup_path=str(backup))


__all__ = [
    "BindingConflictError",
    "ConfirmationRequest",
    "EmailChallenge",
    "EmailChallengeAttempt",
    "EMAIL_CHALLENGE_CANCELED",
    "EMAIL_CHALLENGE_EXPIRED",
    "EMAIL_CHALLENGE_FAILED",
    "EMAIL_CHALLENGE_PENDING",
    "EMAIL_CHALLENGE_VERIFIED",
    "GROUP_CONFLICT",
    "GROUP_PENDING_VERIFICATION",
    "GROUP_REVOKED",
    "GROUP_VERIFIED",
    "GroupBinding",
    "GroupUserMembership",
    "GROUP_DECLARED",
    "LegacyMigrationError",
    "MigrationResult",
    "PERSONAL_DECLARED",
    "PERSONAL_REVOKED",
    "PERSONAL_UNBIND_PENDING",
    "PERSONAL_VERIFIED",
    "MEMBERSHIP_REVOKED",
    "MEMBERSHIP_VERIFIED",
    "QbindStorageError",
    "QbindStore",
    "REQUEST_BACKEND_UNKNOWN",
    "REQUEST_CANCELED",
    "REQUEST_COMPLETED",
    "REQUEST_EXPIRED",
    "REQUEST_FAILED",
    "REQUEST_PENDING",
    "PersonalBinding",
]
