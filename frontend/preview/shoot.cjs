#!/usr/bin/env node
// Dev-only preview harness. Targets an already running Vite dev server, mocks
// every backend route with SYNTHETIC fixtures, screenshots each view at each
// viewport and fails when the main page can scroll in any direction.
//
//   node preview/shoot.cjs --out /path/to/dir [--base http://127.0.0.1:5174]
//        [--viewports 1920x976,390x844] [--views overview,umay] [--reduced-motion]
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const fx = require('./fixtures.cjs');

const VIEWPORTS = ['1920x976', '1440x900', '1280x720', '1024x650', '390x844', '320x700'];
const VIEWS = {
  overview: async () => {},
  'history-drag': async (page) => {
    // Partial drag: the sheet must follow the pointer and keep its released height.
    const handle = await page.locator('#history-toggle').boundingBox();
    const x = handle.x + handle.width / 2, y = handle.y + handle.height / 2;
    const distance = Math.round(Math.min(220, Math.max(60, (y - 120) * 0.45)));
    await page.mouse.move(x, y); await page.mouse.down();
    await page.mouse.move(x, y - distance, { steps: 10 }); await page.mouse.up();
    await page.waitForTimeout(250);
  },
  'history-open': async (page) => {
    await page.locator('#history-toggle').click();
    await page.waitForFunction(() => document.querySelector('#history-panel.expanded') && document.querySelector('#history-status').hidden && document.querySelectorAll('#history-rows tr').length > 0);
  },
  'metric-history': async (page) => {
    await page.locator('.steps-card').click();
    await page.locator('#metric-history-days li').first().waitFor();
  },
  calendar: async (page) => {
    if (!(await page.locator('#header-date').isVisible())) return 'skip';
    await page.locator('#header-date').click();
    await page.locator('#date-calendar').waitFor();
  },
  'umay-intro': async (page) => {
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
    await page.evaluate(() => document.querySelector('.assistant-intro img').decode());
  },
  umay: async (page) => {
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
    await page.locator('#assistant-input').fill('Son haftamı özetler misin?');
    await page.locator('#assistant-send').click();
    await page.waitForFunction(() => document.querySelector('.chat-assistant') && !document.querySelector('#assistant-stop:not([hidden])'));
  },
  measurements: async (page) => {
    await page.locator('[data-page="measurements"]').click();
    await page.locator('#measurement-rows tr').first().waitFor();
  },
  'measurement-dialog': async (page) => {
    await page.locator('[data-page="measurements"]').click();
    await page.locator('#measurements-add').click();
    await page.locator('#measurement-dialog[open]').waitFor();
  },
  system: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.locator('#sync-rows tr').first().waitFor();
  },
  sports: async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('#workout-rows tr').first().waitFor();
  },
  'sports-editor': async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('#workout-rows tr').first().waitFor();
    await page.locator('#workout-add').click();
  },
  appearance: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.waitForFunction(() => 'appearanceReady' in document.documentElement.dataset);
    await page.locator('#appearance-open').click();
    await page.locator('#appearance-dialog[open] .preset-tile').first().waitFor();
  },
  // Lane C presets applied, dialog closed: shows them on the real dashboard.
  'appearance-preset': async (page) => {
    await VIEWS.appearance(page);
    for (const [kind, id] of [['logo', 'dial'], ['umay', 'engraved'], ['background', 'kilim']]) {
      await page.locator(`input[name="appearance-${kind}"][value="${id}"]`).check();
    }
    await page.locator('#appearance-done').click();
    await page.locator('[data-page="overview"]').click();
    await page.waitForFunction(() => [...document.images].every(image => image.complete));
  },
  'appearance-uploaded': async (page) => {
    await VIEWS.appearance(page);
    const files = [['logo', 'logo.svg', 'image/svg+xml', fx.uploadLogoSvg()], ['umay', 'umay.png', 'image/png', fx.uploadAvatarPng()],
      ['background', 'zemin.png', 'image/png', fx.uploadBackgroundPng()]];
    for (const [kind, name, mimeType, buffer] of files) {
      await page.locator(`input[data-upload="${kind}"]`).setInputFiles({ name, mimeType, buffer });
      await page.locator(`input[name="appearance-${kind}"][value="upload"]:checked`).waitFor();
    }
    if (await page.evaluate(() => window.uploadedSvgRan || document.getElementById('uploaded-svg-marker'))) throw new Error('uploaded SVG reached the DOM');
  },
  // Edge cases with alternate fixtures (see scenario() in fixtures.cjs).
  notice: { scenario: 'sync-error', run: async (page) => { await page.locator('#notice:not([hidden])').waitFor(); } },
  'empty-overview': { scenario: 'empty', run: async () => {} },
  'empty-history': { scenario: 'empty', run: async (page) => {
    await page.locator('.steps-card').click();
    await page.locator('#metric-history-chart .history-empty').waitFor();
  } },
  'empty-system': { scenario: 'empty', run: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.locator('#google-empty:not([hidden])').waitFor();
  } },
  'empty-sports': { scenario: 'empty', run: async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('.workouts-panel[data-empty]').waitFor();
  } },
};

