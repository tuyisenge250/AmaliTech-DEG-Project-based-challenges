"""Pulse-Check API: a dead man's switch for remote devices."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse

from . import docs
from .alerts import log_alert
from .monitors import Monitor, MonitorAlreadyExists, MonitorNotFound, MonitorService, Status
from .schemas import MonitorAck, MonitorCreate

MonitorId = Annotated[
    str, Path(description="Id the monitor was registered with.", examples=["device-123"])
]

HEARTBEAT_MESSAGES = {
    Status.ACTIVE: "Heartbeat received. Timer reset to {t} seconds.",
    Status.PAUSED: "Heartbeat received. Monitor resumed, timer reset to {t} seconds.",
    Status.DOWN: "Heartbeat received. Monitor recovered, timer reset to {t} seconds.",
}


def _seconds(value: float) -> str:
    return f"{value:g}"  # 60 -> "60", 0.5 -> "0.5"


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def create_app(*, on_alert: Callable[[Monitor], None] = log_alert) -> FastAPI:
    service = MonitorService(on_alert=on_alert)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service.shutdown()

    app = FastAPI(title="Pulse-Check API", version="1.0.0", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(MonitorNotFound)
    async def on_not_found(_: Request, exc: MonitorNotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(MonitorAlreadyExists)
    async def on_exists(_: Request, exc: MonitorAlreadyExists) -> JSONResponse:
        return _error(409, str(exc))

    # These handlers are `async def` on purpose. The timers live on the event
    # loop, and a plain `def` handler would run in a worker thread with no loop.

    @app.post(
        "/monitors",
        status_code=201,
        summary="Register a monitor",
        description=(
            "Creates a monitor and starts its countdown. If no heartbeat arrives "
            "within `timeout` seconds, an alert fires and the monitor goes `down`."
        ),
        responses=docs.REGISTER_RESPONSES,
    )
    async def register_monitor(body: MonitorCreate) -> MonitorAck:
        monitor = service.register(body.id, body.timeout, body.alert_email)
        return MonitorAck(
            message=(
                f"Monitor '{monitor.id}' created. An alert fires if no heartbeat "
                f"arrives within {_seconds(monitor.timeout)} seconds."
            ),
            id=monitor.id,
            status=monitor.status,
        )

    @app.post(
        "/monitors/{monitor_id}/heartbeat",
        summary="Send a heartbeat",
        description=(
            "Resets the countdown to the full timeout. Also resumes a paused "
            "monitor and brings a `down` monitor back to `active`."
        ),
        responses=docs.HEARTBEAT_RESPONSES,
    )
    async def heartbeat(monitor_id: MonitorId) -> MonitorAck:
        previous = service.get(monitor_id).status
        monitor = service.heartbeat(monitor_id)
        return MonitorAck(
            message=HEARTBEAT_MESSAGES[previous].format(t=_seconds(monitor.timeout)),
            id=monitor.id,
            status=monitor.status,
        )

    @app.post(
        "/monitors/{monitor_id}/pause",
        summary="Pause a monitor",
        description=(
            "Stops the countdown so no alert fires, for example while a device is "
            "being repaired. The next heartbeat resumes monitoring."
        ),
        responses=docs.PAUSE_RESPONSES,
    )
    async def pause(monitor_id: MonitorId) -> MonitorAck:
        monitor = service.pause(monitor_id)
        return MonitorAck(
            message=(
                f"Monitor '{monitor.id}' paused. "
                "No alerts will fire until the next heartbeat."
            ),
            id=monitor.id,
            status=monitor.status,
        )

    return app


app = create_app()
