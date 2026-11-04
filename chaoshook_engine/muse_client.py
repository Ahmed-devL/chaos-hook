"""Muse model routing with a deterministic, locally verified demo cache."""

from copy import deepcopy
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
        self.model_routing["cegis_rounds"] = []
        max_rounds = 2
        started = perf_counter()
        time_budget = 240.0
        for round_number in range(1, max_rounds + 1):
            elapsed = perf_counter() - started
            if round_number > 1 and elapsed * 2 > time_budget:
                self._record_route("wonder", False,
                                   "CEGIS round 2 skipped: projected runtime exceeds "
                                   "240-second total time budget; using verified cache")
                return fallback
            prompt = WONDER_PROMPT
            if round_number > 1:
                prompt += ("\nRepair your own previous patch in handler_code using the "
                           "remaining counterexamples and import_error in prover_report. "
                           "Return the same strict JSON contract.")
            candidate = self._request("wonder", self.wonder_model, prompt,
                                      payload, fallback, valid,
                                      request_timeout=min(self.timeout, time_budget - elapsed))
            # _request handles forced cache, unusable keys and network failures.
            if not self.model_routing["wonder_live"]:
                return fallback
            try:
                verification = verify_candidate_patch(candidate["patched_code"])
            except Exception as exc:
                self._record_route("wonder", False,
                                   "candidate verification failed: " + self.safe_error(exc))
                return fallback
            import_error = (self.safe_error(verification["import_error"])
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
        return fallback

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
