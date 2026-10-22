from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_qbind import qbind

from src.plugins.qbind import group, mail
from src.plugins.qbind.mail import (
    QbindEmailService,
    SMTPConfig,
    SMTPMailSender,
    build_verification_message,
    code_digest,
)


class FakeSender:
    def __init__(self):
        self.messages: list[dict[str, str]] = []

    async def send_code(
        self, *, recipient: str, code: str, purpose: str, expires_minutes: int
    ) -> None:
        self.messages.append(
            {
                "recipient": recipient,
                "code": code,
                "purpose": purpose,
                "expires_minutes": str(expires_minutes),
            }
        )


class PlainArg:
    def __init__(self, value: str):
        self.value = value

    def extract_plain_text(self) -> str:
        return self.value


class FakeEvent:
    self_id = "bot-a"
    message_id = "message-1"
    group_id = None

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.group_id = None
        self.message_type = "private"
        self.sender = SimpleNamespace(role="member")

    def get_user_id(self) -> str:
        return self.user_id


class FakeGroupEvent(FakeEvent):
    def __init__(
        self,
        user_id: str,
        group_id: str = "virtual-group",
        role: str = "owner",
    ):
        super().__init__(user_id)
        self.group_id = group_id
        self.message_type = "group"
        self.sender = SimpleNamespace(role=role)


class FakeBot:
    async def get_group_info(self, *, group_id: str):
        return {"group_name": "Amia 测试群"}

    async def send(self, event, message):
        return None


class FakeGroupMappingClient:
    async def set_group_mapping(self, virtual_group_id: object, real_group_id: object):
        return None

    async def clear_group_mapping(self, virtual_group_id: object):
        return None


class FakeSMTPClient:
    def __init__(self):
        self.logged_in = False
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def login(self, username: str, password: str):
        self.logged_in = True

    def send_message(self, message):
        self.sent = True


class FinishSignal(Exception):
    def __init__(self, value):
        super().__init__(str(value))
        self.value = value


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


