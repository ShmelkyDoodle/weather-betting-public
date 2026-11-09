"""Alerting: push notifications + a local alert log.

Kept dependency-free (stdlib ``urllib``) so it works from short CLI jobs. Every
alert is appended to ``logs/alerts.log`` and, if a push URL is configured
(``WA_NTFY_URL`` or ``WA_NTFY_TOPIC`` -> ntfy.sh), pushed to your phone. Subscribe
to your ntfy topic in the free ntfy app to get the pushes.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from .config import Settings

log = logging.getLogger(__name__)

_ALERT_LOG = "logs/alerts.log"


class AlertManager:
    """Sends alerts to ntfy (if configured) and always to the local alert log."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.ntfy_url

    def send(self, title: str, message: str, *, priority: str = "high",
             tags: str = "warning") -> None:
        """Record an alert and push it (best-effort — never raises).

        ``tags`` are ntfy emoji shortcodes, shown on the phone before the title;
        they are how a *trade fired* push is told apart at a glance from a *health
        warning* push. ``priority`` accepts ntfy's ``min|low|default|high|urgent``
        ("urgent" is the one that bypasses Do Not Disturb).

        ⚠ **Blocking.** This does a synchronous HTTP POST with a 10 s timeout, so
        never call it directly from the async engine — use ``asyncio.to_thread``.
        Stalling the event loop during a lock-in would cost exactly the latency
        the strategy exists to capture.
        """
        self._append("ALERT", title, message)
        if not self._url:
            return
        try:
            request = urllib.request.Request(
                self._url,
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": tags},
            )
            urllib.request.urlopen(request, timeout=10)  # noqa: S310 - user-set URL
        except Exception as exc:  # noqa: BLE001 - alerting must never crash a job
            log.warning("ntfy push failed: %s", exc)

    def log_ok(self, message: str) -> None:
        """Record a healthy check (no push)."""
        self._append("OK", "", message)

    @staticmethod
    def _append(kind: str, title: str, message: str) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "title": title,
            "message": message,
        }
        try:
            os.makedirs("logs", exist_ok=True)
            with open(_ALERT_LOG, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass
