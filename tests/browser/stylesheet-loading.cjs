// Catch missing or reordered stylesheet imports in the production build.
// Synthetic dashboard data; fonts and artwork must load from the real local assets.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const problems = [];
      const stylesheets = [];
      page.on('pageerror', error => problems.push(error.message));
      page.on('response', response => {
        const type = response.request().resourceType();
        if (['stylesheet', 'font', 'image'].includes(type) && !response.ok()) {
          problems.push(`${response.status()} ${response.url()}`);
        }
        if (type === 'stylesheet' && response.ok()) stylesheets.push(new URL(response.url()).pathname);
      });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== new URL(BASE).origin) {
          problems.push(`external request: ${url.href}`);
          return route.abort();
        }
        if (url.pathname === '/health') return route.fulfill({ json: fx.health });
        if (url.pathname === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
        if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
        if (/^\/(api|sync|auth)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(url => url.searchParams.has('token'), ws => ws.send(JSON.stringify({ type: 'connected' })));
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.documentElement.dataset.appearanceReady === '');
      const state = await page.evaluate(async () => {
        const style = (selector, pseudo) => getComputedStyle(document.querySelector(selector), pseudo);
        const fonts = await document.fonts.load('600 16px "Inter Variable"', 'Eşitleme ığüşöç İĞÜŞÖÇ');
        // CSS backgrounds have no naturalWidth: decode their actual computed URLs explicitly.
        const artwork = [style('html').backgroundImage, style('body', '::before').backgroundImage];
        const urls = artwork.flatMap(value => [...value.matchAll(/url\("?([^"\)]+)"?\)/g)].map(match => match[1]));
        for (const url of urls) {
          const image = new Image();
          image.src = url;
          await image.decode();
        }
        await Promise.all([...document.querySelectorAll('.assistant-avatar')].map(image => image.decode()));
        return {
          fontFamily: style('body').fontFamily,
          fontsLoaded: fonts.length > 0 && fonts.every(font => font.status === 'loaded'),
          modelPosition: style('#body-model').position,
          panelRadius: style('.profile-panel').borderTopLeftRadius,
          bodyBackground: style('body').backgroundColor,
          pageBackground: style('html').backgroundColor,
          sheets: [...document.styleSheets].map(sheet => sheet.cssRules.length),
          artwork: urls.map(url => new URL(url).pathname),
          brandStroke: style('.brand .brand-mark').stroke,
          overflow: document.scrollingElement.scrollHeight > innerHeight || document.scrollingElement.scrollWidth > innerWidth,
        };
      });
      assert.match(state.fontFamily, /Inter Variable/);
      assert.equal(state.fontsLoaded, true, 'bundled Turkish font subsets must load');
      assert.equal(state.modelPosition, 'relative');
      assert.equal(state.panelRadius, '14px', 'visual polish must override the base panel radius');
      assert.ok(stylesheets.length > 0, 'the built stylesheet must be fetched');
      assert.ok(state.sheets.length > 0 && state.sheets.every(rules => rules > 0), 'every stylesheet must parse');
      assert.equal(state.bodyBackground, 'rgba(0, 0, 0, 0)', 'asset canvas must override the polish background');
      assert.equal(state.pageBackground, 'rgb(24, 24, 20)', 'the root canvas keeps the --page colour');
      assert.deepEqual(state.artwork, ['/assets/bg/grain-tile.webp', '/assets/bg/vignette.webp'],
        'the root background must not reset the grain tile (no `background` shorthand on :root)');
      assert.equal(state.brandStroke, 'none', 'appearance must preserve the full-colour header artwork');
      assert.equal(state.overflow, false);
      await page.locator('[data-page="system"]').click();
      await page.locator('#appearance-open').click();
      const label = page.locator('#appearance-dialog .preset-label').first();
      await label.waitFor();
      assert.equal(await label.evaluate(element => getComputedStyle(element).whiteSpace), 'normal');
      assert.equal(await label.evaluate(element => getComputedStyle(element).webkitLineClamp), '2');
      // A background chosen in Görünüm must reach the page canvas behind the dashboard.
      await page.locator('input[name="appearance-background"]:checked').focus();
      await page.keyboard.press('ArrowRight');
      await page.locator('input[name="appearance-background"][value="fine-grain"]:checked').waitFor();
      await page.locator('#appearance-done').click();
      await page.locator('[data-page="overview"]').click();
      const chosen = await page.evaluate(() => getComputedStyle(document.documentElement).backgroundImage);
      assert.match(chosen, /url\("?[^")]*\/assets\/bg\/grain-overlay\.webp"?\)/, 'the chosen background must show on the dashboard');
      assert.deepEqual(problems, []);
      console.log(`PASS ${width}x${height}: stylesheet cascade, Turkish fonts, artwork and model layout load`);
      await page.close();
    }
  } finally { await browser.close(); }
});
