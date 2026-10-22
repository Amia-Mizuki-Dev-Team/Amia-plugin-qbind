import asyncio
import atexit
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import nonebot

nonebot.init()

# Importing the plugin initialises its default store as an import side effect,
# and that store creates/migrates ``<plugin>/data/qbind.sqlite3``.  Redirect
# the import-time store to a throwaway file so the suite never opens, writes or
# migrates the real database; each test still selects its own temp directory.
_IMPORT_STORE_ROOT = Path(tempfile.mkdtemp(prefix="qbind-import-store-"))
atexit.register(shutil.rmtree, _IMPORT_STORE_ROOT, ignore_errors=True)
os.environ["QBIND_DB_PATH"] = str(_IMPORT_STORE_ROOT / "qbind.sqlite3")

# ``failure_log`` freezes ``LOG_DIR`` when it is imported -- which happens while
# the plugin package is imported below -- and the plugin attaches an ERROR
# handler at that same moment.  Point the import-time data directory at the same
# throwaway root, otherwise every *expected* failure in this suite appends to
# ``<plugin>/data/qbind_log/failures.jsonl``.  Must precede ``load_qbind``.
os.environ["QBIND_DATA_DIR"] = str(_IMPORT_STORE_ROOT)

from standalone_loader import load_core, load_qbind


core = load_core()
_real_require = nonebot.require
nonebot.require = lambda name: core if name == "amia_core" else _real_require(name)
qbind = load_qbind()

from src.plugins.amia_core.identity import UserIdentityKey
from src.plugins.qbind.mail import QbindEmailService


# The historical storage/adapter tests exercise the pre-email declaration
# contract directly.  New email-specific tests inject a required service; all
# other tests use this explicit legacy-mode service instead of inheriting the
# developer's local .env setting.
qbind.set_email_service(
    QbindEmailService(replace(qbind._email_service.config, required=False))
)


class TestQbind(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-test-")
        self.db_path = Path(self.temporary.name) / "qbind.sqlite3"
        self.legacy_path = Path(self.temporary.name) / "binds.json"
        qbind.configure_storage(self.db_path, self.legacy_path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_declaration_is_not_canonical_until_trusted_verification(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="virtual-user-1",
            canonical_user_id="1234567",
            bound_by_user_id="virtual-user-1",
            verification_source="test:type5",
        )

        self.assertEqual(
            qbind.get_real_qq("virtual-user-1", "bot-a"), "1234567"
        )
        self.assertIsNone(qbind.get_verified_qq("virtual-user-1", "bot-a"))
        self.assertEqual(qbind.get_binding_status("virtual-user-1", "bot-a"), "declared")

        resolver = qbind.QbindIdentityResolver()
        key = UserIdentityKey(self_id="bot-a", user_id="virtual-user-1")
        unresolved = asyncio.run(resolver.resolve_identity(key))
        self.assertIsNone(unresolved.canonical_user_id)

        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="virtual-user-1",
            verified_by="trusted-admin",
            verification_source="test:review",
        )
        resolved = asyncio.run(resolver.resolve_identity(key))
        self.assertEqual(resolved.canonical_user_id, "1234567")
        self.assertEqual(
            asyncio.run(resolver.resolve_user_identity(key)).verification_status,
            "verified",
        )

    def test_bot_scope_is_never_inferred_by_global_scan(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="same-virtual-id",
            canonical_user_id="1234567",
            bound_by_user_id="same-virtual-id",
            verification_source="test",
        )
        self.assertEqual(qbind.get_real_qq("same-virtual-id", "bot-a"), "1234567")
        self.assertIsNone(qbind.get_real_qq("same-virtual-id", "bot-b"))
        self.assertEqual(qbind.get_binding_status("same-virtual-id", "bot-b"), "unbound")

    def test_legacy_json_is_migrated_as_declared_and_backed_up(self):
        self.legacy_path.write_text(
            json.dumps(
                {
                    "virtual-user-1": "1234567",
                    "1234567": "1234567",
                }
            ),
            encoding="utf-8",
        )
        qbind.configure_storage(self.db_path, self.legacy_path)
        record = qbind.get_store().get_personal("legacy", "virtual-user-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.binding_status, "declared")
        self.assertIsNone(qbind.get_real_qq("virtual-user-1", "legacy", verified_only=True))
        self.assertTrue(list(self.legacy_path.parent.glob("binds.json.migration-*.bak")))

    def test_context_resolution_keeps_group_and_user_status_separate(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-1",
            canonical_user_id="1234567",
            bound_by_user_id="user-1",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="user-1",
            verified_by="trusted-admin",
            verification_source="test:review",
        )
        qbind.get_store().set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="user-1",
            verification_source="test:manual",
        )
        context = asyncio.run(
            qbind.resolve_context_identity(
                self_id="bot-a", user_id="user-1", group_id="virtual-group-1"
            )
        )
        self.assertEqual(context["canonical_user_id"], "1234567")
        self.assertEqual(context["user_verification_status"], "verified")
        self.assertIsNone(context["canonical_group_id"])
        self.assertEqual(context["group_verification_status"], "pending_verification")

    def test_membership_archive_requires_both_verified_bindings(self):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope="bot-a",
            external_user_id="user-1",
            canonical_user_id="1234567",
            bound_by_user_id="user-1",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="user-1",
            verified_by="trusted-admin",
            verification_source="test:review",
        )
        store.set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test Group",
            bound_by_user_id="user-1",
            verification_source="test:manual",
        )

        self.assertIsNone(
            qbind.record_verified_membership(
                self_id="bot-a",
                group_id="virtual-group-1",
                user_id="user-1",
                group_name="Test Group",
            )
        )

        store.verify_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            verified_by="trusted-admin",
            verification_source="test:review",
        )
        membership = qbind.record_verified_membership(
            self_id="bot-a",
            group_id="virtual-group-1",
            user_id="user-1",
            group_name="Test Group",
        )
        self.assertIsNotNone(membership)
        self.assertEqual(membership.real_group_id, "9876543")
        self.assertEqual(membership.real_user_id, "1234567")
        self.assertEqual(
            [row.real_user_id for row in qbind.get_group_members("9876543", "bot-a")],
            ["1234567"],
        )
        self.assertEqual(
            [row.real_group_id for row in qbind.get_user_groups("1234567", "bot-a")],
            ["9876543"],
        )


if __name__ == "__main__":
    unittest.main()
