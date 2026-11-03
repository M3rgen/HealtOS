// Exercise native keyboard behavior against synthetic HTTP/WebSocket responses.
const assert = require('node:assert/strict');
const { BASE, launch, run, until } = require('./lib.cjs');
const { mock, tabTo, active, visibleFocus, contained, scrollKey } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

const sizes = [{width: 1280, height: 800}, {width: 390, height: 844}, {width: 360, height: 640}];
const press = async (page, selector, key = 'Enter', reverse = false) => {
  await tabTo(page, selector, reverse);
  await page.keyboard.press(key);
  await visibleFocus(page);
};
const dialogOpen = (page, id) => page.waitForFunction(id => document.getElementById(id).open, id);
const dialogClosed = (page, id) => page.waitForFunction(id => !document.getElementById(id).open, id);
// Native modal containment: past either end Chromium moves focus to the browser UI (the
// document body here) and the next key returns into the dialog; page content stays inert.
async function trap(page, id, count = 22) {
  for (const key of ['Tab', 'Shift+Tab']) {
    let chrome = 0;
    for (let i = 0; i < count; i++) {
      await page.keyboard.press(key);
      const where = await page.evaluate(id => document.activeElement === document.body ? 'chrome'
        : document.getElementById(id).contains(document.activeElement) ? 'dialog' : document.activeElement.outerHTML.slice(0, 100), id);
      chrome = where === 'chrome' ? chrome + 1 : 0;
      assert(where === 'dialog' || (where === 'chrome' && chrome === 1), `${id}: ${key} escaped to ${where}`);
      await visibleFocus(page);
    }
  }
}
async function scrollEnd(page, selector) {
  await tabTo(page, selector);
  const scrolls = await page.locator(selector).evaluate(el => el.scrollHeight > el.clientHeight);
  await scrollKey(page, selector, 'End');
  if (scrolls) assert(await page.locator(selector).evaluate(el => el.scrollTop > 0), `${selector}: End scrolls`);
  await contained(page);
  await scrollKey(page, selector, 'Home');
}

async function profile(page, state) {
  await tabTo(page, '#profile-weight');
  const original = await page.locator('#profile-weight').elementHandle();
  const previous = await page.locator('#profile-weight').textContent();
  const before = state.dashboards;
  await page.clock.fastForward(60_001);
  await until(() => state.dashboards > before, 'dashboard poll');
  await page.waitForFunction(previous => document.querySelector('#profile-weight').textContent !== previous, previous);
  assert(await original.evaluate(el => el === document.activeElement && el.isConnected), 'poll must preserve the focused profile button');
  const button = page.locator('#profile-weight');
  assert((await button.getAttribute('aria-label')).includes((await button.textContent()).trim()), 'visible value is in the accessible name');
  await page.keyboard.press('Space');
  await dialogOpen(page, 'measurement-dialog');
  assert(await active(page, '#measurement-value'));
  assert.equal(await page.getByRole('dialog', {name: 'Ölçüm ekle', exact: true}).count(), 1);
  await trap(page, 'measurement-dialog');
  await press(page, '#measurement-cancel', 'Space');
  await dialogClosed(page, 'measurement-dialog');
  assert(await active(page, '#profile-weight'));
  await page.keyboard.press('Enter');
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'measurement-dialog');
  assert(await active(page, '#profile-weight'));
  const beforeSave = await page.locator('#profile-weight').textContent();
  await page.keyboard.press('Enter');
  await press(page, '#measurement-save');
  await dialogClosed(page, 'measurement-dialog');
  await page.waitForFunction(previous => document.querySelector('#profile-weight').textContent !== previous, beforeSave);
  assert(await original.evaluate(el => el === document.activeElement && el.isConnected), 'save refresh must preserve native return focus');
}

