import { useId, useState } from 'react';
import { LoaderCircle } from 'lucide-react';

function edgeKinds(cluster) {
  const kinds = [];
  if (cluster.cells.some((id) => id >= 1 && id <= 24)) kinds.push('ordering');
  if (cluster.cells.includes(25)) kinds.push('race');
  if (cluster.cells.some((id) => id === 26 || id === 27)) kinds.push('crash');
  return kinds;
}

function LifecycleDiagram({ clusters, activeCluster }) {
  const markerId = useId();
  const broken = new Set(clusters.flatMap(edgeKinds));
  const highlighted = new Set(activeCluster === null ? [] : edgeKinds(clusters[activeCluster]));
  const edge = (kind) => ({
    stroke: broken.has(kind) ? '#EF4444' : '#64748B',
    strokeDasharray: broken.has(kind) ? '6 4' : undefined,
    strokeWidth: highlighted.has(kind) ? 4 : 2,
    opacity: activeCluster === null || highlighted.has(kind) ? 1 : 0.35,
    fill: 'none',
    markerEnd: `url(#${markerId})`,
    className: 'transition-all',
    'data-edge': kind,
  });

  return (
    <figure className="my-5 rounded-lg border border-slate-800 bg-slate-950 p-3">
      <svg viewBox="0 0 640 235" role="img" aria-label="Payment lifecycle with ordering, duplicate race, and crash-retry edges" className="w-full">
        <defs>
          <marker id={markerId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
          </marker>
        </defs>
        <g fill="none" stroke="#64748B" strokeWidth="2" markerEnd={`url(#${markerId})`}>
          <path d="M 100 110 H 220" /><path d="M 260 110 H 380" /><path d="M 420 110 H 540" />
        </g>
        <path d="M 240 88 Q 400 -25 560 88" {...edge('ordering')} />
        <path d="M 390 89 C 325 5 475 5 410 89" {...edge('race')} />
        <path d="M 65 132 C 0 205 155 205 95 132" {...edge('crash')} />
        <path d="M 385 132 C 320 205 475 205 415 132" {...edge('crash')} />
        <g fill="#94A3B8" fontSize="12" textAnchor="middle">
          <text x="530" y="40">e2/e4 ordering</text>
          <text x="400" y="20">e3 duplicate race</text>
          <text x="80" y="218">checkout crash-retry</text>
          <text x="400" y="218">invoice crash-retry</text>
        </g>
        {['checkout', 'sub created', 'invoice paid', 'sub deleted'].map((label, index) => (
          <g key={label}>
            <circle cx={80 + index * 160} cy="110" r="22" fill="#0F172A" stroke="#D97706" strokeWidth="2" />
            <text x={80 + index * 160} y="115" textAnchor="middle" fill="#F8FAFC" fontSize="14">e{index + 1}</text>
            <text x={80 + index * 160} y="154" textAnchor="middle" fill="#CBD5E1" fontSize="12">{label}</text>
          </g>
        ))}
      </svg>
      <figcaption className="text-xs text-slate-400">Crimson dashed edges mark diagnosed failures. Hover or focus a cluster to highlight its edges.</figcaption>
    </figure>
  );
}

export default function MuseNeuroSymbolicPanel({ analysis, loading, onSelectCell, onOpenPatch }) {
  const [activeCluster, setActiveCluster] = useState(null);
  const cached = analysis?.model_routing.used_fallback_cache;

  return (
    <section aria-label="Muse neuro-symbolic analysis" aria-busy={loading} className="mt-6 rounded-lg border border-slate-800 bg-slate-900 p-5">
      <header className="mb-5 flex flex-wrap items-center gap-3 text-xs font-semibold">
        <span className="rounded border border-slate-700 px-3 py-2">Meta Muse Spark 1.3</span>
        <span className="rounded border border-slate-700 px-3 py-2">Meta Muse Wonder</span>
        <span className={`rounded border px-3 py-2 ${cached === true ? 'border-brass text-brass' : cached === false ? 'border-emerald text-emerald' : 'border-slate-700 text-slate-400'}`}>
          {cached === true ? 'VERIFIED CACHE' : cached === false ? 'LIVE MODEL' : 'SOURCE PENDING'}
        </span>
      </header>
      {loading ? (
        <p role="status" className="flex items-center gap-2 text-brass"><LoaderCircle aria-hidden="true" size={18} className="animate-spin" />Diagnosing failures and synthesizing an atomic patch…</p>
      ) : !analysis ? (
        <p className="text-slate-400">Run the prover, then Diagnose</p>
      ) : (
        <>
          <div className="grid gap-8 lg:grid-cols-2">
            <section aria-labelledby="spark-heading" className="min-w-0">
              <h2 id="spark-heading" className="mb-3 text-lg font-semibold">Muse Spark 1.3 Diagnosis</h2>
              <p className="text-slate-300">{analysis.diagnosis.summary_headline}</p>
              <LifecycleDiagram clusters={analysis.diagnosis.failure_clusters} activeCluster={activeCluster} />
              <div className="space-y-3">
                {analysis.diagnosis.failure_clusters.map((cluster, index) => (
                  <article key={cluster.title} tabIndex={0}
                    onMouseEnter={() => setActiveCluster(index)} onMouseLeave={() => setActiveCluster(null)}
                    onFocus={() => setActiveCluster(index)} onBlur={() => setActiveCluster(null)}
                    className={`rounded border p-4 focus-visible:outline focus-visible:outline-brass ${activeCluster === index ? 'border-crimson bg-crimson/10' : 'border-slate-700'}`}>
                    <h3 className="mb-2 font-medium text-crimson">{cluster.title}</h3>
                    <p className="text-sm leading-relaxed text-slate-300">{cluster.description}</p>
                  </article>
                ))}
              </div>
              <h3 className="mb-2 mt-5 font-medium text-brass">Remediation proof plan</h3>
              <p className="text-sm leading-relaxed text-slate-300">{analysis.diagnosis.remediation_proof_plan}</p>
            </section>
            <section aria-labelledby="wonder-heading" className="min-w-0">
              <h2 id="wonder-heading" className="mb-4 text-lg font-semibold">Muse Wonder · Formal Conservation Invariants</h2>
              <div className="space-y-4">
                {analysis.invariants.map((invariant) => (
                  <article key={invariant.id} className="rounded border border-slate-700 p-4">
                    <h3 className="font-medium"><span className="mr-2 font-mono text-brass">{invariant.id}</span>{invariant.title}</h3>
                    <p className="my-3 break-words rounded bg-slate-950 p-3 font-mono text-sm text-brass">{invariant.formal_expr}</p>
                    <p className="text-sm leading-relaxed text-slate-300">{invariant.description}</p>
                    <p className="mb-2 mt-3 text-xs text-slate-400">Violated by cells</p>
                    <div className="flex flex-wrap gap-2">
                      {invariant.violated_by_cells.map((cellId) => (
                        <button type="button" key={cellId} onClick={() => onSelectCell(cellId)} aria-label={`${invariant.id}: inspect cell #${cellId}`}
                          className="rounded border border-crimson/40 bg-crimson/5 px-2 py-1 font-mono text-xs text-crimson hover:bg-crimson/20 focus-visible:outline focus-visible:outline-brass">#{cellId}</button>
                      ))}
                      {invariant.violated_by_cells.length === 0 && <span className="text-xs text-emerald">No violations</span>}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          </div>
          <button type="button" onClick={onOpenPatch} className="mt-6 rounded bg-brass px-5 py-3 font-semibold text-slate-950 hover:bg-amber-500 focus-visible:outline focus-visible:outline-offset-4 focus-visible:outline-brass">View Atomic Patch</button>
        </>
      )}
    </section>
  );
}
