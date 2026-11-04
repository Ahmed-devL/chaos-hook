"""Independent business contract for the parameterized four-event NovaCloud scenario.

All 24 delivery orders must converge for this scenario. This is an explicit
scenario requirement, not a claim about webhook handlers in general. Neither
expected state nor validation is derived from a candidate or example repair.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from target_app.models import CreditLedger, OutboxEmail, Subscription, User


class PropertyViolation(TypedDict):
    field: str
    expected: object
    actual: object


@dataclass(frozen=True)
class LifecycleScenarioSpec:
    """Immutable outcomes for the seeded user and the single paid invoice."""

    user_id: str = "usr_alice_01"
    customer_id: str = "cus_nova_99"
    email: str = "alice@novacloud.ai"
    checkout_id: str = "cs_nova_123"
    event_ids: tuple[str, ...] = (
        "evt_01_checkout", "evt_02_sub_created", "evt_03_invoice_paid", "evt_04_sub_deleted",
    )
    event_timestamps: tuple[int, ...] = (1700000100, 1700000200, 1700000300, 1700000400)
    seed: int | None = None
    bystander: bool = False
    subscription_id: str = "sub_nova_777"
    invoice_id: str = "in_nova_555"
    initial_credit_balance: int = 0
    invoice_credit_delta: int = 100
    plan_tier: Literal["canceled"] = "canceled"
    subscription_status: Literal["canceled"] = "canceled"
    current_period_end: int = 1702592000
    welcome_template: Literal["welcome_pro"] = "welcome_pro"

    def __post_init__(self):
        if len(self.event_ids) != 4 or len(set(self.event_ids)) != 4:
            raise ValueError("Four distinct event IDs are required")
        if (len(self.event_timestamps) != 4 or self.event_timestamps[0] <= 0
                or any(a >= b for a, b in zip(self.event_timestamps, self.event_timestamps[1:]))):
            raise ValueError("Four increasing positive lifecycle timestamps are required")
        if self.current_period_end <= self.event_timestamps[-1]:
            raise ValueError("Period end must follow lifecycle timestamps")
        if self.invoice_credit_delta != 100:
            raise ValueError("The frozen invoice contract grants exactly 100 credits")

    def initial_users(self) -> list[dict]:
        users = [{
            "id": self.user_id, "email": self.email,
            "stripe_customer_id": self.customer_id, "plan_tier": "free",
            "credit_balance": self.initial_credit_balance,
            "welcome_email_sent": 0, "last_event_ts": 0,
        }]
        if self.bystander:
            users.append({
                "id": "bystander_" + self.user_id, "email": "bystander@novacloud.ai",
                "stripe_customer_id": "bystander_" + self.customer_id,
                "plan_tier": "free", "credit_balance": 37,
                "welcome_email_sent": 0, "last_event_ts": 0,
            })
        return users

    def expected_state(self) -> dict:
        """Build a fresh legacy report snapshot using only declared outcomes.

        ``welcome_email_count`` observes this user's ``welcome_pro`` records;
        the total outbox count also excludes extra or misdirected messages.
        """
        return {
            "user": {
                "id": self.user_id,
                "plan_tier": self.plan_tier,
                "credit_balance": self.initial_credit_balance + self.invoice_credit_delta,
                "welcome_email_count": 1,
            },
            "subscription": {
                "id": self.subscription_id,
                "status": self.subscription_status,
                "current_period_end": self.current_period_end,
            },
            "credit_ledger_rows": 1,
            "credit_ledger_sum": self.invoice_credit_delta,
            "outbox_emails_count": 1,
        }

    def validate(
        self, session: Session, actual: dict, errors: Sequence[str] = (),
    ) -> list[PropertyViolation]:
        """Check observable outcomes, ledger attribution, and execution errors.

        Aggregate counts and sums alone cannot establish which invoice or user
        received the credit. Read those identities from the real database too.
        Expected injected crashes are handled by the runner; all remaining
        handler errors and missing synchronization/crash evidence fail here.
        """
        from chaoshook_engine.snapshot import diff_states

        expected = self.expected_state()
        violations = diff_states(expected, actual)
        for entity in ("user", "subscription"):
            observed_id = (actual.get(entity) or {}).get("id")
            if observed_id != expected[entity]["id"]:
                violations.append({
                    "field": f"{entity}.id",
                    "expected": expected[entity]["id"], "actual": observed_id,
                })

        invalid_entries = [
            {"user_id": user_id, "invoice_id": invoice_id}
            for user_id, invoice_id in session.execute(
                select(CreditLedger.user_id, CreditLedger.invoice_id)
                .order_by(CreditLedger.id)
            )
            if user_id != self.user_id or invoice_id != self.invoice_id
        ]
        if invalid_entries:
            violations.append({
                "field": "credit_ledger.identity",
                "expected": {"user_id": self.user_id, "invoice_id": self.invoice_id},
                "actual": invalid_entries,
            })
        invalid_emails = [
            {"user_id": user_id, "template": template}
            for user_id, template in session.execute(
                select(OutboxEmail.user_id, OutboxEmail.template).order_by(OutboxEmail.id)
            )
            if user_id != self.user_id or template != self.welcome_template
        ]
        if invalid_emails:
            violations.append({
                "field": "outbox_emails.identity",
                "expected": {"user_id": self.user_id, "template": self.welcome_template},
                "actual": invalid_emails,
            })
        # Observe all identities and all bystander columns, not just sums.
        users = {user.id: user for user in session.scalars(select(User))}
        expected_users = self.initial_users()
        if set(users) != {u["id"] for u in expected_users}:
            violations.append({"field": "users.identity", "expected": sorted(u["id"] for u in expected_users),
                               "actual": sorted(users)})
        for initial in expected_users:
            user = users.get(initial["id"])
            fields = initial if initial["id"] != self.user_id else {
                key: initial[key] for key in ("id", "email", "stripe_customer_id")
            }
            for key, value in fields.items():
                observed = getattr(user, key, None)
                if observed != value:
                    violations.append({"field": f"users.{initial['id']}.{key}",
                                       "expected": value, "actual": observed})
        identities = sorted((sub.id, sub.user_id, sub.stripe_customer_id)
                            for sub in session.scalars(select(Subscription)))
        expected_identities = [(self.subscription_id, self.user_id, self.customer_id)]
        if identities != expected_identities:
            violations.append({"field": "subscriptions.identity", "expected": expected_identities,
                               "actual": identities})
        if errors:
            violations.append({
                "field": "execution.errors", "expected": [], "actual": list(errors),
            })
        return violations


SCENARIO_SPEC = LifecycleScenarioSpec()
