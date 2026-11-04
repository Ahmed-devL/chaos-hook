"""Local JSON API for the deterministic prover and Muse analysis."""

import base64
import binascii
import importlib
from dataclasses import asdict
from hashlib import sha256
import json
from threading import RLock
from time import perf_counter

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from chaoshook_engine.muse_client import MuseNeuroSymbolicClient, SPARK, WONDER
from chaoshook_engine.patcher import (
    HANDLER_DIR, apply_patch_to_active, get_active_handler_status, reset_active_to_vulnerable,
    verify_candidate_patch,
)
from chaoshook_engine.runner import export_pytest_code, run_algebraic_prover
from chaoshook_engine.scenario_spec import SCENARIO_SPEC
from target_app.handlers import active_handler


app = FastAPI(title="The Brass Escapement — ChaosHook", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"],
)

# Synchronous endpoints run in FastAPI's worker pool. Keep module reloads and
# file mutations out of another request's proof or analysis.
_lock = RLock()
app.state.prover_report = None
app.state.prover_source = None
app.state.analysis = None
app.state.active_provenance = None
app.state.model_routing = {
    "muse_wonder_model": WONDER, "muse_spark_model": SPARK,
    "used_fallback_cache": False,
    "wonder_live": False, "spark_live": False, "fallback_reason": None, "elapsed_ms": 0,
}


class DiagnoseRequest(BaseModel):
    diagram_image: str | None = None
    diagram_description: str | None = None
    use_cache: bool = False


class PatchRequest(BaseModel):
    patched_code: str | None = None


class UpdateSourceRequest(BaseModel):
    source_code: str


def _prove(handler_status: dict, *, force: bool = False) -> dict:
    source = handler_status["active_code"]
    if force or app.state.prover_report is None or app.state.prover_source != source:
        # Remove timestamp-based bytecode caches, including for edits made outside
        # the API, before reloading the active module.
        from pathlib import Path

        Path(importlib.util.cache_from_source(active_handler.__file__)).unlink(missing_ok=True)
        importlib.invalidate_caches()
        module = importlib.reload(active_handler)
        report = run_algebraic_prover(module.process_stripe_webhook)
        app.state.prover_report = report
        app.state.prover_source = source
        app.state.analysis = None
    return app.state.prover_report


def _verification_evidence(handler_status: dict, report: dict) -> dict:
    """Presentation evidence only: never changes proof or acceptance decisions."""
    source_hash = sha256(handler_status["active_code"].encode()).hexdigest()
    provenance = app.state.active_provenance or {}
    properties = []
    groups = (
        ("credits", "Invoice credits and attribution", ("user.credit_balance", "credit_ledger")),
        ("welcome", "Exactly one welcome message", ("user.welcome_email_count", "outbox_emails")),
        ("lifecycle", "Canceled plan and subscription", ("user.plan_tier", "subscription.status", "subscription.current_period_end")),
        ("entities", "Account isolation and ownership", ("user.id", "subscription.id", "users.", "subscriptions.identity")),
    )
    for identifier, label, prefixes in groups:
        passed = failed = unavailable = 0
        for cell in report["cells"]:
            violations = cell.get("property_violations")
            if not isinstance(violations, list):
                unavailable += 1
                continue
            fields = [item["field"] for item in violations]
            if any(field.startswith(prefixes) for field in fields):
                failed += 1
            elif "execution.errors" in fields:
                unavailable += 1
            else:
                passed += 1
        properties.append({"id": identifier, "label": label, "passed": passed,
                           "failed": failed, "not_evaluated": unavailable})
    return {
        "candidate_sha256": source_hash,
        "contract_sha256": sha256(json.dumps(asdict(SCENARIO_SPEC), sort_keys=True).encode()).hexdigest(),
        "provenance": provenance.get("provenance", "prepared")
                      if provenance.get("candidate_sha256") == source_hash else "prepared",
        "business_properties": properties,
        "scope": "Current handler; legacy 27 schedules only",
        "execution_error_cells": sum(any(v["field"] == "execution.errors"
                                         for v in cell.get("property_violations", [])) for cell in report["cells"]),
    }


