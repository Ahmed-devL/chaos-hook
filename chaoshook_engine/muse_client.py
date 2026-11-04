"""Muse model routing with a deterministic, locally verified demo cache."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
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

from chaoshook_engine.patcher import HANDLER_DIR, compute_unified_diff, verify_candidate_patch


logger = logging.getLogger(__name__)


def counterexample_view(report: dict, max_chars: int = 30000) -> dict:
    """Select distinct failures and bounded race/crash evidence without mutating report.

    If mandatory evidence alone exceeds the budget, fail explicitly rather than
    silently removing a distinct failure or returning an oversized payload.
    """
    failures = [cell for cell in report.get("cells", []) if cell.get("passed") is False]
    counterexamples = []
    seen = set()
    for cell in failures:
        reason = cell["failure_reason"]
        fault = cell["category"] in {"concurrency", "crash_retry"}
        if reason not in seen or fault:
            example = {key: deepcopy(cell[key]) for key in
                       ("id", "category", "label", "sequence", "failure_reason", "state_diff")}
            if fault:
                example["sql_trace"] = deepcopy(cell["sql_trace"][:12])
            counterexamples.append(example)
        seen.add(reason)
    result = {
        "golden_state": deepcopy(report.get("golden_state", {})),
        "summary": {"passed_cells": int(report.get("passed_cells", 0)),
                    "total_cells": 27, "failed_cell_ids": [cell["id"] for cell in failures]},
        "counterexamples": counterexamples,
    }
    if len(json.dumps(result)) > max_chars:
        for example in counterexamples:
            example.pop("sql_trace", None)
        # A permutation representative can also be covered by a mandatory fault cell.
        for example in list(reversed(counterexamples)):
            if len(json.dumps(result)) <= max_chars:
                break
            if example["category"] == "permutation" and any(
                other is not example and other["failure_reason"] == example["failure_reason"]
                for other in counterexamples
            ):
                counterexamples.remove(example)
    if len(json.dumps(result)) > max_chars:
        raise ValueError("counterexample budget is too small for mandatory failure evidence")
    return result


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
process_stripe_webhook(session, raw_body, sig_header) -> dict.
The handler_contract is an interface description:
Allowed imports:
from sqlalchemy import text
from target_app.hmac_auth import verify_stripe_signature
from target_app.models import CreditLedger, Subscription, User, WebhookEvent, OutboxEmail
from target_app.schemas import StripeEvent
from target_app.side_effects import send_welcome_email
Model columns:
User: id, email, stripe_customer_id, plan_tier, credit_balance, welcome_email_sent, last_event_ts
Subscription: id, user_id, stripe_customer_id, status, current_period_end, last_event_ts
CreditLedger: id, user_id, invoice_id, delta, reason
WebhookEvent: event_id, event_type, created_ts, status
OutboxEmail: id, user_id, template, dedupe_key
Golden-state expectations: credit_balance 100, one welcome email, final subscription
status and user plan_tier canceled."""
SPARK_PROMPT = """You are Meta Muse Spark 1.3, a Multimodal Distributed State & Payment
Flow Auditor. Given a webhook lifecycle report and optional diagram description,
return strict JSON with summary_headline, failure_clusters (three objects with
title, description, cells), and remediation_proof_plan. Cover Order Commutativity
Breakdown (18/24 permutations in the vulnerable baseline), Parallel TOCTOU Race
(Cell #25), and Mid-Handler Crash Atomicity (Cells #26-27). Explain tombstone drops,
stale overwrites, SELECT-before-INSERT races and intermediate-commit retry
amplification. Distinguish observed failures from baseline expectations. Explain
how atomic claims, transactional outbox and timestamp watermarks restore 27/27.
When a diagram image is provided, optionally add diagram_findings: a list of up to
3 short strings, each naming a diagram element or assumption and the failing
cells that contradict it. Omit findings unsupported by the image or report."""


