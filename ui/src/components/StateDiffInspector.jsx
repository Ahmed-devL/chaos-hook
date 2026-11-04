import { EVENT_NAMES, cellState } from './ProofMatrixGrid.jsx';

function flatten(state, prefix = '', fields = {}) {
  for (const [key, value] of Object.entries(state ?? {})) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      flatten(value, path, fields);
    } else {
      fields[path] = value;
    }
  }
  return fields;
}

function format(value) {
  if (value === undefined) return '(missing)';
  return JSON.stringify(value);
}

// SQL records are emitted before cursor execution; controller markers confirm
// barrier release and crash position. Never infer affected rows from an INSERT.
function parseTrace(trace) {
  if (!Array.isArray(trace) || trace.some((line) => typeof line !== 'string')) return null;
  return trace.map((raw, index) => {
    const match = raw.match(/^\[([^\]]+)\] ([\s\S]*)$/);
    const message = match ? match[2] : raw;
    return { raw, index, thread: match?.[1], message,
      sql: message.startsWith('SQL: ') ? message.slice(5).split(' parameters=')[0] : null };
  });
}

function sqlLabel(sql) {
  const compact = sql.replace(/\s+/g, ' ');
  const table = compact.match(/\b(?:FROM|INTO|UPDATE)\s+["`]?([\w]+)/i)?.[1];
  return table ? `${compact.split(' ')[0]} ${table}` : compact;
}

function effectKey(entry) {
  return entry.sql?.match(/^(INSERT(?: OR \w+)? INTO|UPDATE)\s+["`]?((?:credit_ledger|users|outbox_emails))\b/i)?.[2]?.toLowerCase();
}

function TraceUnavailable() {
  return <p className="my-4 rounded border border-slate-700 p-3 text-sm text-slate-400">Trace unavailable: required SQL and scheduling markers could not be parsed.</p>;
}

function SqlStep({ entry, x, y, width = 220, duplicate, passed }) {
  const color = duplicate ? '#F17481' : passed ? '#55D4A0' : '#94a3b8';
  const label = entry.sql ? sqlLabel(entry.sql) : entry.message;
  return <g>
    <title>{`Trace #${entry.index + 1}: ${entry.raw}${duplicate ? ' — repeated side-effect candidate; see terminal state diff' : ''}`}</title>
    <rect x={x} y={y} width={width} height="25" rx="4" fill="#10151D" stroke={color} />
    <text x={x + 7} y={y + 17} fill={color} fontSize="11" fontFamily="monospace">{`${entry.index + 1}. ${label}`.slice(0, Math.floor(width / 6.7))}</text>
  </g>;
}

function ScheduleDiagram({ cell }) {
  const entries = parseTrace(cell.sql_trace);
  const outcome = cell.state_diff.map((diff) => `${diff.field}: expected ${format(diff.expected)}, got ${format(diff.actual)}`).join('; ');
  let diagram;
  let note;
  if (cell.category === 'permutation') {
    // Terminal diffs identify fields, not the causative arrival. Only emphasize
    // an event when the failure reason explicitly identifies it.
    const cause = cell.failure_reason?.match(/\b(e[1-4])\b/)?.[1];
    diagram = <svg viewBox="0 0 600 64" width="100%" role="img" aria-label="Event arrival order">
      {cell.sequence.map((event, index) => <g key={index}>
        <rect x={10 + index * 150} y="16" width="110" height="32" rx="5" fill="#10151D" stroke={!cell.passed && cause === event ? '#F17481' : '#D99A53'} />
        <text x={65 + index * 150} y="37" textAnchor="middle" fontSize="16" fill={!cell.passed && cause === event ? '#F17481' : '#e2e8f0'}>{event}</text>
        {index < cell.sequence.length - 1 && <text x={130 + index * 150} y="37" fill="#94a3b8" fontSize="18">→</text>}
      </g>)}
    </svg>;
  } else if (cell.category === 'concurrency') {
    if (!entries) return <TraceUnavailable />;
    const lanes = ['RaceWorker-A', 'RaceWorker-B'].map((name) => {
      const records = entries.filter((entry) => entry.thread === name);
      const reached = records.find((entry) => entry.message.startsWith('Reached SQL barrier before:'));
      const releases = records.filter((entry) => entry.message === 'Released from SQL barrier');
      const release = releases.at(-1);
      // The rendezvous statement is recorded before the barrier hook blocks it.
      return { name, records, reached, release,
        before: records.filter((entry) => entry.sql && entry.index < (release?.index ?? -1)),
        after: records.filter((entry) => entry.sql && entry.index > (release?.index ?? Infinity)) };
    });
    if (lanes.some((lane) => !lane.reached || !lane.release || !lane.before.length)) return <TraceUnavailable />;
    let top = 40;
    const layouts = lanes.map((lane) => {
      const y = top;
      top += Math.max(lane.before.length, lane.after.length, 1) * 31 + 65;
      return { ...lane, y };
    });
    const repeated = new Set(lanes[0].after.map(effectKey).filter((key) => key && lanes[1].after.some((entry) => effectKey(entry) === key)));
    diagram = <svg viewBox={`0 0 600 ${top}`} width="100%" role="img" aria-label="Two worker SQL swimlanes and synchronization barrier">
      <text x="15" y="18" fill="#94a3b8" fontSize="12">Before release</text>
      <text x="350" y="18" fill="#94a3b8" fontSize="12">After release →</text>
      <line x1="310" x2="310" y1="32" y2={top - 15} stroke="#D99A53" strokeWidth="3" />
      <text x="310" y="29" textAnchor="middle" fill="#D99A53" fontSize="12">Barrier(2)</text>
      {layouts.map((lane) => <g key={lane.name}>
        <text x="15" y={lane.y + 10} fill="#e2e8f0" fontSize="13">{lane.name}</text>
        <line x1="15" x2="585" y1={lane.y + 20} y2={lane.y + 20} stroke="#334155" />
        <circle cx="310" cy={lane.y + 20} r="5" fill="#D99A53"><title>{lane.reached.raw}; {lane.release.raw}</title></circle>
        {lane.before.map((entry, index) => <SqlStep key={entry.index} entry={entry} x={15} y={lane.y + 30 + index * 31} width={270} passed={cell.passed} />)}
        {lane.after.map((entry, index) => <SqlStep key={entry.index} entry={entry} x={335} y={lane.y + 30 + index * 31} width={250} passed={cell.passed} duplicate={!cell.passed && repeated.has(effectKey(entry))} />)}
      </g>)}
    </svg>;
    note = cell.passed ? 'Single-winner outcome: state matches S*. SQL records show attempts; conflict INSERTs may affect zero rows.' : 'Crimson marks side-effect writes recorded in both workers; terminal state confirms the divergence.';
  } else if (cell.category === 'crash_retry') {
    if (!entries) return <TraceUnavailable />;
    const crash = entries.find((entry) => entry.message.startsWith('Attempt 1: injecting crash '));
    const retry = entries.find((entry) => entry.message === 'Attempt 2: replay identical event');
    const start = crash && entries.filter((entry) => entry.index < crash.index && entry.message.startsWith('Deliver ')).at(-1);
    const next = retry && entries.find((entry) => entry.index > retry.index && entry.message.startsWith('Deliver '));
    const end = next && entries.find((entry) => entry.index > next.index && entry.message.startsWith('Deliver '));
    if (!crash || !retry || !start || !next) return <TraceUnavailable />;
    const attempts = [entries.filter((entry) => entry.sql && entry.index > start.index && entry.index < crash.index),
      entries.filter((entry) => entry.sql && entry.index > next.index && entry.index < (end?.index ?? Infinity))];
    if (attempts.some((attempt) => !attempt.length)) return <TraceUnavailable />;
    const effects = new Set(attempts[0].map(effectKey).filter(Boolean));
    let top = 10;
    const layouts = attempts.map((steps, index) => {
      const y = top;
      top += Math.ceil(steps.length / 3) * 43 + 80;
      return { steps, y, index };
    });
    diagram = <svg viewBox={`0 0 600 ${top}`} width="100%" role="img" aria-label="Crash injection and retry SQL timelines">
      {layouts.map(({ steps, y, index }) => <g key={index}>
        <text x="10" y={y + 16} fill="#e2e8f0" fontSize="14">{index === 0 ? 'Attempt 1' : 'Attempt 2 (retry)'}</text>
        {steps.map((entry, step) => <g key={entry.index}>
          <SqlStep entry={entry} x={10 + step % 3 * 197} y={y + 30 + Math.floor(step / 3) * 43} width={182} passed={cell.passed} duplicate={!cell.passed && index === 1 && effects.has(effectKey(entry))} />
          {step < steps.length - 1 && <text x={194 + step % 3 * 197} y={y + 47 + Math.floor(step / 3) * 43} fill="#64748b" fontSize="11">{step % 3 === 2 ? '↓' : '→'}</text>}
        </g>)}
        {index === 0 && <g transform={`translate(10 ${y + 35 + Math.ceil(steps.length / 3) * 43})`}>
          <title>{crash.raw}</title>
          <path d="M10 -14 L2 -3 H9 L5 8 L18 -6 H11 L15 -14 Z" fill="#F17481" />
          <text x="25" fill="#F17481" fontSize="12">Injected crash · trace #{crash.index + 1}</text>
        </g>}
      </g>)}
    </svg>;
    note = cell.passed ? 'Rolled back, retry ran once — state matches S*.' : 'Crimson marks side-effect writes repeated on retry; terminal state shows the persisted duplicate.';
  } else return null;
  return <figure className="my-5 min-w-0 rounded border border-slate-700 bg-slate-950 p-2">
    {diagram}
    <figcaption className="space-y-2 px-1 pb-1 text-xs leading-relaxed text-slate-400">
      <p>SQL labels follow trace order; hover for full records. SQL is logged before execution.</p>
      {note && <p className={cell.passed ? 'text-emerald' : 'text-slate-300'}>{note}</p>}
      {outcome && <p className="break-words text-crimson">{outcome}</p>}
    </figcaption>
  </figure>;
}

function explainDiscrepancy(cell) {
  const diff = cell.state_diff.find((entry) => entry.field === 'user.credit_balance') ?? cell.state_diff[0];
  if (!diff) return cell.passed ? 'This schedule reached the required business state.' : 'No terminal state difference was recorded. Execution or scheduling requirements failed; this check did not pass.';
  if (diff.field === 'user.credit_balance') return `The account ended with ${format(diff.actual)} credits; the contract requires ${format(diff.expected)}. The balance is incorrect for this customer.`;
  const labels = {
    'subscription.status': 'The subscription status', 'user.plan_tier': 'The account plan',
    'user.welcome_email_count': 'The number of welcome messages for this account',
    'outbox_emails_count': 'The total number of queued emails',
    'credit_ledger_rows': 'The number of credit ledger entries',
    'credit_ledger_sum': 'The credits recorded in the ledger',
    'credit_ledger.identity': 'The credit recipient or invoice',
    'outbox_emails.identity': 'The email recipient or template',
  };
  return `${labels[diff.field] ?? diff.field} should be ${format(diff.expected)}, but was ${format(diff.actual)}.`;
}

export default function StateDiffInspector({ cell, goldenState, traceOnly = false, onViewTrace }) {
  if (!cell) return <p className="empty-state">Select a check to inspect its state and trace.</p>;
  const expected = flatten(goldenState);
  const actual = flatten(cell.actual_state);
  const differences = new Map((cell.state_diff ?? []).map((entry) => [entry.field, entry]));
  const fields = [...new Set([...differences.keys(), ...Object.keys(expected), ...Object.keys(actual)])];
  const violations = cell.property_violations?.filter((v) => v.field !== 'execution.errors');
  const errors = cell.property_violations?.filter((v) => v.field === 'execution.errors');
  const state = cellState(cell);
  return <section className="state-inspector" aria-label={traceOnly ? 'Selected schedule SQL trace' : 'Selected schedule state difference'}>
    <header className="selected-summary">
      <div><h3>Cell #{cell.id} · {cell.category === 'permutation' ? 'Arrival order' : cell.category === 'concurrency' ? 'Concurrent race' : cell.id === 26 ? 'Invoice crash / retry' : 'Checkout crash / retry'}</h3><span className={`state-label ${state}`}>{state === 'pass' ? '✓ PASS' : state === 'error' ? '! ERROR' : '× FAIL'}</span></div>
      <p className="sequence">{cell.sequence.join(' → ')}</p>
      <ol className="full-sequence" aria-label="Full event sequence">{cell.sequence.map((event, i) => <li key={i}>{event.replace(/e[1-4]/g, (id) => EVENT_NAMES[id])}</li>)}</ol>
      {!cell.passed && <code className={state}>{cell.failure_tag ?? 'Failure code not recorded'}</code>}
    </header>
    {traceOnly ? <>
      <p className="muted trace-note">SQL is logged before execution. A recorded statement does not prove a write committed.</p>
      <details className="schedule-details"><summary>Schedule visualization</summary><ScheduleDiagram cell={cell} /></details>
      <h3 className="content-heading">SQL execution trace <span>{cell.sql_trace?.length ?? 0} records</span></h3>
      {!parseTrace(cell.sql_trace) ? <TraceUnavailable /> : cell.sql_trace.length === 0 ? <p>Trace unavailable: no records for this check.</p> : <ol aria-label="SQL trace steps" className="sql-steps">{cell.sql_trace.map((entry, index) => <li key={index}><span aria-label={`Step ${index + 1}`}>{index + 1}</span><code>{entry}</code></li>)}</ol>}
    </> : <>
      <p className="discrepancy">{explainDiscrepancy(cell)}</p>
      <table className="state-table"><caption>Business state · Golden State S* vs. observed</caption><thead><tr><th scope="col">Property</th><th scope="col">Expected</th><th scope="col">Observed</th></tr></thead><tbody>{fields.map((field) => {
        const diff = differences.get(field);
        const wanted = diff ? diff.expected : expected[field];
        const received = diff ? diff.actual : actual[field];
        const mismatch = !!diff || format(wanted) !== format(received);
        return <tr key={field} className={mismatch ? 'mismatch' : ''}><th scope="row">{mismatch && <span aria-label="Mismatch">× </span>}{field}</th><td>{format(wanted)}</td><td>{format(received)}</td></tr>;
      })}</tbody></table>
      {cell.failure_reason && <details className="failure-detail"><summary>Reported failure details</summary><p>{cell.failure_reason}</p></details>}
      {!!errors?.length && <section className="execution-errors"><h4>Execution / synchronization errors</h4>{errors.map((v, i) => <p key={i}>Expected {format(v.expected)}; observed {format(v.actual)}</p>)}<p>Missing outcomes do not count as property passes.</p></section>}
      {violations === undefined && <p className="warning">Property evidence: Not evaluated.</p>}
      {!!violations?.length && <section className="property-violations"><h4>Business-property violations</h4><ul>{violations.map((v, i) => <li key={i}><code>{v.field}</code><span>Expected {format(v.expected)}; observed {format(v.actual)}</span></li>)}</ul></section>}
      {cell.passed && <p className="pass state-match">✓ Terminal state matches Golden State S*</p>}
      <button type="button" className="trace-link" onClick={onViewTrace}>Inspect SQL trace · {cell.sql_trace?.length ?? 0} records →</button>
    </>}
  </section>;
}
