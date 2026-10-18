"""Local automation for the executable parts of attachment section 10."""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from nonebot.consts import CMD_KEY, PREFIX_KEY

from test_qbind import qbind
from src.plugins.qbind import group
from src.plugins.qbind.gensokyo import GensokyoMappingError
from src.plugins.qbind.mail import QbindEmailService, SMTPConfig


class FinishSignal(Exception):
    def __init__(self, value):
        super().__init__(str(value))
        self.value = value


class PlainArg:
    def __init__(self, value: str = ""):
        self.value = value

    def extract_plain_text(self) -> str:
        return self.value


class FakeEvent:
    def __init__(
        self,
        user_id: str,
        *,
        self_id: str = "bot-a",
        group_id: str | None = None,
        role: str = "member",
        text: str = "",
    ):
        self.self_id = self_id
        self.user_id = user_id
        self.group_id = group_id
        self.message_id = str(id(self))
        self._text = text
        self.message_type = "group" if group_id is not None else "private"
        self.sender = SimpleNamespace(role=role)

    def get_user_id(self) -> str:
        return self.user_id

    def get_plaintext(self) -> str:
        return self._text


class FakeBot:
    def __init__(self):
        self.sent = []

    async def get_group_info(self, *, group_id: str):
        return {"group_name": f"group-{group_id}"}

    async def send(self, event, message):
        self.sent.append(message)


class FakeMatcher:
    def __init__(self):
        self.stopped = False

    def stop_propagation(self):
        self.stopped = True


class FakeMapping:
    def __init__(self, *, set_error=None, clear_error=None):
        self.set_error = set_error
        self.clear_error = clear_error
        self.set_calls: list[tuple[str, str]] = []
        self.clear_calls: list[str] = []

    async def set_personal_mapping(self, virtual_user_id: object, real_qq: object):
        self.set_calls.append((str(virtual_user_id), str(real_qq)))
        if self.set_error is not None:
            raise self.set_error

    async def clear_personal_mapping(self, virtual_user_id: object):
        self.clear_calls.append(str(virtual_user_id))
        if self.clear_error is not None:
            raise self.clear_error


def run(coro):
    return asyncio.run(coro)


async def capture_handler(matcher, handler, *args):
    async def finish(value=None, *unused_args, **unused_kwargs):
        raise FinishSignal(value)

    original = matcher.finish
    matcher.finish = finish
    try:
        await handler(*args)
    except FinishSignal as signal:
        return signal.value
    finally:
        matcher.finish = original
    raise AssertionError("handler returned without calling finish")


def expire_request(request_id: str) -> None:
    store = qbind.get_store()
    with store._transaction() as connection:
        connection.execute(
            "UPDATE confirmation_requests SET expires_at = ? WHERE request_id = ?",
            (time.time() - 1, request_id),
        )


def parsed_command(name: str) -> dict:
    return {PREFIX_KEY: {CMD_KEY: (name,)}}


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.original_email_service = qbind._email_service
        qbind.set_email_service(
            QbindEmailService(
                SMTPConfig(
                    host="",
                    port=465,
                    use_ssl=True,
                    username="",
                    password="",
                    from_address="",
                    timeout_seconds=15.0,
                    challenge_ttl_seconds=600.0,
                    max_attempts=5,
                    resend_cooldown_seconds=60.0,
                    required=False,
                    code_secret="test-only",
                )
            )
        )
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-acceptance-")
        root = Path(self.temporary.name)
        qbind.configure_storage(root / "qbind.sqlite3", root / "binds.json")
        qbind._last_cmd_time.clear()
        self.original_markdown = qbind.USE_MARKDOWN
        qbind.USE_MARKDOWN = False
        self.original_mapping = qbind._mapping_client
        self.mapping = FakeMapping()
        qbind.set_mapping_client(self.mapping)

    def tearDown(self):
        qbind.set_email_service(self.original_email_service)
        qbind.USE_MARKDOWN = self.original_markdown
        qbind.set_mapping_client(self.original_mapping)
        self.temporary.cleanup()


