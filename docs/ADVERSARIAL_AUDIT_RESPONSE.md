# Response to the adversarial oracle audit

The independent evaluation rated ChaosHook 7.5/10 and found that a handler
forcing the original canceled endpoint could pass 27/27 legacy checks and
81/81 seeded checks. Varying identifiers alone did not vary the required business
outcome. This was a limitation of the acceptance oracle, not evidence of correct
causal behavior.

## Reproduction and observed results

`HARDCODED_CANCELLATION_PROBE` in `tests/test_gate.py` is test-only source. It
retains the prepared handler's actual signature verification, atomic event
claims, duplicate prevention, invoice attribution, credits and outbox behavior,
then forces the relevant user's plan and any existing subscription to canceled.
It does not import engine code into the target application. It reproduces the
reported behavioral weakness; it is not asserted to be the evaluator's exact
source. The regression executes the old gate and seeds 101, 202, 303: the probe
passes 27/27 and 81/81 respectively.

The new contract is `novacloud-causal-counterfactual-v1`, with evidence scope
`semantic_permutations_prefixes_and_sequential_duplicates`. Observed results:

| History | Families | Schedules | State checks | Prepared passed | Probe passed |
| --- | --- | ---: | ---: | ---: | ---: |
| Original canceled, one paid invoice | A, E | 24 | 216 | 216 | 144 |
| Active, one paid invoice, no deletion | B, E | 6 | 42 | 42 | 6 |
| Active, no paid invoice | C, E | 2 | 10 | 10 | 2 |
| Canceled, no paid invoice | C, E | 6 | 42 | 42 | 30 |
| Active, two distinct paid invoices | D, E | 24 | 216 | 216 | 24 |
| Total | A–E | 62 | 526 | 526 | 206 |

All permutations of each history execute against separate real SQLite databases.
A state check observes the entire business state at S0, after each individual
event prefix, and again after immediately replaying that same event. Thus each
n-event schedule executes 1 + 2n state checks. Individual field comparisons are
not counted as additional checks. E is exercised within every history, not
counted as an extra scenario. These are **62 schedules / 526 checks**, not five
27-cell results.

The prepared repair qualifies. The probe fails strict qualification. Its final
B checks fail precisely `users.plan_tier` and `subscriptions.status`; there are
no handler execution errors anywhere in its semantic run. Its early A failures
also expose premature cancellation that the final-only legacy oracle misses.
The probe's other failing checkpoints are reported as observed, not as extra
invented mutations. The prepared handler's source is unchanged, SHA-256:
`a7a480c713d09c0bf8d73d6239138700dcd7cb66b8802cd3715ba8455a9d1694`.

## Independent expectations

The oracle in `scenario_spec.py` computes obligations from the delivered event
set and the initial state, without calling any repaired/reference handler.
Latest relevant synthetic lifecycle facts determine the plan; subscription facts
alone justify a subscription row. Checkout justifies a welcome message. Each
actual distinct eligible paid invoice justifies its own attributed 100-credit
ledger row, and the balance is initial balance plus those deltas. Empty prefixes
and invoice-only prefixes cannot justify subscription or welcome effects.

The D fixtures have distinct event IDs and distinct invoice IDs; both ledger
identities, individual deltas, reasons, ownership and the balance are compared.
C includes cancellation without any invoice, so cancellation cannot justify
credits. Every observation checks all users, primary identity, all bystander
columns, all subscription identities/ownership, every ledger entry and every
outbox recipient/template. Surrogate IDs and primary implementation watermarks
are excluded. Immediate duplicates must leave these business observations
unchanged. Expectations are not obtained from candidate SQL or code inspection.

## Integration and reproduction

Run without model calls or installation:

```bash
.venv/bin/python -m chaoshook_engine.cli qualify-semantic \
  --source target_app/handlers/v2_patched.py --manifest /tmp/strict-evidence.json
.venv/bin/python -m pytest -q
```

The deterministic manifest binds source and oracle hashes and per-history
fixture hashes. `runtime_acceptance` and its existing schedule count retain
legacy scope. `strict_semantic` contains its own counts, ordered schedules,
checkpoint IDs, property names and sanitized execution error codes.
`strict_qualification.qualified` requires both legacy acceptance and all semantic
checks. An import failure executes no semantic checks and never qualifies.
Repeated CLI exports are byte-identical in the regression.

The existing seeded `qualify` command, legacy runner, Muse calls and React flow
are unchanged. Installation keeps its existing default. Python callers can opt
in with `apply_patch_to_active(code, require_strict_semantic=True)`; rejection
occurs before writing the active file. The unchanged legacy gate still requires
its SQL barrier race and both crash/retry checks. The semantic scope adds
sequential duplicate checks, not new concurrent or crash coverage, and does not
claim such coverage for B–D.

## Limits and the synthetic watermark assumption

This is bounded causal verification, not proof over arbitrary Stripe histories.
It covers one relevant account/subscription plus an untouched bystander, fixed
100-credit eligible invoices, distinct increasing lifecycle timestamps, and
finite histories. It does not cover timestamp ties, multiple subscriptions,
refunds, failed payments, arbitrary invoice eligibility, a second event ID for
the same invoice, arbitrary retry timing, external email delivery, or every
crash location. A candidate tailored to these public fixtures can still pass.
Claims of held-out generalization are not made.

The synthetic contract explicitly treats distinct lifecycle timestamps as an
authoritative monotonic ordering, including a shared plan watermark across
checkout and subscription events. This is a scenario assumption, not Stripe's
real ordering guarantee. Stripe does not guarantee event delivery order and
explicitly warns that snapshot event `created` timestamps have second precision,
can tie, and should not determine event order. Stripe recommends tracking event
IDs for duplicate deliveries and retrieving missing objects via its API. See
[Stripe event ordering](https://docs.stripe.com/webhooks#event-ordering) and
[duplicate handling](https://docs.stripe.com/webhooks#handle-duplicate-events).
Production reconciliation needs authoritative object state/version semantics;
this suite does not establish that timestamp watermarks alone are sufficient.

The audit response is explicitly included by `.gitignore` and added to Git so
normal Git-aware repository snapshots (including Repomix using Git ignore
rules) can include it.

## Verification record

The full Python suite completed: **140 passed**, with one existing
Starlette/httpx deprecation warning. The initial sandboxed run stalled at the
existing FastAPI TestClient test; the complete run outside the sandbox finished
in 103.25 seconds. Model requests in the relevant existing tests are mocked;
no live Muse models were called. Separate CLI verification confirmed vulnerable
6/27 and prepared 27/27; the partial handler separately confirmed 9/27.
`git diff --check` and the staged whitespace check passed. No commit or push
was performed.
