import asyncio
import json

import httpx
import pytest

from app.main import create_app

DEVICE = {"id": "device-123", "timeout": 60, "alert_email": "admin@critmon.com"}


def short(timeout: float, monitor_id: str = "device-123") -> dict:
    return {**DEVICE, "id": monitor_id, "timeout": timeout}


@pytest.fixture
def alerts() -> list[str]:
    return []


@pytest.fixture
async def client(alerts):
    app = create_app(on_alert=lambda m: alerts.append(m.id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.state.service.shutdown()


# --- User story 1: registering a monitor ---------------------------------------------


async def test_register_returns_201_with_a_confirmation(client):
    res = await client.post("/monitors", json=DEVICE)

    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "device-123"
    assert body["status"] == "active"
    assert "60 seconds" in body["message"]


async def test_registering_a_duplicate_id_returns_409(client):
    await client.post("/monitors", json=DEVICE)

    res = await client.post("/monitors", json=DEVICE)

    assert res.status_code == 409
    assert res.json() == {"error": "Monitor 'device-123' already exists."}


@pytest.mark.parametrize(
    "patch",
    [
        {"id": ""},
        {"id": "has space"},
        {"id": "x" * 65},
        {"timeout": 0},
        {"timeout": -5},
        {"timeout": "60"},
        {"timeout": True},
        {"timeout": 999_999_999},
        {"alert_email": "not-an-email"},
        {"unexpected": "field"},
    ],
)
async def test_invalid_registration_returns_422(client, patch):
    res = await client.post("/monitors", json={**DEVICE, **patch})

    assert res.status_code == 422


@pytest.mark.parametrize("missing", ["id", "timeout", "alert_email"])
async def test_missing_field_returns_422(client, missing):
    body = {k: v for k, v in DEVICE.items() if k != missing}

    res = await client.post("/monitors", json=body)

    assert res.status_code == 422


# --- User story 2: heartbeat ---------------------------------------------------------


async def test_heartbeat_returns_200(client):
    await client.post("/monitors", json=DEVICE)

    res = await client.post("/monitors/device-123/heartbeat")

    assert res.status_code == 200
    assert res.json()["status"] == "active"
    assert "Timer reset to 60 seconds" in res.json()["message"]


async def test_heartbeat_for_unknown_monitor_returns_404(client):
    res = await client.post("/monitors/ghost/heartbeat")

    assert res.status_code == 404
    assert res.json() == {"error": "Monitor 'ghost' not found."}


async def test_heartbeat_postpones_the_alert(client, alerts):
    await client.post("/monitors", json=short(0.4))

    await asyncio.sleep(0.25)
    await client.post("/monitors/device-123/heartbeat")
    await asyncio.sleep(0.3)  # past the original deadline
    assert alerts == []

    await asyncio.sleep(0.3)  # past the reset deadline
    assert alerts == ["device-123"]


# --- User story 3: the alert ---------------------------------------------------------


async def test_alert_fires_when_heartbeats_stop(client, alerts):
    await client.post("/monitors", json=short(0.1))

    await asyncio.sleep(0.3)

    assert alerts == ["device-123"]


async def test_alert_is_logged_to_the_console_as_json(capsys):
    app = create_app()  # default alert channel
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/monitors", json=short(0.1))
        await asyncio.sleep(0.3)

    alert = json.loads(capsys.readouterr().out)
    assert alert["ALERT"] == "Device device-123 is down!"
    assert alert["time"]
    app.state.service.shutdown()


async def test_heartbeat_after_an_alert_recovers_the_monitor(client):
    await client.post("/monitors", json=short(0.1))
    await asyncio.sleep(0.3)

    res = await client.post("/monitors/device-123/heartbeat")

    assert res.status_code == 200
    assert res.json()["status"] == "active"
    assert "recovered" in res.json()["message"]


# --- Bonus: pause --------------------------------------------------------------------


async def test_pause_returns_200_and_stops_alerts(client, alerts):
    await client.post("/monitors", json=short(0.1))

    res = await client.post("/monitors/device-123/pause")
    await asyncio.sleep(0.3)

    assert res.status_code == 200
    assert res.json()["status"] == "paused"
    assert alerts == []


async def test_pause_unknown_monitor_returns_404(client):
    res = await client.post("/monitors/ghost/pause")

    assert res.status_code == 404


async def test_heartbeat_unpauses_the_monitor(client, alerts):
    await client.post("/monitors", json=short(0.15))
    await client.post("/monitors/device-123/pause")

    res = await client.post("/monitors/device-123/heartbeat")

    assert res.json()["status"] == "active"
    assert "resumed" in res.json()["message"]

    await asyncio.sleep(0.35)
    assert alerts == ["device-123"]  # monitored again, so it can alert again
