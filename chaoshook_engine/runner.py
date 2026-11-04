"""Execute the 27 real-SQL adversarial schedules against isolated SQLite cells."""

from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
from itertools import permutations
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread, current_thread
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.orm import Session

from chaoshook_engine.fixtures import get_canonical_events, sign_event
from chaoshook_engine.interceptors import (
    CrashAfterStatementController, DualThreadBarrierController,
    SimulatedMidHandlerCrash,
)
from chaoshook_engine.scenario_spec import SCENARIO_SPEC
from chaoshook_engine.snapshot import (
    create_cell_engine, extract_comparable_state, seed_initial_state,
)
from target_app.models import Base


def _json_hash(value) -> str:
    # Match the contract hashes already used by Muse audits and API evidence.
    return sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def candidate_export_provenance(code: str, audit: dict | None = None) -> str:
    """Use a supplied audit only when it describes these exact source/contract bytes.

    This binds a local provenance record; it does not authenticate its author.
    No audit means prepared source, even when it matches the cached reference.
    """
    if audit is None:
        return "prepared"
    record = audit.get("selected_candidate", audit)
    if (not isinstance(record, dict)
            or record.get("candidate_sha256") != sha256(code.encode("utf-8")).hexdigest()
            or record.get("contract_sha256") != _json_hash(asdict(SCENARIO_SPEC))
            or record.get("provenance") not in {"prepared", "cached_fallback", "live_model_response"}):
        raise ValueError("Candidate audit must match the source, contract, and known provenance")
    return record["provenance"]


def _manifest_run(report, scenario, *, import_error=None, verification_error=None, label=None):
    """Project actual cells only. Never copy untrusted messages, SQL, or values."""
    payloads = get_canonical_events(scenario)
    fields = {
        "user.id", "user.plan_tier", "user.credit_balance", "user.welcome_email_count",
        "subscription.id", "subscription.status", "subscription.current_period_end",
        "credit_ledger_rows", "credit_ledger_sum", "credit_ledger.identity",
        "outbox_emails_count", "outbox_emails.identity", "users.identity", "subscriptions.identity",
    }
    user_fields = {}
    for index, user in enumerate(scenario.initial_users()):
        role = "primary" if index == 0 else "bystander"
        user_fields.update({f"users.{user['id']}.{key}": f"users.{role}.{key}" for key in user})
    cells = sorted((report or {}).get("cells", []), key=lambda c: c["id"])
    ids = [cell["id"] for cell in cells]
    if len(set(ids)) != len(ids) or any(type(i) is not int or i not in range(1, 28) for i in ids):
        raise ValueError("Report contains invalid or duplicate executed schedule IDs")
    violations, execution, synchronization = [], [], []
    for cell in cells:
        error_counts = {}
        properties = set()
        for violation in cell.get("property_violations", []):
            field = violation.get("field")
            if field != "execution.errors":
                properties.add(field if field in fields else user_fields.get(field, "unrecognized_business_property"))
                continue
            for error in violation.get("actual", []):
                # Stable codes retain failure evidence without exception text,
                # SQL parameters, environment values, or authentication material.
                if error == "Both workers did not reach the idempotency SQL barrier":
                    code = "barrier_not_reached"
                elif error == "RuntimeError: SQL barrier requires two RaceWorker threads":
                    code = "barrier_broken"
                elif error == "Expected mid-handler crash was not injected":
                    code = "crash_not_injected"
                else:
                    code = "handler_execution_error"
                error_counts[code] = error_counts.get(code, 0) + 1
        violations.extend({"check_id": cell["id"], "property": field} for field in sorted(properties))
        for code, count in sorted(error_counts.items()):
            destination = execution if code == "handler_execution_error" else synchronization
            destination.append({"check_id": cell["id"], "code": code, "count": count})
    return {
        "scenario_id": "legacy" if scenario.seed is None else f"seed:{scenario.seed}",
        "seed": scenario.seed,
        "label": label if label in {"evaluation", "held-out"} else "acceptance",
        "scenario_sha256": _json_hash(asdict(scenario)),
        "fixture_identifiers": {
            "event_ids": list(scenario.event_ids), "user_ids": [u["id"] for u in scenario.initial_users()],
            "customer_id": scenario.customer_id, "checkout_id": scenario.checkout_id,
            "subscription_id": scenario.subscription_id, "invoice_id": scenario.invoice_id,
        },
        "fixture_sha256": _json_hash({"initial_users": scenario.initial_users(), "events": payloads}),
        "executed_schedule_ids": ids, "executed_schedule_count": len(ids),
        "passed_check_ids": [cell["id"] for cell in cells if cell["passed"]],
        "failed_check_ids": [cell["id"] for cell in cells if not cell["passed"]],
        "not_evaluated_check_ids": [i for i in range(1, 28) if i not in ids],
        "business_property_violations": violations,
        "execution_errors": execution, "synchronization_errors": synchronization,
        "import_error": "candidate_import_error" if import_error else None,
        "verification_error": "verification_error" if verification_error else None,
    }


