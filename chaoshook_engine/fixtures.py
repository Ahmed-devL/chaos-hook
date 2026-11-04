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


def scenario_from_seed(seed: int, *, bystander: bool = True):
    """Reproducible valid instances of the frozen four-event business contract."""
    from hashlib import sha256
    from random import Random
    from chaoshook_engine.scenario_spec import LifecycleScenarioSpec

    if type(seed) is not int:
        raise TypeError("Scenario seed must be an integer")
    rng = Random(seed)
    token = sha256(str(seed).encode()).hexdigest()[:12]
    start = 1700000000 + rng.randrange(1_000_000)
    return LifecycleScenarioSpec(
        user_id=f"usr_{token}", customer_id=f"cus_{token}",
        subscription_id=f"sub_{token}", invoice_id=f"in_{token}",
        checkout_id=f"cs_{token}", email=f"{token}@novacloud.ai",
        event_ids=tuple(f"evt_{token}_{i}" for i in range(1, 5)),
        event_timestamps=tuple(start + step * 100 for step in range(1, 5)),
        current_period_end=start + 2592000,
        initial_credit_balance=rng.randrange(1, 1000), seed=seed, bystander=bystander,
    )


def get_canonical_events(scenario=None) -> list[dict]:
    """Return independent payloads so a proof cannot mutate later runs."""
    events = deepcopy(CANONICAL_EVENTS)
    if scenario is None:
        return events
    for index, event in enumerate(events):
        event["id"] = scenario.event_ids[index]
        event["created"] = scenario.event_timestamps[index]
        obj = event["data"]["object"]
        obj["customer"] = scenario.customer_id
        if index == 0:
            obj.update(id=scenario.checkout_id, client_reference_id=scenario.user_id,
                       metadata={"user_id": scenario.user_id}, subscription=scenario.subscription_id)
        elif index == 2:
            obj.update(id=scenario.invoice_id, subscription=scenario.subscription_id)
        else:
            obj.update(id=scenario.subscription_id, current_period_end=scenario.current_period_end)
    return events


def serialize_event(event: dict) -> bytes:
    """Encode a stable JSON body whose exact bytes can be signed and replayed."""
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_event(event: dict, timestamp: int | None = None) -> tuple[bytes, str]:
    """Return handler arguments, signing now by default even for old events."""
    raw_body = serialize_event(event)
    return raw_body, sign_stripe_payload(raw_body, timestamp=timestamp)


def semantic_challenges():
    """Structurally different histories, each with a nonzero balance and bystander."""
    scenario = scenario_from_seed(741)
    events = get_canonical_events(scenario)
    second_invoice = deepcopy(events[2])
    second_invoice["id"] += "_second"
    second_invoice["created"] += 50
    second_invoice["data"]["object"]["id"] += "_second"
    return scenario, [
        ("A-canceled", [events[i] for i in (0, 1, 2, 3)]),
        ("B-active", [events[i] for i in (0, 1, 2)]),
        ("C-active-no-invoice", [events[i] for i in (0, 1)]),
        ("C-canceled-no-invoice", [events[i] for i in (0, 1, 3)]),
        ("D-two-invoices", [*events[:3], second_invoice]),
    ]
