// Synthetic data only. Keyboard helpers never call focus(), click() or scrollIntoView().
const assert = require('node:assert/strict');
const fx = require('../../../frontend/preview/fixtures.cjs');
const { STEP_TIMEOUT } = require('../lib.cjs');

async function contained(page) {
  const size = await page.evaluate(() => ({ width: innerWidth, height: innerHeight,
    x: document.scrollingElement.scrollWidth, y: document.scrollingElement.scrollHeight }));
  assert(size.x <= size.width && size.y <= size.height, JSON.stringify(size));
}

async function visibleFocus(page) {
  const result = await page.evaluate(() => {
    const el = document.activeElement;
    if (el === document.body) return { chrome: true };
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    const modal = document.querySelector('dialog:modal');
    return { name: el.id || el.outerHTML.slice(0, 100),
      visible: !el.closest('[hidden], [inert]') && !el.disabled && style.visibility === 'visible' &&
        r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth,
      contained: !modal || modal.contains(el),
    };
  });
  if (!result.chrome) assert(result.visible && result.contained, JSON.stringify(result));
  await contained(page);
}

async function active(page, selector) {
  return page.evaluate(selector => document.activeElement.matches(selector), selector);
}

async function tabTo(page, selector, reverse = false, limit = 150) {
  for (let step = 0; step < limit; step++) {
    if (await active(page, selector)) { await visibleFocus(page); return; }
    await page.keyboard.press(reverse ? 'Shift+Tab' : 'Tab');
    await visibleFocus(page);
  }
  throw new Error(`Tab could not reach ${selector}`);
}

// Chromium animates keyboard scrolling even with reduced motion, and ignores a further scroll key
// pressed just as that animation lands. Press Home/End and wait for its scroll to finish.
async function scrollKey(page, selector, key) {
  const pending = await page.locator(selector).evaluate((el, key) => {
    const target = key === 'Home' ? 0 : el.scrollHeight - el.clientHeight;
    if (Math.abs(el.scrollTop - target) < 1) return false;
    window.scrollKeyDone = new Promise(resolve => el.addEventListener('scrollend', resolve, {once: true}));
    return true;
  }, key);
  await page.keyboard.press(key);
  if (!pending) return;
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${selector}: ${key} did not scroll`)), STEP_TIMEOUT); });
  try { await Promise.race([page.evaluate(() => window.scrollKeyDone), timeout]); } finally { clearTimeout(timer); }
}

async function mock(page) {
  const state = { dashboards: 0, posts: 0, flags: new Set(), failSave: false, failHistory: false,
    failCoverage: false, failWorkouts: false, workouts: [], appearance: {}, syncPosts: 0 };
  const records = Array.from({length: 45}, (_, i) => ({ ...fx.measurements[i % fx.measurements.length], id: `synthetic-${i}` }));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    if (url.origin !== new URL(page.url() === 'about:blank' ? require('../lib.cjs').BASE : page.url()).origin)
      return route.abort();
    const json = value => route.fulfill({ json: value });
    const error = () => route.fulfill({status: 503, json: {detail: 'Sentetik bağlantı hatası'}});
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') {
      state.dashboards++;
      const result = fx.dashboard(url.searchParams);
      result.profile.weight.value += state.dashboards / 10;
      return json(result);
    }
    if (p === '/api/dashboard/history') return state.failHistory ? error() : json(fx.history(url.searchParams));
    if (p === '/api/dashboard/export.csv') return route.fulfill({contentType: 'text/csv',
      headers: {'content-disposition': 'attachment; filename="synthetic.csv"'}, body: 'Tarih,Adım\n2026-10-01,10\n'});
    if (p === '/sync/all') {
      state.syncPosts++;
      return json({data_types: ['steps', 'heart-rate', 'sleep'], sequence: 40});
    }
    if (p === '/sync/status') return json(Array.from({length: 80}, (_, i) => ({...fx.syncStatus[i % fx.syncStatus.length],
      last_run: {sequence: 41 + i, inserted: 1, updated: 0, failed: false}})));
    if (p === '/api/settings/appearance') {
      if (route.request().method() === 'PUT') Object.assign(state.appearance, route.request().postDataJSON());
      return json({selection: state.appearance, updated_at: null});
    }
    if (p === '/api/dashboard/measurements') {
      if (route.request().method() === 'POST') {
        state.posts++;
        return state.failSave ? error() : json({id: 'saved', ...route.request().postDataJSON()});
      }
      return json(records.map(row => ({...row, flags: state.flags.has(row.id) ? ['test'] : []})));
    }
    if (/\/measurements\/synthetic-\d+\/(flag|unflag)$/.test(p)) {
      const id = p.split('/').at(-2);
      if (p.endsWith('/unflag')) state.flags.delete(id); else state.flags.add(id);
      return json({changed: true});
    }
    if (p === '/api/dashboard/workouts') {
      if (route.request().method() === 'POST') { state.workouts.push(route.request().postDataJSON()); return json({id: 'saved'}); }
      if (state.failWorkouts) return error();
      const month = url.searchParams.get('month').slice(0, 7);
      return json(Array.from({length: 60}, (_, i) => ({id: `session-${i}`, source: i % 2 ? 'Manuel' : 'Fitbit',
        exercise_type: 'Sentetik yürüyüş', start_time: `${month}-01T09:00:00Z`, end_time: `${month}-01T10:00:00Z`,
        duration_seconds: 3600, calories_kcal: i % 2 ? null : 0, distance_meters: 3000})));
    }
    if (p === '/api/sync/coverage') {
      if (state.failCoverage) return error();
      return json({supported_data_types: ['steps'], next_offset: null,
        summary: {days_without_value: 15, unknown_days: 10, incomplete_days: 5}, types: [{data_type: 'steps',
          presence_basis: 'metric_value', source_regimes: [{source: 'sentetik', value_days: 5, record_count: 10}],
          days: Array.from({length: 31}, (_, i) => ({date: `2026-10-${String(i + 1).padStart(2, '0')}`,
            has_value: i % 2 === 0, retrieval: i % 3 ? 'unknown' : 'completed_empty', returned_records: 0}))}]});
    }
    if (p === '/api/webhooks/status') return json({last_notification_at: null,
      silence: {state: 'silent', warning: true, never_received: true, since: '2026-10-01T00:00:00Z', threshold_seconds: 604800},
      counters: {since: '2026-10-01T00:00:00Z', valid: 5, invalid: 2, expired: 1, reasons: {}}, last_error: null});
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return error();
    return route.continue();
  });
  await page.addInitScript(() => {
    const OriginalSocket = window.WebSocket;
    window.WebSocket = class {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
      constructor(url) {
        if (!url.endsWith('/api/codex/chat')) return new OriginalSocket(url);
        window.accessibilitySocket = this; this.readyState = 0;
      }
      emit(event) { this.onmessage?.({data: JSON.stringify(event)}); }
      ready() { this.readyState = 1; this.emit({type: 'ready'}); }
      send(message) { window.accessibilitySent = JSON.parse(message); }
      close() { this.readyState = 3; this.onclose?.(); }
    };
  });
  return state;
}

module.exports = { mock, tabTo, active, visibleFocus, contained, scrollKey };