def _proof_response(handler_status: dict) -> dict:
    report = _prove(handler_status, force=True)
    return {
        "status": "ok", "handler_status": handler_status, "prover_report": report,
        "verification_evidence": _verification_evidence(handler_status, report),
    }


@app.get("/api/status")
def status() -> dict:
    with _lock:
        handler_status = get_active_handler_status()
        report = _prove(handler_status)
        return {
            **handler_status, "handler_status": handler_status,
            "partial_patch_code": (HANDLER_DIR / "v1_partial_idempotent.py").read_text(encoding="utf-8"),
            "model_routing": dict(app.state.model_routing), "prover_report": report,
            "verification_evidence": _verification_evidence(handler_status, report),
        }


@app.post("/api/prove")
def prove() -> dict:
    with _lock:
        return _proof_response(get_active_handler_status())


@app.get("/api/export-pytest")
def export_pytest() -> dict:
    return {"filename": "test_webhook_invariance.py", "pytest_code": export_pytest_code()}


@app.post("/api/diagnose")
def diagnose(body: DiagnoseRequest | None = None) -> dict:
    if body and body.diagram_image is not None:
        image = body.diagram_image
        if not image.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
            raise HTTPException(400, "diagram_image must be a PNG or JPEG base64 data URI")
        if len(image) > 1_500_000:
            raise HTTPException(400, "diagram_image exceeds 1.5MB")
        try:
            if not base64.b64decode(image.split(",", 1)[1], validate=True):
                raise ValueError("empty image")
        except (ValueError, binascii.Error):
            raise HTTPException(400, "diagram_image contains invalid base64") from None
    with _lock:
        started = perf_counter()
        report = _prove(get_active_handler_status())
        client = MuseNeuroSymbolicClient()
        if body and body.use_cache:
            client.force_cache = True
        analysis = client.run_full_neuro_symbolic_analysis(
            report, diagram_description=body.diagram_description if body else None,
            diagram_image=body.diagram_image if body else None,
        )
        analysis["model_routing"]["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        app.state.analysis = analysis
        app.state.model_routing = dict(analysis["model_routing"])
        return {**analysis, "proposed_patch_code": analysis["patched_code"]}


@app.post("/api/patch")
def patch(body: PatchRequest | None = None) -> dict:
    with _lock:
        code = body.patched_code if body else None
        if code is not None:
            verification = verify_candidate_patch(code)
            if not verification["verified"]:
                if verification["import_error"]:
                    verification["import_error"] = MuseNeuroSymbolicClient().safe_error(
                        verification["import_error"])
                return {"status": "rejected", **verification}
        selected = (app.state.analysis or {}).get("model_routing", {}).get("selected_candidate") or {}
        try:
            handler_status = apply_patch_to_active(code)
        except Exception as exc:
            # The patcher restores the previous source when installation fails.
            raise HTTPException(status_code=400, detail="Handler patch could not be loaded") from exc
        source_hash = sha256(handler_status["active_code"].encode()).hexdigest()
        provenance = "cached_fallback" if code is None else "prepared"
        if code is not None and selected.get("candidate_sha256") == source_hash and selected.get("result") == "accepted":
            provenance = selected["provenance"]
        app.state.active_provenance = {"candidate_sha256": source_hash, "provenance": provenance}
        return {**_proof_response(handler_status), "verified": True}


@app.post("/api/reset")
def reset() -> dict:
    with _lock:
        app.state.active_provenance = None
        return _proof_response(reset_active_to_vulnerable())


@app.post("/api/update-source")
def update_source(body: UpdateSourceRequest) -> dict:
    code = body.source_code
    if not code.strip():
        raise HTTPException(400, "Source code must not be empty")
    if len(code.encode("utf-8")) > 100_000:
        raise HTTPException(400, "Source code exceeds 100 KB")
    try:
        compile(code, "active_handler.py", "exec")
    except SyntaxError as exc:
        raise HTTPException(400, f"Line {exc.lineno}: {exc.msg}") from None
    except ValueError:
        raise HTTPException(400, "Source code could not be compiled") from None
    with _lock:
        try:
            handler_status = apply_patch_to_active(code)
        except Exception:
            # Do not expose exception text from user-supplied module execution.
            raise HTTPException(400, "Handler source could not be loaded; previous source restored") from None
        app.state.active_provenance = None
        return _proof_response(handler_status)