def export_verification_manifest(code: str, verification: dict, *, evaluations=(),
                                 requested_seeds=(), candidate_audit=None) -> dict:
    """Build deterministic JSON evidence from existing verifier/evaluation results.

    No execution or model calls occur here. Timings/timestamps and free text are
    deliberately excluded. Mutation regression results never enter acceptance.
    """
    from chaoshook_engine.fixtures import scenario_from_seed

    source_hash = sha256(code.encode("utf-8")).hexdigest()
    provenance = candidate_export_provenance(code, candidate_audit)
    if verification.get("candidate_sha256") != source_hash:
        raise ValueError("Verification source hash does not match candidate")
    legacy = _manifest_run(verification.get("prover_report"), SCENARIO_SPEC,
                           import_error=verification.get("import_error"),
                           verification_error=verification.get("verification_error"))
    additional = []
    seeds = list(requested_seeds)
    if len(set(seeds)) != len(seeds) or any(type(seed) is not int for seed in seeds):
        raise ValueError("Requested seeds must be distinct integers")
    seen = set()
    for evaluation in evaluations:
        if evaluation.get("candidate_sha256") != source_hash:
            raise ValueError("Evaluation source hash does not match candidate")
        for item in evaluation.get("evaluations", []):
            seed = item["seed"]
            if seed not in seeds or seed in seen:
                raise ValueError("Evaluation must match a distinct requested seed")
            seen.add(seed)
            scenario = scenario_from_seed(seed)
            if _json_hash(item["scenario"]) != _json_hash(asdict(scenario)):
                raise ValueError("Evaluation fixture does not match the seeded scenario")
            additional.append(_manifest_run(item.get("prover_report"), scenario, label=item["label"],
                                            import_error=item.get("import_error"),
                                            verification_error=item.get("verification_error")))
    additional.sort(key=lambda run: run["seed"])
    evaluated_seeds = {run["seed"] for run in additional if run["executed_schedule_count"]}
    complete = (bool(seeds) and seen == set(seeds)
                and all(run["executed_schedule_count"] == 27 for run in additional))
    accepted = (legacy["passed_check_ids"] == list(range(1, 28))
                and not legacy["import_error"] and not legacy["verification_error"])
    return {
        "schema_version": 1,
        "contract": {"identifier": "novacloud-four-event-lifecycle-v1",
                     "sha256": _json_hash(asdict(SCENARIO_SPEC)),
                     "oracle_source_sha256": sha256(Path(__file__).with_name("scenario_spec.py").read_bytes()).hexdigest()},
        "candidate": {"source_sha256": source_hash, "provenance": provenance,
                      "provenance_basis": "supplied_hash_bound_audit" if candidate_audit is not None else "prepared_source"},
        "runtime_acceptance": {"accepted": accepted, "scope": "legacy_27_only", "run": legacy},
        "additional_scenarios": {
            "evaluated": any(run["executed_schedule_count"] for run in additional),
            "requested_seeds": sorted(seeds), "runs": additional,
            "not_evaluated_seeds": sorted(set(seeds) - evaluated_seeds),
            "status": ("completed" if complete else "incomplete" if evaluated_seeds else "not_evaluated"),
            "all_passed": (all(run["passed_check_ids"] == list(range(1, 28)) for run in additional)
                           if complete else None),
        },
        "executed_schedule_count": sum(run["executed_schedule_count"] for run in [legacy, *additional]),
        "mutation_controls": {"status": "not_evaluated", "scope": "regression_only_not_acceptance"},
        "redaction": "property_identifiers_and_error_codes_only; no_source_sql_prompts_or_error_messages",
    }


