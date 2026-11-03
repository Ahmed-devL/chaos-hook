"""Muse model routing with a deterministic, locally verified demo cache."""

from itertools import permutations
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
from pathlib import Path
import re
from threading import Lock
from time import perf_counter

import httpx

from chaoshook_engine.patcher import HANDLER_DIR, compute_unified_diff


logger = logging.getLogger(__name__)


def _load_env():
    """Load local KEY=VALUE settings without overriding the process environment."""
    path = Path(__file__).resolve().parents[1] / ".env"
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                name, separator, value = line.partition("=")
                name, value = name.strip(), value.strip()
                if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    continue
                if value.startswith(("'", '"')):
                    quote = value[0]
                    end = value.find(quote, 1)
                    if end < 0:
                        continue
                    value = value[1:end]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                os.environ.setdefault(name, value)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        logger.warning("Could not load local Muse configuration")


_load_env()
WONDER = os.getenv("MUSE_WONDER_MODEL", "muse-wonder-1.0").strip() or "muse-wonder-1.0"
SPARK = os.getenv("MUSE_SPARK_MODEL", "muse-spark-1.3").strip() or "muse-spark-1.3"
WONDER_PROMPT = """You are Meta Muse Wonder, a Principal Distributed Systems & Formal
Verification Architect inside ChaosHook (The Brass Escapement).
Given the SQLAlchemy schema, active handler and 27-cell prover report, return strict
JSON with invariants (exactly four objects with id, title, formal_expr, description,
violated_by_cells) and patched_code. Cover credit conservation, exactly-once outbox,
order commutativity and crash atomicity. Use a single transaction, claim-first
INSERT INTO webhook_events ... ON CONFLICT(event_id) DO NOTHING with rowcount == 0,
tombstone upserts, monotonic last_event_ts, and no intermediate commits.
Return a complete importable Python module defining
process_stripe_webhook(session, raw_body, sig_header). The supplied
verified_reference_code illustrates the schema and handler contract."""
SPARK_PROMPT = """You are Meta Muse Spark 1.3, a Multimodal Distributed State & Payment
Flow Auditor. Given a webhook lifecycle report and optional diagram description,
return strict JSON with summary_headline, failure_clusters (three objects with
title, description, cells), and remediation_proof_plan. Cover Order Commutativity
Breakdown (18/24 permutations in the vulnerable baseline), Parallel TOCTOU Race
(Cell #25), and Mid-Handler Crash Atomicity (Cells #26-27). Explain tombstone drops,
stale overwrites, SELECT-before-INSERT races and intermediate-commit retry
amplification. Distinguish observed failures from baseline expectations. Explain
how atomic claims, transactional outbox and timestamp watermarks restore 27/27."""


