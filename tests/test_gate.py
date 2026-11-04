"""Behavioral gate for the 27-cell real-SQL prover and its report contract."""

import builtins
from dataclasses import FrozenInstanceError
import importlib.util
import json
from itertools import permutations
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

# Support the requested pytest console script without an editable package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chaoshook_engine.runner import run_algebraic_prover
from chaoshook_engine.scenario_spec import SCENARIO_SPEC
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
    assert race["failure_tag"] == "DOUBLE_CREDIT_RACE"
    assert credits["failure_tag"] == "DOUBLE_CREDIT_RETRY"
    assert emails["failure_tag"] == "DUPLICATE_WELCOME_EMAIL"
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
        assert cell["property_violations"] == []
        assert cell["actual_state"] == report["golden_state"]
    assert report["golden_state"] == SCENARIO_SPEC.expected_state()


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
                "property_violations",
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


def test_scenario_spec_is_frozen_and_expected_state_is_fresh():
    with pytest.raises(FrozenInstanceError):
        SCENARIO_SPEC.invoice_credit_delta = 0
    expected = SCENARIO_SPEC.expected_state()
    assert expected["user"]["credit_balance"] == SCENARIO_SPEC.initial_credit_balance + 100
    expected["user"]["credit_balance"] = 0
    assert SCENARIO_SPEC.expected_state()["user"]["credit_balance"] == 100


def _consistent_handler_without_invoice_credit(session, raw_body, sig_header):
    """Test-only negative control: convergent lifecycle, deliberately no credits."""
    from sqlalchemy import text
    from target_app.hmac_auth import verify_stripe_signature
    from target_app.models import Subscription, User
    from target_app.schemas import StripeEvent
    from target_app.side_effects import send_welcome_email

    if not verify_stripe_signature(raw_body, sig_header):
        raise ValueError("Invalid Stripe signature")
    event = StripeEvent.model_validate_json(raw_body)
    obj = event.data.object
    with session.begin():
        claim = session.execute(text(
            "INSERT INTO webhook_events (event_id, event_type, created_ts, status) "
            "VALUES (:id, :type, :ts, 'processed') ON CONFLICT(event_id) DO NOTHING"
        ), {"id": event.id, "type": event.type, "ts": event.created})
        if claim.rowcount == 0:
            return {"status": "duplicate"}
        user = session.query(User).filter_by(stripe_customer_id=obj["customer"]).one()
        if event.type == "invoice.paid":
            # The intentional defect: claim delivery but omit both invoice effects.
            return {"status": "processed"}
        if event.type == "checkout.session.completed":
            plan = "pro"
            send_welcome_email(session, user.id, dedupe_key=event.id)
            user.welcome_email_sent = 1
        else:
            deleted = event.type == "customer.subscription.deleted"
            status = "canceled" if deleted else "active"
            plan = "canceled" if deleted else "pro"
            sub = session.get(Subscription, obj["id"])
            if sub is None:
                sub = Subscription(
                    id=obj["id"], user_id=user.id, stripe_customer_id=obj["customer"],
                    status=status, current_period_end=0, last_event_ts=0,
                )
                session.add(sub)
            if event.created >= sub.last_event_ts:
                sub.status = status
                sub.last_event_ts = event.created
            sub.current_period_end = max(sub.current_period_end, obj["current_period_end"])
        if event.created >= user.last_event_ts:
            user.plan_tier = plan
            user.last_event_ts = event.created
    return {"status": "processed"}


def test_consistency_without_invoice_effect_is_rejected_without_reference(monkeypatch):
    from chaoshook_engine import runner
    from chaoshook_engine.snapshot import diff_states

    def unexpected_reference(*args, **kwargs):
        pytest.fail("the reference handler must not determine acceptance")

    monkeypatch.setattr(v2_patched, "process_stripe_webhook", unexpected_reference)
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "target_app.handlers.v2_patched" or (
            name == "target_app.handlers" and "v2_patched" in (fromlist or ())
        ):
            pytest.fail("the correctness gate must not import the reference handler")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    # Load the runner afresh under the guard, so a cached import cannot hide a
    # production dependency. This neither installs nor replaces any candidate.
    spec = importlib.util.spec_from_file_location("_independent_runner", runner.__file__)
    independent_runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(independent_runner)
    report = independent_runner.run_algebraic_prover(_consistent_handler_without_invoice_credit)

    normal_order = report["cells"][0]["actual_state"]
    assert report["cells"][0]["sequence"] == ["e1", "e2", "e3", "e4"]
    missing_effects = {"user.credit_balance", "credit_ledger_rows", "credit_ledger_sum"}
    assert {item["field"] for item in diff_states(report["golden_state"], normal_order)} == missing_effects
    for cell in report["cells"][:24]:
        # A self-derived normal-order oracle would incorrectly accept every order.
        assert cell["actual_state"] == normal_order
        assert diff_states(normal_order, cell["actual_state"]) == []
        assert not cell["passed"]
        assert {item["field"] for item in cell["property_violations"]} == missing_effects
    assert normal_order["user"]["credit_balance"] == 0
    assert normal_order["credit_ledger_rows"] == normal_order["credit_ledger_sum"] == 0
    assert report["passed_cells"] == 0


@pytest.mark.parametrize("field, wrong_value", [
    ("invoice_id", "in_wrong_invoice"), ("user_id", "usr_wrong_recipient"),
])
def test_independent_contract_rejects_misattributed_ledger(tmp_path, field, wrong_value):
    from sqlalchemy.orm import Session
    from chaoshook_engine.fixtures import get_canonical_events, sign_event
    from chaoshook_engine.snapshot import create_cell_engine, extract_comparable_state, seed_initial_state
    from target_app.models import Base, CreditLedger

    engine = create_cell_engine(tmp_path / "ledger.db")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            seed_initial_state(session)
            for payload in get_canonical_events():
                v2_patched.process_stripe_webhook(session, *sign_event(payload))
            assert SCENARIO_SPEC.validate(session, extract_comparable_state(session)) == []
            setattr(session.query(CreditLedger).one(), field, wrong_value)
            session.commit()
            actual = extract_comparable_state(session)
            assert actual == SCENARIO_SPEC.expected_state()  # Aggregates still match.
            violations = SCENARIO_SPEC.validate(session, actual)
            assert [item["field"] for item in violations] == ["credit_ledger.identity"]
            assert SCENARIO_SPEC.validate(session, actual, ["unexpected delivery error"])[-1] == {
                "field": "execution.errors", "expected": [], "actual": ["unexpected delivery error"],
            }
    finally:
        engine.dispose()


def test_correct_state_with_unexpected_errors_is_rejected():
    def fails_after_commit(session, raw_body, sig_header):
        v2_patched.process_stripe_webhook(session, raw_body, sig_header)
        raise ValueError("failure after commit")

    report = run_algebraic_prover(fails_after_commit)
    assert report["passed_cells"] == 0
    for cell in report["cells"]:
        assert cell["actual_state"] == report["golden_state"]
        assert cell["state_diff"] == []
        assert {item["field"] for item in cell["property_violations"]} == {"execution.errors"}
        assert cell["failure_tag"] == "HANDLER_ERROR"


def test_unexpected_handler_errors_complete_matrix(reports):
    def broken_handler(session, raw_body, sig_header):
        raise ValueError("deliberate handler failure")

    report = run_algebraic_prover(broken_handler)
    assert report["golden_state"] == reports["patched"]["golden_state"]
    assert (report["passed_cells"], report["failed_cells"]) == (0, 27)
    assert len(report["cells"]) == 27
    for cell in report["cells"]:
        assert "ValueError: deliberate handler failure" in cell["failure_reason"]
        assert cell["failure_tag"] == "HANDLER_ERROR"
        assert cell["state_diff"]
    for cell in report["cells"][25:]:
        assert "Expected mid-handler crash was not injected" in cell["failure_reason"]


def test_muse_aliases_and_defaults(monkeypatch):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    for name in ("MUSE_API_KEY", "MUSE_BASE_URL", "MUSE_TIMEOUT_S",
                 "MUSE_WONDER_MODEL", "MUSE_SPARK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "unit-secret")
    monkeypatch.setenv("META_BASE_URL", "https://example.invalid/v1/")
    client = MuseNeuroSymbolicClient()
    assert client.api_key == "unit-secret"
    assert client.base_url == "https://example.invalid/v1"
    assert client.timeout == 180
    monkeypatch.setenv("MUSE_API_KEY", "primary-secret")
    monkeypatch.setenv("MUSE_BASE_URL", "https://primary.invalid/v1")
    monkeypatch.setenv("MUSE_TIMEOUT_S", "25")
    primary = MuseNeuroSymbolicClient()
    assert primary.api_key == "primary-secret"
    assert primary.base_url == "https://primary.invalid/v1"
    assert primary.timeout == 25


def test_muse_fenced_live_patch_and_partial_fallback(monkeypatch):
    from threading import Barrier
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv("MUSE_API_KEY", "unit-secret")
    monkeypatch.setenv("DEMO_FALLBACK_CACHE", "false")
    monkeypatch.setenv("MUSE_WONDER_MODEL", "wonder-live")
    monkeypatch.setenv("MUSE_SPARK_MODEL", "spark-live")
    client = MuseNeuroSymbolicClient()
    calls = []
    simultaneous_calls = Barrier(2)
    code = (Path(v2_patched.__file__)).read_text(encoding="utf-8")

    def request(method, path, **kwargs):
        payload = kwargs["json"]
        calls.append(payload["model"])
        simultaneous_calls.wait(timeout=2)
        assert payload["response_format"] == {"type": "json_object"}
        value = ({"invariants": client._invariants({}), "patched_code": code}
                 if payload["model"] == "wonder-live" else {"failure_clusters": []})
        return {"choices": [{"message": {"content": "```json\n" + json.dumps(value) + "\n```"}}]}

    monkeypatch.setattr(client, "api_request", request)
    result = client.run_full_neuro_symbolic_analysis({})
    assert sorted(calls) == ["spark-live", "wonder-live"]
    assert result["patched_code"] == code
    routing = result["model_routing"]
    assert routing["cegis_rounds"] == [{"round": 1, "passed_cells": 27,
                                       "failed_cell_ids": [], "verified": True, "import_error": None,
                                       "payload_chars": routing["payload_chars"]["wonder"][0]}]
    assert routing["wonder_live"] is True
    assert routing["spark_live"] is False
    assert routing["used_fallback_cache"] is True
    assert routing["elapsed_ms"] >= 0
    assert "spark:" in routing["fallback_reason"]
    assert "structured output contract" in routing["fallback_reason"]
    assert len(result["diagnosis"]["failure_clusters"]) == 3


def test_muse_cache_forced_without_network(monkeypatch):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv("DEMO_FALLBACK_CACHE", "true")
    client = MuseNeuroSymbolicClient()

    def unexpected_request(*args, **kwargs):
        pytest.fail("forced cache must not call the API")

    monkeypatch.setattr(client, "api_request", unexpected_request)
    result = client.run_full_neuro_symbolic_analysis({})
    routing = result["model_routing"]
    assert routing["wonder_live"] is False
    assert routing["spark_live"] is False
    assert routing["used_fallback_cache"] is True
    assert "wonder:" in routing["fallback_reason"]
    assert "spark:" in routing["fallback_reason"]


def test_muse_http_error_redacts_credentials(monkeypatch):
    import httpx
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv("MUSE_API_KEY", "unit-secret")
    client = MuseNeuroSymbolicClient()
    original_client = httpx.Client

    def upstream(request):
        assert request.headers["Authorization"] == "Bearer unit-secret"
        return httpx.Response(401, text="Rejected unit-secret")

    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        transport=transport, **kwargs))
    with pytest.raises(ValueError) as caught:
        client.api_request("GET", "models")
    assert "HTTP 401" in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
    assert "unit-secret" not in str(caught.value)


@pytest.mark.parametrize("broken_import", [False, True])
def test_live_patch_check_is_temporary_and_read_only(monkeypatch, capsys, broken_import):
    from chaoshook_engine import cli

    active = cli.HANDLER_DIR / "active_handler.py"
    original = active.read_bytes()
    source = ("raise ImportError('intentional import failure')\n" if broken_import else
              (cli.HANDLER_DIR / "v2_patched.py").read_text(encoding="utf-8"))

    class FakeMuse:
        wonder_model = "unit-wonder"
        model_routing = {"wonder_live": True, "cegis_rounds": [
            {"round": 1, "passed_cells": 0 if broken_import else 27}]}

        def safe_error(self, value):
            return str(value)

        def extract_invariants_and_patch(self, *args):
            return {"patched_code": source}

    paths = []
    original_spec = cli.importlib.util.spec_from_file_location

    def capture_spec(name, path):
        paths.append(path)
        assert not path.is_relative_to(active.parents[2])
        return original_spec(name, path)

    monkeypatch.setattr(cli, "MuseNeuroSymbolicClient", FakeMuse)
    monkeypatch.setattr(cli.importlib.util, "spec_from_file_location", capture_spec)
    assert cli.main(["live-patch-check"]) == (1 if broken_import else 0)
    assert active.read_bytes() == original
    assert paths and not paths[0].exists()
    output = capsys.readouterr().out
    assert f"CEGIS Round 1: {0 if broken_import else 27}/27 cells passed" in output
    assert ("intentional import failure" if broken_import else "27/27 cells passed") in output