def export_pytest_code() -> str:
    """Return a reusable pytest suite that proves the currently active handler."""
    return '''"""Webhook invariance regression suite exported by ChaosHook."""

from pathlib import Path
import sys

import pytest

# Support plain pytest when this file is saved at the repo root or under tests/.
for root in (*Path(__file__).resolve().parents, Path.cwd(), *Path.cwd().parents):
    if (root / "target_app").is_dir() and (root / "chaoshook_engine").is_dir():
        sys.path.insert(0, str(root))
        break

from target_app.handlers import active_handler
from chaoshook_engine.runner import run_algebraic_prover


@pytest.fixture(scope="module")
def invariance_report():
    return run_algebraic_prover(active_handler.process_stripe_webhook)


@pytest.mark.parametrize("cell_id", range(1, 25))
def test_order_commutativity_all_24_permutations(invariance_report, cell_id):
    cell = invariance_report["cells"][cell_id - 1]
    assert cell["passed"], cell["failure_reason"]


def test_parallel_duplicate_sql_barrier_idempotency(invariance_report):
    cell = invariance_report["cells"][24]
    assert cell["passed"], cell["failure_reason"]


@pytest.mark.parametrize("cell_id", [26, 27])
def test_mid_handler_crash_retry_atomicity(invariance_report, cell_id):
    cell = invariance_report["cells"][cell_id - 1]
    assert cell["passed"], cell["failure_reason"]
'''


@contextmanager
def _cell(path, trace, seed=None, engine=None, scenario=SCENARIO_SPEC):
    if seed is not None:
        # Copy S0, including schema, into an independent on-disk database.
        destination = sqlite3.connect(path)
        try:
            seed.backup(destination)
        finally:
            destination.close()
    engine = engine if engine is not None else create_cell_engine(path)

    # Reuse SQLAlchemy's dialect and compilation cache across sequential cells.
    # The pool is disposed between cells; every new connection opens this cell's
    # unique file, including both independent connections used by the race.
    def route_connection(dialect, connection_record, cargs, cparams):
        cargs[0] = str(path)

    listeners = [("do_connect", route_connection)]
    event.listen(engine, "do_connect", route_connection)
    try:
        if seed is None:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                seed_initial_state(session, scenario)

        def record(conn, cursor, statement, parameters, context, executemany):
            trace.append(
                f"[{current_thread().name}] SQL: {' '.join(statement.split())} "
                f"parameters={parameters!r}"
            )

        event.listen(engine, "before_cursor_execute", record)
        listeners.append(("before_cursor_execute", record))
        for name in ("commit", "rollback"):
            listener = lambda conn, action=name: trace.append(
                f"[{current_thread().name}] {action.upper()}"
            )
            event.listen(engine, name, listener)
            listeners.append((name, listener))
        yield engine
    finally:
        engine.dispose()
        for name, listener in listeners:
            event.remove(engine, name, listener)


def _deliver(engine, handler, payload, trace):
    raw, signature = sign_event(payload)
    trace.append(f"[{current_thread().name}] Deliver {payload['id']}")
    with Session(engine) as session:
        try:
            return handler(session, raw, signature)
        except Exception:
            session.rollback()
            raise


def _safe_delivery(engine, handler, payload, trace, errors):
    try:
        _deliver(engine, handler, payload, trace)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        errors.append(message)
        trace.append(f"[{current_thread().name}] Handler failed: {message}")


def _failure_tag(category, sequence, differences, errors):
    if category == "concurrency" and any(
        item["field"] == "credit_ledger_rows" and (item["actual"] or 0) > (item["expected"] or 0)
        for item in differences
    ):
        return "DOUBLE_CREDIT_RACE"
    if errors:
        return "HANDLER_ERROR"
    if not differences:
        return None
    fields = {item["field"] for item in differences}
    if category == "concurrency":
        return "DOUBLE_CREDIT_RACE"
    if category == "crash_retry":
        return "DUPLICATE_WELCOME_EMAIL" if "outbox_emails_count" in fields else "DOUBLE_CREDIT_RETRY"
    if "subscription.status" in fields:
        return "ORPHAN_SUB_DROP" if sequence.index("e4") < sequence.index("e2") else "ZOMBIE_ACTIVE_SUB"
    return "STALE_PLAN_OVERWRITE"


