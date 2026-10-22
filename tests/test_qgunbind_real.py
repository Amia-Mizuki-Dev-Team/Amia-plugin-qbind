"""Coverage for revoking group bindings by real group number (``qgunbind <id>``).

The preview/execute pair added in commit 00e4ce9 depends on
``QbindStore.list_groups_by_real`` to see *every* active claim on one real
group id, because ``get_group_by_real`` only reports the newest one.
``conflict``/``pending_verification`` rows still occupy the number but
``revoke_group`` refuses to revoke them (``storage.py:2563``); the command
must therefore report them as skipped instead of failing the whole run.
That skip is the current expected behaviour, not a defect.
"""

from __future__ import annotations

import unittest

from test_qbind import qbind
from test_acceptance_matrix import (
    AcceptanceBase,
    FakeEvent,
    PlainArg,
    capture_handler,
    run,
)
from src.plugins.qbind import group


REAL_ID = "389876336"
STALE_REAL_ID = "999999999"
SCOPE = "bot-a"
OPERATOR = "admin-1"
VERIFIED_VIRTUAL = "virtual-group-1"
CONFLICT_VIRTUAL = "virtual-group-2"


class RealRevokeBase(AcceptanceBase):
    """Temporary-DB base with one verified and one conflict claim on REAL_ID."""

    def store(self):
        return qbind.get_store()

    def seed_conflict_row(self, virtual_group_id: str = CONFLICT_VIRTUAL):
        # A conflict row still claims a real id but is invisible to
        # get_group_by_real.  Stage it through pending on an unrelated number
        # first, so the active-claim unique index never sees two active rows.
        store = self.store()
        store.set_group_pending(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            real_group_id=STALE_REAL_ID,
            group_name="stale",
            bound_by_user_id=OPERATOR,
            verification_source="test:manual",
        )
        store.mark_group_conflict(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            error="remote group already mapped elsewhere",
        )
        store.set_group_pending(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            real_group_id=REAL_ID,
            group_name="stale",
            bound_by_user_id=OPERATOR,
            verification_source="test:manual",
        )
        return store.mark_group_conflict(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            error="remote conflict on the shared real group id",
        )

    def seed_verified_row(self, virtual_group_id: str = VERIFIED_VIRTUAL):
        store = self.store()
        store.set_group_pending(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            real_group_id=REAL_ID,
            group_name="verified",
            bound_by_user_id=OPERATOR,
            verification_source="test:manual",
        )
        return store.verify_group(
            bot_scope=SCOPE,
            virtual_group_id=virtual_group_id,
            verified_by=OPERATOR,
            verification_source="test:review",
        )

    def seed_shared_real(self):
        return self.seed_conflict_row(), self.seed_verified_row()

    def operator_event(self, user_id: str = OPERATOR, role: str = "owner"):
        return FakeEvent(user_id, group_id=VERIFIED_VIRTUAL, role=role)

    def invoke(self, event, value: str):
        return run(
            capture_handler(
                group.qgunbind, group.qgunbind_handler, event, PlainArg(value)
            )
        )

    def markdown_data(self, message):
        self.assertEqual(message[0].type, "markdown")
        return message[0].data["data"]


class TestListGroupsByReal(RealRevokeBase):
    def test_lists_verified_and_conflict_rows_newest_version_first(self):
        conflict, verified = self.seed_shared_real()
        rows = self.store().list_groups_by_real(SCOPE, REAL_ID)
        self.assertEqual(
            [(row.virtual_group_id, row.binding_status) for row in rows],
            [
                (CONFLICT_VIRTUAL, qbind.storage.GROUP_CONFLICT),
                (VERIFIED_VIRTUAL, qbind.storage.GROUP_VERIFIED),
            ],
        )
        # The conflict id sorts *after* the verified one, so this order proves
        # the version DESC key wins over the virtual_group_id ASC tiebreaker.
        self.assertGreater(conflict.version, verified.version)
        self.assertEqual(
            [row.version for row in rows],
            [conflict.version, verified.version],
        )

    def test_unknown_real_group_id_returns_empty_list(self):
        self.seed_shared_real()
        self.assertEqual(self.store().list_groups_by_real(SCOPE, "123456789"), [])

    def test_other_bot_scope_never_sees_the_claims(self):
        self.seed_shared_real()
        self.assertEqual(self.store().list_groups_by_real("bot-b", REAL_ID), [])


