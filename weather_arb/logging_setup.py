"""Structured JSON logging built on the standard :mod:`logging` module.

Every record is emitted as a single-line JSON document so downstream log
shippers (Vector, Fluent Bit, Loki, CloudWatch, ...) can index fields without
regex parsing.  Call :func:`configure_logging` once at process start, then use
``logging.getLogger(__name__)`` everywhere.  Arbitrary structured context is
attached via the ``extra`` kwarg::

    log.info("order placed", extra={"context": {"ticker": ticker, "px": 98}})
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

# Attributes that live on every ``LogRecord`` and therefore must not be treated
# as user-supplied structured context.
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render :class:`logging.LogRecord` objects as compact JSON lines."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib name
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Explicit structured context passed via ``extra={"context": {...}}``.
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = context

        # Promote any other non-reserved attributes attached via ``extra=``.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key == "context":
                continue
            if key.startswith("_"):
                continue
            payload.setdefault(key, value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: repeated calls replace existing handlers rather than stacking
    duplicates.  ``level`` is any standard level name (``"DEBUG"`` ...).
    """
    # Structured app logs go to stdout; stderr is left for genuine crashes
    # (uncaught tracebacks), which keeps the launchd out/err split meaningful.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # aiosqlite / aiohttp can be noisy at DEBUG; keep them at WARNING.
    for noisy in ("aiosqlite", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
