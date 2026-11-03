import { useEffect, useRef } from 'react';
import { LoaderCircle } from 'lucide-react';

function lineStyle(line) {
  if (line.startsWith('---') || line.startsWith('+++')) return 'text-slate-500';
  if (line.startsWith('@@')) return 'bg-brass/10 text-brass';
  if (line.startsWith('+')) return 'bg-emerald/10 text-emerald';
  if (line.startsWith('-')) return 'bg-crimson/10 text-crimson';
  return 'text-slate-300';
}

export default function CodeDiffPatchModal({ open, onClose, analysis, onApply, applying }) {
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

  return (
    <dialog ref={dialogRef} aria-labelledby="patch-title" onCancel={(event) => { event.preventDefault(); onClose(); }}
      className="fixed inset-0 m-0 h-screen max-h-none w-screen max-w-none bg-slate-950/95 p-4 text-slate-100 backdrop:bg-black/70 sm:p-8">
      <div className="mx-auto flex h-full max-w-6xl flex-col rounded-lg border border-slate-700 bg-slate-900 p-5">
        <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 id="patch-title" className="text-xl font-semibold text-brass">Atomic Patch · Unified Diff</h2>
          <button type="button" onClick={onClose} className="rounded border border-slate-600 px-4 py-2 hover:bg-slate-800">Close</button>
        </header>
        <div aria-label="Unified diff" tabIndex={0} className="min-h-0 flex-1 overflow-auto rounded border border-slate-800 bg-slate-950 py-3 font-mono text-xs leading-relaxed sm:text-sm">
          {(analysis?.unified_diff ?? '').split('\n').map((line, index) => (
            <div key={index} className={`min-w-max whitespace-pre px-4 ${lineStyle(line)}`}>{line || ' '}</div>
          ))}
        </div>
        <footer className="mt-4">
          <button type="button" onClick={onApply} disabled={applying || !analysis?.patched_code}
            className="flex items-center justify-center gap-2 rounded bg-brass px-5 py-3 font-semibold text-slate-950 hover:bg-amber-500 disabled:cursor-wait disabled:opacity-60">
            {applying && <LoaderCircle aria-hidden="true" size={18} className="animate-spin" />}
            {applying ? 'Applying Atomic Patch & Re-Proving…' : 'Apply Atomic Patch & Re-Prove (27/27)'}
          </button>
          {applying && <p role="status" className="mt-2 text-sm text-brass">Running all 27 proof cells…</p>}
        </footer>
      </div>
    </dialog>
  );
}
