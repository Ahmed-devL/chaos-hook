"""Hour-1 verification gate: python -m chaoshook_engine.cli verify."""

import argparse
import difflib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter


def preflight(live=False) -> int:
    """Standard-library-only local checks; never print configuration values."""
    from importlib import metadata
    import os
    import re
    import shutil
    import sqlite3
    import subprocess

    checks = {"python_3_11_or_newer": sys.version_info >= (3, 11),
              "sqlite_upsert": sqlite3.sqlite_version_info >= (3, 24)}
    for package, module in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                            ("SQLAlchemy", "sqlalchemy"), ("pydantic", "pydantic"),
                            ("httpx", "httpx"), ("pytest", "pytest"), ("python-multipart", "python_multipart")):
        try:
            importlib.import_module(module)
            metadata.version(package)
            checks[package] = True
        except Exception:
            checks[package] = False
    for tool in ("git", "node", "npm"):
        checks[tool] = shutil.which(tool) is not None
        if checks[tool] and tool != "git":
            try:
                output = subprocess.run([tool, '--version'], capture_output=True, text=True, timeout=5, check=True).stdout
                match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)\s*', output)
                version = tuple(map(int, match.groups())) if match else (0, 0, 0)
                checks[tool] = ((22, 20, 0) <= version < (23, 0, 0) if tool == 'node'
                                else (10, 0, 0) <= version < (11, 0, 0))
            except Exception:
                checks[tool] = False
    for label, directory in (("configured_temp_writable", None), ("runner_tmp_writable", "/tmp")):
        try:
            with tempfile.TemporaryDirectory(dir=directory) as folder:
                (Path(folder) / 'probe').write_bytes(b'ok')
            checks[label] = True
        except OSError:
            checks[label] = False
    # Read presence only, independently of the model client and its env loader.
    configured = dict(os.environ)
    env_file = Path(__file__).resolve().parents[1] / '.env'
    try:
        for line in env_file.read_text().splitlines():
            key, separator, value = line.removeprefix('export ').partition('=')
            if separator:
                configured.setdefault(key.strip(), value.strip().strip('\"\''))
    except OSError:
        pass
    def present(name):
        return bool(configured.get(name)) and configured[name] != 'your_api_key_here'
    configuration = {name: present(name) for name in ('MUSE_API_KEY', 'MUSE_BASE_URL', 'DEMO_FALLBACK_CACHE')}
    if live:
        checks['live_configuration'] = configuration['MUSE_API_KEY'] and configuration['MUSE_BASE_URL']
    print(json.dumps({'checks': checks, 'configuration_present': configuration,
                      'live_connectivity_tested': False, 'ok': all(checks.values())}, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


# Let a fresh clone diagnose missing dependencies before third-party imports.
if __name__ == '__main__' and sys.argv[1:2] == ['preflight']:
    preflight_parser = argparse.ArgumentParser(description='Local environment preflight (no network)')
    preflight_parser.add_argument('--live', action='store_true', help='Require live configuration presence only')
    raise SystemExit(preflight(preflight_parser.parse_args(sys.argv[2:]).live))

from chaoshook_engine.muse_client import MuseNeuroSymbolicClient
from chaoshook_engine.patcher import HANDLER_DIR
from chaoshook_engine.runner import (
    candidate_export_provenance, export_pytest_code, export_verification_manifest, run_algebraic_prover,
)
from target_app.handlers import v1_vulnerable, v2_patched


def ping_muse() -> int:
    client = MuseNeuroSymbolicClient()
    emit = lambda message: print(client.safe_error(message))
    if client.force_cache:
        emit("DEMO_FALLBACK_CACHE forces analysis cache; ping explicitly probes the live API.")
    available = None
    ok = True
    try:
        response = client.api_request("GET", "models")
        available = [item["id"] for item in response["data"]
                     if isinstance(item, dict) and isinstance(item.get("id"), str)]
        emit("Available models: " + (", ".join(available) or "(none)"))
    except Exception as exc:
        emit(f"Model discovery FAIL: {type(exc).__name__}: {exc}")
        ok = False
    for role, model in (("Wonder", client.wonder_model), ("Spark", client.spark_model)):
        if available is not None and model not in available:
            matches = difflib.get_close_matches(model, available, n=3, cutoff=0)
            emit(f"{role}: configured ID {model!r} is absent from /models. "
                 f"Closest IDs: {', '.join(matches) or '(none)'}")
        start = perf_counter()
        try:
            response = client.api_request("POST", "chat/completions", json={
                "model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK"}],
            })
            choice = response["choices"][0]
            content = choice["message"]["content"]
            limited = choice.get("finish_reason") == "length"
            if not limited and (not isinstance(content, str) or not content.strip()):
                raise ValueError("completion returned no text")
            note = " (token limit reached before reply)" if not content and limited else ""
            emit(f"{role} {model}: PASS ({(perf_counter() - start) * 1000:.0f} ms){note}")
        except Exception as exc:
            ok = False
            emit(f"{role} {model}: FAIL ({(perf_counter() - start) * 1000:.0f} ms): "
                 f"{type(exc).__name__}: {exc}")
    return 0 if ok else 1


