"""Local automation for the executable parts of attachment section 10."""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nonebot.consts import CMD_KEY, PREFIX_KEY

from test_qbind import qbind
from src.plugins.qbind import group
from src.plugins.qbind.gensokyo import (
    GensokyoMappingError,
    GensokyoMappingMissingError,
)
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

    def _verify_personal(self, user_id: str = "user-a") -> None:
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=user_id,
            canonical_user_id="1234567",
            bound_by_user_id=user_id,
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id=user_id,
            verified_by="trusted-reviewer",
            verification_source="test:review",
        )

    def _run_group_business_command(self, group_id: str = "group-a"):
        event = FakeEvent(
            "user-a", group_id=group_id, role="member", text="/economy balance"
        )
        bot = FakeBot()
        matcher = FakeMatcher()
        run(
            qbind.access_guard_handler(
                event, bot, matcher, state=parsed_command("economy")
            )
        )
        return matcher, bot

    def test_declared_group_blocks_without_dead_end_commands(self):
        qbind.get_store().declare_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verification_source="test:declared",
        )
        self._verify_personal()
        matcher, bot = self._run_group_business_command()

        self.assertTrue(matcher.stopped)
        self.assertEqual(len(bot.sent), 1)
        content = str(bot.sent[0])
        self.assertIn("声明", content)
        # Pin the group branch: a personal-state prompt must not satisfy this.
        self.assertIn("本群", content)
        # A declared row blocks before any remote write, so pointing at the
        # email-code flow or blaming GSK would both be dead ends.
        for dead_end in ("qgcode", "qgconfirm", "GSK"):
            self.assertNotIn(dead_end, content)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [])

    def test_pending_group_blocks_and_points_to_qgcode(self):
        qbind.get_store().set_group_pending(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verification_source="test:pending",
        )
        self._verify_personal()
        matcher, bot = self._run_group_business_command()

        self.assertTrue(matcher.stopped)
        self.assertEqual(len(bot.sent), 1)
        content = str(bot.sent[0])
        self.assertIn("qgcode", content)
        self.assertNotIn("GSK", content)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [])

    def test_personal_declared_with_verified_group_is_not_reported_as_gsk_failure(self):
        # Legacy declaration mode keeps the personal gate open for a declared
        # row, so the block must be explained by the personal state itself.
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
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
        self.assertTrue(qbind.is_bound("user-a", "bot-a"))
        matcher, bot = self._run_group_business_command()

        self.assertTrue(matcher.stopped)
        self.assertEqual(len(bot.sent), 1)
        content = str(bot.sent[0])
        self.assertIn("个人", content)
        # Pin the personal-declared branch: the generic "send qbind" prompt
        # also mentions 个人 but never 声明.
        self.assertIn("声明", content)
        self.assertNotIn("GSK", content)
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [])

    def test_group_prompt_buttons_are_clickable_commands(self):
        self._verify_personal()
        qbind.USE_MARKDOWN = True
        resolvable = {"qgbind ", "qgbind", "qgstatus"}
        cases = (("group-declared", "9876543", False), ("group-revoked", "9876544", True))

        for group_id, real_group_id, revoke in cases:
            with self.subTest(group=group_id):
                qbind.get_store().declare_group(
                    bot_scope="bot-a",
                    virtual_group_id=group_id,
                    real_group_id=real_group_id,
                    group_name="Test",
                    bound_by_user_id="admin-a",
                    verification_source="test:buttons",
                )
                if revoke:
                    qbind.get_store().revoke_group(
                        bot_scope="bot-a",
                        virtual_group_id=group_id,
                        actor_user_id="admin-a",
                    )
                matcher, bot = self._run_group_business_command(group_id)

                self.assertTrue(matcher.stopped)
                self.assertEqual(len(bot.sent), 1)
                data = bot.sent[0][0].data["data"]
                buttons = [
                    button
                    for row in data["keyboard"]["content"]["rows"]
                    for button in row["buttons"]
                ]
                self.assertTrue(buttons)
                for button in buttons:
                    action = button["action"]
                    self.assertIn(action["data"], resolvable)
                    if action["data"] == "qgbind ":
                        # A prefilled command needs the user to add the number.
                        self.assertFalse(action["enter"])

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

    def test_group_binding_completes_when_personal_already_synced(self):
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
            verification_source="test:review",
        )
        store.apply_verified_group(
            bot_scope="bot-a",
            virtual_group_id="group-a",
            real_group_id="9876543",
            group_name="Test",
            bound_by_user_id="admin-a",
            verified_by="trusted-reviewer",
            verification_source="test",
        )
        personal = store.get_personal("bot-a", "user-a")
        store.record_gsk_sync(
            bot_scope="bot-a",
            external_id="user-a",
            mapping_kind=qbind.storage.MAPPING_PERSONAL,
            canonical_id=personal.canonical_user_id,
            local_version=personal.version,
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

        self.assertFalse(matcher.stopped)
        self.assertEqual(bot.sent, [])
        # Only the unrecorded group half reaches Gensokyo.
        self.assertEqual(self.mapping.set_calls, [])
        self.assertEqual(self.mapping.group_set_calls, [("group-a", "9876543")])
        members = qbind.get_group_members("9876543", "bot-a")
        self.assertEqual([row.virtual_user_id for row in members], ["user-a"])

    def test_missing_remote_identity_is_reported_with_its_own_message(self):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope="bot-a",
            external_user_id="890779556",
            canonical_user_id="2338680148",
            bound_by_user_id="890779556",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="890779556",
            verified_by="trusted-reviewer",
            verification_source="test:review",
        )
        self.mapping.set_error = GensokyoMappingMissingError(
            "Gensokyo type=5 virtual identity row is missing", virtual_id="890779556"
        )

        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                FakeEvent("890779556"),
                FakeBot(),
                PlainArg("2338680148"),
            )
        )

        self.assertIn("远端缺少会话身份记录", str(message))
        self.assertNotIn("稍后重试", str(message))
        self.assertNotIn("映射已同步", str(message))
        self.assertEqual(self.mapping.set_calls, [("890779556", "2338680148")])
        self.assertIsNone(
            store.get_gsk_sync("bot-a", "890779556", qbind.storage.MAPPING_PERSONAL)
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
    def test_same_id_legacy_confirmation_remains_declared(self):
        user_id = "890779556"
        event = FakeEvent(user_id)
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg(user_id),
            )
        )
        self.assertIn("确认", message)

        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id=user_id,
            external_user_id=user_id,
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
        record = qbind.get_store().get_personal("bot-a", user_id)
        self.assertEqual(record.canonical_user_id, user_id)
        self.assertEqual(record.binding_status, "declared")
        self.assertEqual(self.mapping.set_calls, [(user_id, user_id)])
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-a"))
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-b"))

    def test_cross_bot_virtual_id_conflict_rejects_confirmation(self):
        virtual_user_id = "890779556"
        qq_a = "1234567"
        qq_b = "7654321"
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=virtual_user_id,
            canonical_user_id=qq_a,
            bound_by_user_id=virtual_user_id,
            verification_source="test",
        )
        event = FakeEvent(virtual_user_id, self_id="bot-b")
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg(qq_b),
            )
        )
        self.assertIn("确认", str(message))
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-b",
            actor_user_id=virtual_user_id,
            external_user_id=virtual_user_id,
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

        self.assertIn("个人映射冲突", str(message))
        self.assertNotIn(virtual_user_id, str(message))
        self.assertNotIn(qq_a, str(message))
        self.assertNotIn(qq_b, str(message))
        self.assertEqual(self.mapping.set_calls, [])
        self.assertIsNone(qbind.get_store().get_personal("bot-b", virtual_user_id))

    def test_cross_bot_same_virtual_and_canonical_pair_is_allowed(self):
        virtual_user_id = "890779556"
        canonical_qq = "1234567"
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=virtual_user_id,
            canonical_user_id=canonical_qq,
            bound_by_user_id=virtual_user_id,
            verification_source="test",
        )
        event = FakeEvent(virtual_user_id, self_id="bot-b")
        run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg(canonical_qq),
            )
        )
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-b",
            actor_user_id=virtual_user_id,
            external_user_id=virtual_user_id,
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

        self.assertIn("已声明", str(message))
        self.assertEqual(self.mapping.set_calls, [(virtual_user_id, canonical_qq)])
        record = qbind.get_store().get_personal("bot-b", virtual_user_id)
        self.assertEqual(record.canonical_user_id, canonical_qq)
        self.assertEqual(record.binding_status, "declared")

    def test_failed_confirmation_does_not_clear_other_bot_same_pair(self):
        virtual_user_id = "890779556"
        canonical_qq = "1234567"
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=virtual_user_id,
            canonical_user_id=canonical_qq,
            bound_by_user_id=virtual_user_id,
            verification_source="test",
        )
        event = FakeEvent(virtual_user_id, self_id="bot-b")
        run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg(canonical_qq),
            )
        )
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-b",
            actor_user_id=virtual_user_id,
            external_user_id=virtual_user_id,
        )
        self.assertIsNotNone(request)
        store = qbind.get_store()

        with patch.object(
            store,
            "declare_personal",
            side_effect=qbind.QbindStorageError("simulated local save failure"),
        ):
            run(
                capture_handler(
                    qbind.confirm,
                    qbind.confirm_handler,
                    event,
                    FakeBot(),
                    PlainArg(request.request_id),
                )
            )

        self.assertEqual(self.mapping.set_calls, [(virtual_user_id, canonical_qq)])
        self.assertEqual(self.mapping.clear_calls, [])
        record = store.get_personal("bot-a", virtual_user_id)
        self.assertEqual(record.canonical_user_id, canonical_qq)

    def test_10_1_normal_binding_then_trusted_verification(self):
        event = FakeEvent("890779556")
        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg("2338680148"),
            )
        )
        self.assertIn("确认", message)
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="890779556",
            external_user_id="890779556",
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
        self.assertEqual(self.mapping.set_calls, [("890779556", "2338680148")])
        self.assertEqual(
            qbind.get_store().get_personal("bot-a", "890779556").verification_source,
            "qbind:declared",
        )
        self.assertIsNone(qbind.get_verified_qq("890779556", "bot-a"))

        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="890779556",
            verified_by="trusted-reviewer",
            verification_source="test:ownership-proof",
        )
        self.assertEqual(qbind.get_verified_qq("890779556", "bot-a"), "2338680148")

    def test_same_qq_reapplies_gensokyo_type5_mapping_without_status_change(self):
        """Repeating `qbind` for the same QQ is a local no-op, not a type=5 replay.

        Gensokyo answers a repeated type=5 write with ``已存在``, so the second
        command must reuse the recorded ``gsk_sync_state`` row and report the
        same success without a single remote write.
        """

        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="890779556",
            canonical_user_id="2338680148",
            bound_by_user_id="890779556",
            verification_source="test",
        )
        qbind.approve_personal_binding(
            self_id="bot-a",
            user_id="890779556",
            verified_by="trusted-reviewer",
            verification_source="test:review",
        )
        before = qbind.get_store().get_personal("bot-a", "890779556")
        event = FakeEvent("890779556")

        first = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg("2338680148"),
            )
        )

        # type=5 contract: virtual user oldRowValue, real QQ newRowValue.
        self.assertEqual(self.mapping.set_calls, [("890779556", "2338680148")])
        self.assertIn("映射已同步", first)

        qbind._last_cmd_time.clear()
        second = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                event,
                FakeBot(),
                PlainArg("2338680148"),
            )
        )

        # Same live row version: zero additional remote writes, same success.
        self.assertEqual(self.mapping.set_calls, [("890779556", "2338680148")])
        self.assertIn("映射已同步", second)
        self.assertNotIn("映射同步失败", second)
        after = qbind.get_store().get_personal("bot-a", "890779556")
        self.assertEqual(after.canonical_user_id, before.canonical_user_id)
        self.assertEqual(after.binding_status, before.binding_status)
        self.assertEqual(after.version, before.version)

    def test_same_qq_mapping_failure_is_reported_without_sync_success(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="890779556",
            canonical_user_id="2338680148",
            bound_by_user_id="890779556",
            verification_source="test",
        )
        self.mapping.set_error = GensokyoMappingError("remote unavailable")

        message = run(
            capture_handler(
                qbind.bind,
                qbind.bind_handler,
                FakeEvent("890779556"),
                FakeBot(),
                PlainArg("2338680148"),
            )
        )

        self.assertIn("映射同步失败", message)
        self.assertIn("remote unavailable", message)
        self.assertNotIn("映射已同步", message)
        self.assertEqual(self.mapping.set_calls, [("890779556", "2338680148")])
        self.assertEqual(
            qbind.get_binding_status("890779556", "bot-a"), qbind.storage.PERSONAL_DECLARED
        )

    def test_remote_mapping_is_compensated_when_local_declaration_fails(self):
        store = qbind.get_store()
        original_declare = store.declare_personal

        def fail_declare(**kwargs):
            raise qbind.QbindStorageError("simulated local write failure")

        store.declare_personal = fail_declare
        try:
            request = store.create_confirmation(
                request_kind="personal_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user-1",
                external_user_id="virtual-user-1",
                candidate_id="2338680148",
                ttl_seconds=120,
            )
            message = run(
                capture_handler(
                    qbind.confirm,
                    qbind.confirm_handler,
                    FakeEvent("virtual-user-1"),
                    FakeBot(),
                    PlainArg(request.request_id),
                )
            )
        finally:
            store.declare_personal = original_declare

        self.assertIn("失败", message)
        self.assertEqual(self.mapping.set_calls, [("virtual-user-1", "2338680148")])
        self.assertEqual(self.mapping.clear_calls, ["virtual-user-1"])
        self.assertIsNone(store.get_personal("bot-a", "virtual-user-1"))

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

    def test_10_1_active_canonical_owner_is_not_hidden_by_newer_revoked_alias(self):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope="bot-a",
            external_user_id="owner-old",
            canonical_user_id="1234567",
            bound_by_user_id="owner-old",
            verification_source="test",
        )
        store.verify_personal(
            bot_scope="bot-a",
            external_user_id="owner-old",
            verified_by="owner-old",
            verification_source="test",
        )
        store.declare_personal(
            bot_scope="bot-a",
            external_user_id="owner-new",
            canonical_user_id="1234567",
            bound_by_user_id="owner-new",
            verification_source="test",
            allow_verified_canonical_reuse=True,
        )
        store.verify_personal(
            bot_scope="bot-a",
            external_user_id="owner-new",
            verified_by="owner-new",
            verification_source="test",
        )
        newer_alias = store.get_personal("bot-a", "owner-new")
        with store._transaction() as connection:
            connection.execute(
                "UPDATE personal_bindings SET binding_status = ?, version = ? "
                "WHERE bot_scope = ? AND external_user_id = ?",
                (
                    qbind.storage.PERSONAL_REVOKED,
                    newer_alias.version + 10,
                    "bot-a",
                    "owner-new",
                ),
            )
        active_owners = store.list_active_personal_by_canonical(
            "bot-a", "1234567"
        )
        active_owner_ids = [owner.external_user_id for owner in active_owners]
        self.assertIn("owner-old", active_owner_ids)
        self.assertNotIn("owner-new", active_owner_ids)

        request = store.create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="challenger",
            external_user_id="challenger",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        message = run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent("challenger"),
                FakeBot(),
                PlainArg(request.request_id),
            )
        )

        result = store.get_confirmation(request.request_id)
        self.assertIn("QQ 号绑定冲突", message)
        self.assertEqual(result.result_code, "conflict")
        self.assertEqual(result.status, qbind.storage.REQUEST_FAILED)
        self.assertEqual(self.mapping.set_calls, [])

    def test_10_1_gensokyo_mapping_failure_prevents_successful_declaration(self):
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
        self.assertIn("映射更新失败", message)
        result = qbind.get_store().get_confirmation(request.request_id)
        self.assertEqual(result.result_code, "failed")
        self.assertEqual(result.status, qbind.storage.REQUEST_FAILED)
        self.assertIsNone(qbind.get_real_qq("user-a", "bot-a"))
        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])

    def test_10_1_pending_reference_blocks_compensation_clear(self):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope="bot-b",
            external_user_id="user-a",
            canonical_user_id="1234567",
            bound_by_user_id="user-a",
            verification_source="test",
        )
        store.mark_personal_unbind_pending(
            bot_scope="bot-b", external_user_id="user-a"
        )
        request = store.create_confirmation(
            request_kind="personal_bind",
            bot_scope="bot-a",
            actor_user_id="user-a",
            external_user_id="user-a",
            candidate_id="1234567",
            ttl_seconds=60,
        )
        with patch.object(
            store,
            "declare_personal",
            side_effect=qbind.storage.QbindStorageError("simulated local save failure"),
        ):
            message = run(
                capture_handler(
                    qbind.confirm,
                    qbind.confirm_handler,
                    FakeEvent("user-a"),
                    FakeBot(),
                    PlainArg(request.request_id),
                )
            )

        self.assertIn("失败", message)
        self.assertEqual(self.mapping.set_calls, [("user-a", "1234567")])
        self.assertEqual(self.mapping.clear_calls, [])
        self.assertIsNone(store.get_personal("bot-a", "user-a"))
        self.assertEqual(
            store.get_personal("bot-b", "user-a").binding_status,
            qbind.storage.PERSONAL_UNBIND_PENDING,
        )

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
