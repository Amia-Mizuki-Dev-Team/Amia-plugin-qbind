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
        message = build_verification_message(
            from_address="Amia@mizuki.top",
            recipient="1234567@qq.com",
            code="483921",
            purpose="personal_bind",
            expires_minutes=10,
            sent_at=datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(message["Subject"], "Amia 身份绑定验证码")
        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("个人 QQ 绑定", plain)
        self.assertIn("483921", plain)
        self.assertIn("Amia_晓山瑞希 | HongXing技术团队", plain)
        self.assertIn("Amia", html)
        self.assertIn("个人 QQ 绑定", html)
        self.assertIn("font-family", html)
        self.assertIn("483921", html)
        self.assertIn("10 分钟", html)
        self.assertIn("Amia_晓山瑞希 | HongXing技术团队", html)

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

    def test_qbindcode_promotes_personal_binding_to_verified(self):
        _, _ = run(
            qbind.start_email_challenge(
                purpose="personal_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user",
                external_user_id="virtual-user",
                candidate_id="1234567",
                qq_number="1234567",
            )
        )
        message = run(
            capture_handler(
                qbind.qbindcode,
                qbind.qbindcode_handler,
                FakeEvent("virtual-user"),
                PlainArg(self.sender.messages[-1]["code"]),
            )
        )
        self.assertIn("完成邮箱认证", message)
        record = qbind.get_store().get_personal("bot-a", "virtual-user")
        self.assertIsNotNone(record)
        self.assertEqual(record.binding_status, "verified")
        self.assertEqual(record.canonical_user_id, "1234567")

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