class TestAccessGate(AcceptanceBase):
    def test_plain_group_chat_is_not_a_binding_trigger(self):
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="大家晚上好"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        run(qbind.access_guard_handler(event, bot, matcher, state={}))
        self.assertFalse(matcher.stopped)
        self.assertEqual(bot.sent, [])

    def test_unbound_group_business_message_is_stopped_with_personal_prompt(self):
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="/economy balance"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("economy")
            )
        )
        self.assertTrue(matcher.stopped)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("qbind", str(bot.sent[0]))

    def test_personal_binding_without_group_binding_is_stopped_with_group_prompt(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
        )
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="/economy balance"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("economy")
            )
        )
        self.assertTrue(matcher.stopped)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("qgbind", str(bot.sent[0]))

    def test_group_prompt_is_markdown_with_binding_buttons(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
        )
        qbind.USE_MARKDOWN = True
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="/economy balance"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("economy")
            )
        )
        self.assertTrue(matcher.stopped)
        self.assertEqual(bot.sent[0][0].type, "markdown")
        data = bot.sent[0][0].data["data"]
        buttons = data["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual(
            [button["render_data"]["label"] for button in buttons],
            ["发起群绑定", "查看群绑定状态"],
        )
        self.assertEqual(
            [button["action"]["data"] for button in buttons],
            ["qgbind", "qgstatus"],
        )

    def test_both_bindings_allow_group_business_message(self):
        qbind.get_store().declare_personal(
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
        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="/economy balance"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        qbind.USE_MARKDOWN = True
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("economy")
            )
        )
        self.assertFalse(matcher.stopped)
        self.assertEqual(bot.sent, [])
        members = qbind.get_group_members("9876543", "bot-a")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].virtual_user_id, "user-a")
        self.assertEqual(members[0].real_user_id, "1234567")
        self.assertEqual(
            members[0].membership_status, qbind.storage.MEMBERSHIP_VERIFIED
        )

    def test_verified_membership_is_idempotent_without_share_notice(self):
        qbind.get_store().declare_personal(
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
        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        event = FakeEvent(
            "user-a", group_id="group-a", role="member", text="/economy balance"
        )
        first_bot = FakeBot()
        run(
            qbind.access_guard_handler(
                event, first_bot, FakeMatcher(), state=parsed_command("economy")
            )
        )
        self.assertEqual(first_bot.sent, [])
        self.assertEqual(len(qbind.get_group_members("9876543", "bot-a")), 1)

        # Recreate the store exactly as a Bot restart would.  The verified
        # membership upsert must survive the new QbindStore instance.
        qbind.configure_storage(
            Path(self.temporary.name) / "qbind.sqlite3",
            Path(self.temporary.name) / "binds.json",
        )
        qbind._last_cmd_time.clear()
        second_bot = FakeBot()
        run(
            qbind.access_guard_handler(
                event, second_bot, FakeMatcher(), state=parsed_command("economy")
            )
        )
        self.assertEqual(second_bot.sent, [])
        self.assertEqual(len(qbind.get_group_members("9876543", "bot-a")), 1)

    def test_help_remains_available_for_an_unbound_user(self):
        event = FakeEvent("user-a", group_id="group-a", text="/help")
        bot = FakeBot()
        matcher = FakeMatcher()
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("help")
            )
        )
        self.assertFalse(matcher.stopped)
        self.assertEqual(bot.sent, [])


