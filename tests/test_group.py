import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from test_qbind import qbind
from src.plugins.qbind import group


class TestGroupBinding(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qgroupbind-test-")
        qbind.configure_storage(
            Path(self.temporary.name) / "qbind.sqlite3",
            Path(self.temporary.name) / "binds.json",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_group_mapping_requires_verification_and_is_reusable(self):
        store = qbind.get_store()
        pending = store.set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test Group",
            bound_by_user_id="admin-1",
            verification_source="manual-declaration",
        )
        self.assertEqual(pending.binding_status, "pending_verification")
        self.assertIsNone(qbind.get_real_group("virtual-group-1", "bot-a"))
        self.assertFalse(qbind.is_group_bound("virtual-group-1", "bot-a"))

        verified = store.verify_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            verified_by="trusted-reviewer",
            verification_source="external-proof",
        )
        self.assertEqual(verified.binding_status, "verified")
        self.assertEqual(qbind.get_real_group("virtual-group-1", "bot-a"), "9876543")
        self.assertTrue(qbind.is_group_bound("virtual-group-1", "bot-a"))

        rebound = store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876544",
            group_name="Test Group 2",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="external-proof-2",
            allow_replace=True,
            expected_version=verified.version,
        )
        self.assertEqual(rebound.binding_status, "verified")
        self.assertEqual(qbind.get_real_group("virtual-group-1", "bot-a"), "9876544")

        revoked = store.revoke_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            actor_user_id="admin-1",
            expected_version=rebound.version,
        )
        self.assertEqual(revoked.binding_status, "revoked")
        self.assertIsNone(qbind.get_real_group("virtual-group-1", "bot-a"))

    def test_same_real_group_cannot_be_claimed_twice_in_one_bot_scope(self):
        store = qbind.get_store()
        store.set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name=None,
            bound_by_user_id="admin-1",
            verification_source="test",
        )
        store.verify_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            verified_by="trusted-reviewer",
            verification_source="test:review",
        )
        with self.assertRaises(qbind.storage.BindingConflictError):
            store.set_group_pending(
                bot_scope="bot-a",
                virtual_group_id="virtual-group-2",
                real_group_id="9876543",
                group_name=None,
                bound_by_user_id="admin-2",
                verification_source="test",
            )

    def test_trusted_adapter_promotes_only_the_matching_group_request(self):
        class VerifiedAdapter:
            async def verify(self, context, bot):
                return group.GroupVerificationResult(
                    status="verified", source="test:external-proof"
                )

        class FakeEvent:
            self_id = "bot-a"
            group_id = "virtual-group-3"
            sender = SimpleNamespace(role="admin")

            def get_user_id(self):
                return "admin-1"

        class FakeBot:
            pass

        store = qbind.get_store()
        request = store.create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-3",
            candidate_id="9876545",
            expected_version=None,
            ttl_seconds=300,
        )
        previous = group.get_group_verifier()
        group.set_group_verifier(VerifiedAdapter())
        try:
            message = asyncio.run(
                group._confirm_group_bind(FakeEvent(), FakeBot(), request)
            )
        finally:
            group.set_group_verifier(previous)
        self.assertIn("已经完成", message)
        self.assertEqual(qbind.get_real_group("virtual-group-3", "bot-a"), "9876545")

    def test_pending_rebind_keeps_old_verified_mapping(self):
        store = qbind.get_store()
        store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-4",
            real_group_id="9876546",
            group_name="Original",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="test:proof",
        )
        current = store.get_group("bot-a", "virtual-group-4")
        request = store.create_confirmation(
            request_kind="group_rebind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-4",
            candidate_id="9876547",
            group_name="Replacement",
            expected_version=current.version,
            ttl_seconds=300,
        )

        class PendingAdapter:
            async def verify(self, context, bot):
                return group.GroupVerificationResult(
                    status="pending", source="test:needs-review", reason="manual review"
                )

        class FakeEvent:
            self_id = "bot-a"
            group_id = "virtual-group-4"
            sender = SimpleNamespace(role="admin")

            def get_user_id(self):
                return "admin-1"

        previous = group.get_group_verifier()
        group.set_group_verifier(PendingAdapter())
        try:
            message = asyncio.run(
                group._confirm_group_bind(FakeEvent(), object(), request)
            )
        finally:
            group.set_group_verifier(previous)
        self.assertIn("待可信来源审核", message)
        self.assertEqual(qbind.get_real_group("virtual-group-4", "bot-a"), "9876546")
        pending = store.get_confirmation(request.request_id)
        self.assertEqual(pending.status, "pending")


if __name__ == "__main__":
    unittest.main()
