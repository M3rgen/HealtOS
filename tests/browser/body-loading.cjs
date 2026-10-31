// Synthetic profile data and a delayed module response exercise the production loader wiring.
const assert = require('node:assert/strict');
const { BASE, launch, run, until } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

const viewer = `export function createBody(container, motion) {
  window.viewerState = { creations: 1, initialMotion: motion, metrics: [], motion: [] };
  return {
    setBodyMetrics(value) { window.viewerState.metrics.push(value); },
    setMotion(value) { window.viewerState.motion.push(value); },
    dispose() {},
  };
}`;
const bodyScript = pathname => /\/(?:assets\/body-[^/]+|src\/body)\.js$/.test(pathname);

async function setup(browser, size, mode = 'delayed') {
  const page = await browser.newPage({ viewport: size });
  const state = { weight: 74.6, dashboards: 0, imports: 0, errors: [] };
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  page.on('pageerror', error => state.errors.push(error.message));
  // Other UI observers continue to use the browser implementation. Only the body's delivery
  // is controlled, so CI speed cannot race the loader's five-second safety timer.
  await page.addInitScript(({ noObserver, nativeVisibility }) => {
    const native = window.IntersectionObserver;
    window.bodyPaints = 0;
    requestAnimationFrame(() => { window.bodyPaints++; requestAnimationFrame(() => window.bodyPaints++); });
    if (nativeVisibility) {
      const watcher = new MutationObserver(() => {
        const model = document.getElementById('body-model');
        if (model) { model.hidden = true; watcher.disconnect(); }
      });
      watcher.observe(document, { childList: true, subtree: true });
    }
    const timeout = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => {
      if (delay === 5000) { window.bodyFallback = callback; return -1; }
      return timeout(callback, delay, ...args);
    };
    if (nativeVisibility) return;
    if (noObserver) { window.IntersectionObserver = undefined; return; }
    window.IntersectionObserver = class {
      constructor(callback, options) {
        this.callback = callback;
        this.native = new native(callback, options);
      }
      observe(element) {
        if (element.id === 'body-model') {
          window.bodyObserved = true;
          window.bodyIntersection = visible => this.callback([{ target: element, isIntersecting: visible }]);
        } else this.native.observe(element);
      }
      unobserve(element) { this.native.unobserve(element); }
      disconnect() { this.native.disconnect(); }
    };
  }, { noObserver: mode === 'no-observer', nativeVisibility: mode === 'native' });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (bodyScript(url.pathname)) {
      state.imports++;
      assert.ok(await page.evaluate(() => window.bodyPaints >= 2), 'body import must follow the initial paint');
      await gate;
      // The build preloads the chunk (<link rel=modulepreload>) while its CSS loads, then imports
      // it. After a network abort Chromium fetches the module again for import() unless the
      // two requests happened to overlap, so an abort yields one or two requests at random.
      // A module that fails to evaluate rejects the same import() with exactly one request.
      if (mode === 'failure') return route.fulfill({ contentType: 'text/javascript', body: 'throw new Error("synthetic body failure");' });
      return route.fulfill({ contentType: 'text/javascript', body: viewer });
    }
    if (url.pathname === '/health') return route.fulfill({ json: fx.health });
    if (url.pathname === '/api/dashboard') {
      state.dashboards++;
      const data = fx.dashboard(url.searchParams);
      data.profile.weight.value = state.weight;
      return route.fulfill({ json: data });
    }
    if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (url.pathname === '/api/dashboard/workouts') return route.fulfill({ json: fx.workouts(url.searchParams) });
    if (/^\/(api|sync|auth)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  return { page, state, release };
}
async function fits(page) {
  assert.ok(await page.evaluate(() => {
    const root = document.scrollingElement;
    return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth;
  }), 'the main page must fit the desktop/mobile viewport');
}
async function refreshWeight(page, state, weight) {
  state.weight = weight;
  const previous = state.dashboards;
  await page.locator('#refresh-system').click();
  await until(() => state.dashboards > previous, 'profile refresh received');
  await page.waitForFunction(weight => document.querySelector('#profile-weight').textContent.includes(weight), String(weight).replace('.', ','));
}

run(async () => {
  const browser = await launch();
  try {
    for (const size of [{ width: 1280, height: 800 }, { width: 390, height: 844 }, { width: 360, height: 640 }]) {
      const native = await setup(browser, size, 'native');
      try {
        await native.page.waitForFunction(() => window.bodyPaints === 2);
        assert.ok(await native.page.locator('#body-model').evaluate(element => element.hidden));
        assert.equal(native.state.imports, 0);
        await native.page.evaluate(() => { document.querySelector('#body-model').hidden = false; });
        await until(() => native.state.imports === 1, 'native observer sees the revealed model');
        native.release();
        await native.page.waitForFunction(() => window.viewerState?.creations === 1);
        await fits(native.page);
        assert.deepEqual(native.state.errors, []);
        console.log(`PASS ${size.width}: real IntersectionObserver loads a newly visible model`);
      } finally { native.release(); await native.page.close(); }

      const { page, state, release } = await setup(browser, size);
      try {
        await page.waitForFunction(() => window.bodyObserved);
        await page.evaluate(() => window.bodyIntersection(false));
        assert.equal(state.imports, 0, 'no body request before intersection');
        assert.equal(await page.locator('#body-model').getAttribute('role'), 'img');
        assert.match(await page.locator('#body-model').getAttribute('aria-label'), /beden modeli/);
        assert.ok(await page.locator('#body-model .model-fallback').isVisible());
        await fits(page);
        await page.locator('[data-page="system"]').click();
        await page.locator('#model-motion').check();
        await refreshWeight(page, state, 81.2);
        assert.equal(state.imports, 0);
        await page.locator('[data-page="overview"]').click();
        await page.evaluate(() => window.bodyIntersection(true));
        await until(() => state.imports === 1, 'one body module request');
        assert.ok(await page.locator('#body-model .model-fallback').isVisible(), 'silhouette stays visible during import');
        await page.locator('[data-page="system"]').click();
        await refreshWeight(page, state, 83.4);
        await page.emulateMedia({ reducedMotion: 'reduce' });
        release();
        await page.waitForFunction(() => window.viewerState?.metrics.length > 0);
        const loaded = await page.evaluate(() => window.viewerState);
        assert.equal(loaded.creations, 1);
        assert.equal(loaded.initialMotion, false, 'hidden model and current motion preference apply at creation');
        assert.deepEqual(loaded.metrics.map(value => value.weightKg), [83.4], 'only the latest pending measurements apply');
        assert.equal(loaded.motion.at(-1), false);
        assert.ok(await page.locator('#refresh-system').evaluate(element => element === document.activeElement), 'module completion preserves focus');
        await page.locator('[data-page="overview"]').click();
        await page.emulateMedia({ reducedMotion: 'no-preference' });
        await page.waitForFunction(() => window.viewerState.motion.at(-1) === true);
        await page.evaluate(() => {
          Object.defineProperty(document, 'hidden', { configurable: true, value: true });
          document.dispatchEvent(new Event('visibilitychange'));
        });
        assert.equal(await page.evaluate(() => window.viewerState.motion.at(-1)), false);
        await page.evaluate(() => {
          delete document.hidden;
          document.dispatchEvent(new Event('visibilitychange'));
          window.bodyIntersection(true); window.bodyIntersection(false); window.bodyFallback();
        });
        assert.equal(await page.evaluate(() => window.viewerState.motion.at(-1)), true);
        assert.equal(state.imports, 1);
        await fits(page);
        assert.deepEqual(state.errors, []);
        console.log(`PASS ${size.width}: deferred single import, latest metrics/motion, silhouette and focus`);
      } finally { release(); await page.close(); }

      const failed = await setup(browser, size, 'failure');
      try {
        await failed.page.waitForFunction(() => window.bodyObserved);
        await failed.page.evaluate(() => window.bodyIntersection(true));
        failed.release();
        await failed.page.waitForFunction(() => document.querySelector('#body-model').dataset.modelFailed === 'true');
        assert.ok(await failed.page.locator('#body-model .model-fallback').isVisible());
        await failed.page.locator('[data-page="system"]').click();
        await refreshWeight(failed.page, failed.state, 79.1);
        await failed.page.locator('[data-page="sports"]').click();
        await failed.page.waitForFunction(() => document.querySelectorAll('.workout-entry').length > 0);
        await failed.page.locator('[data-page="overview"]').click();
        await failed.page.locator('#profile-weight').click();
        assert.ok(await failed.page.locator('#measurement-dialog').evaluate(element => element.open));
        await failed.page.keyboard.press('Escape');
        await fits(failed.page);
        assert.equal(failed.state.imports, 1);
        assert.deepEqual(failed.state.errors, []);
        console.log(`PASS ${size.width}: failed import keeps profile, navigation, sports and measurements working`);
      } finally { failed.release(); await failed.page.close(); }
    }
    const unsupported = await setup(browser, { width: 1280, height: 800 }, 'no-observer');
    try {
      unsupported.release();
      await unsupported.page.waitForFunction(() => window.viewerState?.creations === 1);
      assert.equal(unsupported.state.imports, 1);
      assert.deepEqual(unsupported.state.errors, []);
      console.log('PASS missing IntersectionObserver: body still loads after painting');
    } finally { unsupported.release(); await unsupported.page.close(); }
  } finally { await browser.close(); }
});