async function measurements(page, state) {
  const postsBefore = state.posts;
  await press(page, '[data-page="measurements"]', 'Enter', true);
  await page.waitForFunction(() => document.querySelectorAll('#measurement-rows tr').length === 45);
  assert(await active(page, '[data-page="measurements"]'));
  await press(page, '#measurements-add');
  await dialogOpen(page, 'measurement-dialog');
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.press('Backspace');
  await press(page, '#measurement-save');
  assert(await active(page, '#measurement-value'), 'native required validation focuses the invalid field');
  assert.equal(await page.locator('#measurement-value').getAttribute('aria-invalid'), 'true');
  assert.match(await page.locator('#measurement-value').getAttribute('aria-describedby'), /form-error/);
  await page.keyboard.type('73.2');
  state.failSave = true;
  await press(page, '#measurement-save');
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('Sentetik'));
  assert(await active(page, '#measurement-save'), 'API errors leave the submit control available');
  state.failSave = false;
  await page.keyboard.press('Enter');
  await dialogClosed(page, 'measurement-dialog');
  await until(() => state.posts === postsBefore + 2, 'manual measurement saved');
  assert(await active(page, '#measurements-add'));
  await scrollEnd(page, '#measurements .table-scroll');
  await press(page, '[data-flag-id="synthetic-0"]', 'Space');
  await page.waitForFunction(() => document.activeElement.dataset.flagId === 'synthetic-0' && document.activeElement.textContent === 'İşareti kaldır');
  const flag = page.locator('[data-flag-id="synthetic-0"]');
  assert((await flag.getAttribute('aria-label')).startsWith((await flag.textContent()).trim()));
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.activeElement.dataset.flagId === 'synthetic-0' && document.activeElement.textContent === 'Test olarak işaretle');
  await visibleFocus(page);
}

async function overviewActions(page, state) {
  const [download] = await Promise.all([page.waitForEvent('download'), press(page, '#export-button')]);
  // Playwright's request routing does not see download requests: the response (its file name
  // and outcome) comes from the test server's 503 stub. Assert the keyboard-requested export.
  const exported = new URL(download.url());
  assert.equal(exported.pathname, '/api/dashboard/export.csv');
  assert.equal(exported.searchParams.get('days'), '7');
  await download.cancel();
  await press(page, '#sync-button', 'Space', true);
  await until(() => state.syncPosts === 1, 'keyboard sync starts');
  assert(await active(page, '#sync-button'), 'busy sync must keep its keyboard focus');
  await page.keyboard.press('Enter');
  await page.clock.runFor(2100);
  await page.waitForFunction(() => document.querySelector('#sync-button').getAttribute('aria-busy') === 'false');
  assert.equal(state.syncPosts, 1, 'a busy keyboard action cannot start another run');
  assert.match(await page.locator('#notice').textContent(), /Eşitleme tamamlandı/);
}