def live_patch_check() -> int:
    client = MuseNeuroSymbolicClient()
    emit = lambda message: print(client.safe_error(message))
    report = run_algebraic_prover(v1_vulnerable.process_stripe_webhook)
    start = perf_counter()
    wonder = client.extract_invariants_and_patch(
        (HANDLER_DIR.parent / "models.py").read_text(encoding="utf-8"),
        (HANDLER_DIR / "active_handler.py").read_text(encoding="utf-8"), report,
    )
    emit(f"Wonder {client.wonder_model}: {(perf_counter() - start) * 1000:.0f} ms")
    for round_result in client.model_routing.get("cegis_rounds", []):
        emit(f"CEGIS Round {round_result['round']}: {round_result['passed_cells']}/27 cells passed")
    if not client.model_routing["wonder_live"]:
        emit(f"Live patch unavailable; fallback: {client.model_routing['fallback_reason']}")
        emit("Live patch proof skipped (cached code is not a live result).")
        return 1
    # Always outside the repository, regardless of TMPDIR. Never install a patch.
    with tempfile.TemporaryDirectory(prefix="chaoshook-live-", dir="/tmp") as directory:
        path = Path(directory) / "live_handler.py"
        path.write_text(wonder["patched_code"], encoding="utf-8")
        name = "_chaoshook_live_patch_check"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            handler = module.process_stripe_webhook
            if not callable(handler):
                raise TypeError("process_stripe_webhook is not callable")
        except Exception as exc:
            emit(f"Live patch import FAIL: {type(exc).__name__}: {exc}")
            sys.modules.pop(name, None)
            return 1
        try:
            result = run_algebraic_prover(handler)
        except Exception as exc:
            emit(f"Live patch proof FAIL: {type(exc).__name__}: {exc}")
            return 1
        finally:
            sys.modules.pop(name, None)
    emit(f"Live patch: {result['passed_cells']}/27 cells passed")
    failures = [cell for cell in result["cells"] if not cell["passed"]]
    emit("Failed cells: " + (", ".join(str(cell["id"]) for cell in failures) or "none"))
    for cell in failures:
        emit(f"  Cell {cell['id']}: {cell['failure_reason']}")
    return 1 if failures else 0