class TestEmailVerification(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-email-")
        root = Path(self.temporary.name)
        qbind.configure_storage(root / "qbind.sqlite3", root / "binds.json")
        self.original_service = qbind._email_service
        self.sender = FakeSender()
        self.service = QbindEmailService(
            SMTPConfig(
                host="smtp.test",
                port=465,
                use_ssl=True,
                username="bot@example.test",
                password="test-password",
                from_address="bot@example.test",
                timeout_seconds=5.0,
                challenge_ttl_seconds=600.0,
                max_attempts=5,
                resend_cooldown_seconds=0.0,
                required=True,
                code_secret="test-code-secret",
            ),
            sender=self.sender,
        )
        qbind.set_email_service(self.service)
        qbind._last_cmd_time.clear()
        self.original_markdown = qbind.USE_MARKDOWN
        qbind.USE_MARKDOWN = False

    def tearDown(self):
        qbind.USE_MARKDOWN = self.original_markdown
        qbind.set_email_service(self.original_service)
        self.temporary.cleanup()

    def test_recipient_is_derived_from_qq_and_only_hashes_are_stored(self):
        challenge, masked = run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user",
                external_user_id="virtual-user",
                candidate_id="1234567",
                qq_number="1234567",
            )
        )

        self.assertEqual(self.sender.messages[-1]["recipient"], "1234567@qq.com")
        self.assertEqual(masked, "12*****@qq.com")
        stored = qbind.get_store().get_email_challenge(challenge.challenge_id)
        self.assertIsNotNone(stored)
        self.assertNotIn("1234567@qq.com", stored.target_email_digest)
        self.assertNotEqual(stored.code_digest, self.sender.messages[-1]["code"])

    def test_verification_message_has_html_and_plain_text_fallback(self):
        for purpose in ("personal_bind", "group_bind", "group_rebind"):
            with self.subTest(purpose=purpose):
                message = build_verification_message(
                    from_address="Amia@mizuki.top",
                    recipient="1234567@qq.com",
                    code="483921",
                    purpose=purpose,
                    expires_minutes=10,
                    sent_at=datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc),
                )

                self.assertEqual(
                    message["Subject"],
                    "Amia_\u6653\u5c71\u745e\u5e0c \u9a8c\u8bc1\u7801",
                )
                self.assertNotIn("HongXing\u7f51\u7edc", message["Subject"])
                plain = message.get_body(preferencelist=("plain",)).get_content()
                html = message.get_body(preferencelist=("html",)).get_content()
                self.assertNotIn("HongXing\u7f51\u7edc", plain)
                self.assertNotIn("HongXing\u7f51\u7edc", html)
                for body in (plain, html):
                    self.assertIn("\u8eab\u4efd\u9a8c\u8bc1", body)
                    self.assertIn("483921", body)
                    self.assertIn("10 \u5206\u949f", body)
                    self.assertIn("2026-10-17 18:30 UTC", body)
                    self.assertIn(
                        "Amia_\u6653\u5c71\u745e\u5e0c \u9a8c\u8bc1\u7801",
                        body,
                    )
                    for forbidden in ("QQ", "\u7fa4", "QGroupBind"):
                        self.assertNotIn(forbidden, body)
                self.assertIn("Amia", html)
                self.assertIn("font-family", html)

    def test_qbindcode_button_prefills_command_without_submitting(self):
        original_markdown = qbind.USE_MARKDOWN
        qbind.USE_MARKDOWN = True
        try:
            message = qbind._reply(
                "### 📩 QQ 邮箱验证已发起",
                qbind._personal_email_code_buttons("virtual-user"),
            )

            self.assertEqual(message[0].type, "markdown")
            button = message[0].data["data"]["keyboard"]["content"]["rows"][0][
                "buttons"
            ][0]
            self.assertEqual(button["render_data"]["label"], "输入邮箱验证码")
            self.assertEqual(button["action"]["data"], "qbindcode ")
            self.assertFalse(button["action"]["enter"])
            self.assertEqual(
                button["action"]["permission"]["specify_user_ids"],
                ["virtual-user"],
            )
        finally:
            qbind.USE_MARKDOWN = original_markdown

    def test_qgcode_button_prefills_command_without_submitting(self):
        original_markdown = qbind.USE_MARKDOWN
        qbind.USE_MARKDOWN = True
        try:
            message = group._message(
                "### 📩 群主 QQ 邮箱验证已发起",
                group._group_email_code_buttons("group-owner"),
            )

            self.assertEqual(message[0].type, "markdown")
            button = message[0].data["data"]["keyboard"]["content"]["rows"][0][
                "buttons"
            ][0]
            self.assertEqual(button["render_data"]["label"], "输入群邮箱验证码")
            self.assertEqual(button["action"]["data"], "qgcode ")
            self.assertFalse(button["action"]["enter"])
            self.assertEqual(
                button["action"]["permission"]["specify_user_ids"],
                ["group-owner"],
            )
        finally:
            qbind.USE_MARKDOWN = original_markdown

    def test_smtp_handshake_timeout_retries_connection_only(self):
        config = SMTPConfig(
            host="smtp.test",
            port=465,
            use_ssl=True,
            username="bot@example.test",
            password="test-password",
            from_address="bot@example.test",
            timeout_seconds=5.0,
            challenge_ttl_seconds=600.0,
            max_attempts=5,
            resend_cooldown_seconds=0.0,
            required=True,
            code_secret="test-code-secret",
        )
        sender = SMTPMailSender(config)
        client = FakeSMTPClient()

        with (
            patch.object(
                mail.smtplib,
                "SMTP_SSL",
                side_effect=[TimeoutError("TLS handshake timed out"), client],
            ) as smtp_ssl,
            patch.object(mail.time, "sleep") as sleep,
        ):
            sender._send_sync(
                "1234567@qq.com",
                "123456",
                "personal_bind",
                10,
            )

        self.assertEqual(smtp_ssl.call_count, 2)
        sleep.assert_called_once_with(0.75)
        self.assertTrue(client.logged_in)
        self.assertTrue(client.sent)

    def test_wrong_codes_are_limited_and_correct_code_verifies(self):
        challenge, _ = run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user",
                external_user_id="virtual-user",
                candidate_id="1234567",
                qq_number="1234567",
            )
        )
        for _ in range(4):
            attempt = qbind.get_store().verify_email_challenge(
                challenge.challenge_id,
                code_digest(challenge.challenge_id, "000000", "test-code-secret"),
            )
            self.assertFalse(attempt.accepted)
            self.assertEqual(attempt.reason, "invalid_code")

        correct = self.sender.messages[-1]["code"]
        attempt = qbind.get_store().verify_email_challenge(
            challenge.challenge_id,
            code_digest(challenge.challenge_id, correct, "test-code-secret"),
        )
        self.assertTrue(attempt.accepted)
        self.assertEqual(attempt.reason, "verified")

    def test_same_id_requires_email_code_before_verification(self):
        user_id = "1234567"
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
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-a"))
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-b"))

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
        self.assertIn("邮箱验证已发起", message)
        self.assertEqual(self.sender.messages[-1]["recipient"], f"{user_id}@qq.com")
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-a"))
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-b"))

        mapping = unittest.mock.AsyncMock()
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        mapping.clear_group_mapping = unittest.mock.AsyncMock()
        with patch.object(qbind, "_mapping_client", mapping):
            message = run(
                capture_handler(
                    qbind.qbindcode,
                    qbind.qbindcode_handler,
                    event,
                    PlainArg(self.sender.messages[-1]["code"]),
                )
            )

        self.assertIn("邮箱认证", message)
        self.assertEqual(qbind.get_verified_qq(user_id, "bot-a"), user_id)
        self.assertIsNone(qbind.get_verified_qq(user_id, "bot-b"))
        mapping.set_personal_mapping.assert_awaited_once_with(user_id, user_id)

    def test_cross_bot_conflict_during_email_challenge_blocks_verification(self):
        virtual_user_id = "virtual-user"
        qq_a = "1234567"
        qq_b = "7654321"
        event = FakeEvent(virtual_user_id)
        event.self_id = "bot-b"
        run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-b",
                actor_user_id=virtual_user_id,
                external_user_id=virtual_user_id,
                candidate_id=qq_b,
                qq_number=qq_b,
            )
        )
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=virtual_user_id,
            canonical_user_id=qq_a,
            bound_by_user_id=virtual_user_id,
            verification_source="test",
        )
        mapping = unittest.mock.AsyncMock()
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        mapping.clear_group_mapping = unittest.mock.AsyncMock()

        with patch.object(qbind, "_mapping_client", mapping):
            message = run(
                capture_handler(
                    qbind.qbindcode,
                    qbind.qbindcode_handler,
                    event,
                    PlainArg(self.sender.messages[-1]["code"]),
                )
            )

        self.assertIn("个人映射冲突", str(message))
        self.assertNotIn(virtual_user_id, str(message))
        self.assertNotIn(qq_a, str(message))
        self.assertNotIn(qq_b, str(message))
        mapping.set_personal_mapping.assert_not_awaited()
        self.assertIsNone(qbind.get_store().get_personal("bot-b", virtual_user_id))
        self.assertIsNone(qbind.get_verified_qq(virtual_user_id, "bot-b"))

    def test_pending_cross_bot_conflict_blocks_email_verification(self):
        user_id = "virtual-pending-user"
        qq_a = "1234567"
        qq_b = "7654321"
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id=user_id,
            canonical_user_id=qq_a,
            bound_by_user_id=user_id,
            verification_source="test",
        )
        qbind.get_store().mark_personal_unbind_pending(
            bot_scope="bot-a", external_user_id=user_id
        )
        event = FakeEvent(user_id)
        event.self_id = "bot-b"
        run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-b",
                actor_user_id=user_id,
                external_user_id=user_id,
                candidate_id=qq_b,
                qq_number=qq_b,
            )
        )
        mapping = unittest.mock.AsyncMock()
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        mapping.clear_group_mapping = unittest.mock.AsyncMock()

        with patch.object(qbind, "_mapping_client", mapping):
            message = run(
                capture_handler(
                    qbind.qbindcode,
                    qbind.qbindcode_handler,
                    event,
                    PlainArg(self.sender.messages[-1]["code"]),
                )
            )

        self.assertIn("个人映射冲突", str(message))
        for identifier in (user_id, qq_a, qq_b):
            self.assertNotIn(identifier, str(message))
        mapping.set_personal_mapping.assert_not_awaited()
        self.assertIsNone(qbind.get_store().get_personal("bot-b", user_id))

    def test_qbindcode_promotes_binding_and_opens_only_same_bot_gate(self):
        user_id = "123456"
        event = FakeEvent(user_id)
        _, _ = run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-a",
                actor_user_id=user_id,
                external_user_id=user_id,
                candidate_id="1234567",
                qq_number="1234567",
            )
        )
        mapping = unittest.mock.AsyncMock()
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        mapping.clear_group_mapping = unittest.mock.AsyncMock()
        with patch.object(qbind, "_mapping_client", mapping):
            message = run(
                capture_handler(
                    qbind.qbindcode,
                    qbind.qbindcode_handler,
                    event,
                    PlainArg(self.sender.messages[-1]["code"]),
                )
            )
        self.assertTrue(run(qbind.ensure_context_bound(event, FakeBot())))
        other_bot_event = FakeEvent(user_id)
        other_bot_event.self_id = "bot-b"
        self.assertFalse(
            run(qbind.ensure_context_bound(other_bot_event, FakeBot()))
        )
        self.assertIn("完成邮箱认证", message)
        mapping.set_personal_mapping.assert_awaited_once_with(user_id, "1234567")
        mapping.set_group_mapping.assert_not_awaited()
        mapping.clear_group_mapping.assert_not_awaited()
        record = qbind.get_store().get_personal("bot-a", user_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.binding_status, "verified")
        self.assertEqual(record.canonical_user_id, "1234567")

    def test_qbindcode_syncs_verified_personal_mapping_to_gsk(self):
        user_id = "123456"
        _, _ = run(
            qbind.start_email_challenge(
                purpose="personal_bind", bot_scope="bot-a",
                actor_user_id=user_id, external_user_id=user_id,
                candidate_id="1234567", qq_number="1234567",
            )
        )
        mapping = unittest.mock.AsyncMock()
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        mapping.clear_group_mapping = unittest.mock.AsyncMock()
        with patch.object(qbind, "_mapping_client", mapping):
            message = run(capture_handler(
                qbind.qbindcode, qbind.qbindcode_handler, FakeEvent(user_id),
                PlainArg(self.sender.messages[-1]["code"]),
            ))
        self.assertIn("同步至 GSK", message)
        mapping.set_personal_mapping.assert_awaited_once_with(user_id, "1234567")
        mapping.set_group_mapping.assert_not_awaited()
        mapping.clear_group_mapping.assert_not_awaited()

    def test_qbindcode_sync_failure_keeps_verified_local_binding(self):
        user_id = "123456"
        _, _ = run(
            qbind.start_email_challenge(
                purpose="personal_bind", bot_scope="bot-a",
                actor_user_id=user_id, external_user_id=user_id,
                candidate_id="1234567", qq_number="1234567",
            )
        )
        mapping = unittest.mock.AsyncMock()
        mapping.set_personal_mapping.side_effect = qbind.GensokyoMappingError("unavailable")
        mapping.set_group_mapping = unittest.mock.AsyncMock()
        with patch.object(qbind, "_mapping_client", mapping):
            message = run(capture_handler(
                qbind.qbindcode, qbind.qbindcode_handler, FakeEvent(user_id),
                PlainArg(self.sender.messages[-1]["code"]),
            ))
        self.assertIn("GSK 同步失败", message)
        self.assertIn("本地绑定已验证", message)
        self.assertIn("尚未同步", message)
        self.assertNotIn("已同步", message)
        self.assertNotIn(user_id, str(message))
        self.assertNotIn("1234567", str(message))
        record = qbind.get_store().get_personal("bot-a", user_id)
        self.assertEqual(record.binding_status, "verified")
        self.assertEqual(mapping.set_personal_mapping.await_count, 1)
        self.assertEqual(mapping.set_group_mapping.await_count, 0)
        mapping.set_personal_mapping.side_effect = None
        with patch.object(qbind, "_mapping_client", mapping):
            retry_message = run(capture_handler(
                qbind.qbindcode, qbind.qbindcode_handler, FakeEvent(user_id),
                PlainArg(self.sender.messages[-1]["code"]),
            ))
        self.assertIn("已同步至 GSK", retry_message)
        self.assertEqual(
            mapping.set_personal_mapping.await_args_list,
            [
                unittest.mock.call(user_id, "1234567"),
                unittest.mock.call(user_id, "1234567"),
            ],
        )
        self.assertEqual(mapping.set_group_mapping.await_count, 0)
        mapping.clear_group_mapping.assert_not_awaited()
        record = qbind.get_store().get_personal("bot-a", user_id)
        self.assertEqual(record.binding_status, "verified")
        self.assertEqual(record.canonical_user_id, "1234567")

    def test_pending_email_challenge_remains_rate_limited(self):
        user_id = "123456"
        run(
            qbind.start_email_challenge(
                purpose="personal_bind", bot_scope="bot-a",
                actor_user_id=user_id, external_user_id=user_id,
                candidate_id="1234567", qq_number="1234567",
            )
        )
        code = self.sender.messages[-1]["code"]
        first = run(capture_handler(
            qbind.qbindcode, qbind.qbindcode_handler, FakeEvent(user_id),
            PlainArg("000000" if code != "000000" else "000001"),
        ))
        self.assertIn("验证码不正确", first)
        second = run(capture_handler(
            qbind.qbindcode, qbind.qbindcode_handler, FakeEvent(user_id),
            PlainArg(code),
        ))
        self.assertIn("操作过于频繁", second)
        record = qbind.get_store().get_personal("bot-a", user_id)
        self.assertIsNone(record)

    def test_group_owner_email_code_finalizes_group_binding(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="group-owner",
            canonical_user_id="1234567",
            bound_by_user_id="group-owner",
            verification_source="test",
        )
        qbind.get_store().verify_personal(
            bot_scope="bot-a",
            external_user_id="group-owner",
            verified_by="group-owner",
            verification_source="test:qq_email",
        )
        original_verifier = group.get_group_verifier()
        group.set_group_verifier(group.GensokyoGroupVerifier(FakeGroupMappingClient()))
        try:
            event = FakeGroupEvent("group-owner")
            bot = FakeBot()
            run(
                capture_handler(
                    group.qgbind,
                    group.qgbind_handler,
                    event,
                    bot,
                    PlainArg("9876543"),
                )
            )
            request = qbind.get_store().find_confirmation(
                request_kind="group_bind",
                bot_scope="bot-a",
                actor_user_id="group-owner",
                external_group_id="virtual-group",
            )
            self.assertIsNotNone(request)
            message = run(
                capture_handler(
                    group.qgconfirm,
                    group.qgconfirm_handler,
                    event,
                    bot,
                    PlainArg(request.request_id),
                )
            )
            self.assertIn("qgcode", message)
            self.assertEqual(self.sender.messages[-1]["recipient"], "1234567@qq.com")
            message = run(
                capture_handler(
                    group.qgcode,
                    group.qgcode_handler,
                    event,
                    PlainArg(self.sender.messages[-1]["code"]),
                )
            )
            self.assertIn("群主 QQ 邮箱认证完成", message)
            record = qbind.get_store().get_group("bot-a", "virtual-group")
            self.assertIsNotNone(record)
            self.assertEqual(record.binding_status, "verified")
        finally:
            group.set_group_verifier(original_verifier)

    def test_email_mode_requires_the_current_group_owner(self):
        qbind.get_store().declare_personal(
            bot_scope="bot-a",
            external_user_id="admin-1",
            canonical_user_id="1234567",
            bound_by_user_id="admin-1",
            verification_source="test",
        )
        qbind.get_store().verify_personal(
            bot_scope="bot-a",
            external_user_id="admin-1",
            verified_by="admin-1",
            verification_source="test:qq_email",
        )

        message = run(
            capture_handler(
                group.qgbind,
                group.qgbind_handler,
                FakeGroupEvent("admin-1", role="admin"),
                FakeBot(),
                PlainArg("9876543"),
            )
        )
        self.assertIn("只有当前群主", str(message))
        self.assertIsNone(
            qbind.get_store().find_confirmation(
                request_kind="group_bind",
                bot_scope="bot-a",
                actor_user_id="admin-1",
                external_group_id="virtual-group",
                candidate_id="9876543",
            )
        )


if __name__ == "__main__":
    unittest.main()
