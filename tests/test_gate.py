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
    assert client.timeout == 90
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
    code = "def process_stripe_webhook(session, raw_body, sig_header):\n    return {}\n"

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
        model_routing = {"wonder_live": True}

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
