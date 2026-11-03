import { useEffect, useState } from 'react';
import { Gauge } from 'lucide-react';
import { prove } from './api.js';
import ProofMatrixGrid from './components/ProofMatrixGrid.jsx';
import StateDiffInspector from './components/StateDiffInspector.jsx';

export default function App() {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);

  useEffect(() => {
    let active = true;

    prove()
      .then(({ prover_report }) => {
        if (active) {
          setReport(prover_report);
          setSelectedId((prover_report.cells.find((cell) => !cell.passed) ?? prover_report.cells[0])?.id ?? null);
        }
      })
      .catch((cause) => {
        if (active) setError(cause.message);
      });

    return () => { active = false; };
  }, []);

  return (
    <main className="mx-auto min-h-screen max-w-[1800px] px-4 py-8 sm:px-6">
      <header className="mb-8 flex items-center gap-3 text-brass">
        <Gauge size={28} aria-hidden="true" />
        <h1 className="text-2xl font-semibold tracking-tight">The Brass Escapement</h1>
      </header>
      <section className="mb-6 rounded-lg border border-slate-800 bg-slate-900 p-6" aria-live="polite" aria-busy={!report && !error}>
        <p className="mb-3 font-mono text-xs uppercase tracking-widest text-slate-400">ChaosHook · Prover connection</p>
        {error ? (
          <p role="alert" className="text-crimson">{error}</p>
        ) : report ? (
          <>
            <p className={`text-3xl font-semibold tabular-nums ${report.passed_cells === 27 ? 'text-emerald' : 'text-brass'}`}>
              {report.passed_cells} / 27 cells verified
            </p>
            <p className="mt-3 font-mono text-sm text-slate-400">duration_ms: {report.duration_ms}</p>
          </>
        ) : (
          <p className="text-slate-300">Running 27 proof cells…</p>
        )}
      </section>
      {report && (
        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <ProofMatrixGrid cells={report.cells} selectedId={selectedId} onSelect={setSelectedId} />
          <StateDiffInspector cell={report.cells.find((cell) => cell.id === selectedId)} goldenState={report.golden_state} />
        </div>
      )}
    </main>
  );
}
