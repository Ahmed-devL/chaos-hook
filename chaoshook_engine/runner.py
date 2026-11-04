"""Execute the 27 real-SQL adversarial schedules against isolated SQLite cells."""

from contextlib import closing, contextmanager
from itertools import permutations
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
from chaoshook_engine.snapshot import (
    create_cell_engine, diff_states, extract_comparable_state, seed_initial_state,
)
from target_app.handlers.v2_patched import process_stripe_webhook as reference_handler
from target_app.models import Base


def export_pytest_code() -> str:
    """Return a reusable pytest suite that proves the currently active handler."""
    return '''"""Webhook invariance regression suite exported by ChaosHook."""

from pathlib import Path
import sys

import pytest

# Support plain pytest when this file is saved at the repo root or under tests/.
for root in Path(__file__).resolve().parents:
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
def _cell(path, trace, seed=None, engine=None):
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
                seed_initial_state(session)

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
    if not differences:
        return "HANDLER_ERROR" if errors else None
    fields = {item["field"] for item in differences}
    if category == "concurrency":
        return "DOUBLE_CREDIT_RACE"
    if category == "crash_retry":
        return "DUPLICATE_WELCOME_EMAIL" if "outbox_emails_count" in fields else "DOUBLE_CREDIT_RETRY"
    if "subscription.status" in fields:
        return "ORPHAN_SUB_DROP" if sequence.index("e4") < sequence.index("e2") else "ZOMBIE_ACTIVE_SUB"
    return "STALE_PLAN_OVERWRITE"


def _report_cell(engine, golden, cell_id, category, label, sequence, trace, errors):
    with Session(engine) as session:
        actual = extract_comparable_state(session)
    differences = diff_states(golden, actual)
    reasons = [
        f"{item['field']}: expected {item['expected']!r}, got {item['actual']!r}"
        for item in differences
    ] + errors
    return {
        "id": cell_id, "category": category, "label": label,
        "sequence": sequence, "passed": not differences and not errors,
        "failure_reason": "; ".join(reasons) if reasons else None,
        "failure_tag": _failure_tag(category, sequence, differences, errors),
        "actual_state": actual, "state_diff": differences, "sql_trace": trace,
    }


def run_algebraic_prover(handler_fn) -> dict:
    """Capture S* independently, then exercise every cell without sharing state."""
    started = perf_counter()
    payloads = get_canonical_events()
    cells = []
    with TemporaryDirectory(prefix="chaoshook-") as folder, closing(sqlite3.connect(":memory:")) as seed:
        root = Path(folder)
        engine = create_cell_engine(root / "golden.db")
        with _cell(root / "golden.db", [], engine=engine) as engine:
            raw = engine.raw_connection()
            try:
                raw.driver_connection.backup(seed)
            finally:
                raw.close()
            for payload in payloads:
                _deliver(engine, reference_handler, payload, [])
            with Session(engine) as session:
                golden = extract_comparable_state(session)

        for cell_id, order in enumerate(permutations(range(4)), 1):
            trace, errors = [], []
            sequence = [f"e{index + 1}" for index in order]
            with _cell(root / f"cell-{cell_id}.db", trace, seed, engine) as engine:
                for index in order:
                    _safe_delivery(engine, handler_fn, payloads[index], trace, errors)
                cells.append(_report_cell(
                    engine, golden, cell_id, "permutation", " → ".join(sequence),
                    sequence, trace, errors,
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
                engine, golden, 25, "concurrency", "2-Thread SQL Barrier TOCTOU Race",
                ["e1", "e2", "e3 (RaceWorker-A)", "e3 (RaceWorker-B)", "e4"],
                trace, errors,
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
                    engine, golden, cell_id, "crash_retry", label, sequence, trace, errors,
                ))

    passed = sum(cell["passed"] for cell in cells)
    return {
        "golden_state": golden, "total_cells": 27,
        "passed_cells": passed, "failed_cells": 27 - passed,
        "duration_ms": round((perf_counter() - started) * 1000, 3), "cells": cells,
    }
