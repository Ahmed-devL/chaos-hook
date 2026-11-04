"""Apply and inspect the hot-swappable target handler."""

import difflib
import importlib
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import RLock
from uuid import uuid4


HANDLER_DIR = Path(__file__).resolve().parents[1] / "target_app" / "handlers"
_LOCK = RLock()
_MODULE = "target_app.handlers.active_handler"


def verify_candidate_patch(code: str) -> dict:
    """Import and prove a candidate outside the repo without installing it."""
    from chaoshook_engine.runner import run_algebraic_prover

    result = {"verified": False, "passed_cells": 0, "total_cells": 27,
              "failed_cell_ids": [], "prover_report": None, "import_error": None}
    with TemporaryDirectory(prefix="chaoshook-candidate-", dir="/tmp") as directory:
        path = Path(directory) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        name = f"_chaoshook_candidate_{uuid4().hex}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
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
                failed_cell_ids=[cell["id"] for cell in report["cells"] if not cell["passed"]],
            )
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
        code = (HANDLER_DIR / "active_handler.py").read_text(encoding="utf-8")
        baseline = (HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8")
        return {
            "mode": "vulnerable" if code == baseline else "patched",
            "active_code": code, "line_count": len(code.splitlines()),
        }


def _install(code: str) -> dict:
    path = HANDLER_DIR / "active_handler.py"
    compile(code, str(path), "exec")  # Reject syntax errors before touching the target.
    with _LOCK:
        previous = path.read_text(encoding="utf-8")

        def write_and_reload(source):
            temporary = None
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=HANDLER_DIR,
                                        suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(source)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            # Same-second, same-size edits must not reuse stale bytecode.
            Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
            importlib.invalidate_caches()
            if _MODULE in sys.modules:
                importlib.reload(sys.modules[_MODULE])
            else:
                importlib.import_module(_MODULE)

        try:
            write_and_reload(code)
        except Exception:
            write_and_reload(previous)
            raise
        return get_active_handler_status()


def apply_patch_to_active(patched_code: str | None = None) -> dict:
    code = patched_code if patched_code is not None else (
        HANDLER_DIR / "v2_patched.py"
    ).read_text(encoding="utf-8")
    return _install(code)


def reset_active_to_vulnerable() -> dict:
    return _install((HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8"))

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
        name = f"_chaoshook_evaluation_{uuid4().hex}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
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