async function system(page, state) {
  await press(page, '[data-page="system"]', 'Space', true);
  await page.waitForFunction(() => document.querySelectorAll('#sync-rows tr').length === 80);
  await scrollEnd(page, '#system');
  await press(page, '#coverage-open');
  await dialogOpen(page, 'coverage-dialog');
  assert(await active(page, '#coverage-start'));
  await trap(page, 'coverage-dialog');
  await scrollEnd(page, '.coverage-dialog-body');
  state.failCoverage = true;
  await press(page, '#coverage-form button');
  await page.locator('#coverage-retry').waitFor({state: 'visible'});
  state.failCoverage = false;
  await press(page, '#coverage-retry');
  await page.waitForFunction(() => document.querySelector('#coverage-retry').hidden);
  assert(await active(page, '#coverage-form button'), 'hiding the retry must preserve a visible focus target');
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'coverage-dialog');
  assert(await active(page, '#coverage-open'));
  await press(page, '#coverage-open');
  await press(page, '#coverage-close', 'Space');
  await dialogClosed(page, 'coverage-dialog');
  assert(await active(page, '#coverage-open'));

  await press(page, '#webhook-status-open');
  await dialogOpen(page, 'webhook-status-dialog');
  assert(await active(page, '#webhook-status-close'));
  await trap(page, 'webhook-status-dialog', 9);
  await scrollEnd(page, '.webhook-status-body');
  await press(page, '#webhook-status-retry');
  await page.waitForFunction(() => document.querySelector('#webhook-status-details').getAttribute('aria-busy') === 'false');
  assert(await active(page, '#webhook-status-retry'));
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'webhook-status-dialog');
  assert(await active(page, '#webhook-status-open'));
  await press(page, '#webhook-status-open');
  await press(page, '#webhook-status-close');
  await dialogClosed(page, 'webhook-status-dialog');
  assert(await active(page, '#webhook-status-open'));

  const before = state.dashboards;
  await press(page, '#refresh-system', 'Enter', true);
  await until(() => state.dashboards > before, 'keyboard system refresh');
  assert(await active(page, '#refresh-system'));
  await tabTo(page, '#timezone');
  const previousZone = await page.locator('#timezone').inputValue();
  // Type-ahead selects on every platform; ArrowDown opens the native picker on macOS instead.
  await page.keyboard.press(previousZone === 'UTC' ? 'c' : 'u'); await page.keyboard.press('Tab');
  assert.notEqual(await page.locator('#timezone').inputValue(), previousZone);
  await tabTo(page, '#model-motion');
  const checked = await page.locator('#model-motion').isChecked();
  await page.keyboard.press('Space');
  assert.equal(await page.locator('#model-motion').isChecked(), !checked);
  await page.keyboard.press('Space');
  await press(page, '#appearance-open');
  await dialogOpen(page, 'appearance-dialog');
  assert(await active(page, 'input[name="appearance-logo"]:checked'));
  await trap(page, 'appearance-dialog', 18);
  await tabTo(page, 'input[name="appearance-logo"]:checked');
  const previous = await page.locator('input[name="appearance-logo"]:checked').inputValue();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Space');
  assert.notEqual(await page.locator('input[name="appearance-logo"]:checked').inputValue(), previous);
  const [chooser] = await Promise.all([page.waitForEvent('filechooser'), press(page, '[data-upload="logo"]')]);
  await chooser.setFiles({name: 'synthetic.txt', mimeType: 'text/plain', buffer: Buffer.from('synthetic')});
  await page.waitForFunction(() => !document.querySelector('[data-appearance-kind="logo"] .appearance-error').hidden);
  assert.match(await page.locator('[data-upload="logo"]').getAttribute('aria-describedby'), /appearance-logo-error/);
  assert(await active(page, '[data-upload="logo"]'));
  await scrollEnd(page, '.appearance-body');
  await press(page, '#appearance-reset', 'Space');
  assert(await active(page, '#appearance-reset'));
  await press(page, '#appearance-done');
  await dialogClosed(page, 'appearance-dialog');
  assert(await active(page, '#appearance-open'));
  await page.keyboard.press('Enter');
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'appearance-dialog');
  assert(await active(page, '#appearance-open'));
}

async function sports(page, state) {
  await press(page, '[data-page="sports"]', 'Enter', true);
  await page.waitForFunction(() => document.querySelectorAll('.workout-entry').length === 60);
  await scrollEnd(page, '#workout-timeline');
  state.failWorkouts = true;
  await press(page, '#workout-prev', 'Space', true);
  await page.locator('#workout-retry').waitFor({state: 'visible'});
  state.failWorkouts = false;
  await press(page, '#workout-retry');
  await page.waitForFunction(() => document.querySelector('#workout-retry').hidden);
  assert(await active(page, '#workout-month'));
  // The next-month button becomes disabled at the future boundary; its month
  // field is a visible keyboard fallback, with native editing segments exposed.
  await press(page, '#workout-next');
  assert(await active(page, '#workout-month'));
  assert.equal(await page.locator('.workout-month-label .month-label').evaluate(el => getComputedStyle(el).visibility), 'hidden');
  await press(page, '#workout-add', 'Enter', true);
  assert(await active(page, '[name="exercise_type"]'));
  await page.keyboard.type('Sentetik yürüyüş');
  for (const [name, value] of [['start_time', '2026-09-29T10:00'], ['end_time', '2026-09-29T11:00']]) {
    await tabTo(page, `[name="${name}"]`);
    await page.locator(`[name="${name}"]`).fill(value); // Native date/time segments.
  }
  await tabTo(page, '[name="calories_kcal"]');
  await page.keyboard.type('120');
  await tabTo(page, '[name="distance_km"]');
  await page.keyboard.type('2');
  await press(page, '#workout-save');
  await until(() => state.workouts.length === 1, 'keyboard workout save');
  await page.waitForFunction(() => !document.querySelector('#workout-editor').open);
  assert.equal(state.workouts[0].distance_meters, 2000);
  assert(await active(page, '#workout-add'));
  await page.keyboard.press('Enter');
  await press(page, '#workout-cancel', 'Space');
  assert(await active(page, '#workout-add'));
}