def test_patch_api_verifies_before_install_and_cached_fallback(monkeypatch):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server
    from chaoshook_engine.patcher import HANDLER_DIR, apply_patch_to_active

    active = HANDLER_DIR / "active_handler.py"
    original = active.read_text(encoding="utf-8")
    client = TestClient(server.app)
    try:
        for source, passed, import_failed in (
            ("def broken(", 0, True),
            ("raise ImportError('bad candidate')", 0, True),
            ((HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8"), 6, False),
        ):
            before = active.read_bytes()
            result = client.post("/api/patch", json={"patched_code": source})
            assert result.status_code == 200
            body = result.json()
            assert body["verified"] is False
            assert body["passed_cells"] == passed
            assert bool(body["import_error"]) is import_failed
            if not import_failed:
                assert len(body["failed_cell_ids"]) == 21
                assert body["prover_report"]["passed_cells"] == 6
            assert active.read_bytes() == before

        patched_code = (HANDLER_DIR / "v2_patched.py").read_text(encoding="utf-8")
        candidate = patched_code + "\n# Candidate proved before installation.\n"
        accepted = client.post("/api/patch", json={"patched_code": candidate}).json()
        assert accepted["verified"] is True
        assert accepted["prover_report"]["passed_cells"] == 27
        assert active.read_text(encoding="utf-8") == candidate

        # No body must select reference v2 even when the latest proposal is invalid.
        server.app.state.analysis = {"patched_code": "def broken("}
        cached = client.post("/api/patch").json()
        assert cached["verified"] is True
        assert cached["prover_report"]["passed_cells"] == 27
        assert active.read_text(encoding="utf-8") == patched_code
    finally:
        apply_patch_to_active(original)
        server.app.state.prover_source = None
        server.app.state.analysis = None


def test_diagnose_instant_mode_never_calls_live_models(monkeypatch):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server

    def unexpected_request(*args, **kwargs):
        pytest.fail("instant mode must not call live models")

    monkeypatch.setattr(server.MuseNeuroSymbolicClient, "api_request", unexpected_request)
    response = TestClient(server.app).post("/api/diagnose", json={"use_cache": True})
    assert response.status_code == 200
    analysis = response.json()
    routing = analysis["model_routing"]
    assert routing["wonder_live"] is False and routing["spark_live"] is False
    assert routing["used_fallback_cache"] is True
    assert routing["fallback_reason"]
    assert routing["elapsed_ms"] >= 0
    assert len(analysis["invariants"]) == 4
    assert len(analysis["diagnosis"]["failure_clusters"]) == 3


@pytest.fixture
def partial_code():
    from chaoshook_engine.patcher import HANDLER_DIR

    return (HANDLER_DIR / "v1_partial_idempotent.py").read_text(encoding="utf-8")


def _fake_wonder(monkeypatch, sources):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv("MUSE_API_KEY", "cegis-secret")
    monkeypatch.setenv("MUSE_BASE_URL", "https://private.invalid/v1")
    monkeypatch.setenv("DEMO_FALLBACK_CACHE", "false")
    client = MuseNeuroSymbolicClient()
    calls = []
    responses = iter(sources)

    def request(method, path, **kwargs):
        calls.append(kwargs["json"])
        return {"choices": [{"message": {"content": json.dumps({
            "invariants": client._invariants({}), "patched_code": next(responses),
        })}}]}

    monkeypatch.setattr(client, "api_request", request)
    return client, calls


def _extract_patch(client, report):
    from chaoshook_engine.patcher import HANDLER_DIR

    return client.extract_invariants_and_patch(
        (HANDLER_DIR.parent / "models.py").read_text(encoding="utf-8"),
        (HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8"), report,
    )


def test_live_wonder_is_reference_free_and_repairs_counterexamples(monkeypatch, reports, partial_code):
    reference = Path(v2_patched.__file__).read_text(encoding="utf-8")
    client, calls = _fake_wonder(monkeypatch, [partial_code, reference])
    result = _extract_patch(client, reports["vulnerable"])
    assert result["patched_code"] == reference
    rounds = client.model_routing["cegis_rounds"]
    assert len(rounds) == len(calls) == 2
    assert rounds[0]["verified"] is False
    assert 6 < rounds[0]["passed_cells"] < 27
    assert rounds[1] == {"round": 2, "passed_cells": 27, "failed_cell_ids": [],
                         "verified": True, "import_error": None,
                         "payload_chars": len(json.dumps(calls[1]))}
    assert client.model_routing["wonder_live"] is True
    assert client.model_routing["fallback_reason"] is None
    for call in calls:
        assert "verified_reference_code" not in json.dumps(call)
        assert reference not in call["messages"][0]["content"]
        payload = json.loads(call["messages"][1]["content"])
        assert set(payload) == {"schema_code", "handler_code", "prover_report", "handler_contract"}
        assert reference not in payload.values()
        assert len(json.dumps(call)) < 40000
        for cell in payload["prover_report"]["counterexamples"]:
            fields = {"id", "category", "label", "sequence", "failure_reason", "state_diff"}
            if cell["category"] in {"concurrency", "crash_retry"}:
                fields.add("sql_trace")
            assert set(cell) == fields
    feedback = json.loads(calls[1]["messages"][1]["content"])
    assert feedback["handler_code"] == partial_code
    assert feedback["prover_report"]["summary"]["failed_cell_ids"] == rounds[0]["failed_cell_ids"]


def test_live_wonder_unverified_rounds_fall_back(monkeypatch, reports, partial_code):
    client, calls = _fake_wonder(monkeypatch, [partial_code, partial_code])
    result = _extract_patch(client, reports["vulnerable"])
    assert result["patched_code"] == Path(v2_patched.__file__).read_text(encoding="utf-8")
    assert len(calls) == len(client.model_routing["cegis_rounds"]) == 2
    assert not any(r["verified"] for r in client.model_routing["cegis_rounds"])
    assert client.model_routing["wonder_live"] is False
    assert client.used_fallback_cache is True
    assert "CEGIS exhausted 2 rounds" in client.model_routing["fallback_reason"]


def test_cegis_import_feedback_redacts_key_and_base_url(monkeypatch, reports):
    broken = ("def process_stripe_webhook(session, raw_body, sig_header): pass\n"
              "raise ImportError('cegis-secret https://private.invalid/v1')\n")
    reference = Path(v2_patched.__file__).read_text(encoding="utf-8")
    client, calls = _fake_wonder(monkeypatch, [broken, reference])
    _extract_patch(client, reports["vulnerable"])
    error = client.model_routing["cegis_rounds"][0]["import_error"]
    feedback = json.loads(calls[1]["messages"][1]["content"])["prover_report"]
    assert feedback["import_error"] == error
    assert "cegis-secret" not in error and "private.invalid" not in error
    assert "[REDACTED]" in error
    assert client.model_routing["cegis_rounds"][1]["verified"] is True


def test_partial_handler_proves_atomicity_but_fails_ordering(partial_code):
    from chaoshook_engine.patcher import verify_candidate_patch

    result = verify_candidate_patch(partial_code)
    assert result["import_error"] is None
    assert result["verified"] is False
    assert result["passed_cells"] == 9
    cells = result["prover_report"]["cells"]
    assert all(cell["passed"] for cell in cells[24:])
    assert any(not cell["passed"] for cell in cells[:24])


def test_partial_patch_status_and_api_rejection_leave_active_unchanged(partial_code):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server
    from chaoshook_engine.patcher import HANDLER_DIR

    active = HANDLER_DIR / "active_handler.py"
    original = active.read_bytes()
    client = TestClient(server.app)
    assert client.get("/api/status").json()["partial_patch_code"] == partial_code
    response = client.post("/api/patch", json={"patched_code": partial_code})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "rejected"
    assert result["verified"] is False
    assert 6 < result["passed_cells"] < 27
    assert result["failed_cell_ids"]
    assert all(1 <= cell_id <= 24 for cell_id in result["failed_cell_ids"])
    assert active.read_bytes() == original


def test_counterexample_view_preserves_distinct_failures_and_fault_evidence(reports):
    from chaoshook_engine.muse_client import counterexample_view

    report = reports["vulnerable"]
    before = json.dumps(report)
    view = counterexample_view(report)
    assert len(json.dumps(view)) < 30000
    assert view["golden_state"] == report["golden_state"]
    assert view["summary"] == {"passed_cells": 6, "total_cells": 27,
                               "failed_cell_ids": [c["id"] for c in report["cells"] if not c["passed"]]}
    examples = view["counterexamples"]
    assert {c["failure_reason"] for c in examples} == {
        c["failure_reason"] for c in report["cells"] if not c["passed"]}
    by_id = {cell["id"]: cell for cell in report["cells"]}
    for cell in examples:
        assert not by_id[cell["id"]]["passed"]
        assert "actual_state" not in cell
        if cell["category"] == "permutation":
            assert "sql_trace" not in cell
        else:
            assert cell["sql_trace"] == by_id[cell["id"]]["sql_trace"][:12]
    assert {c["id"] for c in examples if "sql_trace" in c} == {25, 26, 27}
    permutation_reasons = [c["failure_reason"] for c in examples if c["category"] == "permutation"]
    assert len(permutation_reasons) == len(set(permutation_reasons))
    assert json.dumps(report) == before
    # Force trace removal without losing a single distinct failure reason.
    without_traces = json.loads(json.dumps(view))
    for cell in without_traces["counterexamples"]:
        cell.pop("sql_trace", None)
    compact = counterexample_view(report, max_chars=len(json.dumps(without_traces)))
    assert not any("sql_trace" in cell for cell in compact["counterexamples"])
    assert len(json.dumps(compact)) <= len(json.dumps(without_traces))
    assert json.dumps(report) == before


def test_spark_uses_compact_payload_and_records_full_request_size(monkeypatch, reports):
    from chaoshook_engine.muse_client import counterexample_view

    client, _ = _fake_wonder(monkeypatch, [])
    calls = []

    def request(method, path, **kwargs):
        calls.append(kwargs["json"])
        # An invalid response still records the size of the attempted live call.
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(client, "api_request", request)
    client.diagnose_multimodal_divergence(reports["vulnerable"])
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["prover_report"] == counterexample_view(reports["vulnerable"])
    assert len(json.dumps(calls[0])) < 40000
    assert client.model_routing["payload_chars"]["spark"] == [len(json.dumps(calls[0]))]


def test_cegis_skips_second_round_when_projected_runtime_exceeds_budget(monkeypatch, reports, partial_code):
    from chaoshook_engine import muse_client

    client, calls = _fake_wonder(monkeypatch, [partial_code])
    clock = [0.0]
    monkeypatch.setattr(muse_client, "perf_counter", lambda: clock[0])
    request = client.api_request

    def slow_request(*args, **kwargs):
        assert kwargs["timeout"] == 180
        response = request(*args, **kwargs)
        clock[0] = 125.0
        return response

    monkeypatch.setattr(client, "api_request", slow_request)
    result = _extract_patch(client, reports["vulnerable"])
    assert result["patched_code"] == Path(v2_patched.__file__).read_text(encoding="utf-8")
    assert len(calls) == len(client.model_routing["cegis_rounds"]) == 1
    assert client.model_routing["wonder_live"] is False
    assert "round 2 skipped" in client.model_routing["fallback_reason"]
    assert "240-second total time budget" in client.model_routing["fallback_reason"]
    assert client.model_routing["cegis_rounds"][0]["payload_chars"] == len(json.dumps(calls[0]))


@pytest.mark.parametrize('failures, expected_multimodal', [
    ([], True), ([400], True), ([400, 503], False), ([503], False),
    ([400, 503, 503], False),
])
def test_spark_image_retry_chain(monkeypatch, failures, expected_multimodal):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv('MUSE_API_KEY', 'image-unit-secret')
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'false')
    client = MuseNeuroSymbolicClient()
    image = 'data:image/png;base64,aW1hZ2U='
    diagnosis = {
        'summary_headline': 'Observed failures',
        'failure_clusters': [{'title': 'Cluster', 'description': 'Evidence', 'cells': [25]}] * 3,
        'remediation_proof_plan': 'Prove the repair',
        'diagram_findings': ['Handler exactly-once assumption contradicted by cell 25'],
    }
    calls = []

    def request(method, path, **kwargs):
        body = kwargs['json']
        calls.append(body)
        if len(calls) <= len(failures):
            raise ValueError(f'HTTP {failures[len(calls) - 1]}: image-unit-secret {image}')
        return {'choices': [{'message': {'content': '```json\n' + json.dumps(diagnosis) + '\n```'}}]}

    monkeypatch.setattr(client, 'api_request', request)
    result = client.diagnose_multimodal_divergence({}, diagram_image=image)
    assert client.model_routing['spark_multimodal'] is expected_multimodal
    assert client.model_routing['spark_live'] is (len(failures) < 3)
    assert client.model_routing['image_bytes'] == 5
    assert calls[0]['messages'][1]['content'][1] == {
        'type': 'image_url', 'image_url': {'url': image}}
    if failures and failures[0] == 400:
        assert 'response_format' not in calls[1]
        assert calls[1]['messages'][1]['content'][1]['image_url']['url'] == image
    if not expected_multimodal:
        assert isinstance(calls[-1]['messages'][1]['content'], str)
    if len(failures) < 3:
        assert result == diagnosis
    else:
        assert 'diagram_findings' not in result
    recorded = json.dumps(client.model_routing)
    assert image not in recorded and 'image-unit-secret' not in recorded
    text_body = json.loads(json.dumps(calls[0]))
    text_body['messages'][1]['content'] = text_body['messages'][1]['content'][0]['text']
    assert client.model_routing['payload_chars']['spark'] == [len(json.dumps(text_body))]


def test_spark_text_success_and_force_cache_with_image(monkeypatch):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient

    monkeypatch.setenv('MUSE_API_KEY', 'image-unit-secret')
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'true')
    client = MuseNeuroSymbolicClient()

    def unexpected(*args, **kwargs):
        pytest.fail('force_cache must skip image and text requests')

    monkeypatch.setattr(client, 'api_request', unexpected)
    cached = client.diagnose_multimodal_divergence({}, diagram_image='data:image/png;base64,aW1hZ2U=')
    assert client.model_routing['spark_multimodal'] is False
    assert client.model_routing['spark_live'] is False
    client.force_cache = False
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs['json'])
        return {'choices': [{'message': {'content': json.dumps(cached)}}]}

    monkeypatch.setattr(client, 'api_request', request)
    assert client.diagnose_multimodal_divergence({}) == cached
    assert isinstance(calls[0]['messages'][1]['content'], str)
    assert client.model_routing['spark_live'] is True
    assert client.model_routing['spark_multimodal'] is False


@pytest.mark.parametrize('image', [
    'https://example.com/flow.png', 'data:image/svg+xml;base64,PHN2Zz4=',
    'data:image/png;base64,' + 'A' * 1_500_000,
    'data:image/jpeg;base64,invalid!',
])
def test_diagnose_rejects_invalid_images_before_models(monkeypatch, image):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server

    def unexpected(*args, **kwargs):
        pytest.fail('invalid image must be rejected before any model request')

    monkeypatch.setattr(server.MuseNeuroSymbolicClient, 'api_request', unexpected)
    response = TestClient(server.app).post('/api/diagnose', json={'diagram_image': image})
    assert response.status_code == 400
    assert image not in response.text


def test_diagnose_passes_image_to_spark(monkeypatch):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server

    image = 'data:image/jpeg;base64,aW1hZ2U='
    original = server.MuseNeuroSymbolicClient.diagnose_multimodal_divergence
    seen = []

    def diagnose(self, report, diagram_description=None, diagram_image=None):
        seen.append(diagram_image)
        return original(self, report, diagram_description, diagram_image)

    monkeypatch.setattr(server.MuseNeuroSymbolicClient, 'diagnose_multimodal_divergence', diagnose)
    response = TestClient(server.app).post('/api/diagnose', json={'diagram_image': image, 'use_cache': True})
    assert response.status_code == 200
    assert seen == [image]
    assert response.json()['model_routing']['spark_multimodal'] is False


@pytest.fixture
def source_editor():
    from fastapi.testclient import TestClient
    from chaoshook_engine import server
    from chaoshook_engine.patcher import HANDLER_DIR, apply_patch_to_active

    active = HANDLER_DIR / "active_handler.py"
    original = active.read_text(encoding="utf-8")
    try:
        yield TestClient(server.app), active
    finally:
        apply_patch_to_active(original)
        server.app.state.prover_source = None
        server.app.state.analysis = None


