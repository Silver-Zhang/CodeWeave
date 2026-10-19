import { expect, test } from 'vitest';
import { monacoColumn } from './sourceLocation';
// Independently specified UTF-8 byte / UTF-16 positions, including boundaries.
test.each([
  ['abc', 1, 1], ['abc', 3, 3], ['abc', 4, 4], ['abc', 99, 4],
  ['汉x', 4, 2], ['😀x', 5, 3], ['汉😀x', 8, 4],
  ['a\u0301x', 4, 3], ['\t汉x', 5, 3],
  ['汉x', 2, 1], ['汉x', 3, 1], ['😀x', 4, 1],
  ['abc\r', 99, 4], ['汉😀\r\n', 99, 4], ['abc\nnext', 99, 4], ['abc', 2.9, 2],
  ['', 99, 1], ['abc', -4, 1], ['abc', Number.NaN, 1], ['abc', Infinity, 1],
])('maps %j byte column %s to UTF-16 column %s', (line, byte, expected) => {
  expect(monacoColumn(line, byte)).toBe(expected);
});