async function metricHistory(page, state) {
  await press(page, '[data-page="overview"]', 'Enter', true);
  await press(page, '[data-history-metric="steps"]');
  await dialogOpen(page, 'metric-history-dialog');
  assert(await active(page, '#metric-history-close'));
  await page.waitForFunction(() => !document.querySelector('#metric-history-content').hidden);
  await trap(page, 'metric-history-dialog', 18);
  await press(page, '#metric-history-pages button:last-child');
  await page.waitForFunction(() => document.querySelector('#metric-history-page-note').textContent.startsWith('2 /'));
  assert(await active(page, '[data-history-tab="steps"]'), 'pagination replacement keeps focus on a stable control');
  await press(page, '[data-history-tab="sleep"]', 'Space');
  await page.waitForFunction(() => document.querySelector('#metric-history-title').textContent === 'Uyku geçmişi');
  await scrollEnd(page, '#metric-history-content');
  state.failHistory = true;
  await press(page, '[data-history-tab="heart"]');
  await page.locator('#metric-history-status button').waitFor({state: 'visible'});
  state.failHistory = false;
  await press(page, '#metric-history-status button');
  await page.waitForFunction(() => document.querySelector('#metric-history-status').hidden);
  assert(await active(page, '[data-history-tab="heart"]'));
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'metric-history-dialog');
  assert(await active(page, '[data-history-metric="steps"]'));
  await page.keyboard.press('Enter');
  await press(page, '#metric-history-close');
  await dialogClosed(page, 'metric-history-dialog');
  assert(await active(page, '[data-history-metric="steps"]'));
}

