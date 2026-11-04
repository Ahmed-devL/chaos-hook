import { useId, useState } from 'react';
import { LoaderCircle } from 'lucide-react';

const ARCHITECTURE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="650" viewBox="0 0 1100 650">
  <rect width="1100" height="650" fill="#020617"/>
  <g font-family="Arial, sans-serif" fill="#e2e8f0">
    <text x="45" y="55" font-size="28">NovaCloud Stripe webhook flow</text>
    <text x="45" y="95" font-size="20" fill="#fbbf24">assumes in-order, exactly-once delivery</text>
    <text x="45" y="150" font-size="19">Design arrival order: e1 → e2 → e3 → e4</text>
    <g font-size="17" fill="#94a3b8">
      <text x="45" y="185">e1 checkout.session.completed → e2 customer.subscription.created</text>
      <text x="45" y="215">→ e3 invoice.paid → e4 customer.subscription.deleted</text>
    </g>
    <g stroke="#d97706" stroke-width="2" fill="#0f172a">
      <rect x="45" y="335" width="155" height="65" rx="8"/>
      <rect x="270" y="335" width="235" height="65" rx="8"/>
      <rect x="575" y="335" width="150" height="65" rx="8"/>
      <rect x="825" y="255" width="235" height="50" rx="6"/>
      <rect x="825" y="320" width="235" height="50" rx="6"/>
      <rect x="825" y="385" width="235" height="50" rx="6"/>
      <rect x="825" y="450" width="235" height="50" rx="6"/>
      <rect x="825" y="515" width="235" height="50" rx="6"/>
    </g>
    <g stroke="#d97706" stroke-width="3" fill="none">
      <path d="M200 367 H270 M258 360 L270 367 L258 374"/>
      <path d="M505 367 H575 M563 360 L575 367 L563 374"/>
      <path d="M725 367 H775 V280 H825 M775 345 H825 M775 410 H825 M775 367 V540 H825 M775 475 H825"/>
    </g>
    <g font-size="21" text-anchor="middle">
      <text x="122" y="375">Stripe</text><text x="387" y="375">/webhook endpoint</text>
      <text x="650" y="375">handler</text><text x="942" y="287">users</text>
      <text x="942" y="352">subscriptions</text><text x="942" y="417">credit_ledger</text>
      <text x="942" y="482">webhook_events</text><text x="942" y="547">outbox_emails</text>
    </g>
    <text x="45" y="610" font-size="18" fill="#94a3b8">Design assumptions are hypotheses; the 27-cell SQL prover tests their consequences.</text>
  </g>
</svg>`;

export async function builtInDiagramPng() {
  const url = URL.createObjectURL(new Blob([ARCHITECTURE_SVG], { type: 'image/svg+xml' }));
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error('Could not rasterize architecture diagram'));
      image.src = url;
    });
    const canvas = document.createElement('canvas');
    canvas.width = 1100;
    canvas.height = 650;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas is unavailable for diagram conversion');
    context.drawImage(image, 0, 0);
    const uri = canvas.toDataURL('image/png');
    if (uri.length > 400_000) throw new Error('Built-in diagram exceeds 400KB');
    return uri;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function DiagramInput({ enabled, onEnabledChange, image, onImageChange, disabled }) {
  const [error, setError] = useState(null);
  async function upload(event) {
    const file = event.target.files?.[0];
    setError(null);
    if (!file) return;
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 1_100_000) {
      setError('Choose a PNG or JPEG under 1.1MB.');
      event.target.value = '';
      return;
    }
    try {
      const value = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('Could not read diagram image'));
        reader.readAsDataURL(file);
      });
      onImageChange(value);
    } catch (cause) { setError(cause.message); }
  }
  return <div className="mb-5 space-y-3 rounded border border-slate-700 bg-slate-950 p-3">
    <p className="text-sm font-semibold text-brass">NovaCloud Stripe webhook flow</p>
    {image ? <img src={image} alt="Uploaded architecture diagram to send to Spark" className="max-h-64 w-full object-contain" />
      : <div className="[&_svg]:h-auto [&_svg]:w-full" dangerouslySetInnerHTML={{ __html: ARCHITECTURE_SVG }} />}
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={(event) => onEnabledChange(event.target.checked)} disabled={disabled} className="accent-amber-500" />Send diagram to Spark 1.3</label>
    <label className="block text-xs text-slate-300">Upload a PNG/JPEG instead
      <input type="file" accept="image/png,image/jpeg" onChange={upload} disabled={disabled} className="mt-2 block w-full min-w-0 text-xs" />
    </label>
    {image && <button type="button" onClick={() => onImageChange(null)} disabled={disabled} className="text-xs text-brass">Use built-in diagram</button>}
    <p className="text-xs text-slate-400">Diagram settings apply to the next Diagnose run.</p>
    {error && <p role="alert" className="text-xs text-crimson">{error}</p>}
  </div>;
}

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

export default function MuseNeuroSymbolicPanel({ analysis, loading, onSelectCell, onOpenPatch, sendDiagram, onSendDiagramChange, diagramImage, onDiagramImageChange }) {
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
      {analysis && <p className="mb-3 inline-block rounded border border-slate-700 px-2 py-1 text-xs text-brass">Spark saw diagram: {analysis.model_routing.spark_live ? analysis.model_routing.spark_multimodal ? 'yes' : 'no (text-only)' : 'cached'}</p>}
      <DiagramInput enabled={sendDiagram} onEnabledChange={onSendDiagramChange} image={diagramImage} onImageChange={onDiagramImageChange} disabled={loading} />
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
              {analysis.diagnosis.diagram_findings?.length > 0 && <ul className="my-3 list-disc space-y-2 pl-5 text-sm text-brass">{analysis.diagnosis.diagram_findings.map((finding, index) => <li key={index}>{finding}</li>)}</ul>}
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
