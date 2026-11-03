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
