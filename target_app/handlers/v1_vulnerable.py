"""Intentionally vulnerable baseline for ordering, concurrency, and retry proofs."""

from target_app.hmac_auth import verify_stripe_signature
from target_app.models import CreditLedger, Subscription, User, WebhookEvent
from target_app.schemas import StripeEvent
from target_app.side_effects import send_welcome_email


def process_stripe_webhook(session, raw_body: bytes, sig_header: str) -> dict:
    if not verify_stripe_signature(raw_body, sig_header):
        raise ValueError("Invalid Stripe signature")
    event = StripeEvent.model_validate_json(raw_body)
    evt_id = event.id
    existing = session.query(WebhookEvent).filter_by(event_id=evt_id).first()
    if existing:
        return {"status": "duplicate", "event_id": evt_id}

    obj = event.data.object
    customer_id = obj["customer"]
    user = session.query(User).filter_by(stripe_customer_id=customer_id).first()
    if user is None and event.type == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("user_id")
        user = session.get(User, user_id) if user_id else None
        if user is not None:
            user.stripe_customer_id = customer_id
    if user is None:
        raise ValueError(f"Unknown Stripe customer: {customer_id}")

    if event.type == "checkout.session.completed":
        user.plan_tier = "pro"
        user.welcome_email_sent = 1
        send_welcome_email(session, user.id, dedupe_key=evt_id)
        # Deliberately persists the side effect before the idempotency record.
        session.commit()
    elif event.type == "customer.subscription.created":
        sub = session.get(Subscription, obj["id"])
        if sub is None:
            sub = Subscription(
                id=obj["id"], user_id=user.id, stripe_customer_id=customer_id
            )
            session.add(sub)
        sub.status = "active"
        sub.current_period_end = obj.get("current_period_end", 0)
        user.plan_tier = "pro"
    elif event.type == "invoice.paid":
        session.add(CreditLedger(
            user_id=user.id, invoice_id=obj["id"], delta=100, reason="invoice.paid"
        ))
        user.credit_balance = User.credit_balance + 100
        user.plan_tier = "pro"
        # A retry after this commit grants the same invoice's credits again.
        session.commit()
    elif event.type == "customer.subscription.deleted":
        sub = session.get(Subscription, obj["id"])
        if sub:
            sub.status = "canceled"
            user.plan_tier = "canceled"

    session.add(WebhookEvent(
        event_id=evt_id, event_type=event.type, created_ts=event.created,
        status="processed",
    ))
    session.commit()
    return {"status": "processed", "event_id": evt_id}
