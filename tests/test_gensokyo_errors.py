"""Pure-function coverage for Gensokyo type=5 error decoding and classification.

No network is involved: response bodies are fed straight into ``_decode_body``
and ``_classify_mapping_error``, and the ``_update`` adapter is exercised
through an injected ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

# test_qbind installs the isolated ``src.plugins.qbind`` package namespace and
# initialises NoneBot before any sibling submodule is imported.
from test_qbind import qbind  # noqa: F401
from src.plugins.qbind import gensokyo
from src.plugins.qbind.gensokyo import (
    GensokyoMappingClient,
    GensokyoMappingExistsError,
    GensokyoMappingMissingError,
)

GBK_EXISTS_BODY = '{"error":"2338680148 :已存在"}'
UTF8_MISSING_BODY = '{"error":"不存在:890779556"}'
VIRTUAL_USER = "890779556"
REAL_USER = "2338680148"


def run(coro):
    return asyncio.run(coro)


class TestDecodeAndClassify(unittest.TestCase):
    def test_gbk_exists_body_is_classified_as_exists(self):
        response = SimpleNamespace(content=GBK_EXISTS_BODY.encode("gbk"))

        text = gensokyo._decode_body(response)

        self.assertIn("已存在", text)
        self.assertEqual(gensokyo._classify_mapping_error(text), "exists")

    def test_utf8_missing_body_is_classified_as_missing(self):
        response = SimpleNamespace(content=UTF8_MISSING_BODY.encode("utf-8"))

        text = gensokyo._decode_body(response)

        self.assertIn("不存在", text)
        self.assertEqual(gensokyo._classify_mapping_error(text), "missing")

    def test_negated_english_forms_are_missing_not_exists(self):
        cases = [
            ("key not found", "missing", "the substring 'found' must not imply exists"),
            ("does not exist", "missing", "'not exist' must pre-empt the 'exists' branch"),
            ("row not exist", "missing", "bare 'not exist' is still a missing row"),
            ("not exists", "missing", "negated 'exists' stays missing"),
        ]
        for text, expected, label in cases:
            with self.subTest(text=text, label=label):
                self.assertEqual(gensokyo._classify_mapping_error(text), expected)

    def test_exists_english_forms_are_classified_as_exists(self):
        cases = [
            ("mapping already exists", "an anchored 'already exists' is our target"),
            ("value exists already", "the inverted phrasing is still opportunistic"),
            ("duplicate mapping row", "a duplicate row is the same outcome"),
        ]
        for text, label in cases:
            with self.subTest(text=text, label=label):
                self.assertEqual(gensokyo._classify_mapping_error(text), "exists")

    def test_bare_exists_wording_is_not_absorbed(self):
        """Unanchored wording must stay unclassified.

        ``exists``/``already`` alone also show up in unrelated failures, and
        absorbing one of those would hide a write that never happened.
        """

        for text in ("mapping exists", "already mapped", "exists"):
            with self.subTest(text=text):
                self.assertEqual(gensokyo._classify_mapping_error(text), "")

    def test_unclassified_text_reports_no_kind(self):
        for text in ("", "   ", "internal server error", "rate limited"):
            with self.subTest(text=text):
                self.assertEqual(gensokyo._classify_mapping_error(text), "")

    def test_decode_body_handles_text_empty_and_missing_content(self):
        cases = [
            (SimpleNamespace(content='{"error":"x"}'), '{"error":"x"}', "str passes through"),
            (SimpleNamespace(content=b""), "", "empty bytes decode to empty text"),
            (SimpleNamespace(), "", "absent content is empty text"),
            (SimpleNamespace(content=12345), "", "non-text content is empty text"),
        ]
        for response, expected, label in cases:
            with self.subTest(label=label):
                self.assertEqual(gensokyo._decode_body(response), expected)


class TestType5UpdateErrors(unittest.TestCase):
    """Drive ``GensokyoMappingClient._update`` with an injected transport."""

    def _update_with(self, body: bytes):
        client = GensokyoMappingClient(
            base_url="http://127.0.0.1:15630", timeout=1.0
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        real_async_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        shim = SimpleNamespace(
            AsyncClient=factory, TimeoutException=httpx.TimeoutException
        )
        with patch.object(gensokyo, "httpx", shim):
            return run(client._update(VIRTUAL_USER, REAL_USER))

    def test_gbk_exists_body_raises_non_retryable_exists_error(self):
        with self.assertRaises(GensokyoMappingExistsError) as caught:
            self._update_with(GBK_EXISTS_BODY.encode("gbk"))

        self.assertIs(caught.exception.retryable, False)
        self.assertEqual(caught.exception.remote_value, REAL_USER)

    def test_utf8_missing_body_raises_non_retryable_missing_error(self):
        with self.assertRaises(GensokyoMappingMissingError) as caught:
            self._update_with(UTF8_MISSING_BODY.encode("utf-8"))

        self.assertIs(caught.exception.retryable, False)
        self.assertEqual(caught.exception.virtual_id, VIRTUAL_USER)

    def test_success_body_does_not_raise(self):
        body = json.dumps({"status": "success"}).encode("utf-8")

        self.assertIsNone(self._update_with(body))


if __name__ == "__main__":
    unittest.main()
