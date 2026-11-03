"""Behavioral gate for the 27-cell real-SQL prover and its report contract."""

import json
from itertools import permutations
from pathlib import Path
import sys

import pytest

# Support the requested pytest console script without an editable package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chaoshook_engine.runner import run_algebraic_prover
from target_app.handlers import v1_vulnerable, v2_patched


@pytest.fixture(scope="module")
def reports():
    return {
        "vulnerable": run_algebraic_prover(v1_vulnerable.process_stripe_webhook),
        "patched": run_algebraic_prover(v2_patched.process_stripe_webhook),
    }


def test_vulnerable_baseline(reports):
    report = reports["vulnerable"]
    assert (report["passed_cells"], report["failed_cells"]) == (6, 21)
    assert sum(not cell["passed"] for cell in report["cells"][:24]) == 18
    for cell in report["cells"][:24]:
        assert cell["passed"] == (cell["sequence"][-1] == "e4")
    for cell in report["cells"][24:]:
        assert not cell["passed"]
        assert cell["state_diff"]
    race, credits, emails = report["cells"][24:]
    for cell in (race, credits):
        assert cell["actual_state"]["user"]["credit_balance"] == 200
        assert cell["actual_state"]["credit_ledger_rows"] == 2
    assert emails["actual_state"]["outbox_emails_count"] == 2


def test_patched_invariance(reports):
    report = reports["patched"]
    assert (report["passed_cells"], report["failed_cells"]) == (27, 0)
    for cell in report["cells"]:
        assert cell["passed"], cell["failure_reason"]
        assert cell["state_diff"] == []
        assert cell["actual_state"] == report["golden_state"]


def test_report_contract_and_fault_evidence(reports):
    expected_golden = {
        "user": {"id": "usr_alice_01", "plan_tier": "canceled",
                 "credit_balance": 100, "welcome_email_count": 1},
        "subscription": {"id": "sub_nova_777", "status": "canceled",
                         "current_period_end": 1702592000},
        "credit_ledger_rows": 1, "credit_ledger_sum": 100, "outbox_emails_count": 1,
    }
    for report in reports.values():
        assert report["golden_state"] == expected_golden
        assert report["total_cells"] == len(report["cells"]) == 27
        assert report["duration_ms"] > 0
        assert [cell["id"] for cell in report["cells"]] == list(range(1, 28))
        assert [cell["sequence"] for cell in report["cells"][:24]] == [
            list(order) for order in permutations(["e1", "e2", "e3", "e4"])
        ]
        for cell in report["cells"]:
            assert set(cell) == {
                "id", "category", "label", "sequence", "passed", "failure_reason",
                "failure_tag", "actual_state", "state_diff", "sql_trace",
            }
            assert cell["sql_trace"]
            assert bool(cell["failure_tag"]) == (not cell["passed"])
        trace = "\n".join(report["cells"][24]["sql_trace"])
        assert "RaceWorker-A" in trace and "RaceWorker-B" in trace
        assert trace.count("Reached SQL barrier") == 2
        for cell in report["cells"][25:]:
            trace = "\n".join(cell["sql_trace"])
            assert trace.count("Attempt 1: injecting crash") == 1
            assert "Attempt 2: replay identical event" in trace
        json.dumps(report)


def test_unexpected_handler_errors_complete_matrix(reports):
    def broken_handler(session, raw_body, sig_header):
        raise ValueError("deliberate handler failure")

    report = run_algebraic_prover(broken_handler)
    assert report["golden_state"] == reports["patched"]["golden_state"]
    assert (report["passed_cells"], report["failed_cells"]) == (0, 27)
    assert len(report["cells"]) == 27
    for cell in report["cells"]:
        assert "ValueError: deliberate handler failure" in cell["failure_reason"]
        assert cell["state_diff"]
    for cell in report["cells"][25:]:
        assert "Expected mid-handler crash was not injected" in cell["failure_reason"]
