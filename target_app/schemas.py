"""Stripe-compatible event envelope; object fields vary by event type."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class StripeEventData(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: dict[str, Any]


class StripeEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    created: int
    data: StripeEventData
