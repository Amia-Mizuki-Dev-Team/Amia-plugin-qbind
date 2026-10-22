"""Focused coverage for the SUPERUSER personal-mapping batch command."""

from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace

from test_acceptance_matrix import (
    AcceptanceBase,
    FinishSignal,
    PlainArg,
    capture_handler,
    run,
)


class BatchArg(PlainArg):
    def __str__(self):
        return self.value


class BatchMappingSpy:
    def __init__(self, failures=None):
        self.calls = []
        self.group_calls = []
        self.failures = failures or set()

    async def set_personal_mapping(self, virtual_user_id, real_qq):
        pair = (str(virtual_user_id), str(real_qq))
        self.calls.append(pair)
        if pair in self.failures:
            raise RuntimeError(f"private failure {pair[0]} {pair[1]}")

    async def clear_personal_mapping(self, virtual_user_id):
        return None

    async def set_group_mapping(self, *args):
        self.group_calls.append(("set", args))

    async def clear_group_mapping(self, *args):
        self.group_calls.append(("clear", args))


class TestPersonalMappingBatch(AcceptanceBase):
    def setUp(self):
        super().setUp()
        self.spy = BatchMappingSpy()
        qbind.set_mapping_client(self.spy)
        self.bot = SimpleNamespace()

    def add(self, bot, virtual, qq, *, verified=False):
        store = qbind.get_store()
        store.declare_personal(
            bot_scope=bot,
            external_user_id=virtual,
            canonical_user_id=qq,
            bound_by_user_id="operator",
            verification_source="test",
            allow_verified_canonical_reuse=True,
        )
        if verified:
            store.verify_personal(
                bot_scope=bot, external_user_id=virtual, verified_by="operator",
                verification_source="test",
            )

    def invoke(self, text=""):
        async def finish(value=None, *args, **kwargs):
            raise FinishSignal(value)

        matcher = SimpleNamespace(finish=finish)
        return run(
            capture_handler(
                matcher,
                qbind.handle_personal_mapping_resync,
                self.bot,
                matcher,
                BatchArg(text),
            )
        )

    def test_default_preview_reports_counts_and_performs_no_remote_calls(self):
        self.add("bot-a", "virtual-a", "qq-a")
        self.add("bot-b", "virtual-b", "qq-b", verified=True)

        result = self.invoke()

        self.assertIn("待同步 2 条", result)
        self.assertEqual(self.spy.calls, [])
        self.assertEqual(self.spy.group_calls, [])

    def test_conflicting_cross_bot_targets_abort_before_any_remote_write(self):
        self.add("bot-a", "same-virtual", "qq-a")
        self.add("bot-b", "same-virtual", "qq-b")

        result = self.invoke("执行")

        self.assertIn("跨 Bot 映射冲突", result)
        self.assertIn("未调用远端接口", result)
        self.assertEqual(self.spy.calls, [])
        self.assertEqual(self.spy.group_calls, [])

    def test_unbind_pending_divergent_cross_bot_target_aborts_batch_write(self):
        self.add("bot-a", "same-virtual", "qq-a")
        self.add("bot-b", "same-virtual", "qq-b")
        qbind.get_store().mark_personal_unbind_pending(
            bot_scope="bot-b", external_user_id="same-virtual"
        )

        result = self.invoke("执行")

        self.assertIn("跨 Bot 映射冲突", result)
        self.assertEqual(self.spy.calls, [])
        self.assertEqual(self.spy.group_calls, [])

    def test_unbind_pending_identical_cross_bot_pair_is_not_a_conflict(self):
        self.add("bot-a", "same-virtual", "same-qq")
        self.add("bot-b", "same-virtual", "same-qq")
        qbind.get_store().mark_personal_unbind_pending(
            bot_scope="bot-b", external_user_id="same-virtual"
        )

        result = self.invoke("执行")

        self.assertEqual(self.spy.calls, [("same-virtual", "same-qq")])
        self.assertIn("成功 1 条", result)
        self.assertNotIn("映射冲突", result)

    def test_identical_cross_bot_pairs_are_synced_once(self):
        self.add("bot-a", "same-virtual", "same-qq")
        self.add("bot-b", "same-virtual", "same-qq")

        result = self.invoke("执行")

        self.assertEqual(self.spy.calls, [("same-virtual", "same-qq")])
        self.assertIn("成功 1 条", result)
        self.assertIn("重复跳过 1 条", result)
        self.assertEqual(self.spy.group_calls, [])

    def test_inactive_pending_and_revoked_rows_are_not_written(self):
        self.add("bot-a", "active-virtual", "active-qq")
        self.add("bot-a", "verified-virtual", "verified-qq", verified=True)
        self.add("bot-a", "pending-virtual", "pending-qq")
        store = qbind.get_store()
        store.mark_personal_unbind_pending(
            bot_scope="bot-a", external_user_id="pending-virtual"
        )
        self.add("bot-a", "revoked-virtual", "revoked-qq")
        with store._transaction() as connection:
            connection.execute(
                "UPDATE personal_bindings SET binding_status = 'revoked' "
                "WHERE bot_scope = ? AND external_user_id = ?",
                ("bot-a", "revoked-virtual"),
            )

        result = self.invoke("执行")

        self.assertEqual(
            self.spy.calls,
            [
                ("active-virtual", "active-qq"),
                ("verified-virtual", "verified-qq"),
            ],
        )
        self.assertIn("成功 2 条", result)
        self.assertEqual(self.spy.group_calls, [])

    def test_failure_aggregates_without_leaking_identifiers_or_exception(self):
        secret_virtual, secret_qq, secret_error = "secret-virtual-921", "secret-qq-842", "secret-backend-error"
        self.add("bot-a", secret_virtual, secret_qq)
        self.add("bot-b", "good-virtual", "good-qq")
        self.spy.failures.add((secret_virtual, secret_qq))
        # Ensure the exception text itself contains sensitive values.
        original_set = self.spy.set_personal_mapping

        async def failing_set(virtual_user_id, real_qq):
            pair = (str(virtual_user_id), str(real_qq))
            self.spy.calls.append(pair)
            if pair == (secret_virtual, secret_qq):
                raise RuntimeError(secret_error)
            await asyncio.sleep(0)

        self.spy.set_personal_mapping = failing_set
        with self.assertLogs("src.plugins.qbind", level=logging.ERROR) as captured:
            result = self.invoke("执行")

        self.assertEqual(
            self.spy.calls,
            [("good-virtual", "good-qq"), (secret_virtual, secret_qq)],
        )
        self.assertIn("成功 1 条", result)
        self.assertIn("失败 1 条", result)
        for sensitive in (secret_virtual, secret_qq, secret_error):
            self.assertNotIn(sensitive, result)
            self.assertNotIn(sensitive, "\n".join(captured.output))
        self.assertEqual(self.spy.group_calls, [])


# Import the module as a short name, matching the existing standalone tests.
from test_qbind import qbind

