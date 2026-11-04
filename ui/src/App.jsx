import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, Maximize2, Minimize2, Play, RotateCcw, Sparkles } from 'lucide-react';
import { getStatus, prove, diagnose, patch, reset, updateSource, isVerifiedPatchResponse } from './api.js';
import ProofMatrixGrid, { VerificationEvidence, CandidateEvidence, candidateSource, shortHash } from './components/ProofMatrixGrid.jsx';
import StateDiffInspector from './components/StateDiffInspector.jsx';
import MuseNeuroSymbolicPanel, { builtInDiagramPng } from './components/MuseNeuroSymbolicPanel.jsx';
import CodeDiffPatchModal from './components/CodeDiffPatchModal.jsx';

function defaultCell(cells) {
  return (cells.find((cell) => !cell.passed && cell.category === 'concurrency')
    ?? cells.find((cell) => !cell.passed) ?? cells[0])?.id ?? null;
}

function SourceLine({ line }) {
  // A small lexical highlighter; source stays text, never injected HTML.
  const parts = line.split(/(#.*$|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\b(?:def|class|from|import|return|if|elif|else|for|in|with|as|try|except|raise|True|False|None|and|or|not)\b)/g);
  return parts.map((part, i) => <span key={i} className={part.startsWith('#') ? 'syntax-comment' : /^['"]/.test(part) ? 'syntax-string' : /^(def|class|from|import|return|if|elif|else|for|in|with|as|try|except|raise|True|False|None|and|or|not)$/.test(part) ? 'syntax-keyword' : undefined}>{part}</span>);
}

function PaneFocusButton({ name, focused, onToggle }) {
  const Icon = focused ? Minimize2 : Maximize2;
  const label = focused ? 'Restore layout' : `Expand ${name} pane`;
  return <button type="button" className="pane-focus-button" onClick={onToggle}
    aria-label={label} aria-pressed={focused} title={label}>
    <Icon size={14} aria-hidden="true" />
  </button>;
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [editError, setEditError] = useState(null);
  const gutterRef = useRef(null);
  const [report, setReport] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [admission, setAdmission] = useState(null);
  const [workspaceView, setWorkspaceView] = useState('matrix');
  const [focusedPane, setFocusedPane] = useState(null);
  const [candidateAudit, setCandidateAudit] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [patchOpen, setPatchOpen] = useState(false);
  const [patchRejection, setPatchRejection] = useState(null);
  const [sendDiagram, setSendDiagram] = useState(true);
  const [diagramImage, setDiagramImage] = useState(null);
  const [instantMode, setInstantMode] = useState(false);
  const [diagnosisSeconds, setDiagnosisSeconds] = useState(0);
  const [pending, setPending] = useState('load');
  const [error, setError] = useState(null);
  const [inspectorTab, setInspectorTab] = useState('state');
  const requestInFlight = useRef(false);
  const reportSource = useRef(null);
  const inspectorScroll = useRef(null);
  const inspectorHeading = useRef(null);
  const busy = pending !== null;
  const selectedCell = report?.cells.find((cell) => cell.id === selectedId);
  const draftChanged = editing && draft !== status?.active_code;

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
    } finally {
      requestInFlight.current = false;
      setPending(null);
    }
  }

  function displayReport(result) {
    const nextReport = result.prover_report;
    setReport(nextReport);
    setEvidence(result.verification_evidence ?? null);
    setAdmission(result.authentication_admission ?? null);
    const source = result.handler_status?.active_code;
    const sameSource = source && source === reportSource.current;
    setSelectedId((previous) => sameSource && nextReport.cells.some((cell) => cell.id === previous) ? previous : defaultCell(nextReport.cells));
    reportSource.current = source;
    if (result.handler_status) setStatus((previous) => ({ ...previous, ...result.handler_status }));
    setAnalysis(null);
    setInspectorTab('state');
  }

  useEffect(() => {
    request('load', async () => {
      setStatus(await getStatus());
      const result = await prove();
      displayReport(result);
    });
  }, []);

  useEffect(() => {
    if (pending !== 'diagnose') return;
    const started = Date.now();
    setDiagnosisSeconds(0);
    const timer = window.setInterval(() => {
      setDiagnosisSeconds(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [pending]);

  useEffect(() => {
    inspectorScroll.current?.scrollTo(0, 0);
  }, [selectedId, inspectorTab]);

  useEffect(() => {
    if (workspaceView === 'inspector' && (focusedPane === 'inspector' || window.matchMedia('(max-width: 1099px)').matches)) {
      inspectorHeading.current?.focus();
    }
  }, [workspaceView, focusedPane]);

  function runProver() {
    return request('prove', async () => {
      const result = await prove();
      displayReport(result);
      if (!status?.partial_patch_code) {
        const latest = await getStatus();
        setStatus((previous) => ({ ...previous, partial_patch_code: latest.partial_patch_code }));
      }
    });
  }

  function runDiagnosis() {
    setInspectorTab('muse');
    showPane('inspector');
    setPatchRejection(null);
    return request('diagnose', async () => {
      const image = sendDiagram ? diagramImage ?? await builtInDiagramPng() : null;
      const result = await diagnose({ use_cache: instantMode, diagram_image: image });
      setAnalysis(result);
      setCandidateAudit(result.model_routing);
    });
  }

  function applyPatch(patchedCode) {
    if (patchedCode === undefined) return;
    return applyPatchRequest({ patched_code: patchedCode });
  }

  function applyPatchRequest(body) {
    return request('patch', async () => {
      setPatchRejection(null);
      const result = await patch(body);
      if (!isVerifiedPatchResponse(result)) {
        setPatchRejection(result);
        return;
      }
      displayReport(result);
      setEditing(false);
      setEditError(null);
      setPatchOpen(false);
    });
  }

  function resetBaseline() {
    return request('reset', async () => {
      const result = await reset();
      displayReport(result);
      setCandidateAudit(null);
      setPatchRejection(null);
      setPatchOpen(false);
      setEditing(false);
      setEditError(null);
    });
  }

  function saveEdit() {
    return request('edit', async () => {
      setEditError(null);
      try {
        const result = await updateSource(draft);
        displayReport(result);
        setCandidateAudit(null);
        setStatus((previous) => ({ ...previous, ...result.handler_status }));
      } catch (cause) {
        setEditError(cause instanceof Error ? cause.message : String(cause));
      }
    });
  }

  function selectCell(id) {
    setSelectedId(id);
    setInspectorTab('state');
    showPane('inspector');
  }

  function showPane(id) {
    setWorkspaceView(id);
    setFocusedPane((current) => current ? id : null);
  }

  function togglePaneFocus(id) {
    setWorkspaceView(id);
    setFocusedPane((current) => current === id ? null : id);
  }

  function openPatch() { if (!busy) setPatchOpen(true); }
  const progress = pending === 'diagnose'
    ? `${instantMode ? 'Loading cached analysis' : 'Muse diagnosis and synthesis · live requested; fallback may be used'} · ${diagnosisSeconds}s elapsed`
    : pending === 'load' ? 'Reading source and running configured checks…'
    : pending === 'patch' ? 'Verifying candidate and authentication admission before installation…'
    : pending === 'edit' ? 'Validating source and generating a new proof…'
    : pending === 'reset' ? 'Restoring vulnerable baseline and regenerating proof…'
    : 'Running all configured schedules…';

  return (
    <main className="workbench">
      <header className="command-bar">
        <a className="brand" href="#workspace" aria-label="ChaosHook workbench">
          <img src="/favicon.svg" width="32" height="32" alt="" />
          <div><h1><span className="sr-only">ChaosHook</span><img className="brand-wordmark" src="/wordmark.svg" width="208" height="29" alt="" /></h1><p>Find, repair, and verify webhook failures.</p></div>
        </a>
        <div className="handler-identity"><code>active_handler.py</code><span>{status ? status.mode === 'vulnerable' ? 'Vulnerable baseline' : 'Modified source' : 'Source pending'} · {report ? 'Proof recorded' : 'Not evaluated'}</span></div>
        <nav className="command-actions" aria-label="Workbench actions">
          <button type="button" className="primary-button" onClick={runProver} disabled={busy}><Play size={14} aria-hidden="true" />{pending === 'prove' ? 'Verifying…' : 'Run verification'}</button>
          <button type="button" onClick={runDiagnosis} disabled={busy || !report || draftChanged} title={draftChanged ? 'Save and verify source before diagnosis' : 'Diagnose and synthesize with Muse Wonder and Spark 1.3'}><Sparkles size={14} aria-hidden="true" />Diagnose / synthesize</button>
          <button type="button" className="reset-button" onClick={resetBaseline} disabled={busy} title="Reset to the vulnerable baseline"><RotateCcw size={14} aria-hidden="true" />Reset</button>
          <label className="mode-control">Muse mode<select aria-label="Muse response mode" value={instantMode ? 'cached' : 'live'} onChange={(e) => setInstantMode(e.target.value === 'cached')} disabled={busy}><option value="live">Live · fallback enabled</option><option value="cached">Cached · offline</option></select></label>
        </nav>
      </header>
      <VerificationEvidence report={report} evidence={evidence} admission={admission} busy={busy} />
      <div className={`operation-line ${error ? 'has-error' : ''}`}>
        {error ? <p role="alert">{error}</p> : busy ? <p role="status"><LoaderCircle size={13} aria-hidden="true" className="animate-spin" />{progress}</p> : <p>{report ? 'Ready · select a schedule to investigate' : 'Ready · run verification to load evidence'}</p>}
        <span>{report ? `${report.duration_ms.toFixed(1)} ms · ` : ''}27 configured schedules · SQLite</span>
      </div>
      <nav className="workspace-switch" aria-label="Workspace panes">{[['handler', '01 Handler'], ['matrix', '02 Matrix'], ['inspector', '03 Inspector']].map(([id, label]) => <button type="button" key={id} aria-pressed={workspaceView === id} onClick={() => showPane(id)}>{label}</button>)}</nav>
      <div id="workspace" className="workspace" data-focused-pane={focusedPane ?? undefined} tabIndex={-1}>
        <section aria-labelledby="code-heading" className={`pane handler-pane ${focusedPane === 'handler' ? 'is-focused' : ''} ${workspaceView === 'handler' ? 'mobile-active' : ''}`}>
          <header className="pane-header"><h2 id="code-heading"><span>[01]</span> Handler</h2><div className="pane-header-actions"><label className="edit-toggle"><input type="checkbox" checked={editing} disabled={busy || !status} onChange={(e) => { setEditing(e.target.checked); setDraft(status.active_code); setEditError(null); }} />Edit source</label><PaneFocusButton name="handler" focused={focusedPane === 'handler'} onToggle={() => togglePaneFocus('handler')} /></div></header>
          <div className="source-meta"><code title="target_app/handlers/active_handler.py">active_handler.py</code><p>{candidateSource(evidence?.provenance)} <code title={evidence?.candidate_sha256}>{shortHash(evidence?.candidate_sha256)}</code></p></div>
          {editing ? <>
            <p className="draft-notice">{draftChanged ? 'Unsaved draft · proof refers to installed source' : 'Editing installed source'} · Python</p>
            <div className="source-editor">
              <pre ref={gutterRef} aria-hidden="true" className="source-gutter">{draft.split('\n').map((_, i) => i + 1).join('\n')}</pre>
              <textarea aria-label="Edit active handler source" value={draft} disabled={busy} spellCheck={false} wrap="off" onChange={(e) => setDraft(e.target.value)} onScroll={(e) => { gutterRef.current.scrollTop = e.target.scrollTop; }} />
            </div>
            <div className="editor-actions">
              {editError && <p role="alert" className="fail">{editError}</p>}
              <button type="button" className="primary-button" disabled={busy || !draftChanged} onClick={saveEdit}>Save source &amp; verify</button>
              <button type="button" disabled={busy || !status?.partial_patch_code} onClick={() => { setDraft(status.partial_patch_code); setEditError(null); }}>Load naive partial patch</button>
            </div>
          </> : <div role="region" aria-label="Active handler source" tabIndex={0} className="source-view">
            {status?.active_code ? status.active_code.split('\n').map((line, i) => <div key={i} className="source-line"><span aria-hidden="true" className="line-number">{i + 1}</span><code><SourceLine line={line} /></code></div>) : <p className="empty-state">{busy ? 'Loading active handler source…' : 'Source unavailable. Run verification to retry.'}</p>}
          </div>}
          <footer className="pane-footer"><span>Python · {status?.line_count ?? '—'} lines</span><span>{draftChanged ? 'Draft modified' : 'Installed source'}</span></footer>
        </section>
        <section aria-labelledby="matrix-heading" className={`pane matrix-pane ${focusedPane === 'matrix' ? 'is-focused' : ''} ${workspaceView === 'matrix' ? 'mobile-active' : ''}`}>
          <header className="pane-header"><h2 id="matrix-heading"><span>[02]</span> Execution matrix</h2><div className="pane-header-actions"><span className="pane-hint">24 + 1 + 2</span><PaneFocusButton name="execution matrix" focused={focusedPane === 'matrix'} onToggle={() => togglePaneFocus('matrix')} /></div></header>
          <div className="pane-scroll">{report ? <ProofMatrixGrid cells={report.cells} selectedId={selectedId} onSelect={selectCell} /> : <p className="empty-state">{busy ? 'Executing real SQL against isolated database clones…' : 'Run verification to inspect the schedules.'}</p>}</div>
          <footer className="pane-footer"><span>Legacy schedule gate</span><span>Strict qualification: Not evaluated</span></footer>
        </section>
        <aside aria-labelledby="inspector-heading" className={`pane inspector-pane ${focusedPane === 'inspector' ? 'is-focused' : ''} ${workspaceView === 'inspector' ? 'mobile-active' : ''}`}>
          <header className="pane-header"><h2 id="inspector-heading" ref={inspectorHeading} tabIndex={-1}><span>[03]</span> Inspector</h2><div className="pane-header-actions"><span className="pane-hint">{selectedId ? `CELL ${String(selectedId).padStart(2, '0')}` : 'NO SELECTION'}</span><PaneFocusButton name="inspector" focused={focusedPane === 'inspector'} onToggle={() => togglePaneFocus('inspector')} /></div></header>
          <nav className="inspector-tabs" aria-label="Inspector views">{[['state', 'State diff'], ['sql', 'SQL trace'], ['muse', 'Muse'], ['audit', 'Evidence']].map(([id, label]) => <button type="button" key={id} aria-pressed={inspectorTab === id} aria-controls={`inspector-${id}`} onClick={() => setInspectorTab(id)}>{label}</button>)}</nav>
          <div ref={inspectorScroll} className="pane-scroll inspector-content">
            <div id="inspector-state" hidden={inspectorTab !== 'state'}><StateDiffInspector cell={selectedCell} goldenState={report?.golden_state} onViewTrace={() => setInspectorTab('sql')} /></div>
            <div id="inspector-sql" hidden={inspectorTab !== 'sql'}><StateDiffInspector cell={selectedCell} goldenState={report?.golden_state} traceOnly /></div>
            <div id="inspector-muse" hidden={inspectorTab !== 'muse'}><MuseNeuroSymbolicPanel sendDiagram={sendDiagram} onSendDiagramChange={setSendDiagram} diagramImage={diagramImage} onDiagramImageChange={setDiagramImage} analysis={analysis} loading={pending === 'diagnose'} disabled={busy} onSelectCell={selectCell} onOpenPatch={openPatch} /></div>
            <div id="inspector-audit" hidden={inspectorTab !== 'audit'}><CandidateEvidence evidence={evidence} audit={candidateAudit} admission={admission} />{analysis && <button type="button" disabled={busy} onClick={openPatch}>Review candidate diff</button>}</div>
          </div>
          <footer className="pane-footer"><span>{inspectorTab === 'muse' ? 'Guided repair session' : 'Source-bound evidence'}</span>{analysis && <button type="button" disabled={busy} onClick={openPatch}>Review patch →</button>}</footer>
        </aside>
      </div>
      <CodeDiffPatchModal open={patchOpen} onClose={() => setPatchOpen(false)} analysis={analysis} onApply={applyPatch} partialPatchCode={status?.partial_patch_code} onApplyCached={() => applyPatchRequest()} rejection={patchRejection} error={error} applying={pending === 'patch'} />
    </main>
  );
}
