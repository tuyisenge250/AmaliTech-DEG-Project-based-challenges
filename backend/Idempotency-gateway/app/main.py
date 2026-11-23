"""Idempotency Gateway: a "pay-once" layer in front of a payment processor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import IdempotencyStore, StoredResponse

logger = logging.getLogger("idempotency_gateway")

MAX_KEY_LENGTH = 255
CONFLICT_MESSAGE = "Idempotency key already used for a different request body."
KEY_REQUIRED_MESSAGE = "Idempotency-Key header is required."
FAILURE_MESSAGE = "Payment processing failed. Retry with the same Idempotency-Key."

API_DESCRIPTION = """
A **pay-once** layer for FinSafe Transactions Ltd. Send a payment with an
`Idempotency-Key` header and it is processed **exactly once**, however many times
the client retries.

| Situation | Result |
| --- | --- |
| First request for a key | Payment is processed (about 2 s) and the response is saved. `X-Cache-Hit: false` |
| Same key, same body | Saved response is returned instantly. `X-Cache-Hit: true` |
| Same key, same body, **while the first is still processing** | Waits for the first request, then returns its response. `X-Cache-Hit: true` |
| Same key, **different** body | `422` with an error message |

Keys are remembered for 24 hours by default (`IDEMPOTENCY_KEY_TTL_SECONDS`).
"""


class PaymentRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"amount": 100, "currency": "GHS"}]},
    )

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
        description="Amount to charge. Greater than 0, at most 2 decimal places.",
        examples=[100],
    )
    currency: str = Field(
        pattern=r"^[A-Za-z]{3}$",
        description="3-letter currency code. Case-insensitive, stored upper-case.",
        examples=["GHS"],
    )

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def amount_text(self) -> str:
        """100, 100.0 and 100.00 all render as '100'."""
        return format(self.amount.normalize(), "f")

    def fingerprint(self) -> str:
        """Stable hash of the *meaning* of the body, not its byte layout."""
        canonical = json.dumps(
            {"amount": self.amount_text, "currency": self.currency}, sort_keys=True
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class PaymentResponse(BaseModel):
    message: str = Field(examples=["Charged 100 GHS"])
    transaction_id: str = Field(
        description="Generated once per processed payment. A replay returns the same id.",
        examples=["e87f0e2e-39bc-48b8-9b4e-8fd7bafb1373"],
    )
    amount: str = Field(examples=["100"])
    currency: str = Field(examples=["GHS"])


class ErrorResponse(BaseModel):
    error: str


class ValidationErrorResponse(BaseModel):
    detail: list[dict[str, Any]]


def _examples(**named: tuple[str, Any]) -> dict[str, Any]:
    return {
        name: {"summary": summary, "value": value} for name, (summary, value) in named.items()
    }


PAYMENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    201: {
        "model": PaymentResponse,
        "description": (
            "Payment processed. On a duplicate request this is an exact copy of the "
            "first response, with `X-Cache-Hit: true`."
        ),
        "headers": {
            "X-Cache-Hit": {
                "description": (
                    "`false` when this request ran the payment, `true` when the saved "
                    "response was replayed."
                ),
                "schema": {"type": "string", "enum": ["true", "false"]},
            }
        },
    },
    400: {
        "model": ErrorResponse,
        "description": "`Idempotency-Key` header is missing, blank or longer than 255 characters.",
        "content": {
            "application/json": {
                "examples": _examples(
                    missing=("Header missing", {"error": KEY_REQUIRED_MESSAGE}),
                    too_long=(
                        "Key too long",
                        {"error": f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters."},
                    ),
                )
            }
        },
    },
    422: {
        "model": ErrorResponse | ValidationErrorResponse,
        "description": (
            "Either the key was already used with a different request body, "
            "or the body failed validation."
        ),
        "content": {
            "application/json": {
                "examples": _examples(
                    key_reuse=("Same key, different body", {"error": CONFLICT_MESSAGE}),
                    invalid_body=(
                        "Body failed validation",
                        {
                            "detail": [
                                {
                                    "type": "greater_than",
                                    "loc": ["body", "amount"],
                                    "msg": "Input should be greater than 0",
                                }
                            ]
                        },
                    ),
                )
            }
        },
    },
    500: {
        "model": ErrorResponse,
        "description": (
            "The charge itself failed. The key is released, so the client may retry "
            "with the same `Idempotency-Key`."
        ),
        "content": {"application/json": {"example": {"error": FAILURE_MESSAGE}}},
    },
}


async def charge(payment: PaymentRequest, delay: float) -> StoredResponse:
    """Simulated payment processor. Swap this for a real PSP call."""
    await asyncio.sleep(delay)
    return StoredResponse(
        status_code=201,
        body={
            "message": f"Charged {payment.amount_text} {payment.currency}",
            "transaction_id": str(uuid.uuid4()),
            "amount": payment.amount_text,
            "currency": payment.currency,
        },
    )


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def create_app(
    *,
    processing_delay: float | None = None,
    ttl_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    delay = (
        processing_delay
        if processing_delay is not None
        else float(os.getenv("PAYMENT_PROCESSING_DELAY", "2"))
    )
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else float(os.getenv("IDEMPOTENCY_KEY_TTL_SECONDS", "86400"))
    )
    store = IdempotencyStore(ttl_seconds=ttl, clock=clock)

    async def sweep() -> None:
        while True:
            await asyncio.sleep(min(ttl, 60))
            removed = store.purge_expired()
            if removed:
                logger.info("purged %d expired idempotency keys", removed)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        sweeper = asyncio.create_task(sweep())
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(
        title="Idempotency Gateway",
        summary="Process each payment exactly once.",
        description=API_DESCRIPTION,
        version="1.0.0",
        openapi_tags=[
            {"name": "Payments", "description": "Idempotent payment processing."},
        ],
        # shows how long each request took, which makes the 2 s first call
        # versus the instant replay obvious in the UI
        swagger_ui_parameters={"displayRequestDuration": True},
        lifespan=lifespan,
    )
    app.state.store = store

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # The header is declared required so the docs show it as required, but
        # a missing key is a client error (400), not a body-validation 422.
        for err in exc.errors():
            if err["type"] == "missing" and err["loc"] == ("header", "idempotency-key"):
                return _error(400, KEY_REQUIRED_MESSAGE)
        return await request_validation_exception_handler(request, exc)

    async def run_payment(key: str, payment: PaymentRequest) -> StoredResponse:
        try:
            return await charge(payment, delay)
        except Exception:
            # A failed charge must not be cached, otherwise the client could
            # never retry with the same key.
            store.discard(key, asyncio.current_task())
            logger.exception("payment processing failed for key %r", key)
            raise

    @app.post(
        "/process-payment",
        status_code=201,
        tags=["Payments"],
        operation_id="processPayment",
        summary="Process a payment (idempotent)",
        description=(
            "Charges the given amount **once** per `Idempotency-Key`.\n\n"
            "Retry safely with the same key and body: the saved response is returned "
            "with `X-Cache-Hit: true` and no second charge is made. A retry that "
            "arrives while the first request is still processing waits for it and "
            "returns the same result.\n\n"
            "Using the same key with a different body is rejected with `422`."
        ),
        responses=PAYMENT_RESPONSES,
    )
    async def process_payment(
        payment: PaymentRequest,
        idempotency_key: Annotated[
            str,
            Header(
                description=(
                    "Unique string for this payment, chosen by the client "
                    f"(max {MAX_KEY_LENGTH} characters). Reuse it when retrying."
                ),
                examples=["order-1001"],
            ),
        ],
    ) -> JSONResponse:
        key = idempotency_key.strip()
        if not key:
            return _error(400, KEY_REQUIRED_MESSAGE)
        if len(key) > MAX_KEY_LENGTH:
            return _error(400, f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters.")

        fingerprint = payment.fingerprint()

        # No `await` between get() and put(), so on a single event loop this
        # check-and-insert is atomic: two identical requests can never both
        # decide they are the first one.
        entry = store.get(key)
        if entry is None:
            task = asyncio.create_task(run_payment(key, payment))
            entry = store.put(key, fingerprint, task)
            replayed = False
        elif entry.fingerprint != fingerprint:
            return _error(422, CONFLICT_MESSAGE)
        else:
            replayed = True

        try:
            # shield(): a client disconnecting must not cancel a charge that
            # other waiting requests (and future replays) depend on.
            stored = await asyncio.shield(entry.task)
        except Exception:
            return _error(500, FAILURE_MESSAGE)

        return JSONResponse(
            stored.body,
            status_code=stored.status_code,
            headers={"X-Cache-Hit": "true" if replayed else "false"},
        )

    return app


app = create_app()
