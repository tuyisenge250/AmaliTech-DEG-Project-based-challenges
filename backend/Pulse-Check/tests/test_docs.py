from scripts.export_openapi import SPEC_PATH, render_spec


async def test_swagger_ui_is_served(client):
    res = await client.get("/docs")

    assert res.status_code == 200
    assert "swagger-ui" in res.text


async def test_every_endpoint_is_documented_with_its_error_responses(client):
    spec = (await client.get("/openapi.json")).json()
    paths = spec["paths"]

    def statuses(path: str, method: str) -> set[str]:
        return set(paths[path][method]["responses"])

    assert statuses("/monitors", "post") == {"201", "409", "422"}
    assert statuses("/monitors", "get") == {"200", "422"}
    assert statuses("/monitors/{monitor_id}/heartbeat", "post") == {"200", "404", "422"}
    assert statuses("/monitors/{monitor_id}/pause", "post") == {"200", "404", "422"}
    assert statuses("/monitors/{monitor_id}", "get") == {"200", "404", "422"}
    assert statuses("/monitors/{monitor_id}", "delete") == {"204", "404", "422"}


def test_committed_spec_is_up_to_date():
    assert SPEC_PATH.read_text() == render_spec(), (
        "docs/openapi.json is stale. Regenerate it with: python -m scripts.export_openapi"
    )
