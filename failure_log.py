"""Bounded on-disk failure log for QBind.

Operators often run the bot without a terminal attached, so every failure that
reaches the plugin loggers is mirrored here as one JSON object per line.  The
file is capped at :data:`MAX_ENTRIES` records: it is a rolling window for
troubleshooting, not an audit trail.

Nothing in this module may raise: a broken failure log must never break a
business request.

The log assumes a **single process** writing one data directory: writers inside
that process are serialised by a ``threading.Lock``, and there is no cross
process file lock.  Two bot processes pointed at the same data directory may
interleave their writes.
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

LOG_FILENAME = "failures.jsonl"

# How many consecutive trim failures degrade to an in-place rewrite.
_TRIM_FAILURE_LIMIT = 3


def _log_dir() -> Path:
    """Resolve the log directory from the environment on every call.

    ``QBIND_DB_PATH`` wins because the log belongs next to the store it
    describes; otherwise the plugin data directory is used.
    """

    db_path = os.getenv("QBIND_DB_PATH")
    if db_path:
        return Path(db_path).expanduser().parent / "qbind_log"
    data_dir = os.getenv("QBIND_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "qbind_log"
    return Path(__file__).with_name("data") / "qbind_log"


# Read-only compatibility aliases: the import-time snapshot of ``_log_dir()``.
# Every write resolves :func:`_log_dir` again, so setting ``QBIND_DB_PATH``
# after import still moves the log.
LOG_DIR = _log_dir()
LOG_FILE = LOG_DIR / LOG_FILENAME

# Serialise writers: the plugin logs from event threads and from the store.
_LOCK = threading.Lock()

# Marks a logger that already owns a failure handler, so re-installing is a no-op.
_HANDLER_MARKER = "_qbind_failure_handler"

# Marks a record already mirrored, so a child logger and its installed ancestors
# do not write the same failure once per handler.
_RECORD_MARKER = "_qbind_failure_recorded"

# Consecutive trim failures; ``_TRIM_FAILURE_LIMIT`` switches to in-place rewrite.
_trim_failures = 0


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(value: str, limit: int, *, keep_tail: bool = False) -> str:
    """Clamp ``value`` to ``limit`` characters.

    ``keep_tail`` drops the head instead of the tail: tracebacks carry the
    exception type and message at their end, which is the part worth keeping.
    """

    if len(value) <= limit:
        return value
    if limit <= 0:
        return ""
    if keep_tail:
        return "…" + value[-(limit - 1):] if limit > 1 else value[-1:]
    return value[:limit]


def _is_json_line(line: str) -> bool:
    """Whether ``line`` is one record this module could have written."""

    try:
        json.loads(line)
    except (TypeError, ValueError):
        return False
    return True


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
            entry["detail"] = _truncate(str(detail), DETAIL_LIMIT, keep_tail=True)
        line = json.dumps(entry, ensure_ascii=False)

        with _LOCK:
            # Resolved per call and created lazily, so importing the plugin never
            # touches the data dir.
            log_file = _log_dir() / LOG_FILENAME
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _trim_locked(log_file)
    except Exception:
        # The failure log is best-effort: never propagate into business code.
        pass


def _rewrite(log_file: Path, lines: list[str]) -> None:
    """Replace ``log_file`` with ``lines`` through an atomic rename."""

    temporary = log_file.with_name(f"{log_file.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.writelines(lines)
        os.replace(temporary, log_file)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _trim_locked(log_file: Path) -> None:
    """Keep only the newest ``MAX_ENTRIES`` records; caller holds ``_LOCK``.

    Unparsable lines (a half-written record from a hard kill) are dropped on the
    way, so a trimmed file always holds one valid JSON object per line.
    """

    global _trim_failures

    with log_file.open("r", encoding="utf-8") as handle:
        raw_lines = handle.readlines()
    lines = [line for line in raw_lines if _is_json_line(line)]
    if len(lines) <= MAX_ENTRIES and len(lines) == len(raw_lines):
        _trim_failures = 0
        return

    keep = lines[-MAX_ENTRIES:]
    try:
        _rewrite(log_file, keep)
    except Exception:
        # A failing rename must not lose the clamp: after repeated failures,
        # rewrite in place once per attempt instead.  Never raise from here.
        _trim_failures += 1
        if _trim_failures >= _TRIM_FAILURE_LIMIT:
            # A successful in-place rewrite deliberately does not clear the
            # counter: the atomic path is still broken, so the next trim has to
            # stay on the fallback instead of drifting past the cap again.
            try:
                with log_file.open("w", encoding="utf-8") as handle:
                    handle.writelines(keep)
            except Exception:
                pass
    else:
        _trim_failures = 0


class _FailureLogHandler(logging.Handler):
    """Mirror ERROR-level records into the bounded failure log."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        # Logging hands one record to every handler of the logger *and* of its
        # ancestors.  Marking the record keeps a parent/child pair of installed
        # handlers at exactly one line per failure.
        if getattr(record, _RECORD_MARKER, False):
            return
        setattr(record, _RECORD_MARKER, True)
        try:
            error = record.exc_info[1] if record.exc_info else None
            record_failure(
                operation=f"{record.name}:{record.funcName}",
                error=error,
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