class TestRealRevokePreview(RealRevokeBase):
    def test_preview_lists_both_occupying_groups_with_confirm_and_cancel(self):
        self.seed_shared_real()
        qbind.USE_MARKDOWN = True
        message = self.invoke(self.operator_event(), REAL_ID)
        data = self.markdown_data(message)
        content = data["markdown"]["content"]
        self.assertIn(REAL_ID, content)
        self.assertIn(VERIFIED_VIRTUAL, content)
        self.assertIn(CONFLICT_VIRTUAL, content)
        rows = data["keyboard"]["content"]["rows"]
        buttons = [row["buttons"][0] for row in rows]
        self.assertEqual(
            [
                (button["render_data"]["label"], button["action"]["data"])
                for button in buttons
            ],
            [
                ("确认撤销", f"qgunbind confirm {REAL_ID}"),
                ("取消", "qgstatus"),
            ],
        )

    def test_preview_is_read_only_for_local_rows_and_remote_mapping(self):
        conflict, verified = self.seed_shared_real()
        self.invoke(self.operator_event(), REAL_ID)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.clear_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [])
        self.assertEqual(self.mapping.group_clear_calls, [])
        verified_after = self.store().get_group(SCOPE, VERIFIED_VIRTUAL)
        self.assertEqual(verified_after.binding_status, qbind.storage.GROUP_VERIFIED)
        self.assertEqual(verified_after.version, verified.version)
        conflict_after = self.store().get_group(SCOPE, CONFLICT_VIRTUAL)
        self.assertEqual(conflict_after.binding_status, qbind.storage.GROUP_CONFLICT)
        self.assertEqual(conflict_after.version, conflict.version)


class TestRealRevokeExecute(RealRevokeBase):
    def test_confirm_revokes_verified_row_and_clears_its_gsk_sync(self):
        _, verified = self.seed_shared_real()
        self.store().record_gsk_sync(
            bot_scope=SCOPE,
            external_id=VERIFIED_VIRTUAL,
            mapping_kind=qbind.storage.MAPPING_GROUP,
            canonical_id=REAL_ID,
            local_version=verified.version,
        )
        self.assertIsNotNone(
            self.store().get_gsk_sync(
                SCOPE, VERIFIED_VIRTUAL, qbind.storage.MAPPING_GROUP
            )
        )

        message = self.invoke(self.operator_event(), f"confirm {REAL_ID}")

        self.assertIn("已撤销 1 条，跳过 1 条（状态已变化）", message)
        self.assertIsNone(self.store().get_group_by_real(SCOPE, REAL_ID))
        revoked = self.store().get_group(SCOPE, VERIFIED_VIRTUAL)
        self.assertEqual(revoked.binding_status, qbind.storage.GROUP_REVOKED)
        self.assertEqual(revoked.version, verified.version + 1)
        self.assertIsNone(
            self.store().get_gsk_sync(
                SCOPE, VERIFIED_VIRTUAL, qbind.storage.MAPPING_GROUP
            )
        )

    def test_confirm_keeps_conflict_row_and_does_not_clear_remote_mapping(self):
        conflict, _ = self.seed_shared_real()
        self.invoke(self.operator_event(), f"confirm {REAL_ID}")
        survivor = self.store().get_group(SCOPE, CONFLICT_VIRTUAL)
        self.assertEqual(survivor.binding_status, qbind.storage.GROUP_CONFLICT)
        self.assertEqual(survivor.real_group_id, REAL_ID)
        self.assertEqual(survivor.version, conflict.version)
        self.assertEqual(self.mapping.group_clear_calls, [])
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.clear_calls, [])


