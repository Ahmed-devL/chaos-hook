function CellTile({ cell, selected, onSelect, large = false }) {
  return (
    <button
      type="button"
      aria-label={`Cell #${cell.id}: ${cell.label}, ${cell.passed ? 'PASS' : 'FAIL'}`}
      aria-pressed={selected}
      onClick={() => onSelect(cell.id)}
      className={`min-w-0 rounded-lg border p-3 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brass ${
        cell.passed ? 'border-emerald/40 bg-emerald/5 hover:bg-emerald/10' : 'border-crimson/40 bg-crimson/5 hover:bg-crimson/10'
      } ${selected ? 'outline outline-2 outline-offset-2 outline-brass' : ''}`}
    >
      <span className="mb-3 flex items-start justify-between gap-2">
        <span className={large ? 'text-sm font-semibold' : 'font-mono text-sm text-slate-300'}>
          {large ? `Cell #${cell.id}: ${cell.label}` : `#${cell.id}`}
        </span>
        <span className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-xs font-bold ${cell.passed ? 'border-emerald/40 text-emerald' : 'border-crimson/40 text-crimson'}`}>
          {cell.passed ? 'PASS' : 'FAIL'}
        </span>
      </span>
      <span className="block font-mono text-xs leading-relaxed text-slate-300">{cell.sequence.join(' → ')}</span>
      {!cell.passed && (
        <span className="mt-3 block break-words font-mono text-xs text-crimson">{cell.failure_tag}</span>
      )}
    </button>
  );
}

export default function ProofMatrixGrid({ cells, selectedId, onSelect }) {
  const permutations = cells.filter((cell) => cell.id >= 1 && cell.id <= 24);
  const faultCells = cells.filter((cell) => cell.id >= 25 && cell.id <= 27);

  return (
    <section aria-labelledby="matrix-heading" className="min-w-0 space-y-8 rounded-lg border border-slate-800 bg-slate-900 p-5">
      <h2 id="matrix-heading" className="text-lg font-semibold">27-Cell Proof Matrix</h2>
      <section aria-labelledby="permutations-heading">
        <h3 id="permutations-heading" className="mb-4 text-sm font-medium text-slate-300">Section A · 4! = 24 Order Commutativity Permutations</h3>
        <div className="overflow-x-auto p-1">
          <div className="grid min-w-[560px] grid-cols-4 gap-3">
            {permutations.map((cell) => (
              <CellTile key={cell.id} cell={cell} selected={cell.id === selectedId} onSelect={onSelect} />
            ))}
          </div>
        </div>
      </section>
      <section aria-labelledby="faults-heading">
        <h3 id="faults-heading" className="mb-4 text-sm font-medium text-slate-300">Section B · Parallel Concurrency &amp; Crash-Retry Cells</h3>
        <div className="grid gap-4 p-1">
          {faultCells.map((cell) => (
            <CellTile key={cell.id} cell={cell} selected={cell.id === selectedId} onSelect={onSelect} large />
          ))}
        </div>
      </section>
    </section>
  );
}

export function candidateSource(provenance) {
  return ({ live_model_response: 'Live Muse response', cached_fallback: 'Cached fallback', prepared: 'Prepared handler' })[provenance] ?? 'Source not recorded';
}

function shortHash(value) {
  return value ? `${value.slice(0, 12)}…` : 'Not recorded';
}

export function VerificationEvidence({ report, evidence, audit, busy }) {
  const attempts = audit?.candidate_attempts ?? [];
  const rejected = attempts.filter((attempt) => attempt.result === 'rejected').length;
  return <section aria-labelledby="verification-heading" aria-busy={busy} className="mx-auto mb-5 max-w-[2400px] rounded-lg border border-brass/30 bg-slate-900 p-5">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h2 id="verification-heading" className="text-lg font-semibold">Verification evidence</h2>
        <p className="mt-1 text-xs text-slate-400">Active handler · configured schedules and declared business outcomes only</p>
      </div>
      <p className="font-mono text-sm text-brass">{report ? `${report.passed_cells}/${report.total_cells} configured checks passed` : 'Not evaluated'}</p>
    </div>
    <div className="mt-4 grid gap-5 lg:grid-cols-[1.4fr_1fr]">
      <div>
        <ul className="grid gap-2 sm:grid-cols-2" aria-label="Independent business-property results">
          {(evidence?.business_properties ?? []).map((property) => <li key={property.id} className="rounded border border-slate-800 bg-slate-950 p-3 text-xs">
            <p className="mb-1 text-slate-300">{property.label}</p>
            <p className={property.failed ? 'text-crimson' : property.not_evaluated || !property.passed ? 'text-amber-400' : 'text-emerald'}>
              {property.failed ? `Failed in ${property.failed} checks · ` : ''}{property.passed} passed{property.not_evaluated ? ` · ${property.not_evaluated} unavailable` : ''}
            </p>
          </li>)}
        </ul>
        {!evidence && <p className="mt-2 text-xs text-slate-400">Business properties: Not evaluated</p>}
        {!!evidence?.execution_error_cells && <p className="mt-2 text-xs text-amber-400">Execution errors in {evidence.execution_error_cells} checks; missing outcomes do not count as property passes.</p>}
      </div>
      <div className="space-y-2 text-xs text-slate-400">
        <p className="text-slate-200">Candidate source: {candidateSource(evidence?.provenance)}</p>
        <p>Candidate <code title={evidence?.candidate_sha256} className="text-slate-300">{shortHash(evidence?.candidate_sha256)}</code> · Contract <code title={evidence?.contract_sha256} className="text-slate-300">{shortHash(evidence?.contract_sha256)}</code></p>
        <p>Additional scenarios / held-out: Not evaluated in this view</p>
        <p>These finite checks do not establish general correctness. Property passes require a completed check without execution errors.</p>
        {busy && <p role="status" className="text-brass">Operation in progress; showing the last completed proof.</p>}
      </div>
    </div>
    {!!attempts.length && <details className="mt-4 border-t border-slate-800 pt-3">
      <summary className="cursor-pointer text-sm text-brass">Latest synthesis attempts · {attempts.length} candidate{attempts.length === 1 ? '' : 's'}  · {rejected} rejected (separate from active proof)</summary>
      <ol className="mt-3 space-y-3" aria-label="Candidate attempt history">
        {attempts.map((attempt) => <li key={attempt.attempt_index} className="rounded border border-slate-700 bg-slate-950 p-3 text-xs">
          <div className="flex flex-wrap justify-between gap-2">
            <p className="text-slate-200">Attempt {attempt.attempt_index} · {candidateSource(attempt.provenance)}{attempt.cegis_round != null ? ` · Round ${attempt.cegis_round}` : ''}</p>
            <p className={attempt.result === 'accepted' ? 'text-emerald' : 'text-crimson'}>{attempt.result === 'accepted' ? 'Accepted by configured checks' : attempt.result === 'rejected' ? 'Rejected' : 'Outcome not recorded'}</p>
          </div>
          <p className="mt-1 text-slate-400">{Array.isArray(attempt.executed_check_ids) && Array.isArray(attempt.passed_check_ids) && Array.isArray(attempt.configured_check_ids) ? `${attempt.passed_check_ids.length}/${attempt.configured_check_ids.length} passed · ${attempt.executed_check_ids.length} executed` : 'Checks not recorded'}{attempt.model_identifier ? ` · ${attempt.model_identifier}` : ''}</p>
          <p className="mt-1 text-slate-400">Candidate <code title={attempt.candidate_sha256}>{shortHash(attempt.candidate_sha256)}</code> · Contract <code title={attempt.contract_sha256}>{shortHash(attempt.contract_sha256)}</code></p>
          {!!attempt.business_property_violations?.length && <p className="mt-2 break-words text-crimson">Violated properties: {attempt.business_property_violations.map((property) => property.field).join(', ')}{attempt.omitted_property_count ? ` (+${attempt.omitted_property_count} omitted)` : ''}</p>}
          {(attempt.import_error || attempt.verification_error || !!attempt.execution_errors?.length) && <p className="mt-2 text-amber-400">{attempt.import_error ? 'Import failed; checks were not run.' : attempt.verification_error ? 'Verification did not complete.' : 'Execution errors were recorded.'}</p>}
          {!!attempt.feedback_properties?.length && <p className="mt-2 break-words text-slate-400">Synthesis feedback: {attempt.feedback_properties.join(', ')}</p>}
        </li>)}
      </ol>
      <p className="mt-3 text-xs text-slate-400">History records actual candidate attempts in the latest analysis. Cached selections are not live CEGIS rounds.</p>
    </details>}
  </section>;
}