def test_update_source_installs_and_proves_partial_edit(source_editor, partial_code):
    client, active = source_editor
    response = client.post('/api/update-source', json={'source_code': partial_code})
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['handler_status']['mode'] == 'patched'
    assert body['handler_status']['active_code'] == active.read_text(encoding='utf-8') == partial_code
    assert body['prover_report']['total_cells'] == len(body['prover_report']['cells']) == 27
    assert 6 < body['prover_report']['passed_cells'] < 27
    assert client.get('/api/status').json()['prover_report'] == body['prover_report']


@pytest.mark.parametrize('source', ['', ' \n\t', '#' * 100_001, '#' + 'é' * 50_000])
def test_update_source_rejects_empty_and_oversized_code(source_editor, source):
    client, active = source_editor
    before = active.read_bytes()
    response = client.post('/api/update-source', json={'source_code': source})
    assert response.status_code == 400
    assert active.read_bytes() == before


def test_update_source_syntax_error_preserves_source(source_editor):
    client, active = source_editor
    before = active.read_bytes()
    response = client.post('/api/update-source', json={'source_code': '# edit\ndef broken(\n'})
    assert response.status_code == 400
    assert 'Line 2:' in response.json()['detail']
    assert 'never closed' in response.json()['detail']
    assert active.read_bytes() == before


def test_update_source_import_error_rolls_back(source_editor):
    client, active = source_editor
    before = active.read_bytes()
    status = client.get('/api/status').json()
    source = "def process_stripe_webhook(*args): pass\nraise ImportError('private-secret /private/path')\n"
    response = client.post('/api/update-source', json={'source_code': source})
    assert response.status_code == 400
    assert 'private-secret' not in response.text and '/private/path' not in response.text
    assert active.read_bytes() == before
    restored = client.post('/api/prove').json()
    assert restored['handler_status'] == status['handler_status']
    assert restored['prover_report']['passed_cells'] == status['prover_report']['passed_cells']


def test_update_source_runtime_error_completes_matrix(source_editor):
    client, active = source_editor
    source = "def process_stripe_webhook(session, raw_body, sig_header):\n    raise ValueError('edited runtime failure')\n"
    response = client.post('/api/update-source', json={'source_code': source})
    assert response.status_code == 200
    assert active.read_text(encoding='utf-8') == source
    report = response.json()['prover_report']
    assert report['passed_cells'] == 0 and report['failed_cells'] == 27
    assert len(report['cells']) == 27
    assert all('ValueError: edited runtime failure' in cell['failure_reason'] for cell in report['cells'])