async function layeredEscape(page) {
  await press(page, '#history-toggle', 'Space');
  await page.waitForFunction(() => document.querySelector('#history-panel').classList.contains('expanded'));
  // Expanding loads the month asynchronously; End is only meaningful once its rows are in.
  await page.waitForFunction(() => document.querySelector('#history-status').hidden);
  assert.equal(await page.locator('.overview-grid').evaluate(el => el.inert), true);
  await tabTo(page, '#history-table');
  await scrollKey(page, '#history-table', 'End');
  assert(await page.locator('#history-table').evaluate(el => el.scrollTop > 0));
  await press(page, '#header-date', 'Enter', true);
  await page.waitForFunction(() => document.querySelector('#date-calendar').matches(':popover-open'));
  // The popover's toggle event, which moves focus to the day grid, is dispatched in a later task.
  await page.waitForFunction(() => document.querySelector('#date-calendar').contains(document.activeElement));
  assert(await active(page, '.calendar-day[tabindex="0"]'));
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('#date-calendar').matches(':popover-open'));
  assert(await active(page, '#header-date'));
  assert.equal(await page.locator('#history-toggle').getAttribute('aria-expanded'), 'true');
  await page.keyboard.press('Enter');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => !document.querySelector('#date-calendar').matches(':popover-open'));
  assert(await active(page, '#header-date'));

  await press(page, '#assistant-open'); // The socket has deliberately not emitted ready.
  assert(await active(page, '#assistant-close'));
  await page.keyboard.press('Tab');
  assert(await active(page, '#assistant-messages'));
  assert(await page.locator('#assistant-messages').evaluate(el => {
    const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  }), 'expanded history cannot cover the focused chat region');
  await scrollKey(page, '#assistant-messages', 'End');
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  assert(await active(page, '#assistant-open'), 'Umay remains nonmodal');
  await page.evaluate(() => window.accessibilitySocket.ready());
  assert(await active(page, '#assistant-open'), 'ready must not steal focus after the user leaves');
  await page.keyboard.press('Enter');
  assert(await active(page, '#assistant-input'));
  await page.keyboard.type('Sentetik soru');
  await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(() => window.accessibilitySent.text), 'Sentetik soru');
  await press(page, '#assistant-stop', 'Space', true);
  assert.equal(await page.evaluate(() => window.accessibilitySent.type), 'cancel');
  await page.evaluate(() => window.accessibilitySocket.emit({type: 'done', status: 'completed'}));
  assert(await active(page, '#assistant-close'), 'a hidden stop control returns to a visible control');
  await press(page, '#assistant-new');
  await page.evaluate(() => window.accessibilitySocket.ready());
  assert(await active(page, '#assistant-new'), 'new conversation ready must not move focus');
  await page.evaluate(() => window.accessibilitySocket.close());
  await press(page, '#assistant-reconnect');
  assert(await active(page, '#assistant-close'), 'hiding reconnect preserves visible focus');
  await page.evaluate(() => window.accessibilitySocket.ready());
  assert(await active(page, '#assistant-close'));
  await page.evaluate(() => window.accessibilitySocket.emit({type: 'message', id: 'keyboard-answer',
    text: '```text\n' + 'sentetik '.repeat(80) + '\n```'}));
  await tabTo(page, '#assistant-messages pre');
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(() => document.querySelector('#assistant-messages pre').scrollLeft > 0);
  await page.evaluate(() => window.accessibilitySocket.emit({type: 'delta', id: 'keyboard-answer', text: '\n\nSon satır.'}));
  assert(await active(page, '#assistant-messages pre'), 'stream repaint preserves the focused code region');
  assert(await page.locator('#assistant-messages pre').evaluate(el => el.scrollLeft > 0));
  await page.evaluate(() => window.accessibilitySocket.emit({type: 'error', text: 'Sentetik hata açıklaması. '.repeat(50)}));
  await scrollEnd(page, '#assistant-error');
  await page.evaluate(() => window.accessibilitySocket.ready());
  assert(await active(page, '#assistant-close'), 'clearing a focused error preserves visible focus');
  await page.keyboard.press('Escape');
  assert(await active(page, '#assistant-open'));
  assert.equal(await page.locator('#history-toggle').getAttribute('aria-expanded'), 'true', 'chat Escape does not collapse history');

  // A modal in another view must not invoke Escape on the now hidden history.
  await press(page, '[data-page="system"]', 'Enter', true);
  await press(page, '#coverage-open');
  await page.evaluate(() => window.accessibilitySocket.ready());
  assert(await active(page, '#coverage-start'), 'socket events cannot leave the modal');
  await page.keyboard.press('Escape');
  await dialogClosed(page, 'coverage-dialog');
  assert(await active(page, '#coverage-open'));
  await press(page, '[data-page="overview"]', 'Enter', true);
  await press(page, '#history-toggle', 'Escape');
  await page.waitForFunction(() => !document.querySelector('#history-panel').classList.contains('expanded'));
  assert(await active(page, '#history-toggle'));
  assert.equal(await page.locator('.overview-grid').evaluate(el => el.inert), false);
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      const page = await browser.newPage({viewport, reducedMotion: 'reduce'});
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const state = await mock(page);
      // Before load: main.js starts its 60 s dashboard poll during start-up, and only timers
      // created after install() follow the fake clock. Start at the fixtures' NOW: the monthly
      // history shows only the days elapsed so far, so a real early-month date leaves too few
      // rows for its End-key scroll check.
      await page.clock.install({time: new Date(fx.NOW)});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 && 'appearanceReady' in document.documentElement.dataset);
      // DOM order follows the visible menu, in both directions.
      await page.keyboard.press('Tab');
      assert(await active(page, '.brand'));
      for (const name of ['overview', 'measurements', 'system', 'sports']) {
        await page.keyboard.press('Tab'); assert(await active(page, `[data-page="${name}"]`));
      }
      await page.keyboard.press('Shift+Tab'); assert(await active(page, '[data-page="system"]'));
      await profile(page, state);
      await overviewActions(page, state);
      await measurements(page, state);
      await system(page, state);
      await sports(page, state);
      await metricHistory(page, state);
      await layeredEscape(page);
      await contained(page);
      assert.deepEqual(errors, []);
      console.log(`PASS ${viewport.width}×${viewport.height}: four views, native modals, poll/DOM focus, keyboard scrolling, Umay and layered Escape`);
      await page.close();
    }
  } finally { await browser.close(); }
});