def _report_cell(engine, cell_id, category, label, sequence, trace, errors, scenario):
    with Session(engine) as session:
        actual = extract_comparable_state(session, scenario)
        violations = scenario.validate(session, actual, errors)
    differences = [item for item in violations if item["field"] != "execution.errors"]
    reasons = [
        f"{item['field']}: expected {item['expected']!r}, got {item['actual']!r}"
        for item in differences
    ] + errors
    return {
        "id": cell_id, "category": category, "label": label,
        "sequence": sequence, "passed": not violations,
        "failure_reason": "; ".join(reasons) if reasons else None,
        "failure_tag": _failure_tag(category, sequence, differences, errors),
        "actual_state": actual, "state_diff": differences, "sql_trace": trace,
        "property_violations": violations,
    }


def run_algebraic_prover(handler_fn, scenario=SCENARIO_SPEC) -> dict:
    """Check the independent scenario contract across 27 isolated schedules."""
    started = perf_counter()
    payloads = get_canonical_events(scenario)
    golden = scenario.expected_state()
    cells = []
    with TemporaryDirectory(prefix="chaoshook-") as folder, closing(sqlite3.connect(":memory:")) as seed:
        root = Path(folder)
        engine = create_cell_engine(root / "seed.db")
        with _cell(root / "seed.db", [], engine=engine, scenario=scenario) as engine:
            raw = engine.raw_connection()
            try:
                raw.driver_connection.backup(seed)
            finally:
                raw.close()

        for cell_id, order in enumerate(permutations(range(4)), 1):
            trace, errors = [], []
            sequence = [f"e{index + 1}" for index in order]
            with _cell(root / f"cell-{cell_id}.db", trace, seed, engine) as engine:
                for index in order:
                    _safe_delivery(engine, handler_fn, payloads[index], trace, errors)
                cells.append(_report_cell(
                    engine, cell_id, "permutation", " → ".join(sequence),
                    sequence, trace, errors, scenario,
                ))

        trace, errors = [], []
        with _cell(root / "cell-25.db", trace, seed, engine) as engine:
            for payload in payloads[:2]:
                _safe_delivery(engine, handler_fn, payload, trace, errors)
            controller = DualThreadBarrierController(engine)
            controller.trace_log = trace
            controller.enabled = True
            try:
                workers = [Thread(
                    name=f"RaceWorker-{letter}", target=_safe_delivery,
                    args=(engine, handler_fn, payloads[2], trace, errors),
                ) for letter in "AB"]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
                if sum("Reached SQL barrier before:" in line for line in trace) != 2:
                    errors.append("Both workers did not reach the idempotency SQL barrier")
            finally:
                controller.close()
            _safe_delivery(engine, handler_fn, payloads[3], trace, errors)
            cells.append(_report_cell(
                engine, 25, "concurrency", "2-Thread SQL Barrier TOCTOU Race",
                ["e1", "e2", "e3 (RaceWorker-A)", "e3 (RaceWorker-B)", "e4"],
                trace, errors, scenario,
            ))

        for cell_id, index, table, label in (
            (26, 2, "credit_ledger", "Mid-Handler Crash on invoice.paid"),
            (27, 0, "outbox_emails", "Mid-Handler Crash on checkout.session.completed"),
        ):
            trace, errors = [], []
            sequence = [f"e{i + 1}" for i in range(index)] + [
                f"e{index + 1} (attempt 1: crash)", f"e{index + 1} (attempt 2: retry)",
            ] + [f"e{i + 1}" for i in range(index + 1, 4)]
            with _cell(root / f"cell-{cell_id}.db", trace, seed, engine) as engine:
                for payload in payloads[:index]:
                    _safe_delivery(engine, handler_fn, payload, trace, errors)
                controller = CrashAfterStatementController(engine, target_table=table)
                controller.trace_log = trace
                controller.enabled = True
                try:
                    try:
                        _deliver(engine, handler_fn, payloads[index], trace)
                    except SimulatedMidHandlerCrash as exc:
                        trace.append(f"Caught {type(exc).__name__}; rolled back uncommitted state")
                    except Exception as exc:
                        errors.append(f"{type(exc).__name__}: {exc}")
                    if not controller.crashed:
                        errors.append("Expected mid-handler crash was not injected")
                    controller.attempt = 2
                    trace.append("Attempt 2: replay identical event")
                    _safe_delivery(engine, handler_fn, payloads[index], trace, errors)
                finally:
                    controller.close()
                for payload in payloads[index + 1:]:
                    _safe_delivery(engine, handler_fn, payload, trace, errors)
                cells.append(_report_cell(
                    engine, cell_id, "crash_retry", label, sequence, trace, errors, scenario,
                ))

    passed = sum(cell["passed"] for cell in cells)
    return {
        "golden_state": golden, "total_cells": 27,
        "passed_cells": passed, "failed_cells": 27 - passed,
        "duration_ms": round((perf_counter() - started) * 1000, 3), "cells": cells,
    }


