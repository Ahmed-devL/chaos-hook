import { useEffect, useRef, useState } from 'react';
import { Code2, Gauge, LoaderCircle, Play, RotateCcw, Sparkles } from 'lucide-react';
import { getStatus, prove, diagnose, patch, reset } from './api.js';
import ProofMatrixGrid from './components/ProofMatrixGrid.jsx';
import StateDiffInspector from './components/StateDiffInspector.jsx';
import MuseNeuroSymbolicPanel from './components/MuseNeuroSymbolicPanel.jsx';
import CodeDiffPatchModal from './components/CodeDiffPatchModal.jsx';

function defaultCell(cells) {
  return (cells.find((cell) => !cell.passed && cell.category === 'concurrency')
    ?? cells.find((cell) => !cell.passed) ?? cells[0])?.id ?? null;
}

function ModeBadge({ mode }) {
  return (
    <span className={`inline-block rounded border px-2 py-1 font-mono text-[10px] font-semibold tracking-wider ${mode === 'patched' ? 'border-emerald/40 bg-emerald/10 text-emerald' : mode === 'vulnerable' ? 'border-crimson/40 bg-crimson/10 text-crimson' : 'border-slate-700 text-slate-400'}`}>
      {mode === 'patched' ? 'ATOMIC PATCH ACTIVE' : mode === 'vulnerable' ? 'VULNERABLE BASELINE' : 'AWAITING STATUS'}
    </span>
  );
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [report, setReport] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [patchOpen, setPatchOpen] = useState(false);
  const [pending, setPending] = useState('load');
  const [error, setError] = useState(null);
  const [inspectorTab, setInspectorTab] = useState('state');
  const requestInFlight = useRef(false);
  const scoreRef = useRef(null);
  const busy = pending !== null;
  const invariant = report !== null && report.passed_cells === report.total_cells;

  // The ref also guards the interval before React renders disabled controls.
  async function request(action, work) {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    setPending(action);
    setError(null);
    try {
      await work();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      const unreachable = cause instanceof TypeError || /failed \((502|503|504)\)|failed \(500\):\s*$|ECONNREFUSED|Failed to fetch|NetworkError|fetch failed|Load failed/i.test(message);
      setError(unreachable ? 'Backend not reachable on :8000' : message);
      setPatchOpen(false);
    } finally {
      requestInFlight.current = false;
      setPending(null);
    }
  }

  function displayReport(nextReport) {
    setReport(nextReport);
    setSelectedId(defaultCell(nextReport.cells));
    setAnalysis(null);
    setInspectorTab('state');
  }

  useEffect(() => {
    request('load', async () => {
      setStatus(await getStatus());
      const result = await prove();
      displayReport(result.prover_report);
    });
  }, []);

  useEffect(() => {
    if (invariant && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      scoreRef.current?.animate?.(
        [{ transform: 'scale(0.98)', opacity: 0.5 }, { transform: 'scale(1)', opacity: 1 }],
        { duration: 650, easing: 'ease-out' },
      );
    }
  }, [invariant]);

  function runProver() {
    return request('prove', async () => {
      const result = await prove();
      displayReport(result.prover_report);
      setStatus(await getStatus());
    });
  }

  function runDiagnosis() {
    setInspectorTab('muse');
    return request('diagnose', async () => { setAnalysis(await diagnose()); });
  }

  function applyPatch() {
    if (!analysis) return;
    return request('patch', async () => {
      const result = await patch({ patched_code: analysis.patched_code });
      displayReport(result.prover_report);
      setStatus(await getStatus());
      setPatchOpen(false);
    });
  }

  function resetBaseline() {
    return request('reset', async () => {
      const result = await reset();
      displayReport(result.prover_report);
      setStatus(await getStatus());
      setPatchOpen(false);
    });
  }

  const actions = [
    { id: 'prove', label: '1. Run 27-Cell Prover', progress: 'Running 27-Cell Prover…', icon: Play, click: runProver },
    { id: 'diagnose', label: '2. Diagnose & Synthesize Patch (Muse Wonder + Spark 1.3)', progress: 'Diagnosing & Synthesizing Patch…', icon: Sparkles, click: runDiagnosis, needsReport: true },
    { id: 'reset', label: '3. Reset to Vulnerable Baseline', progress: 'Restoring Vulnerable Baseline…', icon: RotateCcw, click: resetBaseline },
  ];

  return (
    <main className="min-h-screen bg-slate-950 px-4 py-6 text-slate-100 sm:px-6">
      <header className="mx-auto mb-6 max-w-[2400px] border-b border-brass/30 pb-6">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="flex items-center gap-4">
            <div className="rounded-full border border-brass/50 bg-brass/5 p-3 text-brass"><Gauge size={30} aria-hidden="true" /></div>
            <div>
              <p className="mb-1 font-mono text-xs uppercase tracking-[0.3em] text-brass">ChaosHook</p>
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">The Brass Escapement</h1>
              <p className="mt-1 text-xs text-slate-400">Horological Instrument &amp; Algebraic Prover</p>
            </div>
          </div>
          <div className="space-y-3 sm:text-right">
            <p ref={scoreRef} role="status" aria-label="Algebraic proof score"
              className={`max-w-3xl rounded-lg border px-4 py-3 font-mono text-xs font-semibold leading-relaxed tracking-wide transition-colors duration-700 motion-reduce:transition-none sm:text-sm ${report ? invariant ? 'border-emerald/50 bg-emerald/10 text-emerald shadow-lg shadow-emerald/5' : 'border-crimson/50 bg-crimson/10 text-crimson' : 'border-slate-700 text-slate-400'}`}>
              {report ? `${report.passed_cells} / ${report.total_cells} CELLS VERIFIED - ${invariant ? 'ALGEBRAICALLY INVARIANT' : 'COMMUTATIVITY & IDEMPOTENCY VIOLATED'}` : 'AWAITING 27-CELL PROOF'}
            </p>
            <div className="flex flex-wrap items-center gap-3 sm:justify-end">
              <span className="font-mono text-xs tabular-nums text-slate-400">Execution time: {report ? `${report.duration_ms.toFixed(3)} ms` : '—'}</span>
              <ModeBadge mode={status?.mode} />
            </div>
          </div>
        </div>
        <nav aria-label="Prover actions" className="mt-6 flex flex-wrap gap-3">
          {actions.map(({ id, label, progress, icon: Icon, click, needsReport }) => (
            <button key={id} type="button" onClick={click} disabled={busy || (needsReport && !report)}
              className={`flex items-center gap-2 rounded border px-4 py-3 text-left text-xs font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass disabled:cursor-wait disabled:opacity-50 ${id === 'diagnose' ? 'border-brass bg-brass text-slate-950 hover:bg-amber-500' : 'border-slate-700 bg-slate-900 text-slate-200 hover:border-brass/70'}`}>
              {pending === id ? <LoaderCircle size={16} aria-hidden="true" className="shrink-0 animate-spin" /> : <Icon size={16} aria-hidden="true" className="shrink-0" />}
              {pending === id ? progress : label}
            </button>
          ))}
        </nav>
        {busy && <p role="status" className="mt-3 flex items-center gap-2 text-xs text-brass"><LoaderCircle size={14} aria-hidden="true" className="animate-spin" />{pending === 'load' ? 'Reading active handler and running all 27 proof cells…' : 'Instrument running · awaiting backend result…'}</p>}
      </header>

      {error && <div role="alert" className="mx-auto mb-6 max-w-[2400px] whitespace-pre-wrap break-words rounded-lg border border-crimson/50 bg-crimson/10 p-4 text-sm text-crimson">{error}</div>}

      <div className="mx-auto grid max-w-[2400px] items-start gap-5 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.3fr)_minmax(0,1fr)]">
        <section aria-labelledby="code-heading" className="min-w-0 rounded-lg border border-slate-800 bg-slate-900">
          <header className="space-y-3 border-b border-slate-800 p-5">
            <h2 id="code-heading" className="flex items-center gap-2 text-lg font-semibold"><Code2 size={18} aria-hidden="true" className="text-brass" />Target Code</h2>
            <ModeBadge mode={status?.mode} />
            <p className="break-all font-mono text-xs text-slate-400">target_app/handlers/active_handler.py</p>
          </header>
          <div role="region" aria-label="Active handler source" tabIndex={0} className="max-h-[65vh] overflow-auto bg-slate-950 py-4 font-mono text-xs leading-6">
            {status ? status.active_code.split('\n').map((line, index) => (
              <div key={index} className="flex min-w-max pr-5 hover:bg-slate-900">
                <span aria-hidden="true" className="sticky left-0 mr-4 w-12 shrink-0 select-none border-r border-slate-800 bg-slate-950 pr-3 text-right tabular-nums text-slate-500">{index + 1}</span>
                <code className="whitespace-pre text-slate-300">{line || ' '}</code>
              </div>
            )) : <p className="px-5 text-slate-500">Waiting for active handler source…</p>}
          </div>
        </section>

        <div className="min-w-0">
          {report ? <ProofMatrixGrid cells={report.cells} selectedId={selectedId} onSelect={(id) => { setSelectedId(id); setInspectorTab('state'); }} />
            : <section className="rounded-lg border border-slate-800 bg-slate-900 p-5 text-slate-400"><h2 className="mb-3 font-semibold text-slate-100">27-Cell Proof Matrix</h2>Run the prover to inspect the schedules.</section>}
        </div>

        <aside aria-label="Evidence and Muse inspector" className="min-w-0">
          <div className="mb-3 flex gap-2" role="group" aria-label="Inspector views">
            <button type="button" aria-pressed={inspectorTab === 'state'} onClick={() => setInspectorTab('state')} className={`rounded border px-4 py-2 text-xs ${inspectorTab === 'state' ? 'border-brass text-brass' : 'border-slate-700 text-slate-400'}`}>State &amp; SQL Trace</button>
            <button type="button" aria-pressed={inspectorTab === 'muse'} onClick={() => setInspectorTab('muse')} className={`rounded border px-4 py-2 text-xs ${inspectorTab === 'muse' ? 'border-brass text-brass' : 'border-slate-700 text-slate-400'}`}>Muse Analysis</button>
          </div>
          {inspectorTab === 'state' ? (
            <StateDiffInspector cell={report?.cells.find((cell) => cell.id === selectedId)} goldenState={report?.golden_state} />
          ) : (
            <div className="[&_.grid]:grid-cols-1 [&_section.mt-6]:mt-0">
              <MuseNeuroSymbolicPanel analysis={analysis} loading={pending === 'diagnose'} onSelectCell={(id) => { setSelectedId(id); setInspectorTab('state'); }} onOpenPatch={() => { if (!busy) setPatchOpen(true); }} />
            </div>
          )}
        </aside>
      </div>
      <CodeDiffPatchModal open={patchOpen} onClose={() => setPatchOpen(false)} analysis={analysis} onApply={applyPatch} applying={pending === 'patch'} />
    </main>
  );
}
