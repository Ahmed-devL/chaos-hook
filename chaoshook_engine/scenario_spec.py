"""Independent business contract for the fixed four-event NovaCloud scenario.

All 24 delivery orders must converge for this scenario. This is an explicit
scenario requirement, not a claim about webhook handlers in general. Neither
expected state nor validation is derived from a candidate or example repair.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaoshook_engine.snapshot import diff_states
from target_app.models import CreditLedger, OutboxEmail


class PropertyViolation(TypedDict):
    field: str
    expected: object
    actual: object


@dataclass(frozen=True)
class LifecycleScenarioSpec:
    """Immutable outcomes for the seeded user and the single paid invoice."""

    user_id: str = "usr_alice_01"
    subscription_id: str = "sub_nova_777"
    invoice_id: str = "in_nova_555"
    initial_credit_balance: int = 0
    invoice_credit_delta: int = 100
    plan_tier: Literal["canceled"] = "canceled"
    subscription_status: Literal["canceled"] = "canceled"
    current_period_end: int = 1702592000
    welcome_template: Literal["welcome_pro"] = "welcome_pro"

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
        if errors:
            violations.append({
                "field": "execution.errors", "expected": [], "actual": list(errors),
            })
        return violations


SCENARIO_SPEC = LifecycleScenarioSpec()
