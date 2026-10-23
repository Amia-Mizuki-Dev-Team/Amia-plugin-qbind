"""Canonical-owner takeover behaviour for personal QBind bindings.

These tests pin the ``confirm_bind`` branch that runs when the requested QQ
number is already claimed inside the same Bot namespace:

* an existing *verified* owner can be displaced only by passing the QQ mailbox
  challenge, and the takeover creates no ``qbind:qq_email_alias`` row;
* a *declared* / *unbind_pending* owner blocks the request outright;
* local declaration mode (email verification disabled) never sends mail.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_qbind import qbind

from src.plugins.qbind import mail
from src.plugins.qbind.mail import QbindEmailService, SMTPConfig

QQ = "1234567"
OWNER = "owner-identity"
INTRUDER = "current-identity"
SCOPE = "bot-a"


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
    def __init__(self, user_id: str, *, self_id: str = SCOPE):
        self.self_id = self_id
        self.user_id = user_id
        self.group_id = None
        self.message_id = str(id(self))
        self.message_type = "private"
        self.sender = SimpleNamespace(role="member")

    def get_user_id(self) -> str:
        return self.user_id


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


class FakeMapping:
    def __init__(self):
        self.personal_set_calls: list[tuple[str, str]] = []

    async def set_personal_mapping(self, virtual_user_id, real_qq):
        self.personal_set_calls.append((str(virtual_user_id), str(real_qq)))

    async def clear_personal_mapping(self, virtual_user_id):
        return None

    async def set_group_mapping(self, virtual_group_id, real_group_id):
        return None

    async def clear_group_mapping(self, virtual_group_id):
        return None


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


class TakeoverBase(unittest.TestCase):
    email_required = True

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-takeover-")
        root = Path(self.temporary.name)
        self.original_email_service = qbind._email_service
        self.original_mapping = qbind._mapping_client
        self.original_markdown = qbind.USE_MARKDOWN
        qbind.configure_storage(root / "qbind.sqlite3", root / "binds.json")
        qbind.USE_MARKDOWN = False
        qbind._last_cmd_time.clear()
        self.sender = FakeSender()
        self.mapping = FakeMapping()
        qbind.set_mapping_client(self.mapping)
        qbind.set_email_service(self.build_service(required=self.email_required))

    def tearDown(self):
        qbind.set_email_service(self.original_email_service)
        qbind.set_mapping_client(self.original_mapping)
        qbind.USE_MARKDOWN = self.original_markdown
        self.temporary.cleanup()

    def build_service(self, *, required: bool) -> QbindEmailService:
        return QbindEmailService(
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
                required=required,
                code_secret="test-code-secret",
            ),
            sender=self.sender,
        )

    # -- flow helpers -----------------------------------------------------
    def seed_owner(self, *, verified: bool = True) -> None:
        store = qbind.get_store()
        store.declare_personal(
            bot_scope=SCOPE,
            external_user_id=OWNER,
            canonical_user_id=QQ,
            bound_by_user_id=OWNER,
            verification_source="test:seed",
        )
        if verified:
            store.verify_personal(
                bot_scope=SCOPE,
                external_user_id=OWNER,
                verified_by=OWNER,
                verification_source="qbind:qq_email",
            )

    def start_request(self, user_id: str = INTRUDER):
        run(
            capture_handler(
                qbind.bind, qbind.bind_handler, FakeEvent(user_id), None, PlainArg(QQ)
            )
        )
        request = qbind.get_store().find_confirmation(
            request_kind="personal_bind",
            bot_scope=SCOPE,
            actor_user_id=user_id,
            external_user_id=user_id,
        )
        self.assertIsNotNone(request)
        return request

    def confirm(self, user_id: str = INTRUDER):
        request = self.start_request(user_id)
        return run(
            capture_handler(
                qbind.confirm,
                qbind.confirm_handler,
                FakeEvent(user_id),
                None,
                PlainArg(request.request_id),
            )
        )

    def submit_code(self, code: str, user_id: str = INTRUDER):
        return run(
            capture_handler(
                qbind.qbindcode,
                qbind.qbindcode_handler,
                FakeEvent(user_id),
                PlainArg(code),
            )
        )

    # -- database helpers -------------------------------------------------
    def scalar(self, sql: str, params: tuple = ()):
        store = qbind.get_store()
        with store._transaction() as connection:
            row = connection.execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def personal_row_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM personal_bindings"))

    def pending_challenge_count(self) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM email_challenges WHERE status = ?",
                ("pending",),
            )
        )

    def alias_row_count(self) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM personal_bindings WHERE verification_source = ?",
                ("qbind:qq_email_alias",),
            )
        )


class TestVerifiedOwnerTakeover(TakeoverBase):
    def test_verified_owner_sends_mailbox_challenge_without_new_binding(self):
        self.seed_owner()
        before = self.personal_row_count()

        message = self.confirm()

        self.assertIn("需验证归属", message)
        self.assertIn("qbindcode", message)
        self.assertIn("12∗∗∗∗∗@qq.com", message)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.sender.messages[0]["recipient"], f"{QQ}@qq.com")
        self.assertEqual(self.pending_challenge_count(), 1)
        self.assertEqual(self.personal_row_count(), before)
        self.assertIsNone(qbind.get_verified_qq(INTRUDER, SCOPE))

    def test_correct_code_transfers_canonical_without_event_alias(self):
        self.seed_owner()
        self.confirm()

        message = self.submit_code(self.sender.messages[-1]["code"])

        self.assertIn("个人 QQ 绑定已完成", message)
        self.assertEqual(qbind.get_binding_status(INTRUDER, SCOPE), "verified")
        self.assertEqual(qbind.get_verified_qq(INTRUDER, SCOPE), QQ)
        self.assertEqual(self.alias_row_count(), 0)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM personal_bindings WHERE external_user_id = ?",
                (QQ,),
            ),
            0,
        )
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM email_challenges WHERE status = ?",
                ("verified",),
            ),
            1,
        )
        owner = qbind.get_store().get_personal(SCOPE, OWNER)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.binding_status, "verified")


class TestDeclaredOwnerBlocksTakeover(TakeoverBase):
    def test_declared_owner_blocks_takeover_without_sending_mail(self):
        self.seed_owner(verified=False)
        before = self.personal_row_count()

        message = self.confirm()

        self.assertIn("该 QQ 号正被其他会话身份占用", message)
        self.assertIn("占用方尚未完成验证", message)
        self.assertNotIn("qbindcode", message)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(self.pending_challenge_count(), 0)
        self.assertEqual(self.personal_row_count(), before)
        self.assertIsNone(qbind.get_store().get_personal(SCOPE, INTRUDER))


class TestLocalDeclarationModeTakeover(TakeoverBase):
    email_required = False

    def test_verified_owner_requires_enabling_mail_authentication(self):
        self.seed_owner()
        before = self.personal_row_count()

        message = self.confirm()

        self.assertIn("该 QQ 号已被其他会话身份绑定", message)
        self.assertIn("邮箱认证", message)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(self.pending_challenge_count(), 0)
        self.assertEqual(self.personal_row_count(), before)
        self.assertIsNone(qbind.get_store().get_personal(SCOPE, INTRUDER))


class TestMaskedDisplay(unittest.TestCase):
    def test_receipt_mask_uses_unicode_asterisk(self):
        self.assertEqual(qbind._masked_for_display("12*****@qq.com"), "12∗∗∗∗∗@qq.com")

    def test_mail_mask_email_keeps_ascii_asterisk(self):
        self.assertEqual(mail.mask_email("12*****@qq.com"), "12*****@qq.com")


if __name__ == "__main__":
    unittest.main()
