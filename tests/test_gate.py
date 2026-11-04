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
