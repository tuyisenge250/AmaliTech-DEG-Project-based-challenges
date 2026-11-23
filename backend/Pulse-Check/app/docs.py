"""OpenAPI documentation for the error responses (the success shape comes from the
return type of each endpoint)."""

from typing import Any

from .schemas import ErrorResponse, ValidationErrorResponse

Responses = dict[int | str, dict[str, Any]]


def error(description: str, example: str) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"error": example}}},
    }


NOT_FOUND = error("No monitor with this id.", "Monitor 'device-123' not found.")

INVALID_BODY: dict[str, Any] = {
    "model": ValidationErrorResponse,
    "description": "The request body failed validation.",
    "content": {
        "application/json": {
            "example": {
                "detail": [
                    {
                        "type": "greater_than",
                        "loc": ["body", "timeout"],
                        "msg": "Input should be greater than 0",
                    }
                ]
            }
        }
    },
}

REGISTER_RESPONSES: Responses = {
    409: error("A monitor with this id already exists.", "Monitor 'device-123' already exists."),
    422: INVALID_BODY,
}

HEARTBEAT_RESPONSES: Responses = {404: NOT_FOUND}
PAUSE_RESPONSES: Responses = {404: NOT_FOUND}
GET_RESPONSES: Responses = {404: NOT_FOUND}
DELETE_RESPONSES: Responses = {404: NOT_FOUND}