function args() {
  const out = { base: 'http://127.0.0.1:5174', viewports: VIEWPORTS, views: Object.keys(VIEWS), reducedMotion: false };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const [key, inline] = argv[i].split('=');
    const value = () => inline ?? argv[++i];
    if (key === '--out') out.out = value();
    else if (key === '--base') out.base = value().replace(/\/$/, '');
    else if (key === '--viewports') out.viewports = value().split(',');
    else if (key === '--views') out.views = value().split(',');
    else if (key === '--reduced-motion') out.reducedMotion = true;
    else if (key === '--help' || key === '-h') { console.log(fs.readFileSync(__filename, 'utf8').split('\n').slice(1, 7).join('\n')); process.exit(0); }
    else throw new Error(`Unknown argument ${argv[i]}`);
  }
  if (!out.out) throw new Error('--out <dir> is required');
  const unknown = out.views.filter(v => !VIEWS[v]);
  if (unknown.length) throw new Error(`Unknown views: ${unknown.join(', ')} (known: ${Object.keys(VIEWS).join(', ')})`);
  return out;
}

async function mock(page, base, problems, scenario) {
  const origin = new URL(base).origin;
  const data = { ...fx, ...fx.scenario(scenario) };
  const call = (value, q) => typeof value === 'function' ? value(q) : value;
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== origin) { problems.push(`remote request blocked: ${url.href}`); return route.abort(); }
    const p = url.pathname, q = url.searchParams;
    if (p === '/health') return json(route, data.health);
    if (p === '/api/dashboard') return json(route, call(data.dashboard, q));
    if (p === '/api/dashboard/history') return json(route, call(data.history, q));
    if (p === '/api/dashboard/measurements') return method === 'POST'
      ? json(route, { id: 'new', ...route.request().postDataJSON(), source: 'Manuel' }, 201) : json(route, data.measurements);
    if (p === '/api/dashboard/workouts') return method === 'POST'
      ? json(route, { id: 'new', ...route.request().postDataJSON(), source: 'Manuel' }, 201) : json(route, call(data.workouts, q));
    if (p === '/api/dashboard/export.csv') return route.fulfill({ body: 'date,steps\n', contentType: 'text/csv' });
    if (p === '/sync/status') return json(route, data.syncStatus);
    if (p === '/sync/all') return json(route, { status: 'accepted', data_types: ['steps'] }, 202);
    if (/^\/(api|sync|auth|docs|health|google-health)(\/|$)/.test(p)) { problems.push(`unmocked ${method} ${p}`); return json(route, { detail: 'preview: not mocked' }, 404); }
    return route.continue();
  });
  // Freeze Vite HMR: other edits to the tree must not reload a page mid-shot.
  await page.routeWebSocket((url) => url.searchParams.has('token') && url.pathname === '/', (ws) => {
    ws.send(JSON.stringify({ type: 'connected' }));
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, (ws) => {
    ws.send(JSON.stringify({ type: 'ready', model: 'önizleme', effort: 'medium' }));
    ws.onMessage((raw) => {
      const message = JSON.parse(String(raw));
      if (!message.text) return;
      ws.send(JSON.stringify({ type: 'working', text: 'Umay düşünüyor…' }));
      const parts = fx.chatAnswer.match(/[\s\S]{1,48}/g);
      parts.forEach((text, i) => setTimeout(() => ws.send(JSON.stringify({ type: 'delta', id: 'a1', text })), 80 + i * 15));
      setTimeout(() => ws.send(JSON.stringify({ type: 'done', status: 'completed' })), 120 + parts.length * 15);
    });
  });
}

