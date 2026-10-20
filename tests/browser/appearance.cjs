// Synthetic fixtures; no live account records.
// Appearance settings: preset selection persists across reloads, "Varsayılana dön"
// restores defaults, uploads are validated, and an uploaded SVG is only ever shown
// through a blob: URL (its markup and script never reach the DOM).
// Run with `cd frontend && npm run test:browser`, or against your own Vite server:
// HEALTHOS_TEST_URL=http://127.0.0.1:5174 node tests/browser/appearance.cjs
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

async function mock(page, problems) {
  const origin = new URL(BASE).origin;
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) { problems.push(`external request: ${url.href}`); return route.abort(); }
    const p = url.pathname;
    if (p === '/health') return route.fulfill({ json: fx.health });
    if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
    if (p === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  // Keep Vite HMR from reloading the page mid-test.
  await page.routeWebSocket(u => u.searchParams.has('token'), ws => ws.send(JSON.stringify({ type: 'connected' })));
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
}

(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      const page = await context.newPage();
      const problems = [];
      page.on('pageerror', e => problems.push(`pageerror: ${e.message}`));
      await mock(page, problems);
      const load = async () => {
        await page.goto(BASE);
        await page.waitForFunction(() => document.documentElement.dataset.appearanceReady === '');
      };
      const state = () => page.evaluate(() => ({
        avatar: document.querySelector('.assistant-avatar').getAttribute('src'),
        tile: getComputedStyle(document.documentElement).getPropertyValue('--bg-tile'),
        inlineMarkHidden: document.querySelector('.topbar .brand-mark').hasAttribute('hidden'),
        customMark: document.querySelector('.brand-mark-custom img')?.getAttribute('src') ?? null,
        favicon: document.getElementById('favicon-primary')?.getAttribute('href'),
        overflow: document.scrollingElement.scrollHeight > innerHeight || document.scrollingElement.scrollWidth > innerWidth,
      }));
      const openDialog = async () => {
        await page.locator('[data-page="system"]').click();
        await page.locator('#appearance-open').click();
        await page.locator('#appearance-dialog[open] .preset-tile').first().waitFor();
      };

      await load();
      let s = await state();
      assert.match(s.avatar, /umay-64\.webp$/); assert.equal(s.inlineMarkHidden, false);
      // Default logo is the yurt ("Otağ"), painted inline in index.html before appearance.js runs.
      assert.equal(await page.locator('.topbar .brand-mark[data-preset="yurt"]').count(), 1);
      assert.match(s.favicon, /logo\/yurt\/favicon-32\.png$/);
      assert.match(await page.locator('#favicon-small').getAttribute('href'), /logo\/yurt\/favicon-16\.png$/);

      // Select presets: mouse for Umay, keyboard (arrow within the radio group) for background.
      await openDialog();
      await page.locator('#appearance-dialog label.preset-tile', { hasText: 'Umay · afiş' }).click();
      await page.locator('input[name="appearance-background"]:checked').focus();
      await page.keyboard.press('ArrowRight');
      await page.locator('input[name="appearance-background"][value="fine-grain"]:checked').waitFor();
      s = await state();
      assert.match(s.avatar, /umay-alt\.webp$/); assert.match(s.tile, /grain-overlay\.webp/);
      assert.equal(s.overflow, false);

      // A lane C logo preset: full-colour mark in the header, PNG tab-icon fallbacks.
      await page.locator('#appearance-dialog label.preset-tile', { hasText: 'Kadran' }).click();
      s = await state();
      assert.equal(s.inlineMarkHidden, true); assert.match(s.customMark, /logo\/dial\/mark\.svg$/); assert.match(s.favicon, /logo\/dial\/favicon\.svg$/);
      assert.match(await page.locator('#favicon-small').getAttribute('href'), /dial\/favicon-16\.png$/);

      // Persists across reload.
      await load();
      s = await state();
      assert.match(s.avatar, /umay-alt\.webp$/, 'Umay preset must persist across reload');
      assert.match(s.tile, /grain-overlay\.webp/, 'background preset must persist across reload');
      assert.match(s.customMark ?? '', /logo\/dial\/mark\.svg$/, 'logo preset must persist across reload');

      // Validation: wrong type and oversize files are rejected with a Turkish message.
      await openDialog();
      const logoInput = page.locator('input[data-upload="logo"]');
      await logoInput.setInputFiles({ name: 'not-an-image.txt', mimeType: 'text/plain', buffer: Buffer.from('merhaba') });
      await page.locator('[data-appearance-kind="logo"] .appearance-error:not([hidden])', { hasText: 'desteklenmiyor' }).waitFor();
      await logoInput.setInputFiles({ name: 'big.png', mimeType: 'image/png', buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
      await page.locator('[data-appearance-kind="logo"] .appearance-error:not([hidden])', { hasText: '2 MB' }).waitFor();

      // Uploaded SVG: rendered through a blob: <img> only; markup and script never in the DOM.
      await logoInput.setInputFiles({ name: 'logo.svg', mimeType: 'image/svg+xml', buffer: fx.uploadLogoSvg() });
      await page.locator('input[name="appearance-logo"][value="upload"]:checked').waitFor();
      const svgSafe = async () => {
        const result = await page.evaluate(() => ({
          marker: Boolean(document.getElementById('uploaded-svg-marker')),
          ran: Boolean(window.uploadedSvgRan),
          markup: document.documentElement.outerHTML.includes('uploadedSvgRan'),
          inlineSvgInCustom: Boolean(document.querySelector('.brand-mark-custom svg, .preset-thumb .appearance-mark svg circle[stroke="#48d88c"]')),
        }));
        assert.deepEqual(result, { marker: false, ran: false, markup: false, inlineSvgInCustom: false });
      };
      await svgSafe();
      s = await state();
      assert.equal(s.inlineMarkHidden, true); assert.match(s.customMark, /^blob:/); assert.match(s.favicon, /^blob:/);
      await page.locator('#appearance-done').click();

      await load();   // upload persists (IndexedDB) and is still only a blob: image
      s = await state();
      assert.match(s.customMark ?? '', /^blob:/, 'uploaded logo must persist across reload');
      await svgSafe();

      // Reset restores every default and stays reset after reload.
      await openDialog();
      await page.locator('#appearance-reset').click();
      s = await state();
      assert.match(s.avatar, /umay-64\.webp$/); assert.match(s.tile, /grain-tile\.webp/); assert.equal(s.inlineMarkHidden, false);
      assert.equal(await page.locator('input[name="appearance-logo"][value="yurt"]').isChecked(), true);
      assert.match(s.favicon, /logo\/yurt\/favicon-32\.png$/);
      assert.match(await page.locator('#favicon-small').getAttribute('href'), /logo\/yurt\/favicon-16\.png$/);
      assert.equal(await page.locator('link[rel="icon"][type="image/svg+xml"]').count(), 0, 'yurt default uses PNG/ICO tab icons only');
      assert.match(await page.locator('#favicon-fallback').getAttribute('href'), /logo\/yurt\/favicon\.ico$/);
      await load();
      s = await state();
      assert.match(s.avatar, /umay-64\.webp$/); assert.match(s.tile, /grain-tile\.webp/); assert.equal(s.inlineMarkHidden, false);
      assert.equal(s.overflow, false);

      // The kept upload can be removed.
      await openDialog();
      await page.locator('[data-appearance-kind="logo"] [data-remove]').click();
      await page.locator('input[name="appearance-logo"][value="upload"]').waitFor({ state: 'detached' });
      assert.equal(await page.locator('[data-appearance-kind="logo"] [data-remove]').isHidden(), true);

      assert.deepEqual(problems, []);
      console.log(`PASS ${width}x${height}: preset persistence, keyboard selection, validation, safe SVG upload, reset, remove`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exit(1); });
