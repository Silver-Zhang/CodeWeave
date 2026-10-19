/** Convert a physical Clang byte column to Monaco's UTF-16 column.
 * Interior byte offsets clamp to the preceding character; out-of-range values
 * clamp to a valid position. Stored facts and their displayed columns stay raw.
 */
export function monacoColumn(line: string, byteColumn: number): number {
  const offset = Number.isFinite(byteColumn) ? Math.max(0, Math.floor(byteColumn) - 1) : 0;
  const encoder = new TextEncoder();
  let bytes = 0, column = 1;
  for (const character of line) {
    if (character === '\r' || character === '\n') break;
    const size = encoder.encode(character).length;
    if (bytes + size > offset) break;
    bytes += size;
    column += character.length;
  }
  return column;
}