def offline_demo(out: Path, seeds) -> int:
    """Compose existing qualification and manifest exports; no model or install path."""
    from hashlib import sha256
    import shlex
    from chaoshook_engine.patcher import qualify_candidate_patch

    seeds = sorted(seeds)
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("Demo seeds must be nonempty and distinct")
    sources = [HANDLER_DIR / name for name in ('v1_vulnerable.py', 'v2_patched.py')]
    artifacts = [out / name for name in ('vulnerable.json', 'repaired.json', 'demo.json', 'summary.md')]
    protected = [*sources, HANDLER_DIR / 'active_handler.py']
    if any(path.resolve() == source.resolve() for path in artifacts for source in protected):
        raise ValueError("Demo output must not overwrite handler source")
    out.mkdir(parents=True, exist_ok=True)
    records = []
    summary = ['ChaosHook offline demo', '', 'Prepared reference repair; no live Muse output.', '']
    for index, (source_path, label) in enumerate(zip(sources, ('vulnerable baseline', 'prepared reference repair'))):
        source = source_path.read_bytes().decode('utf-8')
        try:
            manifest = qualify_candidate_patch(source, seeds)
        except Exception:
            # Only returned reports are evidence; never invent completed checks.
            manifest = export_verification_manifest(source, {
                'candidate_sha256': sha256(source.encode('utf-8')).hexdigest(), 'verification_error': True,
            }, requested_seeds=seeds)
        artifacts[index].write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        run = manifest['runtime_acceptance']['run']
        reproduced = (len(run['passed_check_ids']) == (6 if index == 0 else 27)
                      and len(run['failed_check_ids']) == (21 if index == 0 else 0)
                      and run['executed_schedule_ids'] == list(range(1, 28))
                      and not run['verification_error'] and not run['import_error'])
        if index == 1:
            reproduced = (reproduced and manifest.get('qualification', {}).get('qualified') is True
                          and manifest['additional_scenarios']['requested_seeds'] == seeds
                          and manifest['executed_schedule_count'] == 27 * (1 + len(seeds)))
        else:
            reproduced = (reproduced and not manifest['runtime_acceptance']['accepted']
                          and manifest['executed_schedule_count'] == 27)
        command = ['python', '-m', 'chaoshook_engine.cli', 'qualify', '--source', str(source_path),
                   *[arg for seed in seeds for arg in ('--seed', str(seed))], '--manifest', artifacts[index].name]
        records.append({'label': label, 'manifest': artifacts[index].name, 'replay_command': command,
                        'candidate_sha256': manifest['candidate']['source_sha256'], 'contract': manifest['contract'],
                        'seeds': seeds, 'executed_schedule_count': manifest['executed_schedule_count'],
                        'base_passed': len(run['passed_check_ids']), 'base_failed': len(run['failed_check_ids']),
                        'qualification_status': manifest.get('qualification', {}).get('status', 'incomplete_or_errored'),
                        'expected_results_reproduced': reproduced})
        summary.extend([f"{label}: {len(run['passed_check_ids'])}/27 base checks passed; "
                        f"{manifest['executed_schedule_count']} schedules executed; "
                        f"qualification {records[-1]['qualification_status']}.",
                        f"Candidate SHA-256: {records[-1]['candidate_sha256']}",
                        f"Contract SHA-256: {manifest['contract']['sha256']}",
                        f"Oracle source SHA-256: {manifest['contract']['oracle_source_sha256']}",
                        f"Replay: {shlex.join(command)}", ''])
    ok = all(record['expected_results_reproduced'] for record in records)
    index = {'schema_version': 1, 'offline': True, 'seeds': seeds, 'evaluations': records,
             'expected_results_reproduced': ok}
    artifacts[2].write_text(json.dumps(index, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    summary.extend([f"Additional seeded qualification seeds: {seeds} (not unseen held-out evidence).",
                    f"Expected demonstration results reproduced: {ok}.",
                    'Replay commands run from this output directory with the project on PYTHONPATH.',
                    'The baseline replay exits 1 by design; the demo expects its known failures.'])
    artifacts[3].write_text('\n'.join(summary) + '\n', encoding='utf-8')
    print('\n'.join(summary))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The Brass Escapement proof gate")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="Check local environment without contacting Muse")
    check.add_argument("--live", action="store_true")
    verify = commands.add_parser("verify", help="Run the 27-cell algebraic proof")
    verify.add_argument("--mode", choices=("vulnerable", "patched", "both"), default="both")
    commands.add_parser("ping-muse", help="Discover and ping configured live Muse models")
    commands.add_parser("live-patch-check", help="Prove a live Wonder patch without installing it")
    export = commands.add_parser("export-pytest", help="Export the 27-cell pytest regression suite")
    export.add_argument("--out", type=Path, default=Path("test_webhook_invariance.py"))
    export.add_argument("--manifest", type=Path,
                        help="Also execute candidate verification and write reproducible JSON evidence (no Muse)")
    export.add_argument("--source", type=Path,
                        help="Candidate for manifest verification (default: active_handler.py)")
    export.add_argument("--seed", type=int, action="append", dest="seeds",
                        help="Explicit additional evaluation seed; omit to record not evaluated")
    export.add_argument("--candidate-audit", type=Path,
                        help="Existing candidate audit record or model_routing JSON; hashes must match source/contract")
    evaluate = commands.add_parser("evaluate", help="Evaluate source on seeded scenarios, without Muse")
    evaluate.add_argument("--source", type=Path, required=True)
    evaluate.add_argument("--seed", type=int, action="append", dest="seeds")
    evaluate.add_argument("--history", type=Path,
                          help="JSON history with used_seeds and retired_seeds; updated before returning")
    qualify = commands.add_parser("qualify", help="Post-repair seeded qualification; never installs or invokes Muse")
    qualify.add_argument("--source", type=Path, required=True)
    qualify.add_argument("--seed", type=int, action="append", dest="seeds", required=True)
    qualify.add_argument("--manifest", type=Path, help="Also save the returned schema-v1 JSON manifest")
    qualify.add_argument("--candidate-audit", type=Path, help="Existing hash-bound candidate audit or model_routing JSON")
    semantic = commands.add_parser("qualify-semantic", help="Strict causal/counterfactual qualification; no Muse or install")
    semantic.add_argument("--source", type=Path, required=True)
    semantic.add_argument("--manifest", type=Path)
    demo = commands.add_parser("demo", help="Write deterministic offline baseline and prepared repair evidence")
    demo.add_argument("--out", type=Path, required=True, help="Evidence output directory")
    demo.add_argument("--seed", type=int, action="append", dest="seeds", help="Qualification seeds (default: 101, 202, 303)")
    args = parser.parse_args(argv)
    if args.command == 'preflight':
        return preflight(args.live)
    if args.command == "qualify-semantic":
        from chaoshook_engine.patcher import qualify_semantic_candidate

        if args.manifest and args.manifest.resolve() == args.source.resolve():
            parser.error("Manifest output must differ from source")
        result = qualify_semantic_candidate(args.source.read_bytes().decode("utf-8"))
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.manifest:
            args.manifest.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0 if result["strict_qualification"]["qualified"] else 1
    if args.command == "demo":
        try:
            return offline_demo(args.out, args.seeds if args.seeds is not None else [101, 202, 303])
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "qualify":
        from chaoshook_engine.patcher import qualify_candidate_patch

        if args.manifest and any(args.manifest.resolve() == path.resolve()
                                 for path in [args.source, *([args.candidate_audit] if args.candidate_audit else [])]):
            parser.error("Manifest output must differ from input paths")
        source = args.source.read_bytes().decode("utf-8")
        audit = json.loads(args.candidate_audit.read_text(encoding="utf-8")) if args.candidate_audit else None
        try:
            result = qualify_candidate_patch(source, args.seeds, candidate_audit=audit)
        except ValueError as exc:
            parser.error(str(exc))
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.manifest:
            args.manifest.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0 if result["qualification"]["qualified"] else 1
    if args.command == "evaluate":
        from chaoshook_engine.patcher import EvaluationHistory, evaluate_candidate_patch

        history = None
        if args.history:
            # Require an existing explicit provenance record; never assume a fresh history.
            data = json.loads(args.history.read_text(encoding="utf-8"))
            history = EvaluationHistory(data["used_seeds"], retired_seeds=data["retired_seeds"])
        source = args.source.read_text(encoding="utf-8")
        seeds = args.seeds or (101, 202, 303)
        if len(set(seeds)) != len(seeds):
            parser.error("Evaluation seeds must be distinct")
        if history is not None:
            # Reserve durably before running; interruption must not reuse a label.
            args.history.write_text(json.dumps({"used_seeds": sorted(history.used_seeds | set(seeds)),
                                               "retired_seeds": sorted(history.retired_seeds)}) + "\n",
                                    encoding="utf-8")
        result = evaluate_candidate_patch(source, seeds, history=history)
        print(json.dumps(result, indent=2))
        return 0 if result["all_passed"] else 1
    if args.command == "export-pytest":
        if not args.manifest and (args.source or args.seeds or args.candidate_audit):
            parser.error("--source, --seed, and --candidate-audit require --manifest")
        if args.manifest:
            from dataclasses import asdict
            from hashlib import sha256
            from chaoshook_engine.fixtures import scenario_from_seed
            from chaoshook_engine.patcher import evaluate_candidate_patch, verify_candidate_patch

            source_path = args.source or HANDLER_DIR / "active_handler.py"
            # Hash and execute the same frozen UTF-8 bytes, including original newlines.
            source = source_path.read_bytes().decode("utf-8")
            seeds = args.seeds or []
            if len(set(seeds)) != len(seeds):
                parser.error("Evaluation seeds must be distinct")
            # Reject path collisions before either artifact can overwrite an input.
            inputs = [source_path, *([args.candidate_audit] if args.candidate_audit else [])]
            if (args.out.resolve() == args.manifest.resolve()
                    or any(output.resolve() == path.resolve()
                           for output in (args.out, args.manifest) for path in inputs)):
                parser.error("Export output paths must be distinct from each other and input paths")
            audit = (json.loads(args.candidate_audit.read_text(encoding="utf-8"))
                     if args.candidate_audit else None)
            try:
                candidate_export_provenance(source, audit)
            except ValueError as exc:
                parser.error(str(exc))
            source_hash = sha256(source.encode("utf-8")).hexdigest()
            try:
                verification = verify_candidate_patch(source)
            except Exception:
                # A fatal verifier exception returns no completed report. Do not
                # manufacture results for any schedules it may have started.
                verification = {"candidate_sha256": source_hash, "verification_error": True}
            evaluations = []
            for seed in seeds:
                try:
                    evaluation = evaluate_candidate_patch(source, [seed])
                except Exception:
                    evaluation = {"candidate_sha256": source_hash, "verification_error": True, "evaluations": []}
                if not evaluation["evaluations"]:
                    evaluation["evaluations"] = [{
                        "seed": seed, "scenario": asdict(scenario_from_seed(seed)), "label": "evaluation",
                        "prover_report": None, "import_error": evaluation.get("import_error"),
                        "verification_error": evaluation.get("verification_error"),
                    }]
                evaluations.append(evaluation)
            manifest = export_verification_manifest(source, verification, evaluations=evaluations,
                                                    requested_seeds=seeds, candidate_audit=audit)
            args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.out.write_text(export_pytest_code(), encoding="utf-8")
        print(f"Exported pytest suite to {args.out}")
        if args.manifest:
            print(f"Exported verification manifest to {args.manifest}")
            return 0 if (manifest["runtime_acceptance"]["accepted"] and
                         (not seeds or manifest["additional_scenarios"]["all_passed"] is True)) else 1
        return 0
    if args.command == "ping-muse":
        return ping_muse()
    if args.command == "live-patch-check":
        return live_patch_check()
    print(f"{'Handler':<14} {'Passed':>8} {'Failed':>8} {'Time (ms)':>11} {'Gate':>6}")
    ok = True
    for mode, module, expected in (
        ("vulnerable", v1_vulnerable, 6), ("patched", v2_patched, 27),
    ):
        if args.mode not in (mode, "both"):
            continue
        report = run_algebraic_prover(module.process_stripe_webhook)
        valid = report["passed_cells"] == expected
        ok = ok and valid
        print(f"{mode:<14} {report['passed_cells']:>5}/27 {report['failed_cells']:>8} "
              f"{report['duration_ms']:>11.3f} {'PASS' if valid else 'FAIL':>6}")
        if not valid:
            for cell in report["cells"]:
                if not cell["passed"]:
                    print(f"  Cell {cell['id']}: {cell['failure_reason']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
