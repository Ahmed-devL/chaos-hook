"""Atomic event claims and monotonic lifecycle state for unordered delivery."""

from sqlalchemy import text

from target_app.hmac_auth import verify_stripe_signature
from target_app.models import Subscription, User
from target_app.schemas import StripeEvent
from target_app.side_effects import send_welcome_email


def process_stripe_webhook(session, raw_body: bytes, sig_header: str) -> dict:
    if not verify_stripe_signature(raw_body, sig_header):
        raise ValueError("Invalid Stripe signature")
    event = StripeEvent.model_validate_json(raw_body)
    event_ts = event.created
    obj = event.data.object

    with session.begin():
        # Claim before reading business state; SQLite serializes competing writers.
        result = session.execute(text(
            "INSERT INTO webhook_events (event_id, event_type, created_ts, status) "
            "VALUES (:id, :type, :ts, 'processed') "
            "ON CONFLICT(event_id) DO NOTHING"
        ), {"id": event.id, "type": event.type, "ts": event_ts})
        if result.rowcount == 0:
            return {"status": "duplicate", "event_id": event.id}

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
            if event_ts >= user.last_event_ts:
                user.plan_tier = "pro"
                user.last_event_ts = event_ts
            if user.welcome_email_sent == 0:
                send_welcome_email(session, user.id, dedupe_key=event.id)
                user.welcome_email_sent = 1
        elif event.type in (
            "customer.subscription.created", "customer.subscription.deleted"
        ):
            status = (
                "canceled" if event.type == "customer.subscription.deleted" else "active"
            )
            sub = session.get(Subscription, obj["id"])
            if sub is None:
                # Deletion is also an upsert: preserve a tombstone for late creation.
                sub = Subscription(
                    id=obj["id"], user_id=user.id, stripe_customer_id=customer_id,
                    status=status, current_period_end=0, last_event_ts=event_ts,
                )
                session.add(sub)
            if event_ts >= sub.last_event_ts:
                sub.status = status
                sub.last_event_ts = event_ts
            sub.current_period_end = max(
                sub.current_period_end, obj.get("current_period_end", 0)
            )
            if event_ts >= user.last_event_ts:
                user.plan_tier = "canceled" if status == "canceled" else "pro"
                user.last_event_ts = event_ts
        elif event.type == "invoice.paid":
            # Business identity is separate from delivery identity. The claim
            # INSERT above holds SQLite's writer lock until this transaction
            # ends. This conditional INSERT is therefore atomic across workers;
            # no competing claim can write between its predicate and commit.
            # Invoice IDs are global in this single-provider/account model.
            grant = session.execute(text(
                "INSERT INTO credit_ledger (user_id, invoice_id, delta, reason) "
                "SELECT :user_id, :invoice_id, 100, 'invoice.paid' "
                "WHERE NOT EXISTS (SELECT 1 FROM credit_ledger "
                "WHERE invoice_id = :invoice_id AND reason = 'invoice.paid')"
            ), {"user_id": user.id, "invoice_id": obj["id"]})
            if grant.rowcount == 1:
                user.credit_balance = User.credit_balance + 100

    return {"status": "processed", "event_id": event.id}