class TestPersonalAcceptance(AcceptanceBase):
    def test_10_1_normal_binding_then_trusted_verification(self):
        event = FakeEvent("virtual-user-1")
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg("1234567"),
            )
        )
        self.assertIn("确认", message)
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="virtual-user-1",
            external_user_id="virtual-user-1",
        )
        self.assertIsNotNone(request)

        message = run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                event,
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        self.assertIn("已声明", message)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(
            qbind.get_store().get_personal("bot-a", "virtual-user-1").verification_source,
            "qbind:declared",
        )
        self.assertIsNone(qbind.get_verified_qq("virtual-user-1", "bot-a"))

        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="virtual-user-1",
            verified_by="trusted-reviewer",
            verification_source="test:ownership-proof",
        )
        self.assertEqual(qbind.get_verified_qq("virtual-user-1", "bot-a"), "1234567")

    def test_10_1_invalid_qq_format_is_rejected(self):
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                FakeEvent("virtual-user-1"),
                FakeBot(),
                PlainArg("12"),
            )
        )
        self.assertIn("QQ 号格式不正确", message)
        self.assertIsNone(
            qbind.get_store().find_confirmation(
                request_kind="personal_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user-1",
                external_user_id="virtual-user-1",
            )
        )

    def test_10_1_duplicate_binding_is_rejected(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="virtual-user-1",
            canonical_user_id="1234567",
            bound_by_user_id="virtual-user-1",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="virtual-user-1",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                FakeEvent("virtual-user-1"),
                FakeBot(),
                PlainArg("1234568"),
            )
        )
        self.assertIn("已经存在已验证", message)
        self.assertEqual(self.mapping.set_calls, [])

    def test_personal_unbind_confirmation_is_markdown_with_user_button(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
        )
        qbind.USE_MARKDOWN = True
        message = run(
            capture_handler(
                qbind.unbind,
                qbind.unbind_handler,
                FakeEvent("user-a"),
                FakeBot(),
                PlainArg(""),
            )
        )
        self.assertEqual(message[0].type, "markdown")
        data = message[0].data["data"]
        self.assertIn("确认解绑", data["markdown"]["content"])
        button = data["keyboard"]["content"]["rows"][0]["buttons"][0]
        self.assertEqual(button["render_data"]["label"], "⚠️ 确认解绑")
        self.assertEqual(button["action"]["permission"]["specify_user_ids"], ["user-a"])

    def test_10_1_confirmation_timeout_does_not_call_backend(self):
        request = qbind.get_store().create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="virtual-user-1",
            external_user_id="virtual-user-1",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        expire_request(request.request_id)
        request = qbind.get_store().get_confirmation(request.request_id)
        message = run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent("virtual-user-1"),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        self.assertIn("过期", message)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(
            qbind.get_store().get_confirmation(request.request_id).status,
            qbind.storage.REQUEST_EXPIRED,
        )

    def test_10_1_other_user_cannot_confirm(self):
        request = qbind.get_store().create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        message = run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent("user-b"),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        self.assertIn("does not belong", message)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(
            qbind.get_store().get_confirmation(request.request_id).status,
            qbind.storage.REQUEST_PENDING,
        )

    def test_10_1_canonical_conflict_is_checked_before_backend(self):
        qbind.get_store().declare_personal(
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
            verification_source="test",
        )
        request = qbind.get_store().create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="user-b",
            external_user_id="user-b",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent("user-b"),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        result = qbind.get_store().get_confirmation(request.request_id)
        self.assertEqual(result.result_code, "conflict")
        self.assertEqual(result.status, qbind.storage.REQUEST_FAILED)
        self.assertEqual(self.mapping.set_calls, [])

    def test_10_1_local_declaration_does_not_depend_on_legacy_gensokyo_mapping(self):
        self.mapping.set_error = GensokyoMappingError("timeout", retryable=True)
        request = qbind.get_store().create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        message = run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent("user-a"),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )
        self.assertIn("已声明", message)
        result = qbind.get_store().get_confirmation(request.request_id)
        self.assertEqual(result.result_code, "declared")
        self.assertEqual(result.status, qbind.storage.REQUEST_COMPLETED)
        self.assertEqual(
            qbind.get_real_qq("user-a", "bot-a"),
            "1234567",
        )
        self.assertEqual(self.mapping.set_calls, [])

    def test_10_1_unbind_is_own_record_only_and_preserves_recovery(self):
        qbind.get_store().declare_personal(
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
            verification_source="test",
        )
        current = qbind.get_store().get_personal("bot-a", "user-a")
        request = qbind.get_store().create_confirmation(
            request_kind="personal_unbind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            expected_version=current.version,
            ttl_seconds=60,
        )
        result = run(qbind._execute_personal_unbind(FakeEvent("user-a"), request))
        self.assertIn("解绑", result)
        self.assertEqual(self.mapping.clear_calls, [])
        self.assertEqual(
            qbind.get_store().get_personal("bot-a", "user-a").binding_status,
            qbind.storage.PERSONAL_REVOKED,
        )

        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a-2",
            canonical_user_id="7654321",
            bound_by_user_id="user-a-2",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="user-a-2",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        message = run(
            capture_handler(
                qbind.unbind,
                qbind.unbind_handler,
                FakeEvent("user-a-2"),
                FakeBot(),
                PlainArg("1234567 确认"),
            )
        )
        self.assertIn("不一致", message)

    def test_10_1_unbind_does_not_depend_on_legacy_gensokyo_mapping(self):
        qbind.get_store().declare_personal(
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
            verification_source="test",
        )
        current = qbind.get_store().get_personal("bot-a", "user-a")
        request = qbind.get_store().create_confirmation(
            request_kind="personal_unbind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            expected_version=current.version,
            ttl_seconds=60,
        )
        self.mapping.clear_error = GensokyoMappingError("backend down", retryable=True)
        result = run(qbind._execute_personal_unbind(FakeEvent("user-a"), request))
        self.assertIn("解绑", result)
        self.assertEqual(
            qbind.get_store().get_personal("bot-a", "user-a").binding_status,
            qbind.storage.PERSONAL_REVOKED,
        )
        self.assertEqual(
            qbind.get_store().get_confirmation(request.request_id).status,
            qbind.storage.REQUEST_COMPLETED,
        )
        self.assertEqual(self.mapping.clear_calls, [])

    def test_10_1_restart_preserves_binding_and_pending_request(self):
        store = qbind.get_store()
        store.declare_personal(
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
            verification_source="test",
        )
        request = store.create_confirmation(
            request_kind="personal_unbind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            expected_version=store.get_personal("bot-a", "user-a").version,
            ttl_seconds=60,
        )
        db_path = self.temporary.name + "\\qbind.sqlite3"
        legacy_path = self.temporary.name + "\\binds.json"
        qbind.configure_storage(db_path, legacy_path)
        self.assertEqual(qbind.get_verified_qq("user-a", "bot-a"), "1234567")
        self.assertEqual(qbind.get_store().get_confirmation(request.request_id).status, "pending")