class MuseNeuroSymbolicClient:
    def __init__(self):
        self.api_key = (os.getenv("MUSE_API_KEY") or os.getenv("MODEL_API_KEY", "")).strip()
        self.base_url = (os.getenv("MUSE_BASE_URL") or os.getenv("META_BASE_URL")
                         or "https://api.meta.ai/v1").strip().rstrip("/")
        self.wonder_model = os.getenv("MUSE_WONDER_MODEL", WONDER).strip() or WONDER
        self.spark_model = os.getenv("MUSE_SPARK_MODEL", SPARK).strip() or SPARK
        try:
            self.timeout = float(os.getenv("MUSE_TIMEOUT_S", "180"))
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError("invalid timeout")
        except ValueError:
            self.timeout = 180.0
        self.force_cache = os.getenv("DEMO_FALLBACK_CACHE", "false").strip().lower() in {
            "true", "1", "yes", "on",
        }
        self.used_fallback_cache = False
        self.model_routing = {"spark_multimodal": False, "image_bytes": 0, "spark_errors": [],
                              "wonder_live": False, "spark_live": False,
                              "fallback_reason": None, "payload_chars": {"wonder": [], "spark": []}}
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
        result = re.sub(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+", "[IMAGE REDACTED]", str(value))
        for secret in (self.api_key, os.getenv("MUSE_API_KEY"), os.getenv("MODEL_API_KEY"),
                       self.base_url, os.getenv("MUSE_BASE_URL"), os.getenv("META_BASE_URL")):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        result = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", result)
        return re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)([^\"'\r\n,}]+)",
                      r"\1[REDACTED]", result)

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

    def _request(self, part, model, prompt, payload, fallback, validate, request_timeout=None, diagram_image=None):
        try:
            key = self.api_key.lower()
            if (self.force_cache or not key or not self.base_url
                    or key in {"none", "null", "changeme", "placeholder", "test", "dummy"}
                    or any(marker in key for marker in (
                        "your_", "your-", "replace", "example", "placeholder", "<", ">",
                    ))):
                raise ValueError("offline mode or missing/placeholder Muse configuration")
            # Compact only after the live configuration gate. Cache responses retain
            # the original report, and budget errors use the normal safe fallback.
            report = payload["prover_report"]
            view = counterexample_view(report)
            if "import_error" in report:
                view["import_error"] = report["import_error"]
            payload = {**payload, "prover_report": json.loads(
                self.safe_error(json.dumps(view)))}
            body = {"model": model, "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": prompt},
                                 {"role": "user", "content": json.dumps(payload)}]}
            with self._routing_lock:
                self.model_routing["payload_chars"][part].append(len(json.dumps(body)))
            options = {"timeout": request_timeout} if request_timeout is not None else {}

            def attempt(image, response_format=True):
                request_body = deepcopy(body)
                if image:
                    request_body["messages"][1]["content"] = [
                        {"type": "text", "text": json.dumps(payload)},
                        {"type": "image_url", "image_url": {"url": image}},
                    ]
                if not response_format:
                    request_body.pop("response_format", None)
                response = self.api_request("POST", "chat/completions", json=request_body, **options)
                content = response["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*\n?", "", content, count=1,
                                     flags=re.IGNORECASE)
                    content = re.sub(r"\s*```$", "", content)
                result = json.loads(content)
                if part == "wonder" and isinstance(result, dict) and isinstance(result.get("patched_code"), str):
                    self._wonder_response_source = result["patched_code"]
                    self._wonder_response_model = response.get("model")
                if not validate(result):
                    raise ValueError("model response violates the structured output contract")
                return result

            def record_error(exc):
                # JSON errors can retain the entire response document. Record only
                # their type; sanitize every other error before retaining it.
                reason = type(exc).__name__ if isinstance(exc, json.JSONDecodeError) else self.safe_error(exc)
                self.model_routing["spark_errors"].append(reason)

            if diagram_image:
                try:
                    result = attempt(diagram_image)
                    self.model_routing["spark_multimodal"] = True
                except Exception as first:
                    record_error(first)
                    status = getattr(getattr(first, "response", None), "status_code", None)
                    is_4xx = (status is not None and 400 <= status < 500) or re.search(r"\bHTTP 4\d\d\b", str(first))
                    try:
                        if not is_4xx:
                            raise first
                        result = attempt(diagram_image, response_format=False)
                        self.model_routing["spark_multimodal"] = True
                    except Exception as second:
                        if second is not first:
                            record_error(second)
                        try:
                            result = attempt(None)
                        except Exception as last:
                            record_error(last)
                            raise
            else:
                result = attempt(None)
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

    def _evidence_summary(self, report, import_error=None):
        """Bound untrusted evidence without retaining source, SQL, or headers.

        All failed check IDs are retained; property samples are capped and their
        omitted count is explicit. Full schedules rerun on every candidate.
        """
        properties = {}
        execution_errors = []
        cells = report.get("cells", [])
        for cell in cells:
            for violation in cell.get("property_violations", cell.get("state_diff", [])):
                field = self.safe_error(violation["field"])[:160]
                if field == "execution.errors":
                    execution_errors.extend(self.safe_error(error)[:300]
                                            for error in violation["actual"][:3])
                    continue
                if field not in properties:
                    properties[field] = {"field": field, "cell_ids": [],
                                         "expected": self.safe_error(str(violation.get("expected")))[:240],
                                         "actual": self.safe_error(str(violation.get("actual")))[:240]}
                properties[field]["cell_ids"].append(cell["id"])
        return {
            "role": "synthesis_feedback", "scenario": "legacy", "seed": None,
            "failed_check_ids": [cell["id"] for cell in cells if not cell["passed"]],
            "business_properties": list(properties.values())[:16],
            "omitted_property_count": max(0, len(properties) - 16),
            "execution_errors": execution_errors[:3],
            "import_error": self.safe_error(import_error)[:300] if import_error else None,
        }

    def _audit_candidate(self, code, provenance, round_number, verification=None, error=None):
        report = (verification or {}).get("prover_report") or {}
        summary = self._evidence_summary(report, (verification or {}).get("import_error"))
        cells = report.get("cells", [])
        prior = self.model_routing["regression_evidence"]
        previous_ids = sorted({check for item in prior for check in item["failed_check_ids"]})
        accepted = bool((verification or {}).get("verified")) and error is None
        record = {
            "attempt_index": len(self.model_routing["candidate_attempts"]) + 1,
            "cegis_round": round_number, "provenance": provenance,
            "model_identifier": self.safe_error(self.wonder_model) if provenance == "live_model_response" else None,
            "response_model_identifier": (self.safe_error(self._wonder_response_model)[:160]
                                          if provenance == "live_model_response" and self._wonder_response_model else None),
            "candidate_sha256": sha256(code.encode("utf-8")).hexdigest(),
            "contract_sha256": self.model_routing["verification_scope"]["contract_sha256"],
            "configured_check_ids": list(range(1, 28)),
            "executed_check_ids": [cell["id"] for cell in cells],
            "passed_check_ids": [cell["id"] for cell in cells if cell["passed"]],
            "failed_check_ids": [cell["id"] for cell in cells if not cell["passed"]],
            "business_property_violations": summary["business_properties"],
            "omitted_property_count": summary["omitted_property_count"],
            "execution_errors": summary["execution_errors"], "import_error": summary["import_error"],
            "verification_error": self.safe_error(error)[:300] if error else None,
            "feedback_evidence_indices": [len(prior) - 1] if provenance == "live_model_response" else [],
            "feedback_properties": sorted({p["field"] for item in prior[-1:] for p in item["business_properties"]})
                                   if provenance == "live_model_response" else [],
            "regression_check_ids": previous_ids,
            "regression_passed_check_ids": [cell["id"] for cell in cells
                                             if cell["id"] in previous_ids and cell["passed"]],
            "result": "accepted" if accepted else "rejected",
            "autonomous_repair": accepted and provenance == "live_model_response",
        }
        self.model_routing["candidate_attempts"].append(record)
        if not accepted:
            summary["candidate_sha256"] = record["candidate_sha256"]
            self.model_routing["regression_evidence"].append(summary)
        self.model_routing["selected_candidate"] = deepcopy(record) if accepted else None

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

        payload = {"schema_code": schema_code, "handler_code": handler_code,
                   "prover_report": prover_report,
                   "handler_contract": "process_stripe_webhook(session, raw_body, sig_header) -> dict\n" + WONDER_PROMPT.split(
                       "The handler_contract is an interface description:", 1)[1].strip()}
        # Audit fields are additive; legacy cegis_rounds continues to contain
        # only actual live candidate verifications, never fallback selections.
        from chaoshook_engine.scenario_spec import SCENARIO_SPEC

        self.model_routing.update(
            cegis_rounds=[], candidate_attempts=[], selected_candidate=None,
            regression_evidence=[self._evidence_summary(prover_report)],
            verification_scope={
                "acceptance": "legacy_27_cells", "synthesis_mode": "guided",
                "contract_sha256": sha256(json.dumps(asdict(SCENARIO_SPEC), sort_keys=True).encode()).hexdigest(),
                "additional_scenario_evaluation": "separate_not_run",
                "mutation_controls": "regression_only_not_acceptance",
                "held_out_checks_executed": [],
            },
        )

        def cached_fallback():
            # Record checks actually executed for this source; never invent a
            # live round or infer current success from an earlier cached proof.
            try:
                verification = verify_candidate_patch(reference)
            except Exception as exc:
                self._audit_candidate(reference, "cached_fallback", None, error=exc)
            else:
                self._audit_candidate(reference, "cached_fallback", None, verification)
            return fallback

        max_rounds = 2
        started = perf_counter()
        time_budget = 240.0
        for round_number in range(1, max_rounds + 1):
            elapsed = perf_counter() - started
            if round_number > 1 and elapsed * 2 > time_budget:
                self._record_route("wonder", False,
                                   "CEGIS round 2 skipped: projected runtime exceeds "
                                   "240-second total time budget; using verified cache")
                return cached_fallback()
            prompt = WONDER_PROMPT
            if round_number > 1:
                prompt += ("\nRepair your own previous patch in handler_code using the "
                           "remaining counterexamples and import_error in prover_report. "
                           "Return the same strict JSON contract.")
            self._wonder_response_source = None
            self._wonder_response_model = None
            candidate = self._request("wonder", self.wonder_model, prompt,
                                      payload, fallback, valid,
                                      request_timeout=min(self.timeout, time_budget - elapsed))
            # _request handles forced cache, unusable keys and network failures.
            if not self.model_routing["wonder_live"]:
                if self._wonder_response_source is not None:
                    self._audit_candidate(self._wonder_response_source, "live_model_response", round_number,
                                          error="structured output rejected before verification")
                return cached_fallback()
            try:
                verification = verify_candidate_patch(candidate["patched_code"])
            except Exception as exc:
                self._audit_candidate(candidate["patched_code"], "live_model_response", round_number, error=exc)
                self._record_route("wonder", False,
                                   "candidate verification failed: " + self.safe_error(exc))
                return cached_fallback()
            self._audit_candidate(candidate["patched_code"], "live_model_response", round_number, verification)
            import_error = (self.safe_error(verification["import_error"])[:1000]
                            if verification["import_error"] else None)
            self.model_routing["cegis_rounds"].append({
                "round": round_number, "passed_cells": verification["passed_cells"],
                "failed_cell_ids": verification["failed_cell_ids"],
                "verified": verification["verified"], "import_error": import_error,
                "payload_chars": self.model_routing["payload_chars"]["wonder"][-1],
            })
            if verification["verified"]:
                return candidate
            payload["handler_code"] = candidate["patched_code"]
            payload["prover_report"] = {
                **(verification["prover_report"] or {}), "import_error": import_error,
            }
        self._record_route("wonder", False,
                           f"CEGIS exhausted {max_rounds} rounds without a verified patch; "
                           "using verified cache")
        return cached_fallback()

    def diagnose_multimodal_divergence(self, prover_report: dict,
                                      diagram_description: str | None = None,
                                      diagram_image: str | None = None) -> dict:
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
                    and ("diagram_findings" not in value or (isinstance(value["diagram_findings"], list)
                         and len(value["diagram_findings"]) <= 3
                         and all(isinstance(f, str) for f in value["diagram_findings"])))
                    and isinstance(value.get("failure_clusters"), list)
                    and len(value["failure_clusters"]) == 3
                    and all(isinstance(c, dict)
                            and all(isinstance(c.get(k), str) and c[k]
                                    for k in ("title", "description"))
                            and isinstance(c.get("cells"), list)
                            and all(type(cell) is int and 1 <= cell <= 27
                                    for cell in c["cells"])
                            for c in value["failure_clusters"]))

        self.model_routing["spark_multimodal"] = False
        encoded_image = diagram_image.split(",", 1)[-1] if diagram_image else ""
        self.model_routing["image_bytes"] = len(encoded_image.rstrip("=")) * 3 // 4
        return self._request("spark", self.spark_model, SPARK_PROMPT,
                             {"prover_report": prover_report,
                              "diagram_description": diagram_description}, fallback, valid,
                             diagram_image=diagram_image)

    def run_full_neuro_symbolic_analysis(self, prover_report: dict,
                                        diagram_description: str | None = None,
                                        diagram_image: str | None = None) -> dict:
        started = perf_counter()
        self.used_fallback_cache = False
        self._fallback_reasons.clear()
        self.model_routing.update(wonder_live=False, spark_live=False, fallback_reason=None,
                                  payload_chars={"wonder": [], "spark": []},
                                  spark_multimodal=False, image_bytes=0, spark_errors=[])
        schema = (HANDLER_DIR.parent / "models.py").read_text(encoding="utf-8")
        handler = (HANDLER_DIR / "active_handler.py").read_text(encoding="utf-8")
        if self.force_cache:
            wonder = self.extract_invariants_and_patch(schema, handler, prover_report)
            diagnosis = self.diagnose_multimodal_divergence(prover_report, diagram_description, diagram_image)
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="Muse") as pool:
                wonder_future = pool.submit(
                    self.extract_invariants_and_patch, schema, handler, prover_report,
                )
                spark_future = pool.submit(
                    self.diagnose_multimodal_divergence, prover_report, diagram_description, diagram_image,
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