@pytest.mark.parametrize('handler, expected_exit, summary', [
    ('v2_patched.py', 0, '27 passed'),
    ('v1_vulnerable.py', 1, '21 failed, 6 passed'),
])
def test_exported_pytest_runs_against_isolated_handlers(tmp_path, handler, expected_exit, summary):
    repo = Path(__file__).resolve().parents[1]
    active = repo / 'target_app' / 'handlers' / 'active_handler.py'
    original = active.read_bytes()
    for package in ('target_app', 'chaoshook_engine'):
        shutil.copytree(repo / package, tmp_path / package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    copied_handlers = tmp_path / 'target_app' / 'handlers'
    shutil.copyfile(copied_handlers / handler, copied_handlers / 'active_handler.py')
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    exported = subprocess.run(
        [sys.executable, '-m', 'chaoshook_engine.cli', 'export-pytest',
         '--out', 'test_webhook_invariance.py'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    from chaoshook_engine.runner import export_pytest_code

    assert (tmp_path / 'test_webhook_invariance.py').read_text(encoding='utf-8') == export_pytest_code()
    result = subprocess.run(
        [str(Path(sys.executable).with_name('pytest')), '-q', '--setup-show',
         'test_webhook_invariance.py'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == expected_exit, output
    assert summary in output
    assert output.count('SETUP    M invariance_report') == 1, output
    if expected_exit:
        assert 'test_order_commutativity_all_24_permutations' in output
        assert 'test_parallel_duplicate_sql_barrier_idempotency' in output
        assert 'test_mid_handler_crash_retry_atomicity[26]' in output
        assert 'test_mid_handler_crash_retry_atomicity[27]' in output
        assert 'expected' in output  # Row-level failure reasons reach pytest assertions.
    assert active.read_bytes() == original


def test_export_pytest_api_is_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    from chaoshook_engine import server
    from chaoshook_engine.patcher import HANDLER_DIR
    from chaoshook_engine.runner import export_pytest_code

    def unexpected(*args, **kwargs):
        pytest.fail('export must not run the prover or invoke Muse')

    monkeypatch.setattr(server, 'run_algebraic_prover', unexpected)
    monkeypatch.setattr(server, 'MuseNeuroSymbolicClient', unexpected)
    active = HANDLER_DIR / 'active_handler.py'
    original = active.read_bytes()
    response = TestClient(server.app).get('/api/export-pytest')
    assert response.status_code == 200
    assert response.json() == {
        'filename': 'test_webhook_invariance.py', 'pytest_code': export_pytest_code(),
    }
    assert active.read_bytes() == original


@pytest.mark.parametrize('seed', [101, 202, 303])
def test_seeded_reference_contract(seed):
    from chaoshook_engine.fixtures import scenario_from_seed, get_canonical_events

    scenario = scenario_from_seed(seed)
    events = get_canonical_events(scenario)
    report = run_algebraic_prover(v2_patched.process_stripe_webhook, scenario)
    assert report['passed_cells'] == report['total_cells'] == 27
    assert report['golden_state']['user']['credit_balance'] == scenario.initial_credit_balance + 100
    assert scenario.initial_credit_balance > 0
    assert scenario.customer_id != 'cus_nova_99'
    assert scenario.subscription_id != 'sub_nova_777'
    assert events[0]['data']['object']['client_reference_id'] == scenario.user_id
    assert all(event['data']['object']['customer'] == scenario.customer_id for event in events)
    assert events[2]['data']['object']['subscription'] == scenario.subscription_id
    assert all(cell['actual_state'] == scenario.expected_state() for cell in report['cells'])


def test_seed_reproduction_and_variation():
    from chaoshook_engine.fixtures import scenario_from_seed, get_canonical_events

    first, same, other = [scenario_from_seed(seed) for seed in (101, 101, 202)]
    assert first == same
    assert get_canonical_events(first) == get_canonical_events(same)
    for field in ('user_id', 'customer_id', 'subscription_id', 'invoice_id', 'event_ids',
                  'initial_credit_balance', 'event_timestamps'):
        assert getattr(first, field) != getattr(other, field)
    reports = [run_algebraic_prover(v1_vulnerable.process_stripe_webhook, spec)
               for spec in (first, same)]
    for report in reports:
        report.pop('duration_ms')
        for cell in report['cells']:
            cell.pop('sql_trace')  # Racing thread trace ordering is intentionally unspecified.
    assert reports[0] == reports[1]


@pytest.mark.parametrize('defect', ['wrong_credit', 'bystander', 'subscription', 'ledger', 'outbox'])
def test_entity_corruption_rejected_with_matching_aggregates(tmp_path, defect):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from chaoshook_engine.fixtures import scenario_from_seed, get_canonical_events, sign_event
    from chaoshook_engine.snapshot import create_cell_engine, seed_initial_state, extract_comparable_state
    from target_app.models import Base, User, CreditLedger, OutboxEmail, Subscription

    scenario = scenario_from_seed(404)
    engine = create_cell_engine(tmp_path / 'entity.db')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            seed_initial_state(session, scenario)
            for event in get_canonical_events(scenario):
                v2_patched.process_stripe_webhook(session, *sign_event(event))
            assert scenario.validate(session, extract_comparable_state(session, scenario)) == []
            user = session.get(User, scenario.user_id)
            bystander = session.get(User, scenario.initial_users()[1]['id'])
            total = session.scalar(select(func.sum(User.credit_balance)))
            if defect == 'wrong_credit':
                user.credit_balance -= 100
                bystander.credit_balance += 100
            elif defect == 'bystander':
                bystander.email = 'corrupted@example.com'
            elif defect == 'subscription':
                session.get(Subscription, scenario.subscription_id).user_id = bystander.id
            elif defect == 'ledger':
                session.query(CreditLedger).one().user_id = bystander.id
            else:
                session.query(OutboxEmail).one().user_id = bystander.id
            session.commit()
            assert session.scalar(select(func.sum(User.credit_balance))) == total
            violations = scenario.validate(session, extract_comparable_state(session, scenario))
            assert violations
            if defect == 'ledger':
                assert {v['field'] for v in violations} == {'credit_ledger.identity'}
    finally:
        engine.dispose()


def test_seeded_oracle_never_executes_reference(monkeypatch):
    from chaoshook_engine.fixtures import scenario_from_seed

    def forbidden(*args, **kwargs):
        pytest.fail('Independent oracle executed the known repair')

    monkeypatch.setattr(v2_patched, 'process_stripe_webhook', forbidden)
    scenario = scenario_from_seed(505, bystander=False)
    expected = scenario.expected_state()
    assert expected['user']['credit_balance'] == scenario.initial_credit_balance + 100
    report = run_algebraic_prover(_consistent_handler_without_invoice_credit, scenario)
    assert report['passed_cells'] == 0
    assert report['golden_state'] == expected


def test_separate_evaluation_provenance_and_retirement():
    from chaoshook_engine.patcher import EvaluationHistory, evaluate_candidate_patch

    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    history = EvaluationHistory(synthesis_seeds=[101], retired_seeds=[202])
    result = evaluate_candidate_patch(source, [101, 202, 303], history=history)
    assert result['all_passed']
    assert [item['label'] for item in result['evaluations']] == ['evaluation', 'evaluation', 'held-out']
    assert result['schedule_ids'] == list(range(1, 28))
    assert all(item['prover_report']['total_cells'] == 27 for item in result['evaluations'])
    history.record_feedback([303])
    assert 303 in history.retired_seeds
    rerun = evaluate_candidate_patch(source, [303, 404], history=history)
    assert [item['label'] for item in rerun['evaluations']] == ['evaluation', 'held-out']
    assert evaluate_candidate_patch(source, [505])['evaluations'][0]['label'] == 'evaluation'
    with pytest.raises(ValueError):
        evaluate_candidate_patch(source, [1, 1])


def test_failed_evaluation_is_consumed(partial_code):
    from chaoshook_engine.patcher import EvaluationHistory, evaluate_candidate_patch

    history = EvaluationHistory()
    result = evaluate_candidate_patch(partial_code, [606], history=history)
    assert not result['all_passed']
    assert result['evaluations'][0]['label'] == 'held-out'
    history.record_feedback([606])
    result = evaluate_candidate_patch(partial_code, [606, 707], history=history)
    assert [item['label'] for item in result['evaluations']] == ['evaluation', 'held-out']


# Phase 4: local candidate defects. These fixtures never install source or change
# production verification. Seeded cases are regression inputs, not held-out data.
_MUTATION_SEEDS = (101, 202, 303)
_MUTATION_NAMES = (
    'missing_invoice_credit', 'wrong_user_attribution', 'wrong_lifecycle',
    'missing_welcome_outbox', 'bystander_change',
)


def _mutant_handler(defect):
    """Compose one explicit defect with a valid local candidate's SQL behavior.

    Corrections run only after a newly processed delivery commits. Expected
    injected crashes propagate normally; retries perform the defective effect
    once. SQL writes precede reads to preserve SQLite race-test support.
    """
    from sqlalchemy import delete, select, update
    from target_app.models import CreditLedger, OutboxEmail, Subscription, User
    from target_app.schemas import StripeEvent

    def candidate(session, raw_body, sig_header):
        event = StripeEvent.model_validate_json(raw_body)
        result = v2_patched.process_stripe_webhook(session, raw_body, sig_header)
        if result['status'] != 'processed':
            return result
        obj = event.data.object
        account = User.stripe_customer_id == obj['customer']
        owner = select(User.id).where(account).scalar_subquery()
        bystander = select(User.id).where(
            User.stripe_customer_id != obj['customer']
        ).scalar_subquery()
        with session.begin():
            if event.type == 'invoice.paid':
                if defect == 'missing_invoice_credit':
                    session.execute(update(User).where(account).values(
                        credit_balance=User.credit_balance - 100))
                    session.execute(delete(CreditLedger).where(CreditLedger.invoice_id == obj['id']))
                elif defect == 'wrong_user_attribution':
                    session.execute(update(User).where(account).values(
                        credit_balance=User.credit_balance - 100))
                    session.execute(update(User).where(User.id == bystander).values(
                        credit_balance=User.credit_balance + 100))
                    session.execute(update(CreditLedger).where(
                        CreditLedger.invoice_id == obj['id']).values(user_id=bystander))
                elif defect == 'bystander_change':
                    session.execute(update(User).where(User.id == bystander).values(
                        email='unintended-change@example.com'))
            elif event.type == 'customer.subscription.deleted' and defect == 'wrong_lifecycle':
                session.execute(update(Subscription).where(Subscription.id == obj['id']).values(
                    status='active'))
            elif event.type == 'checkout.session.completed' and defect == 'missing_welcome_outbox':
                session.execute(delete(OutboxEmail).where(OutboxEmail.user_id == owner))
        return result

    return candidate


def _mutation_properties(defect, scenario):
    bystander_id = scenario.initial_users()[1]['id']
    return {
        'missing_invoice_credit': {'user.credit_balance', 'credit_ledger_rows', 'credit_ledger_sum'},
        'wrong_user_attribution': {'user.credit_balance', 'credit_ledger.identity',
                                   f'users.{bystander_id}.credit_balance'},
        'wrong_lifecycle': {'subscription.status'},
        'missing_welcome_outbox': {'user.welcome_email_count', 'outbox_emails_count'},
        'bystander_change': {f'users.{bystander_id}.email'},
        'valid_control': set(),
    }[defect]


def _classify_mutation_cell(cell, intended_properties):
    """Execution failures are never credited as business-contract detections."""
    fields = {v['field'] for v in cell['property_violations']}
    if 'execution.errors' in fields:
        return 'incidental_error'
    if cell['passed']:
        return 'survived'
    if intended_properties and intended_properties <= fields:
        return 'intended_property'
    return 'other_business_property'


@pytest.fixture(scope='module')
def mutation_results(tmp_path_factory):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from chaoshook_engine.fixtures import scenario_from_seed, get_canonical_events, sign_event
    from chaoshook_engine.snapshot import create_cell_engine, seed_initial_state, extract_comparable_state
    from target_app.models import Base, CreditLedger, OutboxEmail, User

    folder = tmp_path_factory.mktemp('mutation-audit')
    results = []
    for name in (*_MUTATION_NAMES, 'valid_control'):
        candidate = (v2_patched.process_stripe_webhook if name == 'valid_control'
                     else _mutant_handler(name))
        for seed in _MUTATION_SEEDS:
            scenario = scenario_from_seed(seed, bystander=True)
            report = run_algebraic_prover(candidate, scenario)
            # Read aggregate and entity evidence independently of the report.
            # This is a separate canonical replay; all 27 matrix cells above
            # still use their own isolated real SQLite databases.
            engine = create_cell_engine(folder / f'{name}-{seed}.db')
            errors = []
            audit = None
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as session:
                    seed_initial_state(session, scenario)
                    for payload in get_canonical_events(scenario):
                        try:
                            candidate(session, *sign_event(payload))
                        except Exception as exc:
                            session.rollback()
                            errors.append(f'{type(exc).__name__}: {exc}')
                    actual = extract_comparable_state(session, scenario)
                    audit = {
                        'credit_balance_total': session.scalar(select(func.sum(User.credit_balance))),
                        'user_balances': dict(session.execute(select(User.id, User.credit_balance)).all()),
                        'ledger_rows': actual['credit_ledger_rows'],
                        'ledger_sum': actual['credit_ledger_sum'],
                        'outbox_rows': session.scalar(select(func.count()).select_from(OutboxEmail)),
                        'ledger_users': list(session.scalars(select(CreditLedger.user_id))),
                        'violations': scenario.validate(session, actual, errors),
                        'errors': errors,
                    }
            finally:
                engine.dispose()
            intended = _mutation_properties(name, scenario)
            results.append({
                'candidate': name, 'seed': seed, 'scenario': scenario,
                'intended_properties': intended, 'report': report, 'audit': audit,
                'classifications': [('valid_control_pass' if name == 'valid_control' and cell['passed']
                                     else _classify_mutation_cell(cell, intended)) for cell in report['cells']],
            })
    return results


@pytest.mark.parametrize('name', _MUTATION_NAMES)
@pytest.mark.parametrize('seed', _MUTATION_SEEDS)
def test_seeded_candidate_mutations(mutation_results, name, seed):
    result = next(r for r in mutation_results if (r['candidate'], r['seed']) == (name, seed))
    # Any incidental exception, unsupported scenario, other-property rejection,
    # or survivor is a failed mutation-test expectation with full evidence.
    assert result['classifications'] == ['intended_property'] * 27, result
    assert result['audit']['errors'] == [], result
    fields = {v['field'] for v in result['audit']['violations']}
    assert result['intended_properties'] <= fields, result
    scenario = result['scenario']
    assert scenario.user_id != SCENARIO_SPEC.user_id and scenario.bystander
    assert scenario.initial_credit_balance > 0
    if name == 'wrong_user_attribution':
        initial_users = scenario.initial_users()
        audit = result['audit']
        assert audit['credit_balance_total'] == sum(u['credit_balance'] for u in initial_users) + 100
        assert (audit['ledger_rows'], audit['ledger_sum'], audit['outbox_rows']) == (1, 100, 1)
        assert audit['user_balances'][scenario.user_id] == scenario.initial_credit_balance
        assert audit['user_balances'][initial_users[1]['id']] == initial_users[1]['credit_balance'] + 100
        assert audit['ledger_users'] == [initial_users[1]['id']]
        for cell in result['report']['cells']:
            assert all(cell['actual_state'][key] == cell_expected for key, cell_expected in (
                ('credit_ledger_rows', 1), ('credit_ledger_sum', 100), ('outbox_emails_count', 1)))


@pytest.mark.parametrize('seed', _MUTATION_SEEDS)
def test_mutation_valid_control(mutation_results, seed):
    result = next(r for r in mutation_results if (r['candidate'], r['seed']) == ('valid_control', seed))
    assert result['report']['passed_cells'] == 27, result
    assert result['audit']['errors'] == result['audit']['violations'] == []


def test_mutation_classification_requires_intended_property():
    def cell(fields, passed=False):
        return {'passed': passed, 'property_violations': [{'field': field} for field in fields]}

    intended = {'credit_ledger.identity'}
    assert _classify_mutation_cell(cell(intended), intended) == 'intended_property'
    assert _classify_mutation_cell(cell(intended | {'execution.errors'}), intended) == 'incidental_error'
    assert _classify_mutation_cell(cell({'subscription.status'}), intended) == 'other_business_property'
    assert _classify_mutation_cell(cell([], passed=True), intended) == 'survived'


def test_mutation_summary(mutation_results, tmp_path):
    """Use pytest -s -k mutation_summary to emit the reproducible JSON artifact."""
    from collections import Counter

    evaluations = []
    for result in mutation_results:
        counts = Counter(result['classifications'])
        evaluations.append({
            'candidate': result['candidate'], 'seed': result['seed'],
            'import_error': None,  # Direct callable fixtures; no dynamic source imports.
            'intended_properties': sorted(result['intended_properties']),
            'observed_business_properties': sorted({v['field'] for cell in result['report']['cells']
                                                    for v in cell['property_violations']
                                                    if v['field'] != 'execution.errors'}),
            'cell_outcomes': {key: counts[key] for key in (
                'intended_property', 'incidental_error', 'other_business_property', 'survived',
                'valid_control_pass')},
            'execution_errors': sorted({error for cell in result['report']['cells']
                                       for v in cell['property_violations'] if v['field'] == 'execution.errors'
                                       for error in v['actual']}),
            'canonical_replay': result['audit'],
        })
    mutants = [item for item in evaluations if item['candidate'] != 'valid_control']
    valid = [item for item in mutants if not item['execution_errors'] and not item['canonical_replay']['errors']]
    detected = {name for name in _MUTATION_NAMES if all(
        item['cell_outcomes']['intended_property'] == 27 for item in mutants if item['candidate'] == name)}
    summary = {
        'schema_version': 1, 'seeds': list(_MUTATION_SEEDS), 'input_role': 'regression',
        'coverage_per_candidate_seed': {'schedule_ids': list(range(1, 28)),
                                        'canonical_audit_replays': 1},
        'mutation_count': len(_MUTATION_NAMES),
        'valid_mutation_count': sum(all(item in valid for item in mutants if item['candidate'] == name)
                                    for name in _MUTATION_NAMES),
        'valid_mutation_evaluations': len(valid),
        'intended_property_detected_count': len(detected),
        'incidental_error_evaluations': sum(bool(item['execution_errors'] or item['canonical_replay']['errors'])
                                           for item in mutants),
        'intended_property_detected_mutations': sorted(detected),
        'surviving_mutations': sorted({item['candidate'] for item in mutants
                                      if item['cell_outcomes']['survived']}),
        'evaluations': evaluations,
    }
    path = tmp_path / 'mutation-summary.json'
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'\nMutation summary: {path}')
    assert json.loads(path.read_text(encoding='utf-8')) == summary
    assert len(valid) == 15 and len(detected) == 5, summary
    assert summary['surviving_mutations'] == [], summary


def test_cegis_audit_first_round_success_preserves_guided_request(monkeypatch, reports):
    from dataclasses import asdict
    from hashlib import sha256
    from chaoshook_engine.muse_client import WONDER_PROMPT

    contract = asdict(SCENARIO_SPEC)
    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    client, calls = _fake_wonder(monkeypatch, [source])
    result = _extract_patch(client, reports['vulnerable'])
    audit = client.model_routing
    assert result['patched_code'] == source
    assert len(calls) == len(audit['cegis_rounds']) == len(audit['candidate_attempts']) == 1
    assert calls[0]['messages'][0]['content'] == WONDER_PROMPT
    assert set(json.loads(calls[0]['messages'][1]['content'])) == {
        'schema_code', 'handler_code', 'prover_report', 'handler_contract'}
    attempt = audit['candidate_attempts'][0]
    assert attempt['attempt_index'] == attempt['cegis_round'] == 1
    assert attempt['candidate_sha256'] == sha256(source.encode()).hexdigest()
    assert attempt['model_identifier'] == client.wonder_model
    assert attempt['provenance'] == 'live_model_response'
    assert attempt['result'] == 'accepted' and attempt['autonomous_repair']
    assert attempt['configured_check_ids'] == attempt['executed_check_ids'] == attempt['passed_check_ids'] == list(range(1, 28))
    assert attempt['failed_check_ids'] == attempt['business_property_violations'] == []
    assert attempt['feedback_evidence_indices'] == [0]
    assert attempt['feedback_properties']
    assert attempt['regression_check_ids'] == attempt['regression_passed_check_ids']
    assert audit['selected_candidate'] == attempt
    assert audit['verification_scope']['additional_scenario_evaluation'] == 'separate_not_run'
    assert audit['verification_scope']['held_out_checks_executed'] == []
    assert audit['verification_scope']['mutation_controls'] == 'regression_only_not_acceptance'
    assert asdict(SCENARIO_SPEC) == contract


def test_cegis_audit_retains_feedback_and_real_candidate_hashes(monkeypatch, reports, partial_code):
    from hashlib import sha256

    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    client, calls = _fake_wonder(monkeypatch, [partial_code, source])
    _extract_patch(client, reports['vulnerable'])
    first, second = client.model_routing['candidate_attempts']
    assert first['result'] == 'rejected' and second['result'] == 'accepted'
    assert [first['candidate_sha256'], second['candidate_sha256']] == [
        sha256(code.encode()).hexdigest() for code in (partial_code, source)]
    evidence = client.model_routing['regression_evidence']
    assert len(evidence) == 2
    assert all(item['role'] == 'synthesis_feedback' and item['scenario'] == 'legacy' for item in evidence)
    assert second['feedback_evidence_indices'] == [1]
    assert second['feedback_properties'] == sorted(p['field'] for p in evidence[1]['business_properties'])
    assert evidence[1]['candidate_sha256'] == first['candidate_sha256']
    assert evidence[1]['business_properties'] == first['business_property_violations']
    assert first['business_property_violations']
    assert first['failed_check_ids'] == evidence[1]['failed_check_ids']
    assert set(first['failed_check_ids']) <= set(second['regression_passed_check_ids'])
    assert all(item['contract_sha256'] == first['contract_sha256'] for item in (first, second))
    # Existing bounded feedback is the next request's actual observed failures.
    feedback = json.loads(calls[1]['messages'][1]['content'])['prover_report']
    assert feedback['summary']['failed_cell_ids'] == first['failed_check_ids']


@pytest.mark.parametrize('forced_cache', [True, False])
def test_cegis_audit_fallback_never_claims_live_generation(monkeypatch, reports, partial_code, forced_cache):
    client, calls = _fake_wonder(monkeypatch, [partial_code, partial_code])
    client.force_cache = forced_cache
    _extract_patch(client, reports['vulnerable'])
    attempts = client.model_routing['candidate_attempts']
    assert len(calls) == len(client.model_routing['cegis_rounds']) == (0 if forced_cache else 2)
    assert len(attempts) == (1 if forced_cache else 3)
    cached = attempts[-1]
    assert cached['attempt_index'] == len(attempts)
    assert cached['cegis_round'] is cached['model_identifier'] is None
    assert cached['provenance'] == 'cached_fallback' and not cached['autonomous_repair']
    assert cached['result'] == 'accepted'
    assert cached['executed_check_ids'] == cached['passed_check_ids'] == list(range(1, 28))
    assert not client.model_routing['wonder_live']
    assert client.model_routing['selected_candidate'] == cached


def test_cegis_audit_import_failure_is_not_business_rejection(monkeypatch, reports):
    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    broken = "def process_stripe_webhook(*args): pass\nraise ImportError('cegis-secret Authorization: Bearer unrelated-private-token')\n"
    client, _ = _fake_wonder(monkeypatch, [broken, source])
    _extract_patch(client, reports['vulnerable'])
    first = client.model_routing['candidate_attempts'][0]
    assert first['result'] == 'rejected'
    assert first['executed_check_ids'] == first['failed_check_ids'] == first['business_property_violations'] == []
    assert first['import_error'] and not first['verification_error']
    recorded = json.dumps(client.model_routing)
    assert 'cegis-secret' not in recorded and 'unrelated-private-token' not in recorded
    assert 'private.invalid' not in recorded


def test_cegis_audit_verifier_failure_and_failed_cache_are_not_success(monkeypatch, reports):
    from chaoshook_engine import muse_client

    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    client, calls = _fake_wonder(monkeypatch, [source])

    def unavailable(*args, **kwargs):
        raise RuntimeError('verifier unavailable cegis-secret')

    monkeypatch.setattr(muse_client, 'verify_candidate_patch', unavailable)
    # Preserve offline availability but never label unchecked output accepted.
    result = _extract_patch(client, reports['vulnerable'])
    assert result['patched_code']
    assert len(calls) == 1 and client.model_routing['cegis_rounds'] == []
    assert len(client.model_routing['candidate_attempts']) == 2
    assert client.model_routing['selected_candidate'] is None
    for attempt in client.model_routing['candidate_attempts']:
        assert attempt['result'] == 'rejected' and not attempt['autonomous_repair']
        assert attempt['verification_error'] and attempt['executed_check_ids'] == []
    assert 'cegis-secret' not in json.dumps(client.model_routing)


def test_cegis_audit_structurally_invalid_response_records_actual_source(monkeypatch, reports):
    from hashlib import sha256

    source = 'def process_stripe_webhook(*args): pass\n'
    client, calls = _fake_wonder(monkeypatch, [])

    def request(*args, **kwargs):
        calls.append(kwargs['json'])
        return {'model': 'muse-wonder-1.0', 'choices': [{'message': {'content': json.dumps({
            'patched_code': source, 'invariants': [],
        })}}]}

    monkeypatch.setattr(client, 'api_request', request)
    _extract_patch(client, reports['vulnerable'])
    first, cached = client.model_routing['candidate_attempts']
    assert first['candidate_sha256'] == sha256(source.encode()).hexdigest()
    assert first['provenance'] == 'live_model_response'
    assert first['response_model_identifier'] == 'muse-wonder-1.0'
    assert first['result'] == 'rejected' and first['executed_check_ids'] == []
    assert 'structured output' in first['verification_error']
    assert cached['provenance'] == 'cached_fallback'
    assert len(calls) == 1 and client.model_routing['cegis_rounds'] == []


def test_cegis_audit_bounds_evidence_and_resets_per_run(monkeypatch, reports):
    from copy import deepcopy

    client, _ = _fake_wonder(monkeypatch, [])
    report = {'cells': [{'id': i, 'passed': False, 'property_violations': [
        {'field': 'property_' + str(j), 'expected': 'cegis-secret' * 1000, 'actual': 'x' * 10000}
        for j in range(40)]} for i in range(1, 28)]}
    summary = client._evidence_summary(report)
    assert len(json.dumps(summary)) < 15000
    assert summary['omitted_property_count'] == 24
    assert summary['failed_check_ids'] == list(range(1, 28))
    assert 'cegis-secret' not in json.dumps(summary)
    client.force_cache = True
    _extract_patch(client, reports['vulnerable'])
    previous = deepcopy(client.model_routing['candidate_attempts'])
    _extract_patch(client, reports['patched'])
    assert len(client.model_routing['candidate_attempts']) == 1
    assert client.model_routing['candidate_attempts'][0]['feedback_evidence_indices'] == []
    assert previous[0]['regression_check_ids']
    assert client.model_routing['regression_evidence'][0]['failed_check_ids'] == []


def test_prepared_verification_source_has_explicit_hash():
    from hashlib import sha256
    from chaoshook_engine.patcher import verify_candidate_patch

    source = 'def process_stripe_webhook(*args): pass\nraise ImportError("prepared")\n'
    result = verify_candidate_patch(source)
    assert result['candidate_sha256'] == sha256(source.encode()).hexdigest()
    assert result['provenance'] == 'prepared'
    assert result['configured_check_ids'] == list(range(1, 28))
    assert result['executed_check_ids'] == [] and not result['verified']


def test_cegis_import_feedback_is_bounded_before_next_request(monkeypatch, reports):
    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    broken = "def process_stripe_webhook(*args): pass\nraise ImportError('" + 'x' * 20000 + "')\n"
    client, calls = _fake_wonder(monkeypatch, [broken, source])
    _extract_patch(client, reports['vulnerable'])
    feedback = json.loads(calls[1]['messages'][1]['content'])['prover_report']
    assert len(feedback['import_error']) <= 1000
    assert len(client.model_routing['regression_evidence'][1]['import_error']) <= 300
    assert client.model_routing['candidate_attempts'][-1]['result'] == 'accepted'


@pytest.mark.parametrize('mode', ['vulnerable', 'patched'])
def test_judge_property_evidence_uses_real_validation_results(reports, monkeypatch, mode):
    from dataclasses import asdict
    from hashlib import sha256
    from chaoshook_engine import server

    monkeypatch.setitem(server.app.state._state, 'active_provenance', None)
    report = reports[mode]
    evidence = server._verification_evidence({'active_code': 'prepared source'}, report)
    assert evidence['candidate_sha256'] == sha256(b'prepared source').hexdigest()
    assert evidence['contract_sha256'] == sha256(json.dumps(asdict(SCENARIO_SPEC), sort_keys=True).encode()).hexdigest()
    assert evidence['provenance'] == 'prepared'
    assert evidence['execution_error_cells'] == (1 if mode == 'vulnerable' else 0)
    assert 'mutation' not in evidence and 'held_out' not in evidence
    assert len(evidence['business_properties']) == 4
    for prop in evidence['business_properties']:
        assert prop['passed'] + prop['failed'] + prop['not_evaluated'] == 27
        if mode == 'patched':
            assert prop['not_evaluated'] == 0
            assert prop['passed'] == 27 and prop['failed'] == 0
    if mode == 'vulnerable':
        properties = {p['id']: p for p in evidence['business_properties']}
        assert properties['credits']['failed'] == 2
        assert properties['welcome']['failed'] == 1
        assert properties['lifecycle']['failed'] == 18


def test_judge_evidence_never_counts_execution_errors_as_property_passes(reports, monkeypatch):
    from copy import deepcopy
    from chaoshook_engine import server

    monkeypatch.setitem(server.app.state._state, 'active_provenance', None)
    report = deepcopy(reports['patched'])
    report['cells'][0]['property_violations'] = [{'field': 'execution.errors', 'actual': ['failed after commit']}]
    report['cells'][0]['passed'] = False
    report['cells'][1].pop('property_violations')
    evidence = server._verification_evidence({'active_code': ''}, report)
    assert evidence['execution_error_cells'] == 1
    assert all((p['passed'], p['failed'], p['not_evaluated']) == (25, 0, 2)
               for p in evidence['business_properties'])


@pytest.mark.parametrize('source_kind', ['live_model_response', 'cached_fallback'])
def test_judge_api_ties_installed_provenance_to_actual_candidate_hash(source_editor, monkeypatch, source_kind):
    from hashlib import sha256
    from chaoshook_engine import server

    client, active = source_editor
    source = Path(v2_patched.__file__).read_text(encoding='utf-8')
    client.post('/api/reset')

    def request(self, *args, **kwargs):
        if kwargs['json']['model'] != self.wonder_model:
            raise ValueError('mock Spark cache')
        return {'choices': [{'message': {'content': json.dumps({
            'invariants': self._invariants({}), 'patched_code': source,
        })}}]}

    monkeypatch.setenv('MUSE_API_KEY', 'unit-secret')
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'false')
    monkeypatch.setattr(server.MuseNeuroSymbolicClient, 'api_request', request)
    analysis = client.post('/api/diagnose', json={'use_cache': source_kind == 'cached_fallback'}).json()
    audit = analysis['model_routing']
    assert len(audit['candidate_attempts']) == 1
    assert audit['selected_candidate']['provenance'] == source_kind
    before = client.get('/api/status').json()['verification_evidence']
    assert before['provenance'] == 'prepared'  # Proposed repair is not active yet.
    response = client.post('/api/patch', json={'patched_code': analysis['patched_code']})
    assert response.status_code == 200
    installed = response.json()['verification_evidence']
    assert installed['candidate_sha256'] == sha256(active.read_bytes()).hexdigest()
    assert installed['candidate_sha256'] == audit['selected_candidate']['candidate_sha256']
    assert installed['provenance'] == source_kind
    assert client.get('/api/status').json()['verification_evidence'] == installed
    # A subsequent manual edit is prepared source, even if text matches a prior repair.
    edited = client.post('/api/update-source', json={'source_code': source + '\n'}).json()
    assert edited['verification_evidence']['provenance'] == 'prepared'
    assert client.post('/api/reset').json()['verification_evidence']['provenance'] == 'prepared'


def test_judge_evidence_ignores_stale_source_provenance(monkeypatch, reports):
    from chaoshook_engine import server

    monkeypatch.setitem(server.app.state._state, 'active_provenance', {
        'candidate_sha256': 'a different source', 'provenance': 'live_model_response'})
    evidence = server._verification_evidence({'active_code': 'external edit'}, reports['patched'])
    assert evidence['provenance'] == 'prepared'


def test_judge_evidence_reports_entity_corruption_from_real_seeded_run(mutation_results, monkeypatch):
    from chaoshook_engine import server

    monkeypatch.setitem(server.app.state._state, 'active_provenance', None)
    result = next(item for item in mutation_results
                  if item['candidate'] == 'wrong_user_attribution' and item['seed'] == 101)
    evidence = server._verification_evidence({'active_code': 'test fixture'}, result['report'])
    properties = {item['id']: item for item in evidence['business_properties']}
    assert properties['credits']['failed'] == properties['entities']['failed'] == 27
    assert evidence['execution_error_cells'] == 0


def _manifest_inputs(report, source=None):
    from hashlib import sha256

    source = source if source is not None else Path(v2_patched.__file__).read_text(encoding='utf-8')
    return source, {'candidate_sha256': sha256(source.encode()).hexdigest(), 'prover_report': report}


def test_manifest_hashes_and_executed_coverage(reports):
    from dataclasses import asdict
    from hashlib import sha256
    from chaoshook_engine.fixtures import get_canonical_events
    from chaoshook_engine.runner import export_verification_manifest
    from chaoshook_engine import scenario_spec

    source, verification = _manifest_inputs(reports['patched'])
    manifest = export_verification_manifest(source, verification)
    digest = lambda value: sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    assert manifest['schema_version'] == 1
    assert manifest['contract']['sha256'] == digest(asdict(SCENARIO_SPEC))
    assert manifest['contract']['oracle_source_sha256'] == sha256(Path(scenario_spec.__file__).read_bytes()).hexdigest()
    assert manifest['candidate'] == {
        'source_sha256': sha256(source.encode()).hexdigest(), 'provenance': 'prepared',
        'provenance_basis': 'prepared_source',
    }
    acceptance = manifest['runtime_acceptance']
    assert acceptance['accepted'] and acceptance['scope'] == 'legacy_27_only'
    run = acceptance['run']
    assert run['scenario_id'] == 'legacy' and run['seed'] is None
    assert run['fixture_sha256'] == digest({
        'initial_users': SCENARIO_SPEC.initial_users(), 'events': get_canonical_events(SCENARIO_SPEC)})
    assert run['scenario_sha256'] == manifest['contract']['sha256']
    assert run['fixture_identifiers']['event_ids'] == list(SCENARIO_SPEC.event_ids)
    assert run['executed_schedule_ids'] == run['passed_check_ids'] == list(range(1, 28))
    assert run['failed_check_ids'] == run['not_evaluated_check_ids'] == []
    assert manifest['executed_schedule_count'] == run['executed_schedule_count'] == 27
    assert manifest['additional_scenarios'] == {
        'evaluated': False, 'requested_seeds': [], 'runs': [], 'not_evaluated_seeds': [],
        'status': 'not_evaluated', 'all_passed': None,
    }
    assert manifest['mutation_controls'] == {
        'status': 'not_evaluated', 'scope': 'regression_only_not_acceptance'}


@pytest.mark.parametrize('provenance', ['live_model_response', 'cached_fallback'])
def test_manifest_uses_actual_cegis_provenance(monkeypatch, reports, provenance):
    from chaoshook_engine.runner import export_verification_manifest

    source, verification = _manifest_inputs(reports['patched'])
    client, calls = _fake_wonder(monkeypatch, [source])
    client.force_cache = provenance == 'cached_fallback'
    _extract_patch(client, reports['vulnerable'])
    assert len(calls) == (1 if provenance == 'live_model_response' else 0)
    manifest = export_verification_manifest(source, verification, candidate_audit=client.model_routing)
    assert manifest['candidate']['provenance'] == provenance
    assert manifest['candidate']['provenance_basis'] == 'supplied_hash_bound_audit'


@pytest.mark.parametrize('changed', ['source', 'contract', 'provenance'])
def test_manifest_rejects_stale_or_unknown_provenance(reports, changed):
    from chaoshook_engine.runner import export_verification_manifest

    source, verification = _manifest_inputs(reports['patched'])
    audit = {'candidate_sha256': verification['candidate_sha256'],
             'contract_sha256': export_verification_manifest(source, verification)['contract']['sha256'],
             'provenance': 'cached_fallback'}
    audit[{'source': 'candidate_sha256', 'contract': 'contract_sha256', 'provenance': 'provenance'}[changed]] = 'stale'
    with pytest.raises(ValueError, match='audit must match'):
        export_verification_manifest(source, verification, candidate_audit=audit)


def test_manifest_rejects_results_from_another_candidate(reports):
    from chaoshook_engine.runner import export_verification_manifest

    source, verification = _manifest_inputs(reports['patched'])
    with pytest.raises(ValueError, match='Verification source hash'):
        export_verification_manifest(source + '\n', verification)
    with pytest.raises(ValueError, match='Evaluation source hash'):
        export_verification_manifest(source, verification, evaluations=[{'candidate_sha256': 'stale'}])


def test_manifest_real_failure_reporting_and_mutation_separation(reports):
    from chaoshook_engine.runner import export_verification_manifest

    source, verification = _manifest_inputs(reports['vulnerable'], Path(v1_vulnerable.__file__).read_text())
    # Regression controls are deliberately outside runtime acceptance.
    verification['mutation_controls'] = {'all_passed': True}
    manifest = export_verification_manifest(source, verification)
    run = manifest['runtime_acceptance']['run']
    assert not manifest['runtime_acceptance']['accepted']
    assert len(run['passed_check_ids']) == 6 and len(run['failed_check_ids']) == 21
    assert run['failed_check_ids'] == [c['id'] for c in reports['vulnerable']['cells'] if not c['passed']]
    assert run['business_property_violations']
    assert {v['property'] for v in run['business_property_violations']} >= {'user.credit_balance', 'outbox_emails_count'}
    assert run['execution_errors'] == [{'check_id': 25, 'code': 'handler_execution_error', 'count': 1}]
    assert run['synchronization_errors'] == []
    assert manifest['mutation_controls']['status'] == 'not_evaluated'


def test_manifest_redacts_untrusted_evidence_and_preserves_error_categories(reports, monkeypatch):
    from copy import deepcopy
    from chaoshook_engine.runner import export_verification_manifest

    secret = 'never-export-this-environment-value'
    monkeypatch.setenv('MUSE_API_KEY', secret)
    monkeypatch.setenv('PRIVATE_PROMPT', secret)
    report = deepcopy(reports['patched'])
    cell = report['cells'][0]
    cell.update(passed=False, sql_trace=[secret], failure_reason=secret,
                actual_state={'key': secret}, sequence=[secret], category=secret)
    cell['property_violations'] = [
        {'field': secret, 'actual': secret, 'expected': secret},
        {'field': 'user.credit_balance', 'actual': secret, 'expected': secret},
        {'field': 'execution.errors', 'actual': [
            'Authorization: Bearer ' + secret,
            'Both workers did not reach the idempotency SQL barrier',
            'RuntimeError: SQL barrier requires two RaceWorker threads',
            'Expected mid-handler crash was not injected',
        ]},
    ]
    source, verification = _manifest_inputs(report, '# ' + secret)
    verification['import_error'] = secret
    verification['verification_error'] = secret
    audit = {'candidate_sha256': verification['candidate_sha256'], 'provenance': 'cached_fallback',
             'contract_sha256': export_verification_manifest(source, verification)['contract']['sha256'],
             'prompt': secret, 'authorization': secret, 'model_identifier': secret}
    manifest = export_verification_manifest(source, verification, candidate_audit=audit)
    encoded = json.dumps(manifest)
    assert secret not in encoded and 'Authorization' not in encoded and 'Bearer' not in encoded
    run = manifest['runtime_acceptance']['run']
    assert run['execution_errors'] == [{'check_id': 1, 'code': 'handler_execution_error', 'count': 1}]
    assert {e['code'] for e in run['synchronization_errors']} == {
        'barrier_not_reached', 'barrier_broken', 'crash_not_injected'}
    assert run['import_error'] == 'candidate_import_error' and run['verification_error'] == 'verification_error'
    assert {v['property'] for v in run['business_property_violations']} == {
        'unrecognized_business_property', 'user.credit_balance'}


def test_manifest_never_invents_unexecuted_results(reports):
    from copy import deepcopy
    from chaoshook_engine.runner import export_verification_manifest

    report = deepcopy(reports['patched'])
    report['cells'] = report['cells'][:2]  # Summary still claims 27; cells are authoritative.
    source, verification = _manifest_inputs(report)
    manifest = export_verification_manifest(source, verification, requested_seeds=[101])
    assert not manifest['runtime_acceptance']['accepted']
    run = manifest['runtime_acceptance']['run']
    assert run['executed_schedule_ids'] == run['passed_check_ids'] == [1, 2]
    assert run['not_evaluated_check_ids'] == list(range(3, 28))
    assert manifest['executed_schedule_count'] == 2
    assert manifest['additional_scenarios']['not_evaluated_seeds'] == [101]
    assert manifest['additional_scenarios']['all_passed'] is None


@pytest.mark.parametrize('handler, expected_exit, accepted', [
    ('v2_patched.py', 0, True), ('v1_vulnerable.py', 1, False),
])
def test_cli_manifest_reproducibility_and_real_seeded_results(tmp_path, handler, expected_exit, accepted):
    from hashlib import sha256

    source = Path(v2_patched.__file__).with_name(handler)
    manifest_path = tmp_path / 'evidence.json'
    command = [sys.executable, '-m', 'chaoshook_engine.cli', 'export-pytest',
               '--out', str(tmp_path / 'test_webhook_invariance.py'), '--manifest', str(manifest_path),
               '--source', str(source), '--seed', '101', '--seed', '202']
    contents = []
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == expected_exit, result.stdout + result.stderr
        contents.append(manifest_path.read_bytes())
    assert contents[0] == contents[1]
    manifest = json.loads(contents[0])
    assert manifest['candidate']['source_sha256'] == sha256(source.read_bytes()).hexdigest()
    assert manifest['runtime_acceptance']['accepted'] is accepted
    assert manifest['executed_schedule_count'] == 81
    additional = manifest['additional_scenarios']
    assert additional['status'] == 'completed' and additional['evaluated']
    assert additional['all_passed'] is accepted and additional['not_evaluated_seeds'] == []
    assert [r['seed'] for r in additional['runs']] == [101, 202]
    for run in additional['runs']:
        assert run['label'] == 'evaluation'  # Export never invents held-out history.
        assert run['executed_schedule_ids'] == list(range(1, 28))
        assert run['executed_schedule_count'] == 27
        assert bool(run['failed_check_ids']) is not accepted


def test_cli_manifest_import_failure_reports_no_executed_checks(tmp_path, capsys):
    from hashlib import sha256
    from chaoshook_engine.cli import main

    source = tmp_path / 'candidate.py'
    # Preserve CRLF bytes and prove hashing does not normalize them.
    source.write_bytes(b'def process_stripe_webhook(*args): pass\r\nraise ImportError("private-token")\r\n')
    manifest_path = tmp_path / 'evidence.json'
    assert main(['export-pytest', '--out', str(tmp_path / 'test_export.py'), '--manifest', str(manifest_path),
                 '--source', str(source), '--seed', '404']) == 1
    manifest = json.loads(manifest_path.read_text())
    assert manifest['candidate']['source_sha256'] == sha256(source.read_bytes()).hexdigest()
    assert manifest['executed_schedule_count'] == 0
    assert not manifest['runtime_acceptance']['accepted']
    runs = [manifest['runtime_acceptance']['run'], *manifest['additional_scenarios']['runs']]
    for run in runs:
        assert run['executed_schedule_ids'] == run['passed_check_ids'] == run['failed_check_ids'] == []
        assert run['not_evaluated_check_ids'] == list(range(1, 28))
        assert run['import_error'] == 'candidate_import_error'
    assert manifest['additional_scenarios']['status'] == 'not_evaluated'
    assert manifest['additional_scenarios']['not_evaluated_seeds'] == [404]
    assert manifest['additional_scenarios']['all_passed'] is None
    assert 'private-token' not in manifest_path.read_text() + capsys.readouterr().out


def test_cli_manifest_verifier_failure_is_not_a_pass(tmp_path, monkeypatch):
    from chaoshook_engine import patcher
    from chaoshook_engine.cli import main

    def broken(*args, **kwargs):
        raise RuntimeError('private-verifier-message')

    monkeypatch.setattr(patcher, 'verify_candidate_patch', broken)
    monkeypatch.setattr(patcher, 'evaluate_candidate_patch', broken)
    path = tmp_path / 'evidence.json'
    assert main(['export-pytest', '--out', str(tmp_path / 'test_export.py'), '--manifest', str(path),
                 '--source', v2_patched.__file__, '--seed', '101']) == 1
    manifest = json.loads(path.read_text())
    assert manifest['executed_schedule_count'] == 0
    assert manifest['runtime_acceptance']['run']['verification_error'] == 'verification_error'
    assert manifest['additional_scenarios']['runs'][0]['verification_error'] == 'verification_error'
    assert manifest['additional_scenarios']['all_passed'] is None
    assert 'private-verifier-message' not in path.read_text()


def test_cli_manifest_keeps_failed_evaluation_separate_from_acceptance(tmp_path, monkeypatch):
    from chaoshook_engine import cli

    def no_muse(*args, **kwargs):
        pytest.fail('manifest export must never instantiate a Muse client')

    monkeypatch.setattr(cli, 'MuseNeuroSymbolicClient', no_muse)
    source = tmp_path / 'candidate.py'
    source.write_text(Path(v2_patched.__file__).read_text() + '''
_original_handler = process_stripe_webhook
def process_stripe_webhook(session, raw_body, signature):
    import json
    if json.loads(raw_body)['data']['object']['customer'] != 'cus_nova_99':
        raise ValueError('additional fixture rejected')
    return _original_handler(session, raw_body, signature)
''')
    path = tmp_path / 'evidence.json'
    assert cli.main(['export-pytest', '--out', str(tmp_path / 'test_export.py'), '--manifest', str(path),
                     '--source', str(source), '--seed', '101']) == 1
    manifest = json.loads(path.read_text())
    assert manifest['runtime_acceptance']['accepted']  # Legacy criterion stays 27/27.
    assert manifest['additional_scenarios']['all_passed'] is False
    assert manifest['executed_schedule_count'] == 54
    run = manifest['additional_scenarios']['runs'][0]
    assert run['failed_check_ids'] == list(range(1, 28))
    assert run['execution_errors'] and run['synchronization_errors']


def test_cli_manifest_audit_is_hash_bound_before_execution(tmp_path, monkeypatch, reports):
    from chaoshook_engine import cli, patcher
    from chaoshook_engine.runner import export_verification_manifest

    source, verification = _manifest_inputs(reports['patched'])
    audit_path, manifest_path = tmp_path / 'audit.json', tmp_path / 'manifest.json'
    audit = {'candidate_sha256': verification['candidate_sha256'], 'provenance': 'cached_fallback',
             'contract_sha256': export_verification_manifest(source, verification)['contract']['sha256']}
    audit_path.write_text(json.dumps(audit))
    command = ['export-pytest', '--out', str(tmp_path / 'test_export.py'), '--manifest', str(manifest_path),
               '--source', v2_patched.__file__, '--candidate-audit', str(audit_path)]
    assert cli.main(command) == 0
    manifest = json.loads(manifest_path.read_text())
    assert manifest['candidate']['provenance'] == 'cached_fallback'
    assert manifest['additional_scenarios']['status'] == 'not_evaluated'
    before = manifest_path.read_bytes()
    audit['candidate_sha256'] = 'stale'
    audit_path.write_text(json.dumps(audit))

    def unexpected_execution(*args, **kwargs):
        pytest.fail('reject stale provenance before execution')

    monkeypatch.setattr(patcher, 'verify_candidate_patch', unexpected_execution)
    with pytest.raises(SystemExit) as caught:
        cli.main(command)
    assert caught.value.code == 2 and manifest_path.read_bytes() == before


def test_qualification_valid_candidate_uses_frozen_source_and_contract(monkeypatch):
    from dataclasses import asdict
    from hashlib import sha256
    from chaoshook_engine import patcher, cli

    source = Path(v2_patched.__file__).read_text()
    active = patcher.HANDLER_DIR / 'active_handler.py'
    before = active.read_bytes()
    calls = []
    original = patcher.evaluate_candidate_patch

    def evaluate(code, seeds):
        calls.append((sha256(code.encode()).hexdigest(), tuple(seeds)))
        return original(code, seeds)

    def unexpected(*args, **kwargs):
        pytest.fail('qualification must not install, reset, or use Muse')

    monkeypatch.setattr(patcher, 'evaluate_candidate_patch', evaluate)
    monkeypatch.setattr(patcher, 'apply_patch_to_active', unexpected)
    monkeypatch.setattr(patcher, 'reset_active_to_vulnerable', unexpected)
    monkeypatch.setattr(cli, 'MuseNeuroSymbolicClient', unexpected)
    manifest = patcher.qualify_candidate_patch(source, [202, 101], contract=SCENARIO_SPEC)
    assert manifest['schema_version'] == 1
    assert manifest['contract']['sha256'] == sha256(json.dumps(asdict(SCENARIO_SPEC), sort_keys=True).encode()).hexdigest()
    assert calls == [(manifest['candidate']['source_sha256'], (seed,)) for seed in (101, 202)]
    assert manifest['qualification'] == {
        'label': 'additional seeded qualification', 'base_verification_status': 'passed',
        'status': 'passed', 'qualified': True, 'completed_seeds': [101, 202],
        'not_evaluated_reason': None, 'errors': [],
    }
    assert manifest['runtime_acceptance']['accepted']
    assert manifest['executed_schedule_count'] == 81
    for run in manifest['additional_scenarios']['runs']:
        assert run['label'] == 'evaluation' and run['seed'] in (101, 202)
        assert run['executed_schedule_ids'] == run['passed_check_ids'] == list(range(1, 28))
        assert not run['business_property_violations']
    assert manifest['mutation_controls']['scope'] == 'regression_only_not_acceptance'
    assert active.read_bytes() == before


@pytest.mark.parametrize('source', ['v1_vulnerable.py', 'v1_partial_idempotent.py'])
def test_qualification_skips_additional_scenarios_when_base_fails(monkeypatch, source):
    from chaoshook_engine import patcher

    def unexpected(*args, **kwargs):
        pytest.fail('additional qualification requires base verification to pass')

    monkeypatch.setattr(patcher, 'evaluate_candidate_patch', unexpected)
    manifest = patcher.qualify_candidate_patch(Path(v2_patched.__file__).with_name(source).read_text(), [101])
    assert not manifest['runtime_acceptance']['accepted']
    assert manifest['qualification']['base_verification_status'] == 'failed'
    assert manifest['qualification']['status'] == 'not_evaluated'
    assert not manifest['qualification']['qualified']
    assert manifest['qualification']['not_evaluated_reason'] == 'base_verification_failed'
    assert manifest['additional_scenarios']['not_evaluated_seeds'] == [101]
    assert manifest['additional_scenarios']['all_passed'] is None


@pytest.mark.parametrize('failure', ['partial_report', 'exception', 'hash_mismatch', 'import_error'])
def test_qualification_incomplete_or_errored_never_passes(monkeypatch, reports, failure):
    from dataclasses import asdict
    from copy import deepcopy
    from chaoshook_engine import patcher
    from chaoshook_engine.fixtures import scenario_from_seed

    source, verification = _manifest_inputs(reports['patched'])
    monkeypatch.setattr(patcher, 'verify_candidate_patch', lambda code: verification)
    original = patcher.evaluate_candidate_patch

    def evaluate(code, seeds):
        if seeds == [101]:
            return original(code, seeds)
        if failure == 'exception':
            raise RuntimeError('private-qualification-error')
        if failure == 'hash_mismatch':
            # Complete successful evidence for different source must be discarded.
            result = original(code, seeds)
            result['candidate_sha256'] = 'different-candidate'
            return result
        if failure == 'import_error':
            return {'candidate_sha256': verification['candidate_sha256'], 'evaluations': [],
                    'import_error': 'private-qualification-import-error'}
        report = deepcopy(reports['patched'])
        report['cells'] = report['cells'][:2]  # Totals falsely still claim 27.
        return {'candidate_sha256': verification['candidate_sha256'], 'all_passed': True,
                'evaluations': [{'seed': 202, 'label': 'evaluation',
                                 'scenario': asdict(scenario_from_seed(202)), 'prover_report': report}]}

    monkeypatch.setattr(patcher, 'evaluate_candidate_patch', evaluate)
    manifest = patcher.qualify_candidate_patch(source, [101, 202])
    assert manifest['runtime_acceptance']['accepted']
    assert manifest['qualification']['base_verification_status'] == 'passed'
    assert manifest['qualification']['status'] == 'incomplete_or_errored'
    assert not manifest['qualification']['qualified']
    assert manifest['qualification']['completed_seeds'] == [101]
    assert manifest['additional_scenarios']['runs'][0]['passed_check_ids'] == list(range(1, 28))
    assert manifest['additional_scenarios']['all_passed'] is None
    if failure == 'hash_mismatch':
        assert manifest['qualification']['errors'] == [
            {'stage': 'additional_evaluation', 'seed': 202, 'code': 'candidate_hash_mismatch'}]
        assert manifest['additional_scenarios']['runs'][1]['executed_schedule_ids'] == []
    assert 'private-qualification' not in json.dumps(manifest)


def test_qualification_rejects_base_source_hash_mismatch(monkeypatch, reports):
    from chaoshook_engine import patcher

    source, verification = _manifest_inputs(reports['patched'])
    verification['candidate_sha256'] = 'another-source'
    monkeypatch.setattr(patcher, 'verify_candidate_patch', lambda code: verification)
    manifest = patcher.qualify_candidate_patch(source, [101])
    assert not manifest['runtime_acceptance']['accepted']
    assert manifest['executed_schedule_count'] == 0
    assert manifest['qualification']['base_verification_status'] == 'incomplete_or_errored'
    assert manifest['qualification']['status'] == 'not_evaluated'
    assert manifest['qualification']['errors'][0]['code'] == 'candidate_hash_mismatch'


@pytest.mark.parametrize('seeds', [[], [101, 101], [True], ['101']])
def test_qualification_requires_explicit_valid_seeds(seeds):
    from chaoshook_engine.patcher import qualify_candidate_patch

    with pytest.raises(ValueError, match='explicit distinct integer seeds'):
        qualify_candidate_patch('unused source', seeds)


def test_qualification_cannot_redefine_the_frozen_contract():
    from dataclasses import replace
    from chaoshook_engine.patcher import qualify_candidate_patch

    with pytest.raises(ValueError, match='existing frozen contract'):
        qualify_candidate_patch('unused source', [101], contract=replace(SCENARIO_SPEC, initial_credit_balance=500))


@pytest.mark.parametrize('candidate, expected_exit, status', [
    ('valid', 0, 'passed'), ('overfit', 1, 'failed'), ('vulnerable', 1, 'not_evaluated'),
])
def test_qualification_cli_manifest_status_and_reproducibility(tmp_path, candidate, expected_exit, status):
    source = tmp_path / 'candidate.py'
    source.write_text(Path(v1_vulnerable.__file__ if candidate == 'vulnerable' else v2_patched.__file__).read_text())
    if candidate == 'overfit':
        # Pass the base lifecycle but violate seeded credit conservation.
        source.write_text(source.read_text() + '''
_original_handler = process_stripe_webhook
def process_stripe_webhook(session, raw_body, signature):
    import json
    from sqlalchemy import text
    result = _original_handler(session, raw_body, signature)
    event = json.loads(raw_body)
    if event['data']['object']['customer'] != 'cus_nova_99':
        session.execute(text('UPDATE users SET credit_balance = 0'))
        session.commit()
    return result
''')
    path = tmp_path / 'qualification.json'
    command = [sys.executable, '-m', 'chaoshook_engine.cli', 'qualify', '--source', str(source),
               '--seed', '101', '--seed', '202', '--manifest', str(path)]
    payloads = []
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == expected_exit, result.stdout + result.stderr
        assert result.stdout.encode() == path.read_bytes()
        payloads.append(path.read_bytes())
    assert payloads[0] == payloads[1]
    manifest = json.loads(payloads[0])
    assert manifest['qualification']['status'] == status
    assert manifest['qualification']['qualified'] is (status == 'passed')
    if candidate == 'overfit':
        assert manifest['runtime_acceptance']['accepted']
        assert manifest['qualification']['completed_seeds'] == [101, 202]
        assert manifest['additional_scenarios']['all_passed'] is False
        assert all(run['business_property_violations'] for run in manifest['additional_scenarios']['runs'])


def test_qualification_cli_records_evaluation_error(tmp_path, monkeypatch, reports, capsys):
    from chaoshook_engine import cli, patcher

    source, verification = _manifest_inputs(reports['patched'])
    monkeypatch.setattr(patcher, 'verify_candidate_patch', lambda code: verification)

    def broken(*args, **kwargs):
        raise RuntimeError('private-qualification-exception')

    monkeypatch.setattr(patcher, 'evaluate_candidate_patch', broken)
    path = tmp_path / 'qualification.json'
    assert cli.main(['qualify', '--source', v2_patched.__file__, '--seed', '101', '--manifest', str(path)]) == 1
    payload = capsys.readouterr().out
    assert payload == path.read_text() and 'private-qualification-exception' not in payload
    manifest = json.loads(payload)
    assert manifest['qualification']['base_verification_status'] == 'passed'
    assert manifest['qualification']['status'] == 'incomplete_or_errored'
    assert manifest['qualification']['errors'] == [
        {'stage': 'additional_evaluation', 'seed': 101, 'code': 'evaluation_error'}]
    assert manifest['additional_scenarios']['not_evaluated_seeds'] == [101]
    assert manifest['additional_scenarios']['all_passed'] is None


def test_offline_demo_smoke_and_reproducibility(tmp_path, monkeypatch):
    from chaoshook_engine import cli, patcher

    def forbidden(*args, **kwargs):
        pytest.fail('offline demo must not invoke Muse or install a handler')

    monkeypatch.setattr(cli, 'MuseNeuroSymbolicClient', forbidden)
    monkeypatch.setattr(patcher, 'apply_patch_to_active', forbidden)
    monkeypatch.setattr(patcher, 'reset_active_to_vulnerable', forbidden)
    active = patcher.HANDLER_DIR / 'active_handler.py'
    before = active.read_bytes()
    outputs = []
    for name in ('first', 'second'):
        folder = tmp_path / name
        assert cli.main(['demo', '--out', str(folder), '--seed', '202', '--seed', '101']) == 0
        outputs.append({path.name: path.read_bytes() for path in folder.iterdir()})
    assert outputs[0] == outputs[1]
    assert set(outputs[0]) == {'vulnerable.json', 'repaired.json', 'demo.json', 'summary.md'}
    baseline = json.loads(outputs[0]['vulnerable.json'])
    repair = json.loads(outputs[0]['repaired.json'])
    index = json.loads(outputs[0]['demo.json'])
    assert len(baseline['runtime_acceptance']['run']['failed_check_ids']) == 21
    assert baseline['qualification']['status'] == 'not_evaluated'
    assert baseline['executed_schedule_count'] == 27
    assert repair['qualification']['qualified'] and repair['executed_schedule_count'] == 81
    assert repair['candidate']['provenance'] == 'prepared'
    assert index['expected_results_reproduced'] and index['seeds'] == [101, 202]
    for record, manifest in zip(index['evaluations'], (baseline, repair)):
        assert record['candidate_sha256'] == manifest['candidate']['source_sha256']
        assert record['contract'] == manifest['contract']
        assert record['replay_command'][3] == 'qualify'
    assert b'prepared reference repair' in outputs[0]['summary.md']
    assert active.read_bytes() == before


@pytest.mark.parametrize('failure', ['unexpected_outcome', 'exception'])
def test_offline_demo_failure_is_visible(tmp_path, monkeypatch, failure):
    from chaoshook_engine import cli, patcher

    original = patcher.qualify_candidate_patch

    def broken(source, seeds):
        if failure == 'exception':
            raise RuntimeError('private-demo-error')
        result = original(source, seeds)
        result['runtime_acceptance']['run']['passed_check_ids'] = []
        return result

    monkeypatch.setattr(patcher, 'qualify_candidate_patch', broken)
    assert cli.main(['demo', '--out', str(tmp_path), '--seed', '101']) == 1
    index = json.loads((tmp_path / 'demo.json').read_text())
    assert not index['expected_results_reproduced']
    assert 'False' in (tmp_path / 'summary.md').read_text()
    assert 'private-demo-error' not in ''.join(path.read_text() for path in tmp_path.iterdir())
    if failure == 'exception':
        assert all(record['executed_schedule_count'] == 0 for record in index['evaluations'])


def test_offline_demo_cli_defaults(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'chaoshook_engine.cli', 'demo', '--out', str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    index = json.loads((tmp_path / 'demo.json').read_text())
    assert index['seeds'] == [101, 202, 303]
    assert index['evaluations'][1]['executed_schedule_count'] == 108


def test_preflight_is_safe_and_runs_without_site_packages():
    result = subprocess.run([sys.executable, '-S', '-m', 'chaoshook_engine.cli', 'preflight'],
                            capture_output=True, text=True, timeout=20,
                            env={**os.environ, 'MUSE_API_KEY': 'preflight-private-value'})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report['ok'] and not report['checks']['fastapi']
    assert report['configuration_present']['MUSE_API_KEY']
    assert report['live_connectivity_tested'] is False
    assert 'preflight-private-value' not in result.stdout + result.stderr


def test_preflight_reports_missing_tools_and_temp_failure(monkeypatch, capsys):
    from chaoshook_engine import cli

    monkeypatch.setattr(shutil, 'which', lambda name: None)
    def unwritable(*args, **kwargs):
        raise PermissionError('private-filesystem-detail')
    monkeypatch.setattr(cli.tempfile, 'TemporaryDirectory', unwritable)
    assert cli.main(['preflight']) == 1
    payload = capsys.readouterr().out
    report = json.loads(payload)
    assert not report['checks']['node'] and not report['checks']['runner_tmp_writable']
    assert 'private-filesystem-detail' not in payload


# Test-only evaluator-style probe. Keep the real claim/credit/outbox behavior,
# then override lifecycle outcomes regardless of which events were delivered.
HARDCODED_CANCELLATION_PROBE = '''
from sqlalchemy import select
from target_app.handlers.v2_patched import process_stripe_webhook as event_driven
from target_app.models import User, Subscription
from target_app.schemas import StripeEvent

def process_stripe_webhook(session, raw_body, sig_header):
    result = event_driven(session, raw_body, sig_header)
    event = StripeEvent.model_validate_json(raw_body)
    with session.begin():
        user = session.scalar(select(User).where(
            User.stripe_customer_id == event.data.object["customer"]))
        user.plan_tier = "canceled"
        for sub in session.scalars(select(Subscription).where(Subscription.user_id == user.id)):
            sub.status = "canceled"
    return result
'''


@pytest.fixture(scope='module')
def semantic_controls():
    from chaoshook_engine.patcher import qualify_semantic_candidate
    return {name: qualify_semantic_candidate(code) for name, code in (
        ('prepared', Path(v2_patched.__file__).read_bytes().decode()),
        ('probe', HARDCODED_CANCELLATION_PROBE),
    )}


def test_semantic_positive_control_exact_coverage_and_preserved_source(semantic_controls):
    from hashlib import sha256
    manifest = semantic_controls['prepared']
    assert sha256(Path(v2_patched.__file__).read_bytes()).hexdigest() == 'a7a480c713d09c0bf8d73d6239138700dcd7cb66b8802cd3715ba8455a9d1694'
    assert manifest['runtime_acceptance']['accepted']
    assert manifest['executed_schedule_count'] == 27  # Legacy scope only.
    assert manifest['strict_qualification']['qualified']
    strict = manifest['strict_semantic']
    assert strict['contract_identifier'] == 'novacloud-causal-counterfactual-v1'
    assert strict['executed_schedule_count'] == 62
    assert strict['executed_check_count'] == strict['passed_check_count'] == 526
    assert [(r['scenario_id'], r['executed_schedule_count'], r['executed_check_count']) for r in strict['runs']] == [
        ('A-canceled', 24, 216), ('B-active', 6, 42), ('C-active-no-invoice', 2, 10),
        ('C-canceled-no-invoice', 6, 42), ('D-two-invoices', 24, 216),
    ]
    for run in strict['runs']:
        assert run['passed']
        for schedule in run['schedules']:
            count = len(schedule['event_ids'])
            assert [c['id'] for c in schedule['checks']] == ['prefix:0'] + [
                item for i in range(1, count + 1) for item in (f'prefix:{i}', f'prefix:{i}:duplicate')]
            assert all(c['passed'] for c in schedule['checks'])


def test_evaluator_probe_accepted_by_old_gate_rejected_for_cancellation(semantic_controls):
    from chaoshook_engine.patcher import evaluate_candidate_patch
    manifest = semantic_controls['probe']
    assert manifest['runtime_acceptance']['accepted']
    assert len(manifest['runtime_acceptance']['run']['passed_check_ids']) == 27
    evaluation = evaluate_candidate_patch(HARDCODED_CANCELLATION_PROBE)
    assert sum(r['prover_report']['passed_cells'] for r in evaluation['evaluations']) == 81
    assert not manifest['strict_qualification']['qualified']
    strict = manifest['strict_semantic']
    assert strict['executed_check_count'] == 526
    checks = [c for r in strict['runs'] for s in r['schedules'] for c in s['checks']]
    assert all(not c['execution_errors'] for c in checks)
    active = next(r for r in strict['runs'] if r['scenario_id'] == 'B-active')
    for schedule in active['schedules']:
        final = schedule['checks'][-1]
        assert not final['passed']
        assert set(final['business_property_violations']) == {'users.plan_tier', 'subscriptions.status'}


def test_semantic_contract_independent_prefix_outcomes():
    from chaoshook_engine.fixtures import semantic_challenges
    from chaoshook_engine.scenario_spec import semantic_expected_state
    scenario, challenges = semantic_challenges()
    events = dict(challenges)['D-two-invoices']
    empty = semantic_expected_state(scenario, [])
    assert empty['subscriptions'] == empty['ledger'] == empty['outbox'] == []
    invoices_only = semantic_expected_state(scenario, events[2:])
    assert invoices_only['subscriptions'] == invoices_only['outbox'] == []
    assert {r['invoice_id'] for r in invoices_only['ledger']} == {e['data']['object']['id'] for e in events[2:]}
    user = next(u for u in invoices_only['users'] if u['id'] == scenario.user_id)
    assert user['credit_balance'] == scenario.initial_credit_balance + 200
    assert user['plan_tier'] == 'free'
    canceled = semantic_expected_state(scenario, dict(challenges)['C-canceled-no-invoice'])
    assert canceled['ledger'] == []
    assert canceled['subscriptions'][0]['status'] == 'canceled'


def test_semantic_cli_deterministic_and_import_failure(tmp_path, monkeypatch, capsys):
    from chaoshook_engine import cli
    def no_muse(*args, **kwargs):
        pytest.fail('semantic qualification must not use Muse')
    monkeypatch.setattr(cli, 'MuseNeuroSymbolicClient', no_muse)
    path = tmp_path / 'semantic.json'
    args = ['qualify-semantic', '--source', v2_patched.__file__, '--manifest', str(path)]
    assert cli.main(args) == 0
    first = path.read_bytes()
    assert cli.main(args) == 0
    assert path.read_bytes() == first
    broken = tmp_path / 'broken.py'
    broken.write_text('raise ImportError("private-semantic-error")')
    assert cli.main(['qualify-semantic', '--source', str(broken), '--manifest', str(path)]) == 1
    evidence = json.loads(path.read_text())
    assert evidence['strict_semantic']['executed_check_count'] == 0
    assert not evidence['strict_qualification']['qualified']
    assert 'private-semantic-error' not in path.read_text() + capsys.readouterr().out


def test_opt_in_strict_install_rejects_probe_before_writing(monkeypatch):
    from chaoshook_engine import patcher
    installed = []
    monkeypatch.setattr(patcher, '_install', lambda code: installed.append(code))
    with pytest.raises(ValueError, match='strict semantic'):
        patcher.apply_patch_to_active(HARDCODED_CANCELLATION_PROBE, require_strict_semantic=True)
    assert installed == []
    source = Path(v2_patched.__file__).read_text()
    patcher.apply_patch_to_active(source, require_strict_semantic=True)
    assert installed == [source]


def _experiment_client(monkeypatch, responses):
    from chaoshook_engine.muse_client import MuseNeuroSymbolicClient
    monkeypatch.setenv('MUSE_API_KEY', 'experiment-secret')
    monkeypatch.setenv('MUSE_BASE_URL', 'https://experiment.invalid/v1')
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'false')
    pending = iter(responses)
    requests = []
    def request(self, method, path, **kwargs):
        requests.append(kwargs['json'])
        result = next(pending)
        if isinstance(result, Exception):
            raise result
        return {'model': 'actual-response-model', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'patched_code': result})}}]}
    monkeypatch.setattr(MuseNeuroSymbolicClient, 'api_request', request)
    return requests