class TestGroupAcceptance(AcceptanceBase):
    def setUp(self):
        super().setUp()
        self.original_verifier = group.get_group_verifier()
        group.set_group_verifier(group._UnavailableGroupVerifier())

    def tearDown(self):
        group.set_group_verifier(self.original_verifier)
        super().tearDown()

    def test_10_2_admin_can_request_but_member_cannot(self):
        admin = FakeEvent("admin-1", group_id="virtual-group-1", role="admin")
        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                admin,
                FakeBot(),
                PlainArg("9876543"),
            )
        )
        self.assertIn("确认", message)
        request = qbind.get_store().find_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
        )
        self.assertIsNotNone(request)

        member = FakeEvent("member-1", group_id="virtual-group-1", role="member")
        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                member,
                FakeBot(),
                PlainArg("9876544"),
            )
        )
        self.assertIn("只有当前群主/管理员", message)
        self.assertIsNone(
            qbind.get_store().find_confirmation(
                request_kind="group_bind",
                bot_scope="bot-a",
                actor_user_id="member-1",
                external_group_id="virtual-group-1",
            )
        )

    def test_expired_same_candidate_request_does_not_block_new_qgbind(self):
        store = qbind.get_store()
        old_request = store.create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
            ttl_seconds=60,
        )
        store.set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-1",
            verification_source="test:old-request",
            operation_id=old_request.request_id,
        )
        expire_request(old_request.request_id)

        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                FakeEvent("admin-1", group_id="virtual-group-1", role="admin"),
                FakeBot(),
                PlainArg("9876543"),
            )
        )

        self.assertIn("确认当前群绑定", message)
        new_request = store.find_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
        )
        self.assertIsNotNone(new_request)
        self.assertNotEqual(new_request.request_id, old_request.request_id)

    def test_10_2_invalid_group_number_and_duplicate_verified_mapping(self):
        admin = FakeEvent("admin-1", group_id="virtual-group-1", role="admin")
        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                admin,
                FakeBot(),
                PlainArg("123"),
            )
        )
        self.assertIn("真实群号格式不正确", message)

        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                admin,
                FakeBot(),
                PlainArg("9876543"),
            )
        )
        self.assertIn("已有已验证映射", message)

    def test_10_2_confirmation_cannot_cross_user_or_group(self):
        request = qbind.get_store().create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
            ttl_seconds=60,
        )
        for event in (
            FakeEvent("member-1", group_id="virtual-group-1", role="admin"),
            FakeEvent("admin-1", group_id="virtual-group-2", role="admin"),
        ):
            message = run(
                capture_handler(
                    group.qgconfirm,
                    group.qgconfirm_handler,
                    event,
                    FakeBot(),
                    PlainArg(request.request_id),
                )
            )
            self.assertIn("does not belong", message)
        self.assertEqual(
            qbind.get_store().get_confirmation(request.request_id).status,
            qbind.storage.REQUEST_PENDING,
        )

    def test_10_2_two_admin_requests_do_not_overwrite_each_other(self):
        first = qbind.get_store().create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
            ttl_seconds=60,
        )
        second = qbind.get_store().create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-2",
            external_group_id="virtual-group-1",
            candidate_id="9876544",
            ttl_seconds=60,
        )
        first_message = run(
            group._confirm_group_bind(
                FakeEvent("admin-1", group_id="virtual-group-1", role="admin"),
                FakeBot(),
                first,
            )
        )
        second_message = run(
            group._confirm_group_bind(
                FakeEvent("admin-2", group_id="virtual-group-1", role="admin"),
                FakeBot(),
                second,
            )
        )
        self.assertIn("待可信来源审核", first_message)
        self.assertIn("未覆盖", second_message)
        self.assertEqual(
            qbind.get_store().get_confirmation(second.request_id).result_code,
            "conflict",
        )
        self.assertEqual(qbind.get_real_group("virtual-group-1", "bot-a"), None)
        self.assertEqual(
            qbind.get_store().get_group("bot-a", "virtual-group-1").real_group_id,
            "9876543",
        )

    def test_10_2_verifier_rejects_wrong_real_group_without_exposing_it(self):
        seen = {}

        class RejectingVerifier:
            async def verify(self, context, bot):
                seen.update(
                    virtual_group_id=context.virtual_group_id,
                    requested_real_group_id=context.requested_real_group_id,
                )
                return group.GroupVerificationResult(
                    status="rejected", source="test:membership-mismatch", reason="mismatch"
                )

        request = qbind.get_store().create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
            ttl_seconds=60,
        )
        previous = group.get_group_verifier()
        group.set_group_verifier(RejectingVerifier())
        try:
            message = run(
                group._confirm_group_bind(
                    FakeEvent("admin-1", group_id="virtual-group-1", role="admin"),
                    FakeBot(),
                    request,
                )
            )
        finally:
            group.set_group_verifier(previous)
        self.assertIn("冲突", message)
        self.assertEqual(seen, {"virtual_group_id": "virtual-group-1", "requested_real_group_id": "9876543"})
        self.assertIsNone(qbind.get_real_group("virtual-group-1", "bot-a"))

    def test_10_2_confirmation_timeout_and_rejected_backend_do_not_verify(self):
        request = qbind.get_store().create_confirmation(
            request_kind="group_bind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
            candidate_id="9876543",
            ttl_seconds=60,
        )
        expire_request(request.request_id)
        request = qbind.get_store().get_confirmation(request.request_id)
        message = run(
            group._confirm_group_bind(
                FakeEvent("admin-1", group_id="virtual-group-1", role="admin"),
                FakeBot(),
                request,
            )
        )
        self.assertIn("过期", message)
        self.assertIsNone(qbind.get_real_group("virtual-group-1", "bot-a"))

    def test_10_2_verified_group_is_reused_by_other_members(self):
        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        self.assertTrue(
            run(
                group.ensure_group_bound(
                    FakeEvent("member-1", group_id="virtual-group-1", role="member"),
                    FakeBot(),
                )
            )
        )
        self.assertEqual(qbind.get_real_group("virtual-group-1", "bot-a"), "9876543")

    def test_10_2_group_unbind_revokes_only_group_mapping(self):
        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        event = FakeEvent("admin-1", group_id="virtual-group-1", role="admin")
        run(
            capture_handler(
                group.qgunbind,
                group.qgunbind_handler,
                event,
                PlainArg(""),
            )
        )
        request = qbind.get_store().find_confirmation(
            request_kind="group_unbind",
            bot_scope="bot-a",
            actor_user_id="admin-1",
            external_group_id="virtual-group-1",
        )
        self.assertIsNotNone(request)
        message = run(
            capture_handler(
                group.qgunbind,
                group.qgunbind_handler,
                event,
                PlainArg(request.request_id),
            )
        )
        self.assertIn("已撤销", message)
        self.assertEqual(
            qbind.get_group_binding_status("virtual-group-1", "bot-a"),
            qbind.storage.GROUP_REVOKED,
        )

    def test_10_2_group_restart_preserves_verified_mapping(self):
        qbind.get_store().apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-1",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-1",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        db_path = self.temporary.name + "\\qbind.sqlite3"
        legacy_path = self.temporary.name + "\\binds.json"
        qbind.configure_storage(db_path, legacy_path)
        self.assertEqual(qbind.get_real_group("virtual-group-1", "bot-a"), "9876543")


