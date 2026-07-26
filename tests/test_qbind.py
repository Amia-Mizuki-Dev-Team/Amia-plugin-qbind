import unittest
import asyncio
import tempfile
import os
import json

import nonebot
nonebot.init()

from standalone_loader import load_core, load_qbind

core = load_core()
_real_require = nonebot.require
nonebot.require = lambda name: core if name == "amia_core" else _real_require(name)

load_qbind()
from src.plugins.amia_core.identity import UserIdentityKey
from src.plugins.qbind import _binds, _BINDS_FILE, _save_binds, is_bound, get_real_qq
from src.plugins.qbind.identity import QbindIdentityResolver

class TestQbind(unittest.TestCase):
    def setUp(self):
        # Backup binds file and clean memory bindings
        self.old_binds = _binds.copy()
        _binds.clear()

    def tearDown(self):
        # Restore memory bindings
        _binds.clear()
        _binds.update(self.old_binds)

    def test_binding_queries(self):
        # Test unbound
        self.assertFalse(is_bound("virtual_user_1"))
        self.assertIsNone(get_real_qq("virtual_user_1"))

        # Add bind
        _binds["virtual_user_1"] = "1234567"
        _binds["1234567"] = "1234567"

        # Test bound
        self.assertTrue(is_bound("virtual_user_1"))
        self.assertEqual(get_real_qq("virtual_user_1"), "1234567")

        # Test identity resolver
        resolver = QbindIdentityResolver()
        key = UserIdentityKey(self_id="test-bot", user_id="virtual_user_1")
        resolved = asyncio.run(resolver.resolve_identity(key))
        self.assertEqual(resolved.canonical_user_id, "1234567")
        self.assertEqual(resolved.opaque_id, "1234567")

        # Test unbound key
        key_unbound = UserIdentityKey(self_id="test-bot", user_id="virtual_user_not_bound")
        resolved_unbound = asyncio.run(resolver.resolve_identity(key_unbound))
        self.assertIsNone(resolved_unbound.canonical_user_id)
        self.assertEqual(resolved_unbound.opaque_id, "unbound:test-bot:virtual_user_not_bound")

if __name__ == "__main__":
    unittest.main()