def test_contract_driven_prompt_separation_and_actual_dual_verification(monkeypatch, tmp_path):
    from chaoshook_engine.muse_client import run_synthesis_experiment, CONTRACT_DRIVEN_PROMPT, WONDER_PROMPT
    partial = Path(v2_patched.__file__).with_name('v1_partial_idempotent.py').read_text()
    prepared = Path(v2_patched.__file__).read_text()
    requests = _experiment_client(monkeypatch, [partial, prepared])
    # No prepared source can be read as a fallback or hidden example.
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == 'v2_patched.py':
            pytest.fail('experiment must not read the reference handler')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    result = run_synthesis_experiment(tmp_path / 'trial', trials=1, run_kind='mocked')
    trial = result['trials'][0]
    assert trial['fully_qualified'] and len(trial['attempts']) == 2
    for request in requests:
        assert request['temperature'] == 0
        assert request['response_format'] == {'type': 'json_object'}
        assert request['messages'][0]['content'] == CONTRACT_DRIVEN_PROMPT
        assert WONDER_PROMPT not in json.dumps(request)
        payload = json.loads(request['messages'][1]['content'])
        assert 'handler_contract' not in payload
        assert 'sql_trace' not in json.dumps(payload)
        # Schema facts are allowed. No strategy prescriptions in either template.
        text = json.dumps({k: v for k, v in payload.items() if k not in {'schema_code', 'handler_code'}}).lower()
        for forbidden in ('claim-first', 'on conflict', 'rowcount', 'tombstone', 'monotonic', 'watermark',
                          'single transaction', 'no intermediate commits'):
            assert forbidden not in text + CONTRACT_DRIVEN_PROMPT.lower()
        assert prepared not in json.dumps(payload)
        assert payload['business_contract']['strict_identifier'] == 'novacloud-causal-counterfactual-v1'
        assert 'last_event_ts' in payload['schema_code']  # Available schema, not usage advice.
    attempts = trial['attempts']
    assert attempts[0]['base_verification']['run']['passed_check_ids'].__len__() == 9
    assert not attempts[0]['strict_semantic']['qualified']
    assert attempts[1]['strict_semantic']['passed_check_count'] == 526
    assert attempts[1]['strict_semantic']['executed_schedule_count'] == 62
    assert all(a['actual_model_identifier'] == 'actual-response-model' for a in attempts)
    assert attempts[1]['evidence']['candidate']['provenance'] == 'live_model_response'
    assert attempts[1]['finish_reason'] == 'stop'
    second = json.loads(requests[1]['messages'][1]['content'])
    assert second['handler_code'] == partial
    assert second['execution_feedback'] == attempts[0]['feedback']
    persisted = json.loads((tmp_path / 'trial/trials.json').read_text())
    assert persisted == result
    comparison = json.loads((tmp_path / 'trial/comparison.json').read_text())
    assert all(row['live_fully_qualified_successes'] == 0 for row in comparison['modes'])


