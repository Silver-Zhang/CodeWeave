import { useEffect, useRef } from 'react';
import * as monaco from 'monaco-editor/editor/editor.api.js';
import 'monaco-editor/languages/definitions/cpp/register.js';
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import type { SourceDiff } from './api';
(self as typeof self & { MonacoEnvironment: { getWorker: () => Worker } }).MonacoEnvironment = { getWorker: () => new EditorWorker() };
export default function DiffPanel({ source }: { source: SourceDiff }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const original = monaco.editor.createModel(source.left?.content ?? '', 'cpp');
    const modified = monaco.editor.createModel(source.right?.content ?? '', 'cpp');
    const editor = monaco.editor.createDiffEditor(host.current, { theme: 'vs-dark', readOnly: true, domReadOnly: true,
      automaticLayout: true, renderSideBySide: true, originalEditable: false, minimap: { enabled: false },
      scrollBeyondLastLine: false, ariaLabel: 'Indexed source difference' });
    editor.setModel({ original, modified });
    editor.getOriginalEditor().updateOptions({ lineNumbers: n => String((source.left?.start_line ?? 1) + n - 1) });
    editor.getModifiedEditor().updateOptions({ lineNumbers: n => String((source.right?.start_line ?? 1) + n - 1) });
    return () => { editor.dispose(); original.dispose(); modified.dispose(); };
  }, [source]);
  return <div className="diff-editor" ref={host} aria-label="Saved source comparison" />;
}
