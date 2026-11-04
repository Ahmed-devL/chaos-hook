export const EVENT_NAMES = {
  e1: 'checkout.session.completed',
  e2: 'customer.subscription.created',
  e3: 'invoice.paid',
  e4: 'customer.subscription.deleted',
};

export function cellState(cell) {
  if (cell.passed) return 'pass';
  return cell.property_violations?.some((v) => v.field === 'execution.errors') ? 'error' : 'fail';
}

const CODES = { DOUBLE_CREDIT_RACE: 'CREDIT×2', DOUBLE_CREDIT_RETRY: 'CREDIT×2', DUPLICATE_WELCOME_EMAIL: 'EMAIL×2', ORPHAN_SUB_DROP: 'ORPHAN', ZOMBIE_ACTIVE_SUB: 'ZOMBIE', STALE_PLAN_OVERWRITE: 'STALE', HANDLER_ERROR: 'EXEC' };
const FAULT_NAMES = { 25: 'Concurrent race', 26: 'Invoice crash / retry', 27: 'Checkout crash / retry' };

function CellTile({ cell, selected, onSelect, fault }) {
  const state = cellState(cell);
  return <button type="button" className={`matrix-cell ${state} ${selected ? 'selected' : ''} ${fault ? 'fault-cell' : ''}`}
    aria-label={`Cell #${cell.id}: ${cell.label}, ${state.toUpperCase()}`}
    aria-pressed={selected} title={cell.failure_tag || cell.label} onClick={() => onSelect(cell.id)}>
    <span className="cell-top"><span className="cell-number">{String(cell.id).padStart(2, '0')}</span><span className={`state-label ${state}`}>{state === 'pass' ? '✓' : state === 'error' ? '!' : '×'} {state.toUpperCase()}</span></span>
    <span className="cell-order">{fault ? FAULT_NAMES[cell.id] : cell.sequence.map((e) => e.replace(/^e/, '')).join('›')}</span>
    <span className="cell-code">{cell.passed ? 'MATCH' : CODES[cell.failure_tag] ?? cell.failure_tag ?? 'DIVERGED'}</span>
  </button>;
}

export default function ProofMatrixGrid({ cells, selectedId, onSelect }) {
  return <div className="matrix-content">
    <div className="section-label"><h3>Arrival order</h3><span>4! · 24 permutations</span></div>
    <div className="permutation-grid" aria-label="Event permutation checks">
      {cells.filter((c) => c.id <= 24).map((cell) => <CellTile key={cell.id} cell={cell} selected={cell.id === selectedId} onSelect={onSelect} />)}
    </div>
    <div className="section-label fault-label"><h3>Fault injection</h3><span>Real SQL · isolated snapshots</span></div>
    <div className="fault-grid" aria-label="Concurrency and crash retry checks">
      {cells.filter((c) => c.id >= 25).map((cell) => <CellTile key={cell.id} cell={cell} selected={cell.id === selectedId} onSelect={onSelect} fault />)}
    </div>
    <section className="event-legend" aria-label="Event identifier legend">
      <h3>Event key <span>1›2›3›4 = e1 → e2 → e3 → e4</span></h3>
      {Object.entries(EVENT_NAMES).map(([id, name]) => <div key={id}><code>{id}</code><span>{name}</span></div>)}
    </section>
    <p className="matrix-note">Select a check to inspect its state and SQL evidence. <span className="selection-key">▸</span> Cyan marks the current selection.</p>
  </div>;
}

export function candidateSource(provenance) {
  return ({ live_model_response: 'Live Muse response', cached_fallback: 'Cached fallback', prepared: 'Prepared handler' })[provenance] ?? 'Source not recorded';
}

export function shortHash(value) { return value ? `${value.slice(0, 12)}…` : 'Not recorded'; }

export function VerificationEvidence({ report, evidence, admission, busy }) {
  return <section className="evidence-strip" aria-label="Current verification evidence" aria-busy={busy}>
    <p className="proof-score" role="status" aria-label="Configured verification check score"><strong className={report ? report.passed_cells === report.total_cells ? 'pass' : 'fail' : ''}>{report ? `${report.passed_cells}/${report.total_cells}` : '—'}</strong> configured checks passed</p>
    <span className="strip-stat"><b className={report && report.total_cells > report.passed_cells ? 'fail' : ''}>{report ? report.total_cells - report.passed_cells : '—'}</b> failed</span>
    <span className="strip-stat"><b className={evidence?.execution_error_cells ? 'warning' : ''}>{evidence?.execution_error_cells ?? '—'}</b> execution / sync errors</span>
    <div className="property-strip" aria-label="Business properties">
      {(evidence?.business_properties ?? []).map((p) => <span key={p.id} className={p.failed ? 'fail' : p.not_evaluated || !p.passed ? 'warning' : 'pass'} title={`${p.label}: ${p.passed} passed, ${p.failed} failed, ${p.not_evaluated} not evaluated`}>{p.failed ? '×' : p.not_evaluated || !p.passed ? '·' : '✓'} {p.id}</span>)}
      {!evidence && <span>Properties: Not evaluated</span>}
    </div>
    <span className={`admission-summary ${admission?.passed === false ? 'fail' : ''}`}>Auth: {admission ? admission.passed ? 'passed' : 'failed' : 'Not evaluated'}</span>
    <code className="strip-hash" title={evidence?.candidate_sha256}>sha {shortHash(evidence?.candidate_sha256)}</code>
  </section>;
}