def test_experiment_retains_three_independent_trials_and_semantic_defect(monkeypatch, tmp_path):
    from hashlib import sha256
    from chaoshook_engine.muse_client import run_synthesis_experiment
    requests = _experiment_client(monkeypatch, [HARDCODED_CANCELLATION_PROBE,
                                                RuntimeError('experiment-secret'),
                                                Path(v2_patched.__file__).read_text()])
    active = Path(v2_patched.__file__).with_name('active_handler.py')
    before = active.read_bytes()
    result = run_synthesis_experiment(tmp_path / 'three', trials=3, max_attempts=1, run_kind='mocked')
    assert result['requested_trial_count'] == len(result['trials']) == len(requests) == 3
    assert [t['status'] for t in result['trials']] == ['exhausted', 'errored', 'qualified']
    first = result['trials'][0]['attempts'][0]
    assert first['candidate_sha256'] == sha256(HARDCODED_CANCELLATION_PROBE.encode()).hexdigest()
    assert first['base_verification']['accepted']
    assert not first['strict_semantic']['qualified'] and not first['fully_qualified']
    assert first['feedback']['strict']['scenarios']
    for request in requests:
        assert json.loads(request['messages'][1]['content'])['handler_code'] == Path(v1_vulnerable.__file__).read_text()
    assert active.read_bytes() == before
    assert 'experiment-secret' not in (tmp_path / 'three/trials.json').read_text()
    assert all(not t['fallback_occurred'] for t in result['trials'])
    with pytest.raises(FileExistsError):
        run_synthesis_experiment(tmp_path / 'three', trials=3)


