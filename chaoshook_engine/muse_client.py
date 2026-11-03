"""Muse model routing with a deterministic, locally verified demo cache."""

from itertools import permutations
import json
import logging
import os

import httpx

from chaoshook_engine.patcher import HANDLER_DIR, compute_unified_diff


logger = logging.getLogger(__name__)
WONDER = "muse-wonder"
SPARK = "muse-spark-1.3"
WONDER_PROMPT = """You are Meta Muse Wonder, a Principal Distributed Systems & Formal
Verification Architect inside ChaosHook (The Brass Escapement).
Given the SQLAlchemy schema, active handler and 27-cell prover report, return strict
JSON with invariants (exactly four objects with id, title, formal_expr, description,
violated_by_cells) and patched_code. Cover credit conservation, exactly-once outbox,
order commutativity and crash atomicity. Use a single transaction, claim-first
INSERT INTO webhook_events ... ON CONFLICT(event_id) DO NOTHING with rowcount == 0,
tombstone upserts, monotonic last_event_ts, and no intermediate commits.
patched_code must equal the supplied verified_reference_code exactly."""
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
        self.api_key = os.getenv("MUSE_API_KEY", "").strip()
        self.base_url = os.getenv("MUSE_BASE_URL", "").strip().rstrip("/")
        self.force_cache = os.getenv("DEMO_FALLBACK_CACHE", "false").lower() in {
            "true", "1", "yes", "on",
        }
        self.used_fallback_cache = False

    def _request(self, model, prompt, payload, fallback, validate):
        try:
            key = self.api_key.lower()
            if (self.force_cache or not key or not self.base_url
                    or key in {"none", "null", "changeme", "placeholder", "test", "dummy"}
                    or any(marker in key for marker in (
                        "your_", "your-", "replace", "example", "placeholder", "<", ">",
                    ))):
                raise ValueError("offline mode or missing/placeholder Muse configuration")
            with httpx.Client(timeout=8.0) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": model, "temperature": 0,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": prompt},
                                       {"role": "user", "content": json.dumps(payload)}]},
                )
                response.raise_for_status()
                result = json.loads(response.json()["choices"][0]["message"]["content"])
            if not validate(result):
                raise ValueError("model response violates the structured output contract")
            return result
        except Exception as exc:
            self.used_fallback_cache = True
            # Never log credentials, request bodies, or remote error content.
            logger.warning("Using verified deterministic neuro-symbolic cache (%s; %s)",
                           model, type(exc).__name__)
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
            return (isinstance(value, dict) and value.get("patched_code") == reference
                    and isinstance(value.get("invariants"), list)
                    and len(value["invariants"]) == 4
                    and all(isinstance(i, dict)
                            and all(isinstance(i.get(k), str) and i[k] for k in
                                    ("id", "title", "formal_expr", "description"))
                            and isinstance(i.get("violated_by_cells"), list)
                            and all(type(c) is int and 1 <= c <= 27
                                    for c in i["violated_by_cells"])
                            for i in value["invariants"]))

        return self._request(WONDER, WONDER_PROMPT,
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
                            for c in value["failure_clusters"]))

        return self._request(SPARK, SPARK_PROMPT,
                             {"prover_report": prover_report,
                              "diagram_description": diagram_description}, fallback, valid)

    def run_full_neuro_symbolic_analysis(self, prover_report: dict,
                                        diagram_description: str | None = None) -> dict:
        self.used_fallback_cache = False
        wonder = self.extract_invariants_and_patch(
            (HANDLER_DIR.parent / "models.py").read_text(encoding="utf-8"),
            (HANDLER_DIR / "active_handler.py").read_text(encoding="utf-8"), prover_report,
        )
        diagnosis = self.diagnose_multimodal_divergence(prover_report, diagram_description)
        return {
            "model_routing": {"muse_wonder_model": WONDER, "muse_spark_model": SPARK,
                              "used_fallback_cache": self.used_fallback_cache},
            "invariants": wonder["invariants"], "diagnosis": diagnosis,
            "patched_code": wonder["patched_code"],
            "unified_diff": compute_unified_diff(
                (HANDLER_DIR / "v1_vulnerable.py").read_text(encoding="utf-8"),
                wonder["patched_code"],
            ),
        }