export function CandidateEvidence({ evidence, audit, admission }) {
  const attempts = audit?.candidate_attempts ?? [];
  return <section className="audit-content" aria-label="Candidate and audit evidence">
    <h3>Active source evidence</h3>
    <p>{candidateSource(evidence?.provenance)}</p>
    <dl className="evidence-list"><dt>Candidate SHA-256</dt><dd><code>{evidence?.candidate_sha256 ?? 'Not recorded'}</code></dd><dt>Contract SHA-256</dt><dd><code>{evidence?.contract_sha256 ?? 'Not recorded'}</code></dd><dt>Verification scope</dt><dd>{evidence?.scope ?? 'Not evaluated'}</dd><dt>Strict qualification / held-out / mutation</dt><dd>Not evaluated in this view</dd></dl>
    <h3>Business-property evidence</h3>
    {evidence?.business_properties ? <ul className="business-evidence">{evidence.business_properties.map((property) => <li key={property.id}><p>{property.label}</p><p className={property.failed ? 'fail' : property.not_evaluated ? 'warning' : 'pass'}>{property.passed} passed · {property.failed} failed · {property.not_evaluated} not evaluated</p></li>)}</ul> : <p>Not evaluated</p>}
    <h3>Authentication admission <span className={admission?.passed ? 'pass' : admission ? 'fail' : ''}>{admission ? admission.passed ? '✓ Passed' : '× Failed' : 'Not evaluated'}</span></h3>
    {admission && <><p>{admission.passed_check_count}/{admission.configured_check_count} passed · {admission.executed_check_count} executed. Separate from business-contract checks.</p><ul className="admission-checks">{admission.checks?.map((check) => <li key={check.id} className={check.passed ? '' : 'fail'}><code>{check.id}</code><span>{check.passed ? '✓ Pass' : '× Fail'}</span></li>)}</ul></>}
    <h3>Latest synthesis attempts <span>{attempts.length} recorded</span></h3>
    <p className="muted">Candidate evaluation is separate from the installed source. These are current session attempts; historical experiment trials are not included.</p>
    {!attempts.length && <p>No synthesis has been recorded in this session.</p>}
    <ol className="attempt-history" aria-label="Candidate attempt history">{attempts.map((attempt) => <li key={attempt.attempt_index}>
      <h4>Attempt {attempt.attempt_index} <span className={attempt.result === 'accepted' ? 'pass' : 'fail'}>{attempt.result === 'accepted' ? 'Accepted by configured checks' : attempt.result === 'rejected' ? 'Rejected' : 'Outcome not recorded'}</span></h4>
      <p>{candidateSource(attempt.provenance)}{attempt.cegis_round != null ? ` · Round ${attempt.cegis_round}` : ''}{attempt.model_identifier ? ` · ${attempt.model_identifier}` : ''}</p>
      <p>{Array.isArray(attempt.executed_check_ids) && Array.isArray(attempt.passed_check_ids) && Array.isArray(attempt.configured_check_ids) ? `${attempt.passed_check_ids.length}/${attempt.configured_check_ids.length} passed · ${attempt.executed_check_ids.length} executed` : 'Checks not recorded'}</p>
      <p>Candidate <code title={attempt.candidate_sha256}>{shortHash(attempt.candidate_sha256)}</code> · Contract <code title={attempt.contract_sha256}>{shortHash(attempt.contract_sha256)}</code></p>
      <p className={attempt.authentication_admission?.passed === false ? 'fail' : ''}>Authentication admission: {attempt.authentication_admission ? attempt.authentication_admission.passed ? 'Passed' : 'Failed' : 'Not evaluated'}</p>
      {!!attempt.business_property_violations?.length && <p className="fail">Violated properties: {attempt.business_property_violations.map((p) => p.field).join(', ')}{attempt.omitted_property_count ? ` (+${attempt.omitted_property_count} omitted)` : ''}</p>}
      {(attempt.import_error || attempt.verification_error || !!attempt.execution_errors?.length) && <p className="warning">{attempt.import_error ? 'Import failed; checks were not run.' : attempt.verification_error ? 'Verification did not complete.' : 'Execution errors were recorded.'}</p>}
      {!!attempt.feedback_properties?.length && <p>Synthesis feedback: {attempt.feedback_properties.join(', ')}</p>}
    </li>)}</ol>
    <p className="muted">Cached selections are not live CEGIS rounds. Finite configured checks do not establish general correctness.</p>
  </section>;
}