class TestRealRevokeEmpty(RealRevokeBase):
    def test_unknown_real_group_id_reports_no_occupying_binding(self):
        for value in (REAL_ID, f"confirm {REAL_ID}"):
            with self.subTest(value=value):
                message = self.invoke(self.operator_event(), value)
                self.assertIn("没有找到占用该真实群号的群绑定", message)


class TestRealRevokePermissions(RealRevokeBase):
    def test_member_is_rejected_by_both_forms_without_touching_rows(self):
        conflict, verified = self.seed_shared_real()
        for value in (REAL_ID, f"confirm {REAL_ID}"):
            with self.subTest(value=value):
                message = self.invoke(self.operator_event(role="member"), value)
                self.assertIn("没有群解绑权限", message)
        self.assertEqual(
            self.store().get_group(SCOPE, VERIFIED_VIRTUAL).version, verified.version
        )
        self.assertEqual(
            self.store().get_group(SCOPE, CONFLICT_VIRTUAL).version, conflict.version
        )
        self.assertEqual(self.mapping.group_clear_calls, [])


class TestRealRevokeFormat(RealRevokeBase):
    def test_four_digit_argument_is_rejected_with_group_number_hint(self):
        self.seed_shared_real()
        message = self.invoke(self.operator_event(), "1234")
        self.assertIn("真实群号格式不正确", message)
        self.assertIn("5-12", message)
        self.assertIn(f"qgunbind {REAL_ID}", message)
        self.assertNotIn("已撤销", message)
        self.assertEqual(
            self.store().get_group(SCOPE, VERIFIED_VIRTUAL).binding_status,
            qbind.storage.GROUP_VERIFIED,
        )

    def test_confirm_with_short_group_number_is_rejected_before_revoking(self):
        self.seed_shared_real()
        message = self.invoke(self.operator_event(), "confirm 123")
        self.assertIn("qgunbind confirm <5-12 位群号>", message)
        self.assertNotIn("已撤销", message)
        self.assertEqual(
            self.store().get_group(SCOPE, VERIFIED_VIRTUAL).binding_status,
            qbind.storage.GROUP_VERIFIED,
        )


class TestQgunbindLegacyPaths(RealRevokeBase):
    def test_empty_argument_still_requests_local_unbind_confirmation(self):
        self.seed_verified_row()
        message = self.invoke(self.operator_event(), "")
        self.assertIn("确认群解绑", message)
        request = self.store().find_confirmation(
            request_kind="group_unbind",
            bot_scope=SCOPE,
            actor_user_id=OPERATOR,
            external_group_id=VERIFIED_VIRTUAL,
        )
        self.assertIsNotNone(request)
        self.assertEqual(
            self.store().get_group(SCOPE, VERIFIED_VIRTUAL).binding_status,
            qbind.storage.GROUP_VERIFIED,
        )

    def test_request_token_still_confirms_the_current_group_unbind(self):
        self.seed_verified_row()
        self.invoke(self.operator_event(), "")
        request = self.store().find_confirmation(
            request_kind="group_unbind",
            bot_scope=SCOPE,
            actor_user_id=OPERATOR,
            external_group_id=VERIFIED_VIRTUAL,
        )
        self.assertIsNotNone(request)
        self.assertRegex(request.request_id, r"^[0-9a-f]{32}$")
        message = self.invoke(self.operator_event(), request.request_id)
        self.assertIn("已撤销", message)
        self.assertEqual(
            self.store().get_group(SCOPE, VERIFIED_VIRTUAL).binding_status,
            qbind.storage.GROUP_REVOKED,
        )


if __name__ == "__main__":
    unittest.main()