class MuseNeuroSymbolicClient:
    def __init__(self):
        self.api_key = (os.getenv("MUSE_API_KEY") or os.getenv("MODEL_API_KEY", "")).strip()
        self.base_url = (os.getenv("MUSE_BASE_URL") or os.getenv("META_BASE_URL")
                         or "https://api.meta.ai/v1").strip().rstrip("/")
        self.wonder_model = os.getenv("MUSE_WONDER_MODEL", WONDER).strip() or WONDER
        self.spark_model = os.getenv("MUSE_SPARK_MODEL", SPARK).strip() or SPARK
        try:
            self.timeout = float(os.getenv("MUSE_TIMEOUT_S", "90"))
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError("invalid timeout")
        except ValueError:
            self.timeout = 90.0
        self.force_cache = os.getenv("DEMO_FALLBACK_CACHE", "false").strip().lower() in {
            "true", "1", "yes", "on",
        }
        self.used_fallback_cache = False
        self.model_routing = {"wonder_live": False, "spark_live": False,
                              "fallback_reason": None}
        self._fallback_reasons = {}
        self._routing_lock = Lock()

    def _record_route(self, part, live, reason=None):
        with self._routing_lock:
            self.model_routing[f"{part}_live"] = live
            if live:
                self._fallback_reasons.pop(part, None)
            else:
                self._fallback_reasons[part] = self.safe_error(reason)
            self.used_fallback_cache = bool(self._fallback_reasons)
            self.model_routing["fallback_reason"] = "; ".join(
                f"{name}: {reason}" for name, reason in sorted(self._fallback_reasons.items())
            ) or None

    def safe_error(self, value):
        """Redact credentials even when an upstream error echoes its input."""
        result = str(value)
        for secret in (self.api_key, os.getenv("MUSE_API_KEY"), os.getenv("MODEL_API_KEY")):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result

    def api_request(self, method, path, **kwargs):
        if not self.api_key:
            raise ValueError("missing MUSE_API_KEY / MODEL_API_KEY")
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method, f"{self.base_url}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {self.api_key}"}, **kwargs,
            )
        if response.is_error:
            raise ValueError(self.safe_error(f"HTTP {response.status_code}: {response.text}"))
        return response.json()

    def _request(self, part, model, prompt, payload, fallback, validate):
        try:
            key = self.api_key.lower()
            if (self.force_cache or not key or not self.base_url
                    or key in {"none", "null", "changeme", "placeholder", "test", "dummy"}
                    or any(marker in key for marker in (
                        "your_", "your-", "replace", "example", "placeholder", "<", ">",
                    ))):
                raise ValueError("offline mode or missing/placeholder Muse configuration")
            response = self.api_request(
                "POST", "chat/completions",
                json={"model": model, "temperature": 0,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": prompt},
                                   {"role": "user", "content": json.dumps(payload)}]},
            )
            content = response["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*\n?", "", content, count=1,
                                 flags=re.IGNORECASE)
                content = re.sub(r"\s*```$", "", content)
            result = json.loads(content)
            if not validate(result):
                raise ValueError("model response violates the structured output contract")
            self._record_route(part, True)
            return result
        except Exception as exc:
            reason = (str(exc) if isinstance(exc, ValueError)
                      and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__)
            self._record_route(part, False, reason)
            # Never log credentials, request bodies, or remote error content.
            logger.warning("Using verified deterministic neuro-symbolic cache (%s; %s)",
                           self.safe_error(model), type(exc).__name__)
            return fallback

    @staticmethod
    def _invariants(report):
        orders = [i for i, order in enumerate(permutations(range(1, 5)), 1)
                  if order[-1] != 4]
        failures = None
        if isinstance(report.get("cells"), list):
            failures = {c.get("id") for c in report["cells"]
                        if isinstance(c, dict) and c.get("passed") is False}

        def inv(identifier, title, expression, description, cells):
            return {"id": identifier, "title": title, "formal_expr": expression,
                    "description": description, "violated_by_cells": [
                        cell for cell in cells if failures is None or cell in failures]}

        return [
            inv("I1", "Credit conservation", "balance(u) = Σ ledger(u).delta = 100",
                "The canonical paid invoice grants exactly 100 credits despite duplicates.", [25, 26]),
            inv("I2", "Exactly-once welcome outbox", "count(outbox(u, welcome_pro)) = 1",
                "A checkout retry must not queue a second welcome email.", [27]),
            inv("I3", "Order commutativity", "∀ π ∈ S₄: fold(H, S₀, π(E)) = S*",
                "Tombstones and monotonic timestamps preserve cancellation in every order.", orders),
            inv("I4", "Atomic crash recovery", "H(retry(crash(H(S,e))),e) = H(S,e)",
                "Event claim, ledger and outbox commit together or all roll back.", [26, 27]),
        ]

    def extract_invariants_and_patch(self, schema_code: str, handler_code: str,
                                     prover_report: dict) -> dict:
        reference = (HANDLER_DIR / "v2_patched.py").read_text(encoding="utf-8")
        fallback = {"invariants": self._invariants(prover_report), "patched_code": reference}

        def valid(value):
            return (isinstance(value, dict) and isinstance(value.get("patched_code"), str)
                    and "process_stripe_webhook" in value["patched_code"]
                    and isinstance(value.get("invariants"), list)
                    and len(value["invariants"]) == 4
                    and all(isinstance(i, dict)
                            and all(isinstance(i.get(k), str) and i[k] for k in
                                    ("id", "title", "formal_expr", "description"))
                            and isinstance(i.get("violated_by_cells"), list)
                            and all(type(c) is int and 1 <= c <= 27
                                    for c in i["violated_by_cells"])
                            for i in value["invariants"]))

        return self._request("wonder", self.wonder_model, WONDER_PROMPT,
                             {"schema_code": schema_code, "handler_code": handler_code,
                              "prover_report": prover_report, "verified_reference_code": reference},
                             fallback, valid)

    def diagnose_multimodal_divergence(self, prover_report: dict,
                                      diagram_description: str | None = None) -> dict:
        fallback = {
            "summary_headline": "The vulnerable baseline loses cancellation ordering and amplifies side effects under races and crash retries.",
            "failure_clusters": [
                {"title": "Order Commutativity Breakdown (18/24 Permutations Failed)",
                 "description": "Baseline: deletion before creation drops the tombstone; stale creation, checkout or invoice handling overwrites cancellation.",
                 "cells": self._invariants({})[2]["violated_by_cells"]},
                {"title": "Parallel TOCTOU Race Breakdown (Cell #25 Failed)",
                 "description": "Both barrier threads pass SELECT before either records the event, granting duplicate credits.", "cells": [25]},
                {"title": "Mid-Handler Crash Atomicity Breakdown (Cells #26–27 Failed)",
                 "description": "Intermediate commits persist credits or email before the event claim; retry repeats the side effect.", "cells": [26, 27]},
            ],
            "remediation_proof_plan": "Claim events atomically with ON CONFLICT in the same transaction as ledger and outbox writes. Upsert deletion tombstones and merge state with monotonic watermarks. Re-run all 24 permutations, the two-thread barrier and both crash retries to verify 27/27 convergence against S*.",
        }

        def valid(value):
            return (isinstance(value, dict)
                    and all(isinstance(value.get(k), str) and value[k]
                            for k in ("summary_headline", "remediation_proof_plan"))
                    and isinstance(value.get("failure_clusters"), list)
                    and len(value["failure_clusters"]) == 3
                    and all(isinstance(c, dict)
                            and all(isinstance(c.get(k), str) and c[k]
                                    for k in ("title", "description"))
                            and isinstance(c.get("cells"), list)
                            and all(type(cell) is int and 1 <= cell <= 27
                                    for cell in c["cells"])
                            for c in value["failure_clusters"]))

        return self._request("spark", self.spark_model, SPARK_PROMPT,
                             {"prover_report": prover_report,
                              "diagram_description": diagram_description}, fallback, valid)

    def run_full_neuro_symbolic_analysis(self, prover_report: dict,
                                        diagram_description: str | None = None) -> dict:
        started = perf_counter()
        self.used_fallback_cache = False
        self._fallback_reasons.clear()
        self.model_routing.update(wonder_live=False, spark_live=False, fallback_reason=None)
        schema = (HANDLER_DIR.parent / "models.py").read_text(encoding="utf-8")
        handler = (HANDLER_DIR / "active_handler.py").read_text(encoding="utf-8")
        if self.force_cache:
            wonder = self.extract_invariants_and_patch(schema, handler, prover_report)
            diagnosis = self.diagnose_multimodal_divergence(prover_report, diagram_description)
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="Muse") as pool:
                wonder_future = pool.submit(
                    self.extract_invariants_and_patch, schema, handler, prover_report,
                )
                spark_future = pool.submit(
                    self.diagnose_multimodal_divergence, prover_report, diagram_description,
                )
                wonder, diagnosis = wonder_future.result(), spark_future.result()
        self.model_routing["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        return {
            "model_routing": {**self.model_routing,
                              "muse_wonder_model": self.wonder_model,
                              "muse_spark_model": self.spark_model,
                              "used_fallback_cache": self.used_fallback_cache},
            "invariants": wonder["invariants"], "diagnosis": diagnosis,
            "patched_code": wonder["patched_code"],
            "unified_diff": compute_unified_diff(
                (HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8"),
                wonder["patched_code"],
            ),
        }