def run_semantic_challenges(handler_fn):
    """Separate strict scope: all permutations, every prefix, then duplicate replay.

    One check is one complete state observation (including S0). One schedule is
    one isolated SQLite history. No race/crash schedules are claimed here; those
    remain mandatory in the unchanged legacy gate.
    """
    from copy import deepcopy
    from chaoshook_engine.fixtures import semantic_challenges
    from chaoshook_engine.scenario_spec import SEMANTIC_CONTRACT_ID, semantic_expected_state
    from chaoshook_engine.snapshot import extract_semantic_state

    scenario, challenges = semantic_challenges()
    runs = []
    with TemporaryDirectory(prefix="chaoshook-semantic-") as folder:
        for scenario_id, events in challenges:
            schedules = []
            for number, order in enumerate(permutations(range(len(events))), 1):
                checks, delivered, errors = [], [], []
                trace = []
                with _cell(Path(folder) / f"{scenario_id}-{number}.db", trace, scenario=scenario) as engine:
                    def observe(check_id):
                        expected = semantic_expected_state(scenario, delivered)
                        with Session(engine) as session:
                            actual = extract_semantic_state(session, scenario)
                        # Evidence exposes fixed property names only, never candidate
                        # exception messages, SQL traces, source, or arbitrary DB values.
                        fields = []
                        for table in expected:
                            if expected[table] == actual[table]:
                                continue
                            if len(expected[table]) != len(actual[table]):
                                fields.append(table + ".rows")
                            columns = sorted({key for row in [*expected[table], *actual[table]] for key in row})
                            for column in columns:
                                if ([row.get(column) for row in expected[table]] !=
                                        [row.get(column) for row in actual[table]]):
                                    fields.append(table + "." + column)
                        checks.append({"id": check_id, "passed": not fields and not errors,
                                       "business_property_violations": fields,
                                       "execution_errors": ["handler_execution_error"] if errors else []})
                    observe("prefix:0")
                    for prefix, index in enumerate(order, 1):
                        payload = events[index]
                        _safe_delivery(engine, handler_fn, deepcopy(payload), trace, errors)
                        delivered.append(payload)
                        observe(f"prefix:{prefix}")
                        _safe_delivery(engine, handler_fn, deepcopy(payload), trace, errors)
                        observe(f"prefix:{prefix}:duplicate")
                schedules.append({"id": number, "event_ids": [events[i]["id"] for i in order],
                                  "checks": checks, "passed": all(c["passed"] for c in checks)})
            runs.append({"scenario_id": scenario_id, "families": [scenario_id[0], "E"],
                         "fixture_sha256": _json_hash({"users": scenario.initial_users(), "events": events}),
                         "executed_schedule_count": len(schedules),
                         "executed_check_count": sum(len(s["checks"]) for s in schedules),
                         "passed_check_count": sum(c["passed"] for s in schedules for c in s["checks"]),
                         "passed": all(s["passed"] for s in schedules), "schedules": schedules})
    return {"contract_identifier": SEMANTIC_CONTRACT_ID,
            "oracle_source_sha256": sha256(Path(__file__).with_name("scenario_spec.py").read_bytes()).hexdigest(),
            "scope": "semantic_permutations_prefixes_and_sequential_duplicates",
            "executed_schedule_count": sum(r["executed_schedule_count"] for r in runs),
            "executed_check_count": sum(r["executed_check_count"] for r in runs),
            "passed_check_count": sum(r["passed_check_count"] for r in runs),
            "qualified": bool(runs) and all(r["passed"] for r in runs), "runs": runs}