class TestCrossGroupAcceptance(AcceptanceBase):
    def test_10_3_two_virtual_user_ids_can_share_one_verified_canonical_identity(self):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope="bot-a",
            external_user_id="virtual-user-group-a",
            canonical_user_id="1234567",
            bound_by_user_id="trusted-reviewer",
            verification_source="test:first-proof",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="virtual-user-group-a",
            verified_by="trusted-reviewer",
            verification_source="test:first-proof",
        )
        second = qbind.approve_personal_alias(
            self_id="bot-a",
            user_id="virtual-user-group-b",
            canonical_user_id="1234567",
            verified_by="trusted-reviewer",
            verification_source="test:second-proof",
        )
        self.assertEqual(second.binding_status, qbind.storage.PERSONAL_VERIFIED)
        self.assertEqual(qbind.get_verified_qq("virtual-user-group-a", "bot-a"), "1234567")
        self.assertEqual(qbind.get_verified_qq("virtual-user-group-b", "bot-a"), "1234567")

    def test_10_3_two_groups_have_distinct_group_identity_but_shared_user_identity(self):
        store = qbind.get_store()
        for user_id in ("user-a", "user-b"):
            store.declare_personal(
                bot_scope="bot-a",
                external_user_id=user_id,
                canonical_user_id="1234567",
                bound_by_user_id="trusted-reviewer",
                verification_source="test",
                allow_verified_canonical_reuse=user_id == "user-b",
            )
            qbind.approve_personal_binding(
                self_id="bot-a",
                user_id=user_id,
                verified_by="trusted-reviewer",
                verification_source="test",
            )
        store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-a",
            real_group_id="9876543",
            group_name="A",
            bound_by_user_id="admin-a",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="virtual-group-b",
            real_group_id="9876544",
            group_name="B",
            bound_by_user_id="admin-b",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        context_a = run(
            qbind.resolve_context_identity(
                self_id="bot-a", user_id="user-a", group_id="virtual-group-a"
            )
        )
        context_b = run(
            qbind.resolve_context_identity(
                self_id="bot-a", user_id="user-b", group_id="virtual-group-b"
            )
        )
        self.assertEqual(context_a["canonical_user_id"], "1234567")
        self.assertEqual(context_b["canonical_user_id"], "1234567")
        self.assertEqual(context_a["canonical_group_id"], "9876543")
        self.assertEqual(context_b["canonical_group_id"], "9876544")

    def test_10_3_group_unbind_does_not_remove_personal_identity_or_other_group(self):
        store = qbind.get_store()
        store.declare_personal(
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
            verification_source="test",
        )
        for group_id, real_id in (("group-a", "9876543"), ("group-b", "9876544")):
            store.apply_verified_group(
                bot_scope="bot-a",
                virtual_group_id=group_id,
                real_group_id=real_id,
                group_name=group_id,
                bound_by_user_id="admin",
                verified_by="trusted-reviewer",
                verification_source="test",
            )
        current = store.get_group("bot-a", "group-a")
        store.revoke_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            actor_user_id="admin",
            expected_version=current.version,
        )
        self.assertIsNone(qbind.get_real_group("group-a", "bot-a"))
        self.assertEqual(qbind.get_real_group("group-b", "bot-a"), "9876544")
        self.assertEqual(qbind.get_verified_qq("user-a", "bot-a"), "1234567")


if __name__ == "__main__":
    unittest.main()
