import { test, expect } from '@playwright/test';

test('refresh preserves a real historical graph, call evidence and saved source', async ({ page, request }) => {
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  const history = versions.items[1].index_id;
  await page.getByLabel('Snapshot identity').selectOption(history);
  await page.getByLabel('Function or method').fill('left');
  await page.locator('.result').click();
  await page.getByRole('button', { name: 'direct · unit.cpp:4:21', exact: true }).click();
  await expect(page.locator('.monaco-editor')).toBeVisible();
  await expect(page.locator('.view-lines')).toContainText('int left()');
  const heading = await page.locator('.source-heading').textContent();
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/indexes');
  await page.getByRole('button', { name: 'Refresh indexes' }).click();
  await response;
  await expect(page.getByText('Loading indexes…')).toHaveCount(0);
  await expect(page.getByLabel('Snapshot identity')).toHaveValue(history);
  await expect(page.locator('.react-flow__node')).toHaveCount(4);
  await expect(page.locator('.react-flow__edge')).toHaveCount(4);
  await expect(page.locator('.source-heading')).toHaveText(heading!);
  await expect(page.locator('.view-lines')).toContainText('int left()');
});

test('a delayed real refresh does not override a newer manual history selection', async ({ page, request }) => {
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  await expect(page.getByLabel('Snapshot identity')).toHaveValue(versions.items[0].index_id);
  let release!: () => void, started!: () => void;
  const gate = new Promise<void>(done => { release = done; });
  const sent = new Promise<void>(done => { started = done; });
  await page.route('**/api/indexes?*', async route => { started(); await gate; await route.continue(); });
  try {
    await page.getByRole('button', { name: 'Refresh indexes' }).click(); await sent;
    await page.getByLabel('Snapshot identity').selectOption(versions.items[1].index_id);
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/indexes');
    release(); await response;
    await expect(page.getByText('Loading indexes…')).toHaveCount(0);
    await expect(page.getByLabel('Snapshot identity')).toHaveValue(versions.items[1].index_id);
    await page.getByLabel('Function or method').fill('left'); await page.locator('.result').click();
    await expect(page.locator('.source-heading')).toContainText(versions.items[1].index_id);
    await expect(page.locator('.view-lines')).toContainText('int left()');
  } finally { release(); }
});

test('catalog failure and retry retain real historical content', async ({ page, request }) => {
  await page.goto('/');
  const versions = await (await request.get('/api/indexes')).json();
  const history = versions.items[1].index_id;
  await page.getByLabel('Snapshot identity').selectOption(history);
  await page.getByLabel('Function or method').fill('left'); await page.locator('.result').click();
  await expect(page.locator('.view-lines')).toContainText('int left()');
  await page.route('**/api/indexes?*', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'database_error', message: 'owned catalog failure injection' } }) }));
  await page.getByRole('button', { name: 'Refresh indexes' }).click();
  await expect(page.locator('.sidebar .error[role="alert"]')).toContainText('database_error');
  await expect(page.getByLabel('Snapshot identity')).toHaveValue(history);
  await expect(page.locator('.react-flow__node')).toHaveCount(4);
  await expect(page.locator('.view-lines')).toContainText('int left()');
  await page.unroute('**/api/indexes?*');
  await page.getByRole('button', { name: 'Retry indexes' }).click();
  await expect(page.locator('.sidebar .error[role="alert"]')).toHaveCount(0);
  await expect(page.getByLabel('Snapshot identity')).toHaveValue(history);
  await expect(page.locator('.source-heading')).toContainText(history);
  await expect(page.locator('.view-lines')).toContainText('int left()');
});
