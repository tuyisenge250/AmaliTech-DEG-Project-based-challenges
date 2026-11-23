"""Request and response models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictFloat, StrictInt

from .monitors import Status

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


class ErrorResponse(BaseModel):
    error: str


class ValidationErrorResponse(BaseModel):
    detail: list[dict[str, Any]]
