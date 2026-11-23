import httpx
import pytest

from app.main import create_app


@pytest.fixture
def alerts() -> list[str]:
    """Ids of the monitors that raised an alert, in order."""
    return []


@pytest.fixture
async def client(alerts):
    app = create_app(on_alert=lambda m: alerts.append(m.id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.state.service.shutdown()
