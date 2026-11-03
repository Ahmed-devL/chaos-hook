"""Apply and inspect the hot-swappable target handler."""

import difflib
import importlib
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from threading import RLock


HANDLER_DIR = Path(__file__).resolve().parents[1] / "target_app" / "handlers"
_LOCK = RLock()
_MODULE = "target_app.handlers.active_handler"


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
