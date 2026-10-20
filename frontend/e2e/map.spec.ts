import { test, expect } from '@playwright/test';

test('real Clang index drives search, graph, calls, Monaco and snapshot switching', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => { if (msg.type()==='error') errors.push(msg.text()); });
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  expect(versions.items).toHaveLength(2);
  const oldId = versions.items[1].index_id;
  await page.getByLabel('Snapshot identity').selectOption(oldId);
  await page.getByLabel('Function or method').fill('over');
  await expect(page.locator('.result')).toHaveCount(2);
  await expect(page.locator('.result code')).toHaveText(['c:@F@over#I#','c:@F@over#d#']);
  await page.getByLabel('Function or method').fill('left');
  await page.locator('.result').click();
  await expect(page.locator('.react-flow__node')).toHaveCount(4);
  await expect(page.locator('.react-flow__edge')).toHaveCount(4);
  await expect(page.locator('.monaco-editor')).toBeVisible();
  await expect(page.locator('.view-lines')).toContainText('int left()');
  await expect(page.locator('.source-heading')).toContainText(oldId);
  await page.getByRole('button',{name:'direct · unit.cpp:4:21',exact:true}).click();
  await expect(page.locator('.source-heading')).toContainText(':4:21');
  await expect(page.locator('.view-lines')).not.toContainText('changed_only');
  await page.getByRole('button',{name:'Center',exact:true}).click();
  await page.getByRole('button',{name:'Reset',exact:true}).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(4);
  await page.getByLabel('Function or method').fill('dispatch');
  await page.locator('.result').click();
  await expect(page.locator('.react-flow__node')).toHaveCount(3);
  await expect(page.locator('.react-flow__edge')).toHaveCount(2);
  await expect(page.getByText('? Unresolved target')).toBeVisible();
  await expect(page.locator('.react-flow__edge-text')).toContainText(['virtual · static target','unknown-function-pointer']);
  await page.locator('.react-flow__edge').filter({hasText:'virtual · static target'}).click({force:true});
  await expect(page.locator('.source-heading')).toContainText(':6:50');
  await page.getByLabel('Function or method').fill('ping'); await page.locator('.result').click();
  await expect(page.locator('.react-flow__node')).toHaveCount(2);
  await page.locator('.react-flow__node').filter({hasText:'pong'}).click();
  await page.getByRole('button',{name:'Expand selected'}).click();
  await expect(page.locator('.counts')).toContainText('2/24 requests');
  await expect(page.locator('.react-flow__node')).toHaveCount(2);
  await page.getByRole('button',{name:'Collapse selected'}).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(2);
  await page.getByLabel('Snapshot identity').selectOption(versions.items[0].index_id);
  await expect(page.locator('.react-flow__node')).toHaveCount(0);
  await expect(page.locator('.monaco-editor')).toHaveCount(0);
  await page.getByLabel('Function or method').fill('changed_only');await page.locator('.result').click();
  await expect(page.locator('.view-lines')).toContainText('return 99');
  await page.getByLabel('Function or method').fill('no_such_symbol');
  await expect(page.getByText('No matching symbols.')).toBeVisible();
  expect(errors).toEqual([]);
});

test('failed graph request is actionable and does not create fake nodes', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Function or method').fill('right');
  await page.route('**/api/neighbors?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'database_error',message:'owned failure injection'}})}));
  await page.locator('.result').click();
  await expect(page.getByRole('alert')).toContainText('database_error');
  await expect(page.locator('.react-flow__node')).toHaveCount(0);
  await page.unroute('**/api/neighbors?*');
  await page.locator('.result').click();
  await expect(page.locator('.react-flow__node')).not.toHaveCount(0);
});


