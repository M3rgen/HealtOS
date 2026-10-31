// Synthetic fixtures; no live account records.
// A manual measurement flagged as a test entry stays in the list with its badge, the profile
// falls back to the measurement before it, clearing the flag brings it back; no page overflow.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, shot, run } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const LATER = { id: 'b3a1c2d4-0000-4000-8000-000000000002', metric: 'weight', value: 99.2, measured_at: '2026-09-30T07:30:00Z', source: 'Manuel' };
const EARLIER = { id: 'b3a1c2d4-0000-4000-8000-000000000001', metric: 'weight', value: 74.6, measured_at: '2026-09-23T07:40:00Z', source: 'Manuel' };

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 760]]) {
      const page = await browser.newPage({ viewport: { width, height }, hasTouch: width === 390 });
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      let flagged = false;
      const calls = [];
      await page.route('**/health', route => route.fulfill({ json: fx.health }));
      await page.route('**/sync/status', route => route.fulfill({ json: fx.syncStatus }));
      await page.route('**/api/dashboard?**', route => {
        const data = fx.dashboard(new URL(route.request().url()).searchParams);
        const weight = flagged ? EARLIER : LATER;
        route.fulfill({ json: { ...data, profile: { ...data.profile, weight: { value: weight.value, measured_at: weight.measured_at, source: 'Manuel' } } } });
      });
      await page.route('**/api/dashboard/measurements', route =>
        route.fulfill({ json: [{ ...LATER, flags: flagged ? ['test'] : [] }, { ...EARLIER, flags: [] }] }));
      await page.route('**/api/dashboard/measurements/*/*', route => {
        const request = route.request();
        const [, id, action] = new URL(request.url()).pathname.match(/measurements\/([^/]+)\/(flag|unflag)$/) ?? [];
        calls.push([request.method(), id, action, request.headers()['x-healthos-request'], request.postDataJSON()]);
        const changed = flagged !== (action === 'flag');
        flagged = action === 'flag';
        route.fulfill({ status: changed ? 201 : 200, json: { id, flags: flagged ? ['test'] : [], changed } });
      });
      const profileWeight = () => page.locator('#profile-values [data-metric="weight"]').textContent();
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelector('#profile-values [data-metric="weight"]')?.textContent.includes('99,2'));

      await page.locator('[data-page="measurements"]').click();
      await page.waitForFunction(() => document.querySelectorAll('#measurement-rows tr').length === 2);
      const first = page.locator('#measurement-rows tr').first();
      assert.equal(await first.locator('.dot').count(), 0);
      assert.match(await first.locator('.row-action').getAttribute('aria-label'), /^Test olarak işaretle: Kilo, \S/);
      await first.locator('.row-action').click();
      await page.waitForFunction(() => document.querySelector('#measurement-rows tr .dot')?.textContent === 'Test kaydı');
      assert.deepEqual(calls[0], ['POST', LATER.id, 'flag', '1', { flag: 'test' }]);
      // Still listed, marked as a test entry; the other row is untouched.
      assert.equal(await page.locator('#measurement-rows tr').count(), 2);
      assert.equal(await first.evaluate(row => row.classList.contains('flagged-row')), true);
      assert.equal(await first.locator('.row-action').textContent(), 'İşareti kaldır');
      // The accessible name starts with the visible text (WCAG 2.5.3).
      assert.match(await first.locator('.row-action').getAttribute('aria-label'), /^İşareti kaldır: Kilo, \S/);
      assert.equal(await page.locator('#measurement-rows tr').nth(1).locator('.dot').count(), 0);
      assert.match(await page.locator('#notice').textContent(), /test kaydı olarak işaretlendi/);
      await page.screenshot({ path: shot(`measurement-flags-${width}.png`), fullPage: false });
      const overflow = () => page.evaluate(() => ({
        x: document.documentElement.scrollWidth > innerWidth,
        y: document.documentElement.scrollHeight > innerHeight,
      }));
      assert.deepEqual(await overflow(), { x: false, y: false });

      await page.locator('[data-page="overview"]').click();
      assert.match(await profileWeight(), /74,6/);

      await page.locator('[data-page="measurements"]').click();
      await page.locator('#measurement-rows tr').first().locator('.row-action').click();
      await page.waitForFunction(() => !document.querySelector('#measurement-rows .dot'));
      assert.deepEqual(calls[1].slice(0, 3), ['POST', LATER.id, 'unflag']);
      assert.equal(await page.locator('#measurement-rows tr').first().locator('.row-action').textContent(), 'Test olarak işaretle');
      await page.locator('[data-page="overview"]').click();
      assert.match(await profileWeight(), /99,2/);
      assert.deepEqual(await overflow(), { x: false, y: false });
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}: test-entry badge, flag/unflag requests, profile falls back and returns, no page overflow`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
