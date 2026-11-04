
## Parameterized verification and evaluation provenance

`LifecycleScenarioSpec` is the immutable input and business contract for one
four-event lifecycle. `scenario_from_seed(seed, bystander=True)` deterministically
varies user/customer/subscription/invoice/checkout/event IDs, increasing event
timestamps, period end, and a nonzero initial balance. The bystander starts with
37 credits. Seed 101, 202, and 303 form the small default evaluation collection;
these are reproducible examples, not secret inputs. Callers may supply fresh
integer seeds. `run_algebraic_prover(handler, scenario)` executes the same 27
isolated real-SQL schedules. Calling it without a scenario retains the original
legacy matrix and JSON state fields. Expected credits are always initial balance
plus the frozen 100-credit invoice effect; no handler produces expected state.

Legacy metrics remain UI compatible. Additional oracle violations check all user
identities, primary email/customer identity, every bystander column, all
subscription identities and ownership, and ledger/outbox attribution. Unexpected
accounts, subscriptions, ledger rows, and messages fail acceptance. Entity-level
violations appear in each cell's existing `property_violations` and `state_diff`.

`evaluate_candidate_patch(source, seeds=(101, 202, 303), history=None)` imports a
frozen source copy outside the repository and returns **separate** evaluation
reports, a source SHA-256, scenario inputs, and exact schedule coverage. It never
installs code or calls Muse. All 27 schedules run for **each** additional seed:
24 permutations, the duplicate SQL barrier race, and both statement crash retries.
The original synthesis/installation gate stays the legacy 27-cell gate; additional
evaluation is explicit and is not automatically fed into candidate selection.

Without provenance, results are labeled `evaluation`. To claim held-out coverage,
retain one `EvaluationHistory(synthesis_seeds=..., retired_seeds=...)` across the
entire repair session and supply the complete prior input history. Only fresh
seeds receive `held-out`. Every executed evaluation seed is consumed immediately,
including failed cases, so subsequent evaluations cannot reuse that label. If a
failure becomes repair feedback, call `history.record_feedback([seed])`; it is
explicitly retired, and use fresh seeds afterward. A held-out label describes the
input's status at that evaluation, not secrecy or unrestricted correctness.

CLI example (no Muse requests):

```bash
python -m chaoshook_engine.cli evaluate --source target_app/handlers/v2_patched.py --seed 101 --seed 202 --seed 303
```

For held-out labeling, pass `--history /tmp/evaluation-history.json`, an existing
JSON document with `used_seeds` and `retired_seeds` arrays declaring the complete
prior synthesis/selection history. The CLI reserves all requested seeds in it before execution, including on interruption. Add
any feedback seed to both arrays before the next repair evaluation. Preserve the
file between candidates; do not reset history to obtain fresh labels. The caller
is responsible for truthful provenance, including external AI exposure; the tool
cannot inspect external synthesis or selection. CLI history is for sequential
local runs, not concurrent evaluation processes. This finite synthetic suite proves only executed scenario/schedule combinations.


## Invoice business-effect idempotency

The prepared handler first claims `event.id` in `webhook_events` within
`session.begin()`. That write acquires SQLite's writer lock before any business
state reads. For `invoice.paid`, a single `INSERT ... SELECT ... WHERE NOT EXISTS`
inserts a positive 100-credit ledger effect only if the invoice ID has no
`invoice.paid` entry. Its affected-row count controls the balance increment.
Invoice identity is global in the synthetic single-provider/account model; user
ID deliberately does not partition deduplication. The ledger records the resolved
customer's user ID. New delivery IDs for an already credited invoice still commit
processed event claims.

This is an atomic uniqueness protocol, not an ORM read-before-write check: the
claim write holds the writer lock through predicate evaluation, ledger insertion,
balance update, and commit. Competing writers must wait, then see committed ledger
state. Rollback removes both the claim and effects; a postcommit redelivery sees
the durable claim or invoice effect. SQL errors propagate and roll back normally;
no blanket integrity-error suppression is used. Welcome and lifecycle handling
are unchanged. All creation paths continue using the same `Base.metadata` schema;
no migration or shared unique index alters deliberately vulnerable controls.

The additive `invoice-business-idempotency-v1` tests in `tests/test_gate.py` use the
unchanged `semantic_expected_state` oracle and real temporary WAL databases.
They cover all 120 permutations of the five-event 853-to-953 audit history and
1,320 full business-state plus event-claim checkpoints. Nine supplementary tests
exercise distinct invoices in both orders, two SQL-barrier races, three rollback
boundaries, lost acknowledgment after durable commit, and an actual SQL error.
Synchronization and injection assertions fail separately from state assertions.
Including the supplementary tests, coverage is 129 isolated histories and 1,341
complete state-and-claim checkpoints, excluding instrumentation assertions.
The 62/526 suite, legacy acceptance, and evidence schemas retain their meanings.

The guarantee applies to this handler's SQLite write protocol. It does not
constrain arbitrary ledger writers, repair preexisting duplicate grants, establish
multi-provider invoice namespaces, or prove filesystem/power-loss durability.
SQLite serializes writers; portability to databases with different isolation
requires a unique business-effect key or equivalent locking. Existing timestamp
ordering assumptions and finite-coverage limitations remain in force.
