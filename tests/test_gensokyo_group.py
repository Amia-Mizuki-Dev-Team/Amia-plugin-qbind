from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_qbind import qbind
from src.plugins.qbind import group
from src.plugins.qbind.gensokyo import (
    GensokyoMappingClient,
    GensokyoMappingError,
    GroupUserResolution,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *unused_args):
        return None

    async def get(self, url, *, params):
        self.calls.append((url, params))
        return self.response


class FakePairClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def resolve_group_user(self, virtual_group_id, virtual_user_id):
        self.calls.append((str(virtual_group_id), str(virtual_user_id)))
        if self.error is not None:
            raise self.error
        return self.result


class FakeGroupMappingClient:
    def __init__(self):
        self.set_calls = []
        self.clear_calls = []

    async def set_group_mapping(self, virtual_group_id, real_group_id):
        self.set_calls.append((str(virtual_group_id), str(real_group_id)))

    async def clear_group_mapping(self, virtual_group_id):
        self.clear_calls.append(str(virtual_group_id))


class TestGensokyoGroupVerification(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-gensokyo-")
        qbind.configure_storage(
            Path(self.temporary.name) / "qbind.sqlite3",
            Path(self.temporary.name) / "binds.json",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _verified_personal_user(self):
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

    def test_type11_resolves_virtual_group_and_user_pair(self):
        client = GensokyoMappingClient(base_url="http://127.0.0.1:15630")
        fake_http = FakeHttpClient(
            FakeResponse({"firstRealValue": "9876543", "secondRealValue": "1234567"})
        )
        with patch("src.plugins.qbind.gensokyo.httpx.AsyncClient", return_value=fake_http):
            result = asyncio.run(client.resolve_group_user("10001", "20002"))
        self.assertEqual(
            result,
            GroupUserResolution(real_group_id="9876543", real_user_id="1234567"),
        )
        self.assertEqual(
            fake_http.calls,
            [
                (
                    "http://127.0.0.1:15630/getid",
                    {"id": "10001", "subid": "20002", "type": "11"},
                )
            ],
        )

    def test_type11_pair_match_does_not_require_personal_identity(self):
        client = FakePairClient(
            result=GroupUserResolution(real_group_id="9876543", real_user_id="1234567")
        )
        verifier = group.GensokyoGroupVerifier(client)
        context = group.GroupVerificationContext(
            bot_scope="bot-a",
            virtual_group_id="10001",
            requested_real_group_id="9876543",
            requested_by_user_id="virtual-user-1",
            event=object(),
        )
        result = asyncio.run(verifier.verify(context, object()))
        self.assertEqual(result.status, "verified")
        self.assertEqual(client.calls, [("10001", "virtual-user-1")])

    def test_type11_pair_match_promotes_group_binding(self):
        self._verified_personal_user()
        client = FakePairClient(
            result=GroupUserResolution(real_group_id="9876543", real_user_id="1234567")
        )
        verifier = group.GensokyoGroupVerifier(client)
        context = group.GroupVerificationContext(
            bot_scope="bot-a",
            virtual_group_id="10001",
            requested_real_group_id="9876543",
            requested_by_user_id="virtual-user-1",
            event=object(),
        )
        result = asyncio.run(verifier.verify(context, object()))
        self.assertEqual(result.status, "verified")
        self.assertEqual(client.calls, [("10001", "virtual-user-1")])

    def test_type11_pair_match_completes_qgroupbind_transition(self):
        client = FakePairClient(
            result=GroupUserResolution(real_group_id="9876543", real_user_id="1234567")
        )
        verifier = group.GensokyoGroupVerifier(client)
        previous = group.get_group_verifier()
        group.set_group_verifier(verifier)
        try:
            request = qbind.get_store().create_confirmation(
                request_kind="group_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user-1",
                external_group_id="10001",
                candidate_id="9876543",
                ttl_seconds=60,
            )
            message = asyncio.run(
                group._confirm_group_bind(
                    type(
                        "Event",
                        (),
                        {
                            "self_id": "bot-a",
                            "group_id": "10001",
                            "sender": type("Sender", (), {"role": "admin"})(),
                            "get_user_id": lambda self: "virtual-user-1",
                        },
                    )(),
                    object(),
                    request,
                )
            )
        finally:
            group.set_group_verifier(previous)
        self.assertIn("已经完成", message)
        self.assertEqual(qbind.get_real_group("10001", "bot-a"), "9876543")

    def test_default_group_confirm_saves_local_declaration_without_type5(self):
        client = FakeGroupMappingClient()
        verifier = group.GensokyoGroupVerifier(client)
        previous = group.get_group_verifier()
        group.set_group_verifier(verifier)
        try:
            request = qbind.get_store().create_confirmation(
                request_kind="group_bind",
                bot_scope="bot-a",
                actor_user_id="virtual-user-1",
                external_group_id="10001",
                candidate_id="9876543",
                ttl_seconds=60,
            )
            message = asyncio.run(
                group._confirm_group_bind(
                    type(
                        "Event",
                        (),
                        {
                            "self_id": "bot-a",
                            "group_id": "10001",
                            "sender": type("Sender", (), {"role": "admin"})(),
                            "get_user_id": lambda self: "virtual-user-1",
                        },
                    )(),
                    object(),
                    request,
                )
            )
        finally:
            group.set_group_verifier(previous)
        self.assertIn("已保存", message)
        self.assertEqual(client.set_calls, [])
        self.assertEqual(qbind.get_real_group("10001", "bot-a"), "9876543")
        self.assertEqual(
            qbind.get_store().get_group("bot-a", "10001").verification_source,
            "qgroupbind:declared",
        )
        resolution = asyncio.run(
            group.resolve_group_identity("bot-a", "10001")
        )
        self.assertEqual(resolution.canonical_group_id, "9876543")
        self.assertEqual(resolution.verification_status, "declared")
        self.assertFalse(resolution.verified)
        self.assertEqual(
            asyncio.run(
                group.GroupIdentityResolver().get_real_group("bot-a", "10001")
            ),
            "9876543",
        )
        context = asyncio.run(
            qbind.resolve_context_identity(
                self_id="bot-a",
                user_id="virtual-user-1",
                group_id="10001",
            )
        )
        self.assertEqual(context["canonical_group_id"], "9876543")
        self.assertEqual(context["group_verification_status"], "declared")
        self.assertIsNone(
            qbind.get_real_group("10001", "bot-a", verified_only=True)
        )

    def test_type11_rejects_conflicting_verified_personal_identity(self):
        self._verified_personal_user()
        client = FakePairClient(
            result=GroupUserResolution(real_group_id="9876543", real_user_id="7654321")
        )
        verifier = group.GensokyoGroupVerifier(client)
        context = group.GroupVerificationContext(
            bot_scope="bot-a",
            virtual_group_id="10001",
            requested_real_group_id="9876543",
            requested_by_user_id="virtual-user-1",
            event=object(),
        )
        result = asyncio.run(verifier.verify(context, object()))
        self.assertEqual(result.status, "rejected")
        self.assertIn("真实用户", result.reason)

    def test_type11_mismatch_rejects_group_binding(self):
        self._verified_personal_user()
        client = FakePairClient(
            result=GroupUserResolution(real_group_id="9876544", real_user_id="1234567")
        )
        verifier = group.GensokyoGroupVerifier(client)
        context = group.GroupVerificationContext(
            bot_scope="bot-a",
            virtual_group_id="10001",
            requested_real_group_id="9876543",
            requested_by_user_id="virtual-user-1",
            event=object(),
        )
        result = asyncio.run(verifier.verify(context, object()))
        self.assertEqual(result.status, "rejected")
        self.assertIn("真实群号", result.reason)

    def test_type11_backend_failure_stays_pending(self):
        self._verified_personal_user()
        client = FakePairClient(
            error=GensokyoMappingError("backend unavailable", retryable=True)
        )
        verifier = group.GensokyoGroupVerifier(client)
        context = group.GroupVerificationContext(
            bot_scope="bot-a",
            virtual_group_id="10001",
            requested_real_group_id="9876543",
            requested_by_user_id="virtual-user-1",
            event=object(),
        )
        result = asyncio.run(verifier.verify(context, object()))
        self.assertEqual(result.status, "pending")
        self.assertIn("backend unavailable", result.reason)


if __name__ == "__main__":
    unittest.main()
