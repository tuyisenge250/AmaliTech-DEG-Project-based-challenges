"""Request and response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictFloat, StrictInt

from .monitors import Monitor, Status

MAX_TIMEOUT_SECONDS = 604_800  # 7 days
ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class MonitorCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"id": "device-123", "timeout": 60, "alert_email": "admin@critmon.com"}]
        },
    )

    id: str = Field(
        pattern=ID_PATTERN,
        description="Unique device id. 1 to 64 letters, digits, dots, dashes or underscores.",
        examples=["device-123"],
    )
    # Strict types: no "60" strings and no `true`. The union keeps 60 an integer
    # in the response while still allowing fractions like 0.5.
    timeout: StrictInt | StrictFloat = Field(
        gt=0,
        le=MAX_TIMEOUT_SECONDS,
        description=(
            "Seconds the device may stay silent before an alert fires. "
            "Fractions are allowed. Maximum is 7 days."
        ),
        examples=[60],
    )
    alert_email: EmailStr = Field(
        description="Who to notify when the device stops sending heartbeats.",
        examples=["admin@critmon.com"],
    )


class MonitorAck(BaseModel):
    """Confirmation returned by the commands (register, heartbeat, pause)."""

    message: str
    id: str = Field(examples=["device-123"])
    status: Status


class MonitorOut(BaseModel):
    """Current state of a monitor."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "device-123",
                    "status": "active",
                    "timeout": 60,
                    "alert_email": "admin@critmon.com",
                    "seconds_remaining": 41.3,
                    "created_at": "2026-01-01T08:00:00+00:00",
                    "last_heartbeat_at": "2026-01-01T08:00:19+00:00",
                    "last_alert_at": None,
                }
            ]
        }
    )

    id: str
    status: Status
    timeout: int | float = Field(description="Seconds allowed between heartbeats.")
    alert_email: str
    seconds_remaining: float | None = Field(
        description="Time left before the alert fires. Null when paused or down."
    )
    created_at: datetime
    last_heartbeat_at: datetime | None = Field(
        description="Null until the first heartbeat is received."
    )
    last_alert_at: datetime | None = Field(
        description="When the most recent alert fired. Null if it never has."
    )

    @classmethod
    def from_monitor(cls, monitor: Monitor, seconds_remaining: float | None) -> "MonitorOut":
        return cls(
            id=monitor.id,
            status=monitor.status,
            timeout=monitor.timeout,
            alert_email=monitor.alert_email,
            seconds_remaining=None if seconds_remaining is None else round(seconds_remaining, 1),
            created_at=monitor.created_at,
            last_heartbeat_at=monitor.last_heartbeat_at,
            last_alert_at=monitor.last_alert_at,
        )


class ErrorResponse(BaseModel):
    error: str


class ValidationErrorResponse(BaseModel):
    detail: list[dict[str, Any]]
