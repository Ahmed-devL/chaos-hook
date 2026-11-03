"""Local JSON API for the deterministic prover and Muse analysis."""

import ast
import importlib
from threading import RLock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from chaoshook_engine.muse_client import MuseNeuroSymbolicClient, SPARK, WONDER
from chaoshook_engine.patcher import (
    apply_patch_to_active, get_active_handler_status, reset_active_to_vulnerable,
)
from chaoshook_engine.runner import run_algebraic_prover
from target_app.handlers import active_handler


app = FastAPI(title="The Brass Escapement — ChaosHook", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Synchronous endpoints run in FastAPI's worker pool. Keep module reloads and
# file mutations out of another request's proof or analysis.
_lock = RLock()
app.state.prover_report = None
app.state.prover_source = None
app.state.analysis = None
app.state.model_routing = {
    "muse_wonder_model": WONDER, "muse_spark_model": SPARK,
    "used_fallback_cache": False,
}


class DiagnoseRequest(BaseModel):
    diagram_description: str | None = None


class PatchRequest(BaseModel):
    patched_code: str | None = None


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


def _proof_response(handler_status: dict) -> dict:
    return {
        "status": "ok", "handler_status": handler_status,
        "prover_report": _prove(handler_status, force=True),
    }


@app.get("/api/status")
def status() -> dict:
    with _lock:
        handler_status = get_active_handler_status()
        report = _prove(handler_status)
        return {
            **handler_status, "handler_status": handler_status,
            "model_routing": dict(app.state.model_routing), "prover_report": report,
        }


@app.post("/api/prove")
def prove() -> dict:
    with _lock:
        return _proof_response(get_active_handler_status())


@app.post("/api/diagnose")
def diagnose(body: DiagnoseRequest | None = None) -> dict:
    with _lock:
        report = _prove(get_active_handler_status())
        analysis = MuseNeuroSymbolicClient().run_full_neuro_symbolic_analysis(
            report, diagram_description=body.diagram_description if body else None,
        )
        app.state.analysis = analysis
        app.state.model_routing = dict(analysis["model_routing"])
        return {**analysis, "proposed_patch_code": analysis["patched_code"]}


@app.post("/api/patch")
def patch(body: PatchRequest | None = None) -> dict:
    with _lock:
        code = body.patched_code if body else None
        # A cached proposal is valid only for the source that was analyzed.
        current = get_active_handler_status()
        if (code is None and app.state.analysis is not None
                and current["active_code"] == app.state.prover_source):
            code = app.state.analysis["patched_code"]
        if code is not None:
            try:
                tree = ast.parse(code)
                if not any(isinstance(node, ast.FunctionDef)
                           and node.name == "process_stripe_webhook" for node in tree.body):
                    raise ValueError("Patch must define process_stripe_webhook")
            except (SyntaxError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid handler patch: {exc}") from exc
        try:
            handler_status = apply_patch_to_active(code)
        except Exception as exc:
            # The patcher restores the previous source when installation fails.
            raise HTTPException(status_code=400, detail="Handler patch could not be loaded") from exc
        return _proof_response(handler_status)


@app.post("/api/reset")
def reset() -> dict:
    with _lock:
        return _proof_response(reset_active_to_vulnerable())
