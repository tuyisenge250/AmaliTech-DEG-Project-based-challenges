import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.alerts import log_alert
from app.monitors import (
    Monitor,
    MonitorAlreadyExists,
    MonitorNotFound,
    MonitorService,
    Status,
)

TIMEOUT = 0.3
EMAIL = "admin@critmon.com"


@pytest.fixture
def alerts() -> list[str]:
    return []


@pytest.fixture
def service(alerts):
    svc = MonitorService(on_alert=lambda m: alerts.append(m.id))
    yield svc
    svc.shutdown()


# --- registering ---------------------------------------------------------------------


async def test_register_starts_an_active_countdown(service):
    monitor = service.register("device-1", TIMEOUT, EMAIL)

    assert monitor.status == Status.ACTIVE
    remaining = service.seconds_remaining(monitor)
    assert remaining is not None and 0 < remaining <= TIMEOUT


async def test_registering_the_same_id_twice_is_rejected(service):
    service.register("device-1", TIMEOUT, EMAIL)

    with pytest.raises(MonitorAlreadyExists):
        service.register("device-1", TIMEOUT, EMAIL)


# --- the alert -----------------------------------------------------------------------


async def test_alert_fires_when_the_timer_runs_out(service, alerts):
    monitor = service.register("device-1", TIMEOUT, EMAIL)

    await asyncio.sleep(TIMEOUT / 2)
    assert alerts == []
    assert monitor.status == Status.ACTIVE

    await asyncio.sleep(TIMEOUT)
    assert alerts == ["device-1"]
    assert monitor.status == Status.DOWN
    assert monitor.last_alert_at is not None
    assert service.seconds_remaining(monitor) is None


async def test_alert_fires_only_once(service, alerts):
    service.register("device-1", TIMEOUT, EMAIL)

    await asyncio.sleep(TIMEOUT * 3)

    assert alerts == ["device-1"]


async def test_each_monitor_has_its_own_timer(service, alerts):
    service.register("short", 0.1, EMAIL)
    service.register("long", 5, EMAIL)

    await asyncio.sleep(0.25)

    assert alerts == ["short"]
    assert service.get("long").status == Status.ACTIVE


async def test_failing_alert_channel_still_marks_the_monitor_down():
    def broken(_: Monitor) -> None:
        raise RuntimeError("smtp is down")

    service = MonitorService(on_alert=broken)
    monitor = service.register("device-1", 0.1, EMAIL)

    await asyncio.sleep(0.25)

    assert monitor.status == Status.DOWN
    service.shutdown()


def test_log_alert_prints_the_expected_json(capsys):
    monitor = Monitor(
        "device-123", 60, EMAIL, created_at=datetime.now(timezone.utc), status=Status.DOWN
    )
    monitor.last_alert_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    log_alert(monitor)

    line = json.loads(capsys.readouterr().out)
    assert line["ALERT"] == "Device device-123 is down!"
    assert line["time"] == "2026-01-01T00:00:00+00:00"


# --- heartbeat -----------------------------------------------------------------------


async def test_heartbeat_resets_the_countdown(service, alerts):
    timeout = 0.4
    monitor = service.register("device-1", timeout, EMAIL)

    await asyncio.sleep(0.25)
    service.heartbeat("device-1")
    assert monitor.last_heartbeat_at is not None

    # 0.55s after registering: the original timer (0.4s) would have fired
    await asyncio.sleep(0.3)
    assert alerts == []
    assert monitor.status == Status.ACTIVE

    # but the reset timer does run out eventually
    await asyncio.sleep(0.3)
    assert alerts == ["device-1"]


async def test_heartbeat_for_unknown_monitor_raises(service):
    with pytest.raises(MonitorNotFound):
        service.heartbeat("ghost")


async def test_heartbeat_revives_a_down_monitor(service, alerts):
    monitor = service.register("device-1", 0.1, EMAIL)
    await asyncio.sleep(0.25)
    assert monitor.status == Status.DOWN

    service.heartbeat("device-1")
    assert monitor.status == Status.ACTIVE

    await asyncio.sleep(0.25)
    assert alerts == ["device-1", "device-1"]  # down again, so a second alert


# --- pause ---------------------------------------------------------------------------


async def test_pause_stops_the_timer_and_prevents_alerts(service, alerts):
    monitor = service.register("device-1", 0.1, EMAIL)

    service.pause("device-1")
    await asyncio.sleep(0.3)

    assert alerts == []
    assert monitor.status == Status.PAUSED
    assert service.seconds_remaining(monitor) is None


async def test_heartbeat_unpauses_and_restarts_the_timer(service, alerts):
    monitor = service.register("device-1", 0.15, EMAIL)
    service.pause("device-1")

    service.heartbeat("device-1")
    assert monitor.status == Status.ACTIVE

    await asyncio.sleep(0.3)
    assert alerts == ["device-1"]


async def test_pausing_twice_is_harmless(service):
    service.register("device-1", TIMEOUT, EMAIL)

    service.pause("device-1")
    monitor = service.pause("device-1")

    assert monitor.status == Status.PAUSED


async def test_pause_unknown_monitor_raises(service):
    with pytest.raises(MonitorNotFound):
        service.pause("ghost")


# --- delete / list -------------------------------------------------------------------


async def test_delete_cancels_the_timer_and_frees_the_id(service, alerts):
    service.register("device-1", 0.1, EMAIL)

    service.delete("device-1")
    await asyncio.sleep(0.25)

    assert alerts == []
    with pytest.raises(MonitorNotFound):
        service.get("device-1")
    service.register("device-1", TIMEOUT, EMAIL)  # id can be reused


async def test_list_can_filter_by_status(service):
    service.register("a", 5, EMAIL)
    service.register("b", 5, EMAIL)
    service.register("c", 0.05, EMAIL)
    service.pause("b")
    await asyncio.sleep(0.15)

    assert [m.id for m in service.list_monitors()] == ["a", "b", "c"]
    assert [m.id for m in service.list_monitors(Status.ACTIVE)] == ["a"]
    assert [m.id for m in service.list_monitors(Status.PAUSED)] == ["b"]
    assert [m.id for m in service.list_monitors(Status.DOWN)] == ["c"]
