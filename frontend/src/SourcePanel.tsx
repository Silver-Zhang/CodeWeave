import { useEffect, useRef } from 'react';
import * as monaco from 'monaco-editor/editor/editor.api.js';
import 'monaco-editor/languages/definitions/cpp/register.js';
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import type { Source } from './api';
import { monacoColumn } from './sourceLocation';
(self as typeof self & { MonacoEnvironment: { getWorker: () => Worker } }).MonacoEnvironment = { getWorker: () => new EditorWorker() };
export default function SourcePanel({ source, line, column }: { source: Source; line: number; column: number }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const model = monaco.editor.createModel(source.content, 'cpp');
    const editor = monaco.editor.create(host.current, { model, theme: 'vs-dark', readOnly: true, domReadOnly: true,
      automaticLayout: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false,
      lineNumbers: n => String(source.start_line + n - 1), ariaLabel: 'Indexed source snapshot', wordWrap: 'off' });
    const at = line - source.start_line + 1;
    // Clang stores UTF-8 byte columns; Monaco positions use UTF-16 code units.
    // Keep the original byte column in the source metadata shown by the UI.
    const textLine = source.content.split(/\r\n|\n|\r/)[at - 1] ?? '';
    const editorColumn = monacoColumn(textLine, column);
    editor.setPosition({ lineNumber: at, column: editorColumn }); editor.revealLineInCenter(at);
    editor.createDecorationsCollection([{ range: new monaco.Range(at, 1, at, 1), options: { isWholeLine: true, className: 'source-highlight' } }]);
    return () => { editor.dispose(); model.dispose(); };
  }, [source, line, column]);
  return <div className="editor" ref={host} />;
}
