"""Apply and inspect the hot-swappable target handler."""

from contextlib import contextmanager
import difflib
import importlib
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import RLock
from uuid import uuid4

from chaoshook_engine.scenario_spec import SCENARIO_SPEC


HANDLER_DIR = Path(__file__).resolve().parents[1] / "target_app" / "handlers"
_LOCK = RLock()
_MODULE = "target_app.handlers.active_handler"


def _execute_source(code, name, path):
    """Execute exact UTF-8 source in a new namespace, without bytecode caches.

    The temporary module registration supports decorators and relative imports.
    This executes trusted local Python; it is not a sandbox.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "target_app.handlers"
    sys.modules[name] = module
    try:
        exec(compile(code, str(path), "exec"), module.__dict__)
        if not callable(getattr(module, "process_stripe_webhook", None)):
            raise TypeError("Source must define callable process_stripe_webhook")
        return module
    except BaseException:
        sys.modules.pop(name, None)
        raise


@contextmanager
def fresh_handler_module(code):
    name = f"target_app.handlers._chaoshook_source_{uuid4().hex}"
    module = _execute_source(code, name, HANDLER_DIR / "active_handler.py")
    try:
        yield module
    finally:
        # An installed module retains its registration for decorator metadata.
        if sys.modules.get(_MODULE) is not module:
            sys.modules.pop(name, None)


def verify_candidate_patch(code: str, *, strict_semantic=False) -> dict:
    """Import and prove a candidate outside the repo without installing it."""
    from chaoshook_engine.runner import run_algebraic_prover

    result = {"verified": False, "passed_cells": 0, "total_cells": 27,
              "failed_cell_ids": [], "prover_report": None, "import_error": None,
              "candidate_sha256": sha256(code.encode("utf-8")).hexdigest(),
              "provenance": "prepared", "configured_check_ids": list(range(1, 28)),
              "executed_check_ids": []}
    with TemporaryDirectory(prefix="chaoshook-candidate-", dir="/tmp") as directory:
        path = Path(directory) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        name = f"target_app.handlers._chaoshook_candidate_{uuid4().hex}"
        try:
            try:
                module = _execute_source(code, name, path)
                handler = module.process_stripe_webhook
                if not callable(handler):
                    raise TypeError("process_stripe_webhook must be callable")
            except Exception as exc:
                result["import_error"] = f"{type(exc).__name__}: {exc}"
                return result
            report = run_algebraic_prover(handler)
            result.update(
                verified=report["passed_cells"] == report["total_cells"] == 27,
                passed_cells=report["passed_cells"], prover_report=report,
                executed_check_ids=[cell["id"] for cell in report["cells"]],
                failed_cell_ids=[cell["id"] for cell in report["cells"] if not cell["passed"]],
            )
            if strict_semantic:
                from chaoshook_engine.runner import run_semantic_challenges
                result["strict_semantic"] = run_semantic_challenges(handler)
            return result
        finally:
            sys.modules.pop(name, None)


def compute_unified_diff(v1_code: str, v2_code: str) -> str:
    return "".join(difflib.unified_diff(
        v1_code.splitlines(keepends=True), v2_code.splitlines(keepends=True),
        fromfile="target_app/handlers/v1_vulnerable.py",
        tofile="target_app/handlers/active_handler.py",
    ))


def get_active_handler_status() -> dict:
    with _LOCK:
        code = (HANDLER_DIR / "active_handler.py").read_bytes().decode("utf-8")
        baseline = (HANDLER_DIR / "v1_vulnerable.py").read_bytes().decode("utf-8")
        return {
            "mode": "vulnerable" if code == baseline else "patched",
            "active_code": code, "line_count": len(code.splitlines()),
        }


def _write_source(path, source):
    temporary = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(source)
        # Remove any old timestamp-based cache before publishing new bytes.
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
        temporary.replace(path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


@contextmanager
def active_installation(code):
    """Stage a fresh module, publish atomically, and roll back on any exception.

    Callers keep this transaction open through post-install proof and response
    construction. Rollback restores the original module object, not a reexecution.
    """
    import target_app.handlers as package

    path = HANDLER_DIR / "active_handler.py"
    with _LOCK:
        previous_bytes = path.read_bytes()
        previous_module = sys.modules.get(_MODULE)
        missing = object()
        previous_attribute = getattr(package, "active_handler", missing)
        with fresh_handler_module(code) as module:
            published = False
            try:
                _write_source(path, code.encode("utf-8"))
                published = True
                sys.modules[_MODULE] = module
                package.active_handler = module
                yield get_active_handler_status()
            except BaseException:
                try:
                    if published:
                        _write_source(path, previous_bytes)
                finally:
                    # Restore memory even if the filesystem becomes unwritable.
                    if previous_module is None:
                        sys.modules.pop(_MODULE, None)
                    else:
                        sys.modules[_MODULE] = previous_module
                    if previous_attribute is missing:
                        package.__dict__.pop("active_handler", None)
                    else:
                        package.active_handler = previous_attribute
                raise
            else:
                if (previous_module is not None and previous_module is not module
                        and previous_module.__name__.startswith("target_app.handlers._chaoshook_source_")):
                    sys.modules.pop(previous_module.__name__, None)


def _install(code: str) -> dict:
    with active_installation(code) as status:
        return status


def apply_patch_to_active(patched_code: str | None = None, *, require_strict_semantic=False) -> dict:
    code = patched_code if patched_code is not None else (
        HANDLER_DIR / "v2_patched.py"
    ).read_bytes().decode("utf-8")
    if require_strict_semantic:
        evidence = qualify_semantic_candidate(code)
        if not evidence["strict_qualification"]["qualified"]:
            raise ValueError("Candidate failed strict semantic qualification")
    return _install(code)


def reset_active_to_vulnerable() -> dict:
    return _install((HANDLER_DIR / "v1_vulnerable.py").read_bytes().decode("utf-8"))

class EvaluationHistory:
    """Caller-maintained provenance for a repair session; retain across candidates.

    Only construct this with the complete set of synthesis/selection input seeds.
    Evaluation consumes seeds immediately, so selecting or repairing a candidate
    using a result cannot cause that input to be labeled held-out again.
    """

    def __init__(self, synthesis_seeds=(), *, retired_seeds=()):
        self.used_seeds = set(synthesis_seeds)
        self.retired_seeds = set(retired_seeds)

    def record_feedback(self, seeds):
        self.used_seeds.update(seeds)
        self.retired_seeds.update(seeds)


def evaluate_candidate_patch(code: str, seeds=(101, 202, 303), *, history=None) -> dict:
    """Evaluate frozen source separately from synthesis and legacy acceptance.

    No history means unknown provenance and results are labeled ``evaluation``.
    A complete caller-supplied history permits held-out labeling for fresh seeds.
    Never feed this report into Muse; retire feedback inputs with record_feedback.
    """
    from dataclasses import asdict
    from hashlib import sha256
    from chaoshook_engine.fixtures import scenario_from_seed
    from chaoshook_engine.runner import run_algebraic_prover

    seeds = tuple(seeds)
    if not seeds or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Provide a nonempty collection of distinct integer seeds")
    result = {"candidate_sha256": sha256(code.encode()).hexdigest(),
              "evaluations": [], "all_passed": False, "import_error": None,
              "schedule_ids": list(range(1, 28)),
              "coverage": "24 permutations, one SQL barrier race, two crash retries per seed"}
    with TemporaryDirectory(prefix="chaoshook-evaluation-", dir="/tmp") as directory:
        path = Path(directory) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        name = f"target_app.handlers._chaoshook_evaluation_{uuid4().hex}"
        try:
            try:
                module = _execute_source(code, name, path)
                handler = module.process_stripe_webhook
                if not callable(handler):
                    raise TypeError("process_stripe_webhook must be callable")
            except Exception as exc:
                result["import_error"] = f"{type(exc).__name__}: {exc}"
                return result
            for seed in seeds:
                held_out = history is not None and seed not in (history.used_seeds | history.retired_seeds)
                if history is not None:
                    # Consume before execution, including failed evaluations.
                    history.used_seeds.add(seed)
                scenario = scenario_from_seed(seed)
                report = run_algebraic_prover(handler, scenario)
                result["evaluations"].append({
                    "seed": seed, "label": "held-out" if held_out else "evaluation",
                    "scenario": asdict(scenario), "prover_report": report,
                })
            result["all_passed"] = all(
                item["prover_report"]["passed_cells"] == 27 for item in result["evaluations"]
            )
            return result
        finally:
            sys.modules.pop(name, None)


def qualify_candidate_patch(code: str, seeds, *, contract=SCENARIO_SPEC, candidate_audit=None) -> dict:
    """Opt-in post-repair qualification; return a redacted schema-v1 manifest.

    Reverify base acceptance, then evaluate the frozen source on explicit seeds.
    Never install, synthesize, change the contract, or infer held-out provenance.
    Completed failing schedules are failures; missing reports are incomplete.
    """
    from dataclasses import asdict
    from chaoshook_engine.fixtures import scenario_from_seed
    from chaoshook_engine.runner import candidate_export_provenance, export_verification_manifest

    seeds = tuple(seeds)
    if not seeds or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Qualification requires explicit distinct integer seeds")
    if type(contract) is not type(SCENARIO_SPEC) or contract != SCENARIO_SPEC:
        raise ValueError("Qualification requires the existing frozen contract")
    seeds = sorted(seeds)
    candidate_export_provenance(code, candidate_audit)
    source_hash = sha256(code.encode("utf-8")).hexdigest()
    errors = []

    def error_result(stage, seed, error_code):
        errors.append({"stage": stage, "seed": seed, "code": error_code})
        return {"candidate_sha256": source_hash, "verification_error": True}

    try:
        verification = verify_candidate_patch(code)
    except Exception:
        verification = error_result("base_verification", None, "verification_error")
    if verification.get("candidate_sha256") != source_hash:
        verification = error_result("base_verification", None, "candidate_hash_mismatch")
    manifest = export_verification_manifest(code, verification, requested_seeds=seeds,
                                            candidate_audit=candidate_audit)
    evaluations = []
    if manifest["runtime_acceptance"]["accepted"]:
        for seed in seeds:
            try:
                evaluation = evaluate_candidate_patch(code, [seed])
            except Exception:
                evaluation = error_result("additional_evaluation", seed, "evaluation_error")
            if evaluation.get("candidate_sha256") != source_hash:
                evaluation = error_result("additional_evaluation", seed, "candidate_hash_mismatch")
            if not evaluation.get("evaluations"):
                evaluation["evaluations"] = [{
                    "seed": seed, "scenario": asdict(scenario_from_seed(seed)), "label": "evaluation",
                    "prover_report": None, "import_error": evaluation.get("import_error"),
                    "verification_error": evaluation.get("verification_error"),
                }]
            # Validate before retaining a report; malformed evidence cannot erase
            # earlier results or accidentally qualify the expected candidate.
            try:
                export_verification_manifest(code, verification, requested_seeds=[seed], evaluations=[evaluation])
            except (ValueError, KeyError, TypeError):
                evaluation = error_result("additional_evaluation", seed, "invalid_evaluation_evidence")
                evaluation["evaluations"] = [{
                    "seed": seed, "scenario": asdict(scenario_from_seed(seed)), "label": "evaluation",
                    "prover_report": None, "verification_error": True,
                }]
            evaluations.append(evaluation)
        manifest = export_verification_manifest(code, verification, requested_seeds=seeds,
                                                evaluations=evaluations, candidate_audit=candidate_audit)

    def completed(run):
        return (run["executed_schedule_ids"] == list(range(1, 28))
                and not run["import_error"] and not run["verification_error"])

    base = manifest["runtime_acceptance"]
    base_status = "passed" if base["accepted"] else "failed" if completed(base["run"]) else "incomplete_or_errored"
    runs = manifest["additional_scenarios"]["runs"]
    completed_seeds = [run["seed"] for run in runs if completed(run)]
    if not base["accepted"]:
        status = "not_evaluated"
    elif completed_seeds != seeds:
        status = "incomplete_or_errored"
    else:
        status = "failed" if any(run["failed_check_ids"] or run["business_property_violations"]
                                 or run["execution_errors"] or run["synchronization_errors"] for run in runs) else "passed"
    manifest["qualification"] = {
        "label": "additional seeded qualification", "base_verification_status": base_status,
        "status": status, "qualified": status == "passed", "completed_seeds": completed_seeds,
        "not_evaluated_reason": f"base_verification_{base_status}" if not base["accepted"] else None,
        "errors": errors,
    }
    return manifest


def qualify_semantic_candidate(code: str) -> dict:
    """Freeze source and export legacy acceptance separately from strict semantics.

    Always attempt semantic checks, even after a business failure of the legacy
    gate. Import/execution infrastructure failures can never qualify a candidate.
    """
    from chaoshook_engine.runner import export_verification_manifest
    from chaoshook_engine.scenario_spec import SEMANTIC_CONTRACT_ID

    try:
        verification = verify_candidate_patch(code, strict_semantic=True)
    except Exception:
        verification = {"candidate_sha256": sha256(code.encode()).hexdigest(),
                        "verification_error": True}
    manifest = export_verification_manifest(code, verification)
    semantic = verification.get("strict_semantic")
    manifest["strict_semantic"] = semantic or {
        "contract_identifier": SEMANTIC_CONTRACT_ID,
        "scope": "semantic_permutations_prefixes_and_sequential_duplicates",
        "executed_schedule_count": 0, "executed_check_count": 0,
        "passed_check_count": 0, "qualified": False, "runs": [],
        "error": "candidate_import_error" if verification.get("import_error") else "verification_error",
    }
    manifest["strict_qualification"] = {
        "qualified": manifest["runtime_acceptance"]["accepted"] and bool(semantic and semantic["qualified"]),
        "requires": ["legacy_27", SEMANTIC_CONTRACT_ID],
        "installation_default": "legacy_flow_unchanged; strict_requires_explicit_option",
    }
    # Retain the legacy manifest's schedule scope; semantic counts are separate.
    return manifest
