import asyncio
import time

import httpx
import pytest

from app import main
from app.main import CONFLICT_MESSAGE, create_app

DELAY = 0.3
PAYMENT = {"amount": 100, "currency": "GHS"}


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client():
    async with client_for(create_app(processing_delay=DELAY, ttl_seconds=60)) as c:
        yield c


def pay(client, key="key-1", body=PAYMENT):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/process-payment", json=body, headers=headers)


# --- User story 1: first transaction -------------------------------------------------


async def test_first_request_is_processed(client):
    start = time.perf_counter()
    res = await pay(client)
    elapsed = time.perf_counter() - start

    assert res.status_code == 201
    assert res.json()["message"] == "Charged 100 GHS"
    assert res.headers["X-Cache-Hit"] == "false"
    assert elapsed >= DELAY


async def test_missing_idempotency_key_is_rejected(client):
    res = await pay(client, key=None)
    assert res.status_code == 400


async def test_blank_idempotency_key_is_rejected(client):
    res = await pay(client, key="   ")
    assert res.status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"amount": -5, "currency": "GHS"},
        {"amount": 0, "currency": "GHS"},
        {"amount": 10, "currency": "CEDIS"},
        {"amount": "abc", "currency": "GHS"},
        {"currency": "GHS"},
        {"amount": 10, "currency": "GHS", "note": "extra"},
    ],
)
async def test_invalid_body_is_rejected(client, body):
    res = await pay(client, body=body)
    assert res.status_code == 422


# --- User story 2: duplicate attempt -------------------------------------------------


async def test_duplicate_returns_saved_response_without_reprocessing(client):
    first = await pay(client)

    start = time.perf_counter()
    second = await pay(client)
    elapsed = time.perf_counter() - start

    assert second.status_code == first.status_code
    # transaction_id is generated during processing, so an identical id proves
    # the charge was not run a second time.
    assert second.json() == first.json()
    assert second.headers["X-Cache-Hit"] == "true"
    assert elapsed < DELAY / 2


async def test_equivalent_body_is_treated_as_duplicate(client):
    first = await pay(client, body={"amount": 100, "currency": "GHS"})
    second = await pay(client, body={"currency": "ghs", "amount": 100.0})

    assert second.headers["X-Cache-Hit"] == "true"
    assert second.json() == first.json()


async def test_different_keys_are_processed_independently(client):
    a = await pay(client, key="a")
    b = await pay(client, key="b")

    assert a.json()["transaction_id"] != b.json()["transaction_id"]
    assert b.headers["X-Cache-Hit"] == "false"


# --- User story 3: same key, different body ------------------------------------------


async def test_same_key_different_body_is_rejected(client):
    await pay(client)
    res = await pay(client, body={"amount": 500, "currency": "GHS"})

    assert res.status_code == 422
    assert res.json() == {"error": CONFLICT_MESSAGE}


async def test_same_key_different_currency_is_rejected(client):
    await pay(client)
    res = await pay(client, body={"amount": 100, "currency": "USD"})

    assert res.status_code == 422


# --- Bonus: in-flight requests -------------------------------------------------------


async def test_concurrent_duplicates_wait_for_first_and_share_its_result(client):
    start = time.perf_counter()
    a, b, c = await asyncio.gather(pay(client), pay(client), pay(client))
    elapsed = time.perf_counter() - start

    assert {r.status_code for r in (a, b, c)} == {201}
    assert len({r.json()["transaction_id"] for r in (a, b, c)}) == 1
    assert sorted(r.headers["X-Cache-Hit"] for r in (a, b, c)) == ["false", "true", "true"]
    # one charge worth of waiting, not three
    assert elapsed < DELAY * 2


async def test_conflicting_body_while_in_flight_is_rejected_immediately(client):
    first = asyncio.create_task(pay(client))
    await asyncio.sleep(DELAY / 10)  # let the first request start processing

    start = time.perf_counter()
    conflict = await pay(client, body={"amount": 999, "currency": "GHS"})
    elapsed = time.perf_counter() - start

    assert conflict.status_code == 422
    assert elapsed < DELAY / 2
    assert (await first).status_code == 201


# --- Failure handling ----------------------------------------------------------------


async def test_failed_charge_is_not_cached(monkeypatch):
    real_charge = main.charge
    calls = 0

    async def flaky(payment, delay):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("processor down")
        return await real_charge(payment, delay)

    monkeypatch.setattr(main, "charge", flaky)

    async with client_for(create_app(processing_delay=0, ttl_seconds=60)) as client:
        failed = await pay(client)
        retried = await pay(client)

    assert failed.status_code == 500
    assert retried.status_code == 201
    assert retried.headers["X-Cache-Hit"] == "false"
    assert calls == 2


# --- Developer's choice: key expiry --------------------------------------------------


async def test_keys_expire_after_ttl():
    now = 0.0
    app = create_app(processing_delay=0, ttl_seconds=100, clock=lambda: now)

    async with client_for(app) as client:
        first = await pay(client)
        now = 99
        replay = await pay(client)
        now = 101
        after_expiry = await pay(client)

    assert replay.headers["X-Cache-Hit"] == "true"
    assert after_expiry.headers["X-Cache-Hit"] == "false"
    assert after_expiry.json()["transaction_id"] != first.json()["transaction_id"]


async def test_purge_removes_only_expired_completed_entries():
    now = 0.0
    app = create_app(processing_delay=0, ttl_seconds=100, clock=lambda: now)
    store = app.state.store

    async with client_for(app) as client:
        await pay(client, key="old")
        now = 50
        await pay(client, key="new")
        now = 120
        assert store.purge_expired() == 1
        assert len(store) == 1
        assert (await pay(client, key="new")).headers["X-Cache-Hit"] == "true"
