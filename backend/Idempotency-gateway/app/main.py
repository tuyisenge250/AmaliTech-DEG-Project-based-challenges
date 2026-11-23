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
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import IdempotencyStore, StoredResponse

logger = logging.getLogger("idempotency_gateway")

MAX_KEY_LENGTH = 255
CONFLICT_MESSAGE = "Idempotency key already used for a different request body."


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")

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

    app = FastAPI(title="Idempotency Gateway", version="1.0.0", lifespan=lifespan)
    app.state.store = store

    async def run_payment(key: str, payment: PaymentRequest) -> StoredResponse:
        try:
            return await charge(payment, delay)
        except Exception:
            # A failed charge must not be cached, otherwise the client could
            # never retry with the same key.
            store.discard(key, asyncio.current_task())
            logger.exception("payment processing failed for key %r", key)
            raise

    @app.post("/process-payment", status_code=201)
    async def process_payment(
        payment: PaymentRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        key = (idempotency_key or "").strip()
        if not key:
            return _error(400, "Idempotency-Key header is required.")
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
            return _error(
                500, "Payment processing failed. Retry with the same Idempotency-Key."
            )

        return JSONResponse(
            stored.body,
            status_code=stored.status_code,
            headers={"X-Cache-Hit": "true" if replayed else "false"},
        )

    return app


app = create_app()
