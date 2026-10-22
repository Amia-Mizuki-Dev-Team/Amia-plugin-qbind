"""Coverage for the executable next steps on a refused group binding.

``group._group_conflict_message`` used to answer every conflict with the
dead-end notice "该请求已进入冲突状态".  Two causes now name the command that
can actually resolve them (``qgunbind <real id>`` / ``qgrebind <real id>``),
and ``group._group_confirm_buttons`` mirrors the first one in the keyboard.
``qgconfirm_handler`` re-reads the stored outcome before picking the buttons,
so the end-to-end test seeds the "another virtual group already verified the
same real group id" scenario and asserts the reply the user actually gets.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from test_acceptance_matrix import (
    AcceptanceBase,
    FakeBot,
    FakeEvent,
    PlainArg,
    capture_handler,
    run,
)
from test_qbind import qbind
from src.plugins.qbind import group
from src.plugins.qbind.storage import ConfirmationRequest


REAL_ID = "389876336"
SCOPE = "bot-a"
OPERATOR = "admin-1"
OCCUPYING_VIRTUAL = "virtual-group-1"
CANDIDATE_VIRTUAL = "virtual-group-2"
ALREADY_CLAIMED = "the real group ID is already claimed in this Bot namespace"
VERIFIED_EXISTS = "a verified mapping already exists"
DEFAULT_BUTTONS = ["qgstatus", "qgrebind", "qgunbind"]


def make_request(**overrides) -> ConfirmationRequest:
    """Build a stored conflict request without touching any database."""

    base = ConfirmationRequest(
        request_id="a" * 32,
        request_kind="group_bind",
        bot_scope=SCOPE,
        actor_user_id=OPERATOR,
        external_user_id=None,
        external_group_id=CANDIDATE_VIRTUAL,
        candidate_id=REAL_ID,
        group_name="Test Group",
        expected_version=None,
        status=qbind.storage.REQUEST_PENDING,
        result_code="conflict",
        last_error=ALREADY_CLAIMED,
        created_at="2026-10-23T00:00:00+00:00",
        expires_at=0.0,
        updated_at="2026-10-23T00:00:00+00:00",
    )
    return replace(base, **overrides)


def button_commands(buttons) -> list[str]:
    return [button["action.data"] for row in buttons for button in row]


def button_commands_rows(payload) -> list[str]:
    rows = payload["keyboard"]["content"]["rows"]
    return [button["action"]["data"] for row in rows for button in row["buttons"]]


class _DeclaringVerifier:
    """Deterministic adapter that only saves a local declaration."""

    async def verify(self, context, bot):
        return group.GroupVerificationResult(
            status="declared", source="test:declared"
        )


class TestGroupConflictMessage(unittest.TestCase):
    def message(self, error, candidate_id=REAL_ID) -> str:
        return group._group_conflict_message(
            make_request(last_error=error, candidate_id=candidate_id)
        )

    def test_already_claimed_points_to_qgunbind_on_the_real_group_id(self):
        text = self.message(ALREADY_CLAIMED)
        self.assertIn("该真实群号已被其他群绑定占用", text)
        self.assertIn(f"qgunbind {REAL_ID}", text)
        self.assertIn("qgbind <真实群号>", text)

    def test_already_claimed_no_longer_reads_as_a_dead_end(self):
        text = self.message(ALREADY_CLAIMED)
        self.assertNotIn("该请求已进入冲突状态", text)

    def test_already_claimed_without_candidate_uses_the_generic_hint(self):
        for candidate in (None, "", "   "):
            with self.subTest(candidate=repr(candidate)):
                text = self.message(ALREADY_CLAIMED, candidate)
                self.assertIn("qgunbind <真实群号>", text)
                self.assertNotIn("qgunbind None", text)
                self.assertNotIn("该请求已进入冲突状态", text)

    def test_verified_mapping_points_to_qgrebind(self):
        text = self.message(VERIFIED_EXISTS)
        self.assertIn("qgrebind <真实群号>", text)
        self.assertNotIn("qgunbind", text)
        self.assertNotIn("该请求已进入冲突状态", text)

    def test_unknown_conflict_keeps_the_notice_and_adds_qgstatus(self):
        text = self.message("another group binding request changed the target")
        self.assertIn("该请求已进入冲突状态", text)
        self.assertIn("qgstatus", text)

    def test_cause_matching_ignores_letter_case(self):
        text = self.message(ALREADY_CLAIMED.upper())
        self.assertIn(f"qgunbind {REAL_ID}", text)
        self.assertNotIn("该请求已进入冲突状态", text)


class TestGroupConfirmButtons(unittest.TestCase):
    def buttons(self, **overrides):
        return group._group_confirm_buttons(
            make_request(**overrides), OPERATOR, ""
        )

    def test_already_claimed_with_candidate_offers_qgunbind_then_qgstatus(self):
        buttons = self.buttons()
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0][0]["action.data"], f"qgunbind {REAL_ID}")
        self.assertEqual(buttons[1][0]["action.data"], "qgstatus")

    def test_already_claimed_without_candidate_keeps_default_navigation(self):
        for candidate in (None, "", "   "):
            with self.subTest(candidate=repr(candidate)):
                self.assertEqual(
                    button_commands(self.buttons(candidate_id=candidate)),
                    DEFAULT_BUTTONS,
                )

    def test_other_conflict_keeps_default_navigation(self):
        self.assertEqual(
            button_commands(self.buttons(last_error=VERIFIED_EXISTS)),
            DEFAULT_BUTTONS,
        )

    def test_email_pending_puts_the_code_prefill_first(self):
        buttons = self.buttons(result_code="email_pending", last_error=None)
        code_button = buttons[0][0]
        self.assertEqual(code_button["action.data"], "qgcode ")
        self.assertFalse(code_button["action.enter"])
        self.assertEqual(
            code_button["action.permission.specify_user_ids"], [OPERATOR]
        )
        self.assertIn("qgstatus", button_commands(buttons))


class TestConflictReceiptEndToEnd(AcceptanceBase):
    """Another virtual group verified the real id, so qgconfirm must refuse."""

    def setUp(self):
        super().setUp()
        self.original_verifier = group.get_group_verifier()
        group.set_group_verifier(_DeclaringVerifier())

    def tearDown(self):
        group.set_group_verifier(self.original_verifier)
        super().tearDown()

    def seed_occupying_verified_group(self):
        store = qbind.get_store()
        store.set_group_pending(
            bot_scope=SCOPE,
            virtual_group_id=OCCUPYING_VIRTUAL,
            real_group_id=REAL_ID,
            group_name="occupying",
            bound_by_user_id=OPERATOR,
            verification_source="test:manual",
        )
        return store.verify_group(
            bot_scope=SCOPE,
            virtual_group_id=OCCUPYING_VIRTUAL,
            verified_by=OPERATOR,
            verification_source="test:review",
        )

    def operator_event(self):
        return FakeEvent(OPERATOR, group_id=CANDIDATE_VIRTUAL, role="admin")

    def request_confirmation(self) -> ConfirmationRequest:
        run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                self.operator_event(),
                FakeBot(),
                PlainArg(REAL_ID),
            )
        )
        request = qbind.get_store().find_confirmation(
            request_kind="group_bind",
            bot_scope=SCOPE,
            actor_user_id=OPERATOR,
            external_group_id=CANDIDATE_VIRTUAL,
        )
        self.assertIsNotNone(request)
        return request

    def test_conflict_reply_names_qgunbind_in_text_and_keyboard(self):
        self.seed_occupying_verified_group()
        qbind.USE_MARKDOWN = True
        bot = FakeBot()
        request = self.request_confirmation()
        message = run(
            capture_handler(
                group.qgconfirm,
                group.qgconfirm_handler,
                self.operator_event(),
                bot,
                PlainArg(request.request_id),
            )
        )
        # ``capture_handler`` raises on the first ``finish`` call, so exactly one
        # reply message crosses the boundary.
        self.assertEqual(len(message), 1)
        payload = message[0].data["data"]
        content = payload["markdown"]["content"]
        self.assertIn(f"qgunbind {REAL_ID}", content)
        self.assertNotIn("该请求已进入冲突状态", content)
        self.assertIn(f"qgunbind {REAL_ID}", button_commands_rows(payload))

    def test_conflict_reply_revokes_before_generic_navigation(self):
        self.seed_occupying_verified_group()
        qbind.USE_MARKDOWN = True
        request = self.request_confirmation()
        message = run(
            capture_handler(
                group.qgconfirm,
                group.qgconfirm_handler,
                self.operator_event(),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        rows = message[0].data["data"]["keyboard"]["content"]["rows"]
        commands = [row["buttons"][0]["action"]["data"] for row in rows]
        self.assertEqual(commands, [f"qgunbind {REAL_ID}", "qgstatus"])

    def test_conflict_keeps_local_rows_and_writes_no_remote_mapping(self):
        occupying = self.seed_occupying_verified_group()
        request = self.request_confirmation()
        run(
            capture_handler(
                group.qgconfirm,
                group.qgconfirm_handler,
                self.operator_event(),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        store = qbind.get_store()
        outcome = store.get_confirmation(request.request_id)
        self.assertEqual(outcome.result_code, "conflict")
        self.assertIn("already claimed", outcome.last_error)
        self.assertIsNone(store.get_group(SCOPE, CANDIDATE_VIRTUAL))
        survivor = store.get_group(SCOPE, OCCUPYING_VIRTUAL)
        self.assertEqual(survivor.binding_status, qbind.storage.GROUP_VERIFIED)
        self.assertEqual(survivor.version, occupying.version)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.clear_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [])
        self.assertEqual(self.mapping.group_clear_calls, [])


if __name__ == "__main__":
    unittest.main()