async function measure(page) {
  return page.evaluate(() => {
    const s = document.scrollingElement;
    return { sh: s.scrollHeight, ih: innerHeight, sw: s.scrollWidth, iw: innerWidth };
  });
}

(async () => {
  const opt = args();
  fs.mkdirSync(opt.out, { recursive: true });
  // channel 'chromium' = full Chromium in new headless mode; it uses the GPU for
  // the WebGL body model instead of SwiftShader, which is ~10x cheaper.
  const browser = await chromium.launch({ headless: true, channel: 'chromium' });
  const rows = [];
  let failed = false;
  try {
    await Promise.all(opt.viewports.map(async (viewport) => {
      const [width, height] = viewport.split('x').map(Number);
      const contextOptions = { viewport: { width, height }, deviceScaleFactor: 1,
        locale: 'tr-TR', timezoneId: 'UTC', reducedMotion: opt.reducedMotion ? 'reduce' : 'no-preference',
        hasTouch: width < 600, isMobile: false };
      for (const view of opt.views) {
        // A fresh context per view: stored appearance/uploads never leak between views.
        const context = await browser.newContext(contextOptions);
        const page = await context.newPage();
        const problems = [];
        page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
        await page.clock.setFixedTime(new Date(fx.NOW));
        const spec = typeof VIEWS[view] === 'function' ? { run: VIEWS[view] } : VIEWS[view];
        await mock(page, opt.base, problems, spec.scenario);
        await page.goto(opt.base, { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
        await page.evaluate(() => document.fonts.ready);
        await page.waitForSelector('#body-model[data-model-loaded]', { timeout: 6000 }).catch(() => {});
        await page.waitForTimeout(500); // let the body model fade in and settle
        let result;
        try { result = await spec.run(page); } catch (e) { problems.push(`view step failed: ${e.message.split('\n')[0]}`); }
        const order = opt.viewports.indexOf(viewport) * 100 + opt.views.indexOf(view);
        if (result === 'skip') { rows.push({ order, viewport, view, skipped: true }); await context.close(); continue; }
        await page.waitForFunction(() => document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getComputedTiming().iterations === Infinity), null, { timeout: 2000 }).catch(() => {});
        await page.waitForTimeout(150);
        const m = await measure(page);
        const overflow = m.sh > m.ih || m.sw > m.iw;
        await page.screenshot({ path: path.join(opt.out, `${viewport}-${view}.png`) });
        if (overflow || problems.length) failed = true;
        rows.push({ order, viewport, view, ...m, overflow, problems });
        await context.close();
      }
    }));
  } finally { await browser.close(); }

  rows.sort((a, b) => a.order - b.order);
  const pad = (s, n) => String(s).padEnd(n);
  console.log(`${pad('viewport', 11)}${pad('view', 20)}${pad('scrollH/innerH', 16)}${pad('scrollW/innerW', 16)}result`);
  for (const r of rows) {
    if (r.skipped) { console.log(`${pad(r.viewport, 11)}${pad(r.view, 20)}${pad('-', 16)}${pad('-', 16)}skipped (not applicable)`); continue; }
    console.log(`${pad(r.viewport, 11)}${pad(r.view, 20)}${pad(`${r.sh}/${r.ih}`, 16)}${pad(`${r.sw}/${r.iw}`, 16)}${r.overflow ? 'OVERFLOW' : 'ok'}`);
    for (const p of r.problems) console.log(`    ! ${p}`);
  }
  console.log(`\nScreenshots: ${path.resolve(opt.out)}`);
  console.log(failed ? 'FAIL: page overflow or problems detected' : 'PASS: no main-page overflow in any view');
  process.exit(failed ? 1 : 0);
})().catch((error) => { console.error(error); process.exit(2); });
