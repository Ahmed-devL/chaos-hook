"""SQLite cell setup and stable, JSON-compatible state comparisons."""

from pathlib import Path

from sqlalchemy import URL, create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from chaoshook_engine.scenario_spec import SCENARIO_SPEC
from target_app.models import CreditLedger, OutboxEmail, Subscription, User


def create_cell_engine(db_path: str | Path) -> Engine:
    """Create an engine for one isolated file; the caller owns schema and cleanup."""
    engine = create_engine(
        URL.create("sqlite", database=str(db_path)),
        connect_args={"check_same_thread": False, "timeout": 30.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
        finally:
            cursor.close()

    return engine


def seed_initial_state(session: Session, scenario=SCENARIO_SPEC) -> None:
    """Persist S0 in a newly created cell before invoking any handler."""
    session.add_all(User(**user) for user in scenario.initial_users())
    session.commit()


def extract_comparable_state(session: Session, scenario=SCENARIO_SPEC) -> dict:
    """Read real database state, excluding delivery order and surrogate row IDs."""
    user = session.get(User, scenario.user_id)
    sub = session.get(Subscription, scenario.subscription_id)
    welcome_count = session.scalar(
        select(func.count()).select_from(OutboxEmail).where(
            OutboxEmail.user_id == scenario.user_id,
            OutboxEmail.template == "welcome_pro",
        )
    )
    return {
        "user": None if user is None else {
            "id": user.id,
            "plan_tier": user.plan_tier,
            "credit_balance": user.credit_balance,
            "welcome_email_count": welcome_count,
        },
        "subscription": None if sub is None else {
            "id": sub.id,
            "status": sub.status,
            "current_period_end": sub.current_period_end,
        },
        "credit_ledger_rows": session.scalar(
            select(func.count()).select_from(CreditLedger)
        ),
        "credit_ledger_sum": session.scalar(
            select(func.coalesce(func.sum(CreditLedger.delta), 0))
        ),
        "outbox_emails_count": session.scalar(
            select(func.count()).select_from(OutboxEmail)
        ),
    }


extract_ComparableState = extract_comparable_state


def diff_states(golden: dict, actual: dict) -> list[dict]:
    """Return deterministic field/expected/actual records for proof invariants."""
    fields = (
        "user.plan_tier",
        "user.credit_balance",
        "user.welcome_email_count",
        "subscription.status",
        "subscription.current_period_end",
        "credit_ledger_rows",
        "credit_ledger_sum",
        "outbox_emails_count",
    )

    def value_at(state, field):
        value = state
        for key in field.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    differences = []
    for field in fields:
        expected = value_at(golden, field)
        observed = value_at(actual, field)
        if expected != observed:
            differences.append({"field": field, "expected": expected, "actual": observed})
    return differences


def extract_semantic_state(session, scenario):
    """Observe every business row, retaining identities and individual deltas."""
    def rows(model, fields):
        return [{field: getattr(row, field) for field in fields}
                for row in session.scalars(select(model))]

    users = rows(User, ("id", "email", "stripe_customer_id", "plan_tier",
                        "credit_balance", "welcome_email_sent", "last_event_ts"))
    for user in users:
        if user["id"] == scenario.user_id:
            user.pop("last_event_ts")
    return {
        "users": sorted(users, key=lambda u: u["id"]),
        "subscriptions": sorted(rows(Subscription, ("id", "user_id", "stripe_customer_id",
                                                      "status", "current_period_end")), key=lambda s: s["id"]),
        "ledger": sorted(rows(CreditLedger, ("user_id", "invoice_id", "delta", "reason")),
                         key=lambda r: (r["invoice_id"], r["user_id"], r["delta"], r["reason"])),
        "outbox": sorted(rows(OutboxEmail, ("user_id", "template")),
                         key=lambda r: (r["user_id"], r["template"])),
    }
