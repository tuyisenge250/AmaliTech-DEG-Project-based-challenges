import asyncio

DEVICE = {"id": "device-123", "timeout": 60, "alert_email": "admin@critmon.com"}


def device(monitor_id: str, timeout: float = 60) -> dict:
    return {**DEVICE, "id": monitor_id, "timeout": timeout}


# --- GET /monitors/{id} --------------------------------------------------------------


async def test_get_shows_an_active_monitor(client):
    await client.post("/monitors", json=DEVICE)

    res = await client.get("/monitors/device-123")

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "device-123"
    assert body["status"] == "active"
    assert body["timeout"] == 60
    assert body["alert_email"] == "admin@critmon.com"
    assert 59 <= body["seconds_remaining"] <= 60
    assert body["created_at"]
    assert body["last_heartbeat_at"] is None
    assert body["last_alert_at"] is None


async def test_get_shows_the_time_of_the_last_heartbeat(client):
    await client.post("/monitors", json=DEVICE)
    await client.post("/monitors/device-123/heartbeat")

    body = (await client.get("/monitors/device-123")).json()

    assert body["last_heartbeat_at"] is not None


async def test_seconds_remaining_counts_down(client):
    await client.post("/monitors", json=device("device-123", timeout=5))

    first = (await client.get("/monitors/device-123")).json()["seconds_remaining"]
    await asyncio.sleep(0.3)
    second = (await client.get("/monitors/device-123")).json()["seconds_remaining"]

    assert second < first


async def test_get_shows_a_down_monitor(client):
    await client.post("/monitors", json=device("device-123", timeout=0.1))
    await asyncio.sleep(0.3)

    body = (await client.get("/monitors/device-123")).json()

    assert body["status"] == "down"
    assert body["seconds_remaining"] is None
    assert body["last_alert_at"] is not None


async def test_get_shows_a_paused_monitor(client):
    await client.post("/monitors", json=DEVICE)
    await client.post("/monitors/device-123/pause")

    body = (await client.get("/monitors/device-123")).json()

    assert body["status"] == "paused"
    assert body["seconds_remaining"] is None


async def test_get_unknown_monitor_returns_404(client):
    res = await client.get("/monitors/ghost")

    assert res.status_code == 404
    assert res.json() == {"error": "Monitor 'ghost' not found."}


# --- GET /monitors -------------------------------------------------------------------


async def test_list_is_empty_to_begin_with(client):
    res = await client.get("/monitors")

    assert res.status_code == 200
    assert res.json() == []


async def test_list_filters_by_status(client):
    await client.post("/monitors", json=device("alive"))
    await client.post("/monitors", json=device("repairing"))
    await client.post("/monitors", json=device("dead", timeout=0.1))
    await client.post("/monitors/repairing/pause")
    await asyncio.sleep(0.3)

    async def ids(query: str = "") -> list[str]:
        return [m["id"] for m in (await client.get(f"/monitors{query}")).json()]

    assert await ids() == ["alive", "repairing", "dead"]
    assert await ids("?status=active") == ["alive"]
    assert await ids("?status=paused") == ["repairing"]
    assert await ids("?status=down") == ["dead"]


async def test_list_rejects_an_unknown_status(client):
    res = await client.get("/monitors?status=exploded")

    assert res.status_code == 422


# --- DELETE /monitors/{id} -----------------------------------------------------------


async def test_delete_removes_the_monitor(client):
    await client.post("/monitors", json=DEVICE)

    res = await client.delete("/monitors/device-123")

    assert res.status_code == 204
    assert res.content == b""
    assert (await client.get("/monitors/device-123")).status_code == 404


async def test_deleted_monitor_never_alerts(client, alerts):
    await client.post("/monitors", json=device("device-123", timeout=0.1))
    await client.delete("/monitors/device-123")

    await asyncio.sleep(0.3)

    assert alerts == []


async def test_deleted_id_can_be_registered_again(client):
    await client.post("/monitors", json=DEVICE)
    await client.delete("/monitors/device-123")

    res = await client.post("/monitors", json=DEVICE)

    assert res.status_code == 201


async def test_delete_unknown_monitor_returns_404(client):
    res = await client.delete("/monitors/ghost")

    assert res.status_code == 404
