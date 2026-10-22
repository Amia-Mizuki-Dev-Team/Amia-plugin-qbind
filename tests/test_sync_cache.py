"""Coverage for the SQLite sync ledger that fronts the GSK type=5 writes.

Gensokyo's type=5 endpoint is not idempotent: replaying the identical write
answers ``已存在`` and a virtual ID without a session row answers ``不存在``.
The production path therefore records every accepted write in
``gsk_sync_state`` and skips the HTTP call while the recorded
``(canonical_id, local_version)`` still matches the live binding row.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from test_qbind import qbind
from src.plugins.qbind import group
from src.plugins.qbind.gensokyo import (
    GensokyoMappingError,
    GensokyoMappingExistsError,
    GensokyoMappingMissingError,
)

PERSONAL_KIND = qbind.storage.MAPPING_PERSONAL
GROUP_KIND = qbind.storage.MAPPING_GROUP
PERSONAL_ROW = ("bot-a", "user-a")
GROUP_ROW = ("bot-a", "group-a")


def run(coro):
    return asyncio.run(coro)


class RecordingMapping:
    """Record every type=5 write and fail on demand.

    Installed through :func:`qbind.set_mapping_client`, which also bridges the
    group half into ``group._group_mapping_client``: the pair path therefore
    hits this single fake for both writers.
    """

    def __init__(self):
        self.set_error: Exception | None = None
        self.clear_error: Exception | None = None
        self.set_calls: list[tuple[str, str]] = []
        self.clear_calls: list[str] = []
        self.group_set_calls: list[tuple[str, str]] = []
        self.group_clear_calls: list[str] = []

    async def set_personal_mapping(self, virtual_user_id: object, real_qq: object):
        self.set_calls.append((str(virtual_user_id), str(real_qq)))
        if self.set_error is not None:
            raise self.set_error

    async def clear_personal_mapping(self, virtual_user_id: object):
        self.clear_calls.append(str(virtual_user_id))
        if self.clear_error is not None:
            raise self.clear_error

    async def set_group_mapping(self, virtual_group_id: object, real_group_id: object):
        self.group_set_calls.append((str(virtual_group_id), str(real_group_id)))
        if self.set_error is not None:
            raise self.set_error

    async def clear_group_mapping(self, virtual_group_id: object):
        self.group_clear_calls.append(str(virtual_group_id))
        if self.clear_error is not None:
            raise self.clear_error


class SyncStateBase(unittest.TestCase):
    """Sandbox mirroring ``AcceptanceBase`` plus the group mapping client."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-sync-state-")
        root = Path(self.temporary.name)
        self.original_markdown = qbind.USE_MARKDOWN
        qbind.USE_MARKDOWN = False
        self.original_mapping = qbind._mapping_client
        self.original_group_mapping = group.get_group_mapping_client()
        self.mapping = RecordingMapping()
        qbind.configure_storage(root / "qbind.sqlite3", root / "binds.json")
        qbind.set_mapping_client(self.mapping)
        qbind._last_cmd_time.clear()
        self.store = qbind.get_store()

    def tearDown(self):
        qbind.USE_MARKDOWN = self.original_markdown
        qbind.set_mapping_client(self.original_mapping)
        group.set_group_mapping_client(self.original_group_mapping)
        self.temporary.cleanup()

    def seed_verified_personal(self):
        self.store.declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="user-a",
            verified_by="trusted-reviewer",
            verification_source="test:review",
        )
        return self.store.get_personal(*PERSONAL_ROW)

    def seed_verified_group(self):
        return self.store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verified_by="trusted-reviewer",
            verification_source="test",
        )

    def record_personal_sync(self, personal) -> None:
        self.store.record_gsk_sync(
            bot_scope="bot-a",
            external_id="user-a",
            mapping_kind=PERSONAL_KIND,
            canonical_id=personal.canonical_user_id,
            local_version=personal.version,
        )

    def record_group_sync(self, group_binding) -> None:
        self.store.record_gsk_sync(
            bot_scope="bot-a",
            external_id="group-a",
            mapping_kind=GROUP_KIND,
            canonical_id=group_binding.real_group_id,
            local_version=group_binding.version,
        )

    def bump_personal_version(self) -> None:
        """Simulate a later local transition of the personal row."""

        with self.store._transaction() as connection:
            connection.execute(
                "UPDATE personal_bindings SET version = version + 1 "
                "WHERE bot_scope = ? AND external_user_id = ?",
                PERSONAL_ROW,
            )


