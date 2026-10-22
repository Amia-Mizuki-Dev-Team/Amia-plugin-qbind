"""Bounded on-disk failure log for QBind.

Operators often run the bot without a terminal attached, so every failure that
reaches the plugin loggers is mirrored here as one JSON object per line.  The
file is capped at :data:`MAX_ENTRIES` records: it is a rolling window for
troubleshooting, not an audit trail.

Nothing in this module may raise: a broken failure log must never break a
business request.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 100

MESSAGE_LIMIT = 2000
DETAIL_LIMIT = 4000

# Same data-directory resolution as ``_DEFAULT_DB_PATH`` in ``__init__.py``.
LOG_DIR = (
    Path(os.getenv("QBIND_DATA_DIR", str(Path(__file__).with_name("data"))))
    / "qbind_log"
)
LOG_FILE = LOG_DIR / "failures.jsonl"

# Serialise writers: the plugin logs from event threads and from the store.
_LOCK = threading.Lock()

# Marks a logger that already owns a failure handler, so re-installing is a no-op.
_HANDLER_MARKER = "_qbind_failure_handler"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def record_failure(
    operation: str,
    error: BaseException | None = None,
    *,
    detail: str | None = None,
) -> None:
    """Append one failure record and trim the file to the newest entries."""

    try:
        entry: dict[str, object] = {
            "ts": _utc_timestamp(),
            "operation": _truncate(str(operation), MESSAGE_LIMIT),
            "error_type": type(error).__name__ if error is not None else None,
            "message": _truncate(str(error), MESSAGE_LIMIT) if error is not None else "",
        }
        if detail is not None:
            entry["detail"] = _truncate(str(detail), DETAIL_LIMIT)
        line = json.dumps(entry, ensure_ascii=False)

        with _LOCK:
            # Created lazily so importing the plugin never touches the data dir.
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _trim_locked()
    except Exception:
        # The failure log is best-effort: never propagate into business code.
        pass


def _trim_locked() -> None:
    """Keep only the newest ``MAX_ENTRIES`` lines; caller holds ``_LOCK``."""

    with LOG_FILE.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) <= MAX_ENTRIES:
        return

    temporary = LOG_FILE.with_name(f"{LOG_FILE.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.writelines(lines[-MAX_ENTRIES:])
        os.replace(temporary, LOG_FILE)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


class _FailureLogHandler(logging.Handler):
    """Mirror ERROR-level records into the bounded failure log."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            record_failure(
                operation=f"{record.name}:{record.funcName}",
                detail=self.format(record),
            )
        except Exception:
            pass


def install_failure_handler(logger: logging.Logger) -> None:
    """Attach the failure handler to ``logger`` once per logger object."""

    if getattr(logger, _HANDLER_MARKER, False):
        return
    for handler in logger.handlers:
        if isinstance(handler, _FailureLogHandler):
            setattr(logger, _HANDLER_MARKER, True)
            return
    handler = _FailureLogHandler()
    logger.addHandler(handler)
    setattr(logger, _HANDLER_MARKER, True)
