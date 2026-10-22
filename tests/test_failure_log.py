"""Tests for the bounded on-disk failure log (``failure_log.py``).

``failure_log`` resolves ``LOG_DIR``/``LOG_FILE`` once, at import time, so a
single test process can only exercise one data directory.  Every case that needs
a different ``QBIND_DATA_DIR`` therefore runs a short probe in a subprocess that
imports ``failure_log.py`` standalone (it has no package-relative imports) under
a scrubbed environment.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from test_qbind import qbind  # noqa: F401  -- bootstraps QBIND_DATA_DIR before the import below

from src.plugins.qbind import failure_log

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "failure_log.py"
MAX_ENTRIES = 100
PROBE_TIMEOUT_SECONDS = 120

_PROBE_PREAMBLE = '''\
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("failure_log_probe", r"{module}")
assert spec is not None and spec.loader is not None
failure_log = importlib.util.module_from_spec(spec)
spec.loader.exec_module(failure_log)

BLOCKER = Path(os.environ["QBIND_DATA_DIR"])


def emit_boom(logger):
    logger.error("boom")
'''


class _ProbeTestCase(unittest.TestCase):
    """Runs ``failure_log`` in a subprocess whose data dir points at a temp path."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qbind-failure-log-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_probe(self, body: str, *, data_dir: Path) -> dict:
        script = self.root / "probe.py"
        script.write_text(
            _PROBE_PREAMBLE.format(module=MODULE_PATH) + textwrap.dedent(body),
            encoding="utf-8",
        )

        # Drop any QBIND_* inherited from the suite so each case owns its paths.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("QBIND_")
        }
        env["QBIND_DATA_DIR"] = str(data_dir)
        env["PYTHONIOENCODING"] = "utf-8"

        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"probe exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

        output = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(output, msg="probe produced no result line")
        return json.loads(output[-1])

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


class TestFailureLogTrim(_ProbeTestCase):
    def test_one_hundred_and_five_records_keep_the_newest_hundred(self):
        result = self.run_probe(
            """
            for index in range(1, 106):
                failure_log.record_failure(f"op-{index:03d}")
            text = failure_log.LOG_FILE.read_text(encoding="utf-8")
            print(json.dumps({"lines": text.splitlines(), "log_file": str(failure_log.LOG_FILE)}))
            """,
            data_dir=self.data_dir,
        )

        lines = result["lines"]
        self.assertEqual(len(lines), MAX_ENTRIES)
        operations = [json.loads(line)["operation"] for line in lines]
        self.assertEqual(
            operations, [f"op-{index:03d}" for index in range(6, 106)]
        )
        self.assertEqual(Path(result["log_file"]), self.data_dir / "qbind_log" / "failures.jsonl")


class TestFailureLogHandler(_ProbeTestCase):
    def test_logger_error_writes_one_record_naming_the_calling_function(self):
        result = self.run_probe(
            """
            logger = logging.getLogger("qbind.test.failure-log")
            failure_log.install_failure_handler(logger)
            emit_boom(logger)
            text = failure_log.LOG_FILE.read_text(encoding="utf-8")
            print(json.dumps({"lines": text.splitlines()}))
            """,
            data_dir=self.data_dir,
        )

        self.assertEqual(len(result["lines"]), 1)
        entry = json.loads(result["lines"][0])
        self.assertEqual(entry["operation"], "qbind.test.failure-log:emit_boom")
        self.assertIn("boom", entry["detail"])

    def test_installing_the_handler_three_times_keeps_one_handler_and_one_record(self):
        result = self.run_probe(
            """
            logger = logging.getLogger("qbind.test.idempotent")
            for _ in range(3):
                failure_log.install_failure_handler(logger)
            emit_boom(logger)
            text = failure_log.LOG_FILE.read_text(encoding="utf-8")
            print(json.dumps({"handlers": len(logger.handlers), "lines": text.splitlines()}))
            """,
            data_dir=self.data_dir,
        )

        self.assertEqual(result["handlers"], 1)
        self.assertEqual(len(result["lines"]), 1)


class TestFailureLogIsBestEffort(_ProbeTestCase):
    def test_unwritable_data_dir_does_not_raise_and_touches_nothing(self):
        blocker = self.root / "data"
        blocker.write_text("sentinel", encoding="utf-8")

        result = self.run_probe(
            """
            returned = failure_log.record_failure("op-on-blocked-dir", RuntimeError("boom"))
            print(json.dumps({
                "returned_none": returned is None,
                "log_dir_exists": failure_log.LOG_DIR.exists(),
                "blocker_is_file": BLOCKER.is_file(),
                "blocker_text": BLOCKER.read_text(encoding="utf-8"),
            }))
            """,
            data_dir=blocker,
        )

        self.assertTrue(result["returned_none"])
        self.assertFalse(result["log_dir_exists"])
        self.assertTrue(result["blocker_is_file"])
        self.assertEqual(result["blocker_text"], "sentinel")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()), ["data", "probe.py"]
        )

    def test_missing_error_is_recorded_as_null_not_the_string_none(self):
        result = self.run_probe(
            """
            failure_log.record_failure("op-without-error")
            raw = failure_log.LOG_FILE.read_text(encoding="utf-8").splitlines()[0]
            print(json.dumps({"raw": raw, "entry": json.loads(raw)}))
            """,
            data_dir=self.data_dir,
        )

        entry = result["entry"]
        self.assertIsNone(entry.get("error_type"))
        self.assertEqual(entry["message"], "")
        self.assertNotIn("detail", entry)
        self.assertNotIn("None", result["raw"])


class TestFailureLogBootstrap(unittest.TestCase):
    def test_suite_redirects_the_import_time_log_dir_outside_the_repository(self):
        log_dir = Path(failure_log.LOG_DIR).resolve()

        self.assertEqual(log_dir.name, "qbind_log")
        self.assertNotEqual(log_dir, (PLUGIN_ROOT / "data" / "qbind_log").resolve())
        self.assertFalse(
            str(log_dir).startswith(str(PLUGIN_ROOT.resolve()) + os.sep),
            msg=f"{log_dir} still points into the repository",
        )

    def test_plugin_logger_mirrors_errors_into_the_redirected_log(self):
        token = "bootstrap-probe-token"
        qbind.logger.error(token)

        text = Path(failure_log.LOG_FILE).read_text(encoding="utf-8")
        self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
