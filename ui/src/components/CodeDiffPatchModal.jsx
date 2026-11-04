import { useEffect, useRef } from 'react';
import { LoaderCircle } from 'lucide-react';
import { candidateSource, shortHash } from './ProofMatrixGrid.jsx';

function rejectionExplanation(rejection) {
  if (rejection.authentication_admission?.passed === false && !rejection.import_error) {
    return 'Authentication admission failed. The previous handler remains installed.';
  }
  if (rejection.import_error) return 'The candidate could not be imported.';
  const failed = rejection.failed_cell_ids ?? [];
  const categories = [];
  if (failed.some((id) => id >= 1 && id <= 24)) categories.push('event ordering');
  if (failed.includes(25)) categories.push('concurrent duplicate delivery');
  if (failed.some((id) => id === 26 || id === 27)) categories.push('crash recovery');
  return categories.length ? `Remaining failures: ${categories.join(', ')}. The previous handler remains installed.` : 'Installation was not confirmed. Complete acceptance evidence is required.';
}

function lineStyle(line) {
  if (line.startsWith('---') || line.startsWith('+++')) return 'text-slate-500';
  if (line.startsWith('@@')) return 'bg-brass/10 text-brass';
  if (line.startsWith('+')) return 'bg-emerald/10 text-emerald';
  if (line.startsWith('-')) return 'bg-crimson/10 text-crimson';
  return 'text-slate-300';
}

export default function CodeDiffPatchModal({ open, onClose, analysis, onApply, onApplyCached, partialPatchCode, rejection, error, applying }) {
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    return () => {
      dialog.close();
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  const selected = analysis?.model_routing?.selected_candidate;
  const attempt = selected ?? analysis?.model_routing?.candidate_attempts?.at(-1);
  return <dialog ref={dialogRef} aria-labelledby="patch-title" onCancel={(event) => { event.preventDefault(); if (!applying) onClose(); }} className="patch-dialog">
    <div className="patch-workspace">
      <header className="patch-header"><div><p className="eyebrow">Candidate review</p><h2 id="patch-title">Source diff · active_handler.py</h2></div><button type="button" onClick={onClose} disabled={applying}>Close</button></header>
      <div className="patch-metadata"><p>{candidateSource(attempt?.provenance)} <code title={attempt?.candidate_sha256}>{shortHash(attempt?.candidate_sha256)}</code></p>
        <p className={attempt?.result === 'rejected' ? 'fail' : ''}>Candidate evaluation: {attempt?.result === 'accepted' ? 'Accepted by configured checks; not yet installed' : attempt?.result === 'rejected' ? 'Rejected' : 'Not evaluated'}</p>
        <p>Authentication admission: {attempt?.authentication_admission ? attempt.authentication_admission.passed ? 'Passed' : 'Failed' : 'Not evaluated'}</p>
        {!!analysis?.model_routing?.cegis_rounds?.length && <p>{analysis.model_routing.cegis_rounds.map((round) => `Round ${round.round}: ${round.passed_cells}/27`).join(' → ')}</p>}
      </div>
      <div aria-label="Unified diff" tabIndex={0} className="unified-diff">{(analysis?.unified_diff ?? 'No candidate diff recorded.').split('\n').map((line, index) => <div key={index} className={lineStyle(line)}><span aria-hidden="true" className="diff-line-number">{index + 1}</span><code>{line || ' '}</code></div>)}</div>
      <footer className="patch-footer">
        <div className="patch-messages">
          {error && <p role="alert" className="fail">{error}</p>}
          {rejection && <div role="alert" className="patch-rejection"><h3>Installation not accepted</h3>
            {rejection.candidate_sha256 && <p>Evaluated candidate <code title={rejection.candidate_sha256}>{shortHash(rejection.candidate_sha256)}</code></p>}
            <p>Business checks: {rejection.passed_cells ?? rejection.prover_report?.passed_cells ?? 'Not evaluated'} / {rejection.total_cells ?? rejection.prover_report?.total_cells ?? '—'} passed</p>
            <p>Failing cells: {rejection.failed_cell_ids?.join(', ') || (rejection.import_error ? 'Not run (import failed)' : 'None reported in the legacy matrix')}</p>
            <p className={rejection.authentication_admission?.passed === false ? 'warning' : ''}>Authentication admission: {rejection.authentication_admission ? `${rejection.authentication_admission.passed ? 'Passed' : 'Failed'} · ${rejection.authentication_admission.passed_check_count ?? '—'}/${rejection.authentication_admission.configured_check_count ?? '—'} checks passed (separate scope)` : 'Not evaluated'}</p>
            <p>{rejectionExplanation(rejection)}</p>
            {rejection.import_error && <p>{rejection.import_error}</p>}
          </div>}
        </div>
        <div className="patch-actions"><button type="button" className="primary-button" onClick={() => onApply(analysis.patched_code)} disabled={applying || !analysis?.patched_code}>{applying && <LoaderCircle aria-hidden="true" size={14} className="animate-spin" />}{applying ? 'Verifying candidate…' : 'Verify & install candidate'}</button>
          <button type="button" onClick={() => onApply(partialPatchCode)} disabled={applying || !partialPatchCode}>Test naive idempotency-only patch</button>
          {rejection && <button type="button" onClick={onApplyCached} disabled={applying}>Verify & install cached patch</button>}
        </div>
        <p className="patch-scope" role={applying ? 'status' : undefined}>{applying ? 'Checking all 27 schedules and authentication admission. Awaiting installation confirmation…' : 'Installation requires backend acceptance of the exact candidate source. Strict qualification is not evaluated here.'}</p>
      </footer>
    </div>
  </dialog>;
}