test('UTF-8 physical columns map to UTF-16 for nodes and call sites', async ({ page, request }) => {
  // Enable only Monaco's public observation API, without changing facts or positions.
  await page.addInitScript(() => Object.assign(globalThis, { MonacoEnvironment: { globalAPI: true } }));
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  const indexId = versions.items[1].index_id;
  await page.getByLabel('Snapshot identity').selectOption(indexId);
  const cases = [
    ['unicode_entry', 6, 'int unicode_entry() { const char *s = u8"汉😀"; (void)s; return ', 'int '],
    ['han_entry', 7, 'int han_entry() { const char *s = u8"汉"; (void)s; return ', 'int '],
    ['emoji_entry', 8, 'int emoji_entry() { const char *s = u8"😀"; (void)s; return ', 'int '],
    ['ascii_entry', 9, 'int ascii_entry() { const char *s = "ascii"; (void)s; return ', 'int '],
    ['named_after_unicode', 10, '/*汉😀*/ int named_after_unicode() { return ', '/*汉😀*/ int '],
    ['combining_entry', 11, 'int combining_entry() { const char *s = u8"a\u0301"; (void)s; return ', 'int '],
    ['eol_entry', 12, '/*汉😀*/ int eol_entry() { return ', '/*汉😀*/ int '],
  ] as const;
  for (const [name, line, prefix, definitionPrefix] of cases) {
    await page.getByLabel('Function or method').fill(name);
    await page.locator('.result').filter({ hasText: name }).click();
    await expect(page.locator('.react-flow__edge')).toHaveCount(1);
    const byteColumn = new TextEncoder().encode(prefix).length + 1;
    const definitionByte = new TextEncoder().encode(definitionPrefix).length + 1;
    const calls = await (await request.get('/api/calls', { params: { index_id: indexId, usr: `c:@F@${name}#`, direction: 'callees' } })).json();
    expect(calls.items).toHaveLength(1);
    expect(calls.items[0].column_number).toBe(byteColumn);
    const start = Math.max(1, line - 8);
    const saved = await (await request.get('/api/source', { params: { index_id: indexId, path: calls.items[0].file, start_line: start, end_line: line + 16, clamp_end: 'true' } })).json();
    const model = () => page.evaluate(() => {
      const host = globalThis as typeof globalThis & { monaco: { editor: { getEditors(): Array<{ getPosition(): { lineNumber: number; column: number } | null; getModel(): { getValue(): string } | null }> } } };
      const editor = host.monaco?.editor.getEditors()[0];
      return editor ? { position: editor.getPosition(), content: editor.getModel()?.getValue() } : null;
    });
    await expect(page.locator('.source-heading')).toContainText(`:${line}:${definitionByte}`);
    await expect.poll(model).toEqual({ position: { lineNumber: line - start + 1, column: definitionPrefix.length + 1 }, content: saved.content });
    await page.locator('.react-flow__edge').click({ force: true });
    await expect(page.locator('.source-heading')).toContainText(`:${line}:${byteColumn}`);
    await expect(page.locator('.source-heading')).toContainText(indexId);
    await expect.poll(model).toEqual({ position: { lineNumber: line - start + 1, column: prefix.length + 1 }, content: saved.content });
  }
});

test('a delayed expansion preserves a later real node selection', async ({ page, request }) => {
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  const indexId = versions.items[1].index_id;
  await page.getByLabel('Snapshot identity').selectOption(indexId);
  await page.getByLabel('Function or method').fill('ping');
  await page.locator('.result').filter({ hasText: 'ping' }).click();
  const ping = page.locator('.react-flow__node').filter({ hasText: 'ping' });
  const pong = page.locator('.react-flow__node').filter({ hasText: 'pong' });
  await pong.click();
  await expect(page.locator('.call-sites > strong')).toHaveText('pong');
  let release!: () => void, started!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const sent = new Promise<void>(resolve => { started = resolve; });
  await page.route('**/api/neighbors?*', async route => {
    if (new URL(route.request().url()).searchParams.get('usr') === 'c:@F@pong#I#') { started(); await gate; }
    // Control timing only; all Clang/SQLite/API facts and responses remain real.
    await route.continue();
  });
  try {
    await page.getByRole('button', { name: 'Expand selected' }).click();
    await sent;
    await ping.click();
    await expect(page.locator('.call-sites > strong')).toHaveText('ping');
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/neighbors' && new URL(r.url()).searchParams.get('usr') === 'c:@F@pong#I#');
    release(); await response;
    await expect(page.getByRole('button', { name: 'Expand selected' })).toBeEnabled();
    await expect(page.locator('.call-sites > strong')).toHaveText('ping');
    await expect(page.locator('.source-heading')).toContainText('/left/unit.cpp:5:5');
    await expect(page.locator('.source-heading')).toContainText(indexId);
  } finally { release(); }
});