def test_experiment_guided_prompt_comparison_and_no_fallback(monkeypatch, tmp_path):
    from chaoshook_engine.muse_client import run_synthesis_experiment, WONDER_PROMPT, summarize_synthesis_experiments
    requests = _experiment_client(monkeypatch, [Path(v2_patched.__file__).read_text()])
    guided = run_synthesis_experiment(tmp_path / 'guided', trials=1, synthesis_mode='guided', max_attempts=1, run_kind='mocked')
    assert requests[0]['messages'][0]['content'] == WONDER_PROMPT
    payload = json.loads(requests[0]['messages'][1]['content'])
    assert payload['handler_contract'].endswith(WONDER_PROMPT.split('The handler_contract is an interface description:', 1)[1].strip())
    assert guided['trials'][0]['fully_qualified']
    # Offline configuration is retained as unsuccessful slots; no reference fallback.
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'true')
    blocked = run_synthesis_experiment(tmp_path / 'blocked', trials=3, max_attempts=1, run_kind='mocked')
    assert len(requests) == 1
    assert all(t['status'] == 'configuration_blocked' and not t['fully_qualified'] for t in blocked['trials'])
    assert all(not t['fallback_occurred'] for t in blocked['trials'])
    summary, prose = summarize_synthesis_experiments([guided, blocked])
    assert all(r['live_trials'] == 0 for r in summary['modes'])
    assert 'success rate not evaluated' in prose


