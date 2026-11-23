"""Monitors and their countdown timers (the dead man's switch).

Every monitor owns one timer, scheduled on the running asyncio event loop with
``loop.call_later``. A heartbeat cancels the timer and schedules a new one. If
the timer is allowed to run out, the monitor goes ``down`` and the alert
callback fires.

Everything here is synchronous code that runs on the event loop, so a heartbeat
and a timer expiry can never interleave: whichever the loop runs first wins.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

logger = logging.getLogger("pulse_check")


class Status(StrEnum):
    ACTIVE = "active"  # timer running
    PAUSED = "paused"  # timer stopped on purpose, no alerts
    DOWN = "down"  # timer ran out, alert has fired


class MonitorNotFound(Exception):
    def __init__(self, monitor_id: str) -> None:
        super().__init__(f"Monitor '{monitor_id}' not found.")


class MonitorAlreadyExists(Exception):
    def __init__(self, monitor_id: str) -> None:
        super().__init__(f"Monitor '{monitor_id}' already exists.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Monitor:
    id: str
    timeout: float  # seconds without a heartbeat before the alert fires
    alert_email: str
    created_at: datetime
    status: Status = Status.ACTIVE
    last_heartbeat_at: datetime | None = None
    last_alert_at: datetime | None = None
    deadline: float | None = None  # event-loop time at which the timer fires
    timer: asyncio.TimerHandle | None = field(default=None, repr=False)


class MonitorService:
    def __init__(
        self,
        on_alert: Callable[[Monitor], None],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._on_alert = on_alert
        self._now = now
        self._monitors: dict[str, Monitor] = {}

    # --- commands ---------------------------------------------------------------

    def register(self, monitor_id: str, timeout: float, alert_email: str) -> Monitor:
        if monitor_id in self._monitors:
            raise MonitorAlreadyExists(monitor_id)
        monitor = Monitor(monitor_id, timeout, alert_email, created_at=self._now())
        self._monitors[monitor_id] = monitor
        self._arm(monitor)
        return monitor

    def heartbeat(self, monitor_id: str) -> Monitor:
        """Reset the countdown. Also un-pauses a paused monitor and revives a down one."""
        monitor = self.get(monitor_id)
        monitor.status = Status.ACTIVE
        monitor.last_heartbeat_at = self._now()
        self._arm(monitor)
        return monitor

    def pause(self, monitor_id: str) -> Monitor:
        """Stop the timer. No alert fires until the next heartbeat."""
        monitor = self.get(monitor_id)
        self._disarm(monitor)
        monitor.status = Status.PAUSED
        return monitor

    def delete(self, monitor_id: str) -> None:
        monitor = self.get(monitor_id)
        self._disarm(monitor)
        del self._monitors[monitor_id]

    def shutdown(self) -> None:
        for monitor in self._monitors.values():
            self._disarm(monitor)

    # --- queries ----------------------------------------------------------------

    def get(self, monitor_id: str) -> Monitor:
        try:
            return self._monitors[monitor_id]
        except KeyError:
            raise MonitorNotFound(monitor_id) from None

    def list_monitors(self, status: Status | None = None) -> list[Monitor]:
        return [m for m in self._monitors.values() if status is None or m.status == status]

    def seconds_remaining(self, monitor: Monitor) -> float | None:
        """Time left on the countdown, or None when no timer is running."""
        if monitor.deadline is None:
            return None
        return max(0.0, monitor.deadline - asyncio.get_running_loop().time())

    # --- timer plumbing ---------------------------------------------------------

    def _arm(self, monitor: Monitor) -> None:
        self._disarm(monitor)
        loop = asyncio.get_running_loop()
        monitor.deadline = loop.time() + monitor.timeout
        monitor.timer = loop.call_later(monitor.timeout, self._expire, monitor)

    def _disarm(self, monitor: Monitor) -> None:
        if monitor.timer is not None:
            monitor.timer.cancel()
        monitor.timer = None
        monitor.deadline = None

    def _expire(self, monitor: Monitor) -> None:
        monitor.timer = None
        monitor.deadline = None
        monitor.status = Status.DOWN
        monitor.last_alert_at = self._now()
        try:
            self._on_alert(monitor)
        except Exception:
            # The status is already `down`; a broken alert channel must not
            # leave the monitor looking healthy.
            logger.exception("alert callback failed for monitor %r", monitor.id)
