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

export default function StateDiffInspector({ cell, goldenState }) {
  if (!cell) {
    return <section className="rounded-lg border border-slate-800 bg-slate-900 p-5">Select a cell to inspect its state and trace.</section>;
  }

  const expected = flatten(goldenState);
  const actual = flatten(cell.actual_state);
  const differences = new Map(cell.state_diff.map((entry) => [entry.field, entry]));
  const fields = [...new Set([...Object.keys(expected), ...Object.keys(actual), ...differences.keys()])];

  return (
    <section aria-labelledby="inspector-heading" className="min-w-0 self-start rounded-lg border border-slate-800 bg-slate-900 p-5">
      <header className="mb-6 space-y-3">
        <h2 id="inspector-heading" className="text-lg font-semibold">Cell #{cell.id}: {cell.label}</h2>
        <p className="text-xs uppercase tracking-widest text-slate-400">{cell.category === 'permutation' ? 'Arrival order' : 'Fault schedule'}</p>
        <p className="break-words font-mono text-sm leading-relaxed text-slate-300">{cell.sequence.join(' → ')}</p>
        {!cell.passed && <p className="break-words font-mono text-sm text-crimson">{cell.failure_tag}</p>}
      </header>

      {cell.passed ? (
        <p role="status" className="rounded border border-emerald/40 bg-emerald/10 p-4 font-medium text-emerald">State matches S*</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">Golden and actual terminal state for cell {cell.id}</caption>
            <thead className="border-b border-slate-700 text-slate-300">
              <tr>
                <th scope="col" className="p-2">Field</th>
                <th scope="col" className="p-2">Golden State S*</th>
                <th scope="col" className="p-2">Actual Terminal State</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {fields.map((field) => {
                const diff = differences.get(field);
                const wanted = diff ? diff.expected : expected[field];
                const received = diff ? diff.actual : actual[field];
                const mismatched = !!diff || format(wanted) !== format(received);
                return (
                  <tr key={field} className={`border-b border-slate-800 ${mismatched ? 'bg-crimson/10 text-crimson' : 'text-slate-300'}`}>
                    <th scope="row" className="break-words p-2 align-top font-normal">{field}</th>
                    <td className="break-words p-2 align-top">{format(wanted)}</td>
                    <td className="break-words p-2 align-top">
                      {format(received)}
                      {mismatched && <span className="mt-1 block text-xs">expected {format(wanted)}, got {format(received)}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <section aria-labelledby="trace-heading" className="mt-8">
        <h3 id="trace-heading" className="mb-4 text-sm font-semibold">SQL Interceptor &amp; Barrier Trace</h3>
        {cell.sql_trace.length === 0 ? (
          <p className="text-sm text-slate-400">No interception trace for this cell</p>
        ) : (
          <ol key={cell.id} aria-label="SQL trace steps" className="max-h-[36rem] overflow-y-auto rounded border border-slate-800 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-300">
            {cell.sql_trace.map((entry, index) => (
              <li key={index} className="flex gap-3 border-b border-slate-800/60 py-2 last:border-0">
                <span className="w-7 shrink-0 text-right tabular-nums text-brass" aria-label={`Step ${index + 1}`}>{index + 1}.</span>
                <span className="min-w-0 whitespace-pre-wrap break-words">{entry}</span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </section>
  );
}
