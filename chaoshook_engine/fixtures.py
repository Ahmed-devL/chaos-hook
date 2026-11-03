"""Canonical NovaCloud lifecycle; event time is independent of signing time."""

from copy import deepcopy
import json

from target_app.hmac_auth import sign_stripe_payload

SEED_USER = {
    "id": "usr_alice_01",
    "email": "alice@novacloud.ai",
    "stripe_customer_id": "cus_nova_99",
    "plan_tier": "free",
    "credit_balance": 0,
    "welcome_email_sent": 0,
    "last_event_ts": 0,
}

CANONICAL_EVENTS = [
    {
        "id": "evt_01_checkout",
        "type": "checkout.session.completed",
        "created": 1700000100,
        "data": {"object": {
            "id": "cs_nova_123",
            "customer": "cus_nova_99",
            "client_reference_id": "usr_alice_01",
            "metadata": {"user_id": "usr_alice_01"},
            "subscription": "sub_nova_777",
        }},
    },
    {
        "id": "evt_02_sub_created",
        "type": "customer.subscription.created",
        "created": 1700000200,
        "data": {"object": {
            "id": "sub_nova_777",
            "customer": "cus_nova_99",
            "status": "active",
            "current_period_end": 1702592000,
        }},
    },
    {
        "id": "evt_03_invoice_paid",
        "type": "invoice.paid",
        "created": 1700000300,
        "data": {"object": {
            "id": "in_nova_555",
            "customer": "cus_nova_99",
            "subscription": "sub_nova_777",
        }},
    },
    {
        "id": "evt_04_sub_deleted",
        "type": "customer.subscription.deleted",
        "created": 1700000400,
        "data": {"object": {
            "id": "sub_nova_777",
            "customer": "cus_nova_99",
            "status": "canceled",
            "current_period_end": 1702592000,
        }},
    },
]


def get_canonical_events() -> list[dict]:
    """Return independent payloads so a proof cannot mutate later runs."""
    return deepcopy(CANONICAL_EVENTS)


def serialize_event(event: dict) -> bytes:
    """Encode a stable JSON body whose exact bytes can be signed and replayed."""
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_event(event: dict, timestamp: int | None = None) -> tuple[bytes, str]:
    """Return handler arguments, signing now by default even for old events."""
    raw_body = serialize_event(event)
    return raw_body, sign_stripe_payload(raw_body, timestamp=timestamp)
