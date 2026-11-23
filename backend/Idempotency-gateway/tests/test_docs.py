import httpx
import pytest

from app.main import create_app
from scripts.export_openapi import SPEC_PATH, render_spec


@pytest.fixture
async def client():
    app = create_app(processing_delay=0, ttl_seconds=60)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_swagger_ui_is_served(client):
    res = await client.get("/docs")
    assert res.status_code == 200
    assert "swagger-ui" in res.text


async def test_openapi_spec_is_served(client):
    res = await client.get("/openapi.json")
    assert res.status_code == 200
    assert "/process-payment" in res.json()["paths"]


async def test_payment_operation_documents_every_outcome(client):
    spec = (await client.get("/openapi.json")).json()
    op = spec["paths"]["/process-payment"]["post"]

    assert set(op["responses"]) == {"201", "400", "422", "500"}
    assert "X-Cache-Hit" in op["responses"]["201"]["headers"]

    key_param = next(p for p in op["parameters"] if p["name"] == "idempotency-key")
    assert key_param["in"] == "header"
    assert key_param["required"] is True


def test_committed_spec_is_up_to_date():
    assert SPEC_PATH.read_text() == render_spec(), (
        "docs/openapi.json is stale. Regenerate it with: python -m scripts.export_openapi"
    )
