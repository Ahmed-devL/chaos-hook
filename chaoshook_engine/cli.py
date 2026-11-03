"""Hour-1 verification gate: python -m chaoshook_engine.cli verify."""

import argparse

from chaoshook_engine.runner import run_algebraic_prover
from target_app.handlers import v1_vulnerable, v2_patched


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The Brass Escapement proof gate")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Run the 27-cell algebraic proof")
    verify.add_argument("--mode", choices=("vulnerable", "patched", "both"), default="both")
    args = parser.parse_args(argv)
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