class TestPersonalSyncState(SyncStateBase):
    def test_personal_sync_writes_once_then_skips(self):
        self.seed_verified_personal()

        first = run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))
        second = run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])
        self.assertEqual(self.mapping.group_set_calls, [])
        self.assertEqual(second.version, first.version)
        state = self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND)
        self.assertIsNotNone(state)
        self.assertEqual(state.canonical_id, "1234567")
        self.assertEqual(state.local_version, first.version)

    def test_personal_version_advance_forces_rewrite(self):
        self.seed_verified_personal()
        run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))
        self.assertEqual(len(self.mapping.set_calls), 1)

        self.bump_personal_version()
        run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")] * 2)
        state = self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND)
        self.assertEqual(state.local_version, self.store.get_personal(*PERSONAL_ROW).version)

    def test_exists_is_absorbed_and_recorded(self):
        self.seed_verified_personal()
        # Gensokyo answers ``<value> :已存在``; the reported value is our own
        # canonical QQ, so the requested target state is already reached.
        self.mapping.set_error = GensokyoMappingExistsError(
            "Gensokyo type=5 target mapping already exists", remote_value="1234567"
        )

        record = run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])
        self.assertEqual(record.binding_status, qbind.storage.PERSONAL_VERIFIED)
        state = self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND)
        self.assertIsNotNone(state)
        self.assertEqual(state.canonical_id, "1234567")
        self.assertEqual(state.local_version, record.version)

    def test_exists_for_another_value_fails_closed(self):
        """A value already mapped to someone else is a conflict, not our target."""

        self.seed_verified_personal()
        self.mapping.set_error = GensokyoMappingExistsError(
            "Gensokyo type=5 target mapping already exists", remote_value="7654321"
        )

        with self.assertRaises(GensokyoMappingExistsError):
            run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])
        self.assertIsNone(self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND))

    def test_missing_propagates_and_is_not_recorded(self):
        self.seed_verified_personal()
        self.mapping.set_error = GensokyoMappingMissingError(
            "Gensokyo type=5 virtual identity row is missing", virtual_id="1"
        )

        with self.assertRaises(GensokyoMappingMissingError):
            run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])
        self.assertIsNone(self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND))

    def test_unknown_failure_does_not_record_a_write(self):
        self.seed_verified_personal()
        self.mapping.set_error = GensokyoMappingError("remote unavailable")

        with self.assertRaises(GensokyoMappingError):
            run(qbind.sync_verified_personal_mapping("bot-a", "user-a"))

        self.assertIsNone(self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND))


class TestPairSyncState(SyncStateBase):
    def test_pair_writes_only_the_missing_group(self):
        personal = self.seed_verified_personal()
        self.record_personal_sync(personal)
        self.seed_verified_group()

        run(qbind.sync_verified_pair("bot-a", "user-a", "group-a"))
        run(qbind.sync_verified_pair("bot-a", "user-a", "group-a"))

        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [("group-a", "9876543")])
        state = self.store.get_gsk_sync("bot-a", "group-a", GROUP_KIND)
        self.assertIsNotNone(state)
        self.assertEqual(state.canonical_id, "9876543")

    def test_pair_repeat_writes_nothing(self):
        self.seed_verified_personal()
        self.seed_verified_group()

        first = run(qbind.sync_verified_pair("bot-a", "user-a", "group-a"))
        self.assertEqual(len(self.mapping.set_calls), 1)
        self.assertEqual(len(self.mapping.group_set_calls), 1)

        second = run(qbind.sync_verified_pair("bot-a", "user-a", "group-a"))

        self.assertEqual(len(self.mapping.set_calls), 1)
        self.assertEqual(len(self.mapping.group_set_calls), 1)
        self.assertEqual(second.personal.version, first.personal.version)
        self.assertEqual(second.group.version, first.group.version)


class TestSyncStateInvalidation(SyncStateBase):
    def test_unbind_and_revoke_clear_sync_state(self):
        personal = self.seed_verified_personal()
        self.record_personal_sync(personal)
        group_binding = self.seed_verified_group()
        self.record_group_sync(group_binding)
        self.assertIsNotNone(self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND))
        self.assertIsNotNone(self.store.get_gsk_sync("bot-a", "group-a", GROUP_KIND))

        self.store.mark_personal_unbind_pending(
            bot_scope="bot-a", external_user_id="user-a"
        )
        self.store.complete_personal_unbind(
            bot_scope="bot-a", external_user_id="user-a"
        )

        self.assertIsNone(self.store.get_gsk_sync("bot-a", "user-a", PERSONAL_KIND))
        # A different subject keeps its own record across the personal unbind.
        self.assertIsNotNone(self.store.get_gsk_sync("bot-a", "group-a", GROUP_KIND))

        self.store.revoke_group(
            bot_scope="bot-a", virtual_group_id="group-a", actor_user_id="admin-a"
        )

        self.assertIsNone(self.store.get_gsk_sync("bot-a", "group-a", GROUP_KIND))


if __name__ == "__main__":
    unittest.main()