def test_experiment_malformed_and_import_error_retained(monkeypatch, tmp_path):
    from chaoshook_engine.muse_client import run_synthesis_experiment, MuseNeuroSymbolicClient
    _experiment_client(monkeypatch, [])
    outputs = iter(['{malformed', json.dumps({'patched_code': 'raise ImportError("experiment-secret")'})])
    def request(*args, **kwargs):
        return {'choices': [{'message': {'content': next(outputs)}}]}
    monkeypatch.setattr(MuseNeuroSymbolicClient, 'api_request', request)
    result = run_synthesis_experiment(tmp_path / 'bad', trials=2, max_attempts=1, run_kind='mocked')
    first, second = [t['attempts'][0] for t in result['trials']]
    assert first['status'] == 'errored' and first['response']
    assert first['actual_model_identifier'] is None
    assert second['status'] == 'rejected'
    assert second['base_verification']['run']['import_error'] == 'candidate_import_error'
    assert second['strict_semantic']['executed_check_count'] == 0
    assert not second['fully_qualified']
    assert 'experiment-secret' not in json.dumps(result)


def test_experiment_cli_count_required_and_recorded_comparison(monkeypatch, tmp_path, capsys):
    from chaoshook_engine import cli
    with pytest.raises(SystemExit):
        cli.main(['synthesis-experiment', '--out', str(tmp_path / 'missing')])
    _experiment_client(monkeypatch, [])
    monkeypatch.setenv('DEMO_FALLBACK_CACHE', 'true')
    out = tmp_path / 'blocked'
    assert cli.main(['synthesis-experiment', '--trials', '3', '--max-attempts', '1', '--out', str(out)]) == 1
    manifest = json.loads((out / 'trials.json').read_text())
    assert len(manifest['trials']) == 3 and manifest['status'] == 'completed'
    assert cli.main(['compare-synthesis', '--record', str(out / 'trials.json'), '--out', str(tmp_path / 'compare')]) == 0
    summary = json.loads((tmp_path / 'compare/comparison.json').read_text())
    row = next(r for r in summary['modes'] if r['synthesis_mode'] == 'contract-driven')
    assert row['live_trials'] == row['recorded_trials'] == 3
    assert row['live_legacy_successes'] == row['live_fully_qualified_successes'] == 0
    assert 'experiment-secret' not in capsys.readouterr().out


def _atomic_event_last_handler(session, raw_body, sig_header):
    """Generic test control: all effects and the final event record commit together."""
    from sqlalchemy.exc import IntegrityError
    from target_app.hmac_auth import verify_stripe_signature
    from target_app.models import CreditLedger, Subscription, User, WebhookEvent
    from target_app.schemas import StripeEvent
    from target_app.side_effects import send_welcome_email

    if not verify_stripe_signature(raw_body, sig_header):
        raise ValueError('Invalid signature')
    payload = StripeEvent.model_validate_json(raw_body)
    if session.get(WebhookEvent, payload.id):
        return {'status': 'duplicate'}
    obj = payload.data.object
    user = session.query(User).filter_by(stripe_customer_id=obj['customer']).one()
    if payload.type == 'checkout.session.completed':
        if not user.welcome_email_sent:
            send_welcome_email(session, user.id, dedupe_key=payload.id)
            user.welcome_email_sent = 1
    elif payload.type == 'invoice.paid':
        session.add(CreditLedger(user_id=user.id, invoice_id=obj['id'], delta=100, reason='invoice.paid'))
        user.credit_balance += 100
    else:
        sub = session.get(Subscription, obj['id'])
        if sub is None:
            sub = Subscription(id=obj['id'], user_id=user.id, stripe_customer_id=obj['customer'],
                               status=obj['status'], current_period_end=0, last_event_ts=0)
            session.add(sub)
        sub.current_period_end = max(sub.current_period_end, obj['current_period_end'])
        if payload.created > sub.last_event_ts:
            sub.status, sub.last_event_ts = obj['status'], payload.created
    if payload.type != 'invoice.paid' and payload.created > user.last_event_ts:
        user.plan_tier = 'canceled' if payload.type.endswith('deleted') else 'pro'
        user.last_event_ts = payload.created
    # Explicitly establish event-last order independently of ORM flush ordering.
    session.flush()
    session.add(WebhookEvent(event_id=payload.id, event_type=payload.type,
                             created_ts=payload.created, status='processed'))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if session.get(WebhookEvent, payload.id):
            return {'status': 'duplicate'}
        raise
    return {'status': 'processed'}


def test_atomic_event_last_actually_injects_and_recovers_both_crashes():
    report = run_algebraic_prover(_atomic_event_last_handler)
    assert report['passed_cells'] == 27
    for cell in report['cells'][25:]:
        trace = '\n'.join(cell['sql_trace'])
        assert trace.count('Attempt 1: injecting crash') == 1
        assert 'inside event-last transaction' in trace
        assert 'Caught SimulatedMidHandlerCrash' in trace
        assert 'Attempt 2: replay identical event' in trace
        assert cell['actual_state'] == report['golden_state']
        assert cell['property_violations'] == []


def test_event_last_broken_retry_fails_despite_real_injection():
    from chaoshook_engine.interceptors import SimulatedMidHandlerCrash
    from target_app.schemas import StripeEvent
    dropped = set()
    def broken_retry(session, raw_body, sig_header):
        event_id = StripeEvent.model_validate_json(raw_body).id
        if event_id in dropped:
            return {'status': 'duplicate'}  # Deliberately skips rolled-back effects.
        try:
            return _atomic_event_last_handler(session, raw_body, sig_header)
        except SimulatedMidHandlerCrash:
            dropped.add(event_id)
            raise
    report = run_algebraic_prover(broken_retry)
    for cell, property_name in zip(report['cells'][25:], ('credit_ledger_rows', 'outbox_emails_count')):
        assert sum('Attempt 1: injecting crash' in line for line in cell['sql_trace']) == 1
        assert not cell['passed']
        assert property_name in {v['field'] for v in cell['state_diff']}
        assert 'execution.errors' not in {v['field'] for v in cell['property_violations']}


def test_durable_intermediate_commits_still_inject_and_fail(reports):
    for cell in reports['vulnerable']['cells'][25:]:
        assert not cell['passed']
        assert sum('Attempt 1: injecting crash after committed' in line for line in cell['sql_trace']) == 1
        assert cell['state_diff']
        assert not any(v['field'] == 'execution.errors' for v in cell['property_violations'])


def test_no_crash_injection_evidence_still_rejects_correct_final_state(monkeypatch):
    from chaoshook_engine.interceptors import CrashAfterStatementController
    monkeypatch.setattr(CrashAfterStatementController, '_armed', lambda self: False)
    report = run_algebraic_prover(_atomic_event_last_handler)
    assert report['passed_cells'] == 25
    for cell in report['cells'][25:]:
        assert not cell['passed'] and cell['state_diff'] == []
        assert cell['property_violations'] == [{'field': 'execution.errors', 'expected': [],
                                               'actual': ['Expected mid-handler crash was not injected']}]
        assert not any('Attempt 1: injecting crash' in line for line in cell['sql_trace'])


@pytest.mark.parametrize('exception_name,category', [
    ('ReadTimeout', 'read_timeout'), ('ConnectTimeout', 'connect_timeout'),
    ('WriteTimeout', 'write_timeout'), ('PoolTimeout', 'pool_timeout'),
    ('TimeoutException', 'timeout'), ('ConnectError', 'transport_error'),
    ('ValueError', 'request_error'),
])
def test_experimental_timeout_diagnostics_are_measured_and_sanitized(
        monkeypatch, tmp_path, semantic_controls, exception_name, category):
    import httpx
    from copy import deepcopy
    from chaoshook_engine import muse_client, patcher
    _experiment_client(monkeypatch, [])
    monkeypatch.setenv('MUSE_TIMEOUT_S', '23.5')
    # Reuse a real baseline report: this test isolates request diagnostics.
    monkeypatch.setattr(patcher, 'qualify_semantic_candidate', lambda source: deepcopy(semantic_controls['prepared']))
    clock = iter((100.0, 102.25, 200.0, 204.5))
    monkeypatch.setattr(muse_client, 'perf_counter', lambda: next(clock))
    def failing_request(*args, **kwargs):
        error_type = ValueError if exception_name == 'ValueError' else getattr(httpx, exception_name)
        raise error_type('experiment-secret Authorization: Bearer private-response')
    monkeypatch.setattr(muse_client.MuseNeuroSymbolicClient, 'api_request', failing_request)
    result = muse_client.run_synthesis_experiment(tmp_path / 'timeouts', trials=2, max_attempts=1, run_kind='mocked')
    for i, trial in enumerate(result['trials'], 1):
        diagnostic = trial['attempts'][0]['request_diagnostics']
        assert diagnostic == {'attempt': 1, 'effective_timeout_seconds': 23.5,
                              'elapsed_seconds': 2.25 if i == 1 else 4.5, 'error_category': category}
        assert trial['status'] == 'errored'
        assert not any(token in json.dumps(diagnostic) for token in ('secret', 'Bearer', 'private-response', 'messages'))
    assert 'private-response' not in json.dumps(result)
    assert json.loads((tmp_path / 'timeouts/trials.json').read_text()) == result


def test_experimental_success_diagnostics_exclude_verification_time(monkeypatch, tmp_path, semantic_controls):
    from copy import deepcopy
    from chaoshook_engine import muse_client, patcher
    _experiment_client(monkeypatch, [Path(v2_patched.__file__).read_text()])
    monkeypatch.setenv('MUSE_TIMEOUT_S', '17')
    clock = [10.0]
    def qualify(source):
        clock[0] += 50  # Baseline and candidate verification take time too.
        return deepcopy(semantic_controls['prepared'])
    monkeypatch.setattr(patcher, 'qualify_semantic_candidate', qualify)
    monkeypatch.setattr(muse_client, 'perf_counter', lambda: clock[0])
    original = muse_client.MuseNeuroSymbolicClient.api_request
    def request(*args, **kwargs):
        clock[0] += 1.5
        return original(*args, **kwargs)
    monkeypatch.setattr(muse_client.MuseNeuroSymbolicClient, 'api_request', request)
    result = muse_client.run_synthesis_experiment(tmp_path / 'success', trials=1, run_kind='mocked')
    attempt = result['trials'][0]['attempts'][0]
    assert attempt['request_diagnostics'] == {'attempt': 1, 'effective_timeout_seconds': 17.0,
                                              'elapsed_seconds': 1.5, 'error_category': None}
    assert attempt['fully_qualified']
