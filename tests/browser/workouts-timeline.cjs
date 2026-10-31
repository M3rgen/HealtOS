// Synthetic sessions only. Exercise the shipped UI and request path, including deferred responses.
const assert = require('node:assert/strict');
const { BASE, launch, run, shot, until } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

const session = (id, start, end, extra = {}) => ({
  id, start_time: start, end_time: end, source: 'Fitbit', exercise_type: 'Yürüyüş',
  duration_seconds: 3600, calories_kcal: null, distance_meters: null, ...extra,
});
const boundary = session('shared', '2025-12-31T22:30:00Z', '2025-12-31T23:30:00Z', {
  exercise_type: 'RUNNING', duration_seconds: 3500,
});
const initial = [
  boundary,
  {...boundary, source: 'Manuel', exercise_type: 'Manuel yürüyüş', duration_seconds: 3600, calories_kcal: 0, distance_meters: 0},
  session('overnight', '2026-01-01T20:30:00Z', '2026-01-01T22:00:00Z', {duration_seconds: 5400}),
  session('december', '2025-12-30T09:00:00Z', '2025-12-30T10:00:00Z', {exercise_type: 'Aralık koşusu'}),
  session('october', '2025-10-01T09:00:00Z', '2025-10-01T10:00:00Z'),
  ...Array.from({length: 72}, (_, i) => {
    const day = String(Math.floor(i / 6) + 1).padStart(2, '0');
    const hour = String(i % 6 + 7).padStart(2, '0');
    return session(`dense-${i}`, `2026-01-${day}T${hour}:00:00Z`, `2026-01-${day}T${hour}:30:00Z`, {
      exercise_type: i === 0 ? '<img src=x onerror=alert(1)>' : 'Çok uzun spor adı '.repeat(7), duration_seconds: 1800,
    });
  }),
];
const localMonth = (value, zone) => {
  const parts = new Intl.DateTimeFormat('en', {timeZone: zone, year: 'numeric', month: '2-digit'}).formatToParts(new Date(value));
  return `${parts.find(p => p.type === 'year').value}-${parts.find(p => p.type === 'month').value}`;
};

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844], [360, 640]]) {
      const page = await browser.newPage({viewport: {width, height}, timezoneId: 'America/New_York',
        reducedMotion: width === 360 ? 'reduce' : 'no-preference', hasTouch: width < 500});
      const errors = [], calls = [], records = [...initial];
      let heldMonth, held, failed = false, saved;
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date('2026-01-15T12:00:00Z'));
      await page.addInitScript(() => localStorage.setItem('healtos.timezone.v2', 'Europe/Istanbul'));
      await page.route('**/health', route => route.fulfill({json: fx.health}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json: fx.dashboard(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route => route.fulfill({json: fx.syncStatus}));
      await page.route('**/api/dashboard/workouts**', async route => {
        const request = route.request();
        if (request.method() === 'POST') {
          saved = request.postDataJSON();
          assert.equal(request.headers()['x-healthos-request'], '1');
          const item = {...saved, id: 'saved', source: 'Manuel', duration_seconds: (Date.parse(saved.end_time) - Date.parse(saved.start_time)) / 1000};
          records.push(item);
          return route.fulfill({status: 201, json: item});
        }
        const params = new URL(request.url()).searchParams;
        const month = params.get('month').slice(0, 7), zone = params.get('timezone');
        calls.push({month, zone});
        if (month === heldMonth) { held = route; return; }
        if (month === '2025-10' && !failed) {
          failed = true;
          return route.fulfill({status: 503, json: {detail: 'Antrenmanlar alınamadı.'}});
        }
        return route.fulfill({json: records.filter(row => localMonth(row.start_time, zone) === month)});
      });
      const noPageScroll = async () => {
        const bounds = await page.evaluate(() => ({width: innerWidth, height: innerHeight,
          scrollWidth: document.scrollingElement.scrollWidth, scrollHeight: document.scrollingElement.scrollHeight}));
        assert(bounds.scrollWidth <= bounds.width && bounds.scrollHeight <= bounds.height, JSON.stringify(bounds));
      };
      const ready = () => page.waitForFunction(() => document.querySelector('#workout-timeline').getAttribute('aria-busy') === 'false');
      const choose = async value => {
        await page.locator('#workout-month').fill(value);
        await page.locator('#workout-month').dispatchEvent('change');
      };
      const release = async response => {
        const completed = page.waitForResponse(result => result.url() === held.request().url());
        await held.fulfill(response); await (await completed).finished();
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        heldMonth = null; held = null;
      };
      const reachable = async selector => {
        const control = page.locator(selector);
        await control.scrollIntoViewIfNeeded();
        await control.focus();
        const box = await control.boundingBox();
        assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height, selector);
        // Focus and hit testing catch controls clipped or covered by another element.
        assert(await control.evaluate(el => {
          const b = el.getBoundingClientRect(), hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
          return document.activeElement === el && (el === hit || el.contains(hit));
        }), `${selector} must be operable`);
        await noPageScroll();
      };
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.locator('[data-page="sports"]').click();
      await ready();
      assert.equal(await page.locator('#workout-month').inputValue(), '2026-01');
      assert.equal(await page.locator('#workout-next').isDisabled(), true);
      assert.equal(await page.locator('.workout-entry').count(), 75);
      assert.match(await page.locator('#workout-zone').textContent(), /Europe\/Istanbul/);
      assert.deepEqual(await page.locator('.workout-day').evaluateAll(days => days.map(day => day.dataset.date)),
        Array.from({length: 12}, (_, i) => `2026-01-${String(12 - i).padStart(2, '0')}`));
      const google = page.locator('[data-source="Fitbit"][data-workout-id="shared"]');
      const manual = page.locator('[data-source="Manuel"][data-workout-id="shared"]');
      assert.equal(await google.locator('..').evaluate(el => el.closest('.workout-day').dataset.date), '2026-01-01');
      assert.match(await google.textContent(), /Google · Fitbit/);
      assert.match(await google.locator('.workout-time').textContent(), /01:30.*02:30/);
      assert.deepEqual(await google.locator('dd').allTextContents(), ['0 sa 58 dk', '—', '—']);
      assert.deepEqual(await manual.locator('dd').allTextContents(), ['1 sa 0 dk', '0 kcal', '0 km']);
      assert.match(await manual.locator('.workout-source').textContent(), /Manuel/);
      assert.match(await page.locator('[data-workout-id="overnight"] .workout-time').textContent(), /23:30.*2 Ocak 2026.*01:00/);
      assert.equal(await page.locator('.workout-entry img').count(), 0);
      assert.match(await page.locator('[data-workout-id="dense-0"] h4').textContent(), /<img/);
      await noPageScroll();
      const scrolls = await page.locator('#workout-timeline').evaluate(el => {
        const before = el.scrollTop; el.scrollTop = el.scrollHeight;
        return el.scrollTop > before;
      });
      assert(scrolls, 'Dense sessions scroll inside the timeline');
      await google.scrollIntoViewIfNeeded();
      await noPageScroll();
      if (width === 360) assert.equal(await google.evaluate(el => getComputedStyle(el.closest('.workout-day')).animationName), 'none');
      await reachable('#workout-prev');
      await page.locator('#workout-prev').click();
      await ready();
      assert.equal(await page.locator('#workout-month').inputValue(), '2025-12');
      assert.match(await page.locator('.workout-day h3').textContent(), /30 Aralık 2025/);
      await page.locator('#workout-next').click();
      await ready();
      assert.equal(await page.locator('#workout-month').inputValue(), '2026-01');
      assert.equal(await page.locator('#workout-next').isDisabled(), true);
      assert(calls.every(call => call.month <= '2026-01'), 'No request beyond the current month');

      // A slow December response must not replace the newly selected empty November.
      heldMonth = '2025-12';
      await page.locator('#workout-prev').click();
      await until(() => held, 'December request is held');
      assert.equal(await page.locator('#workout-timeline').getAttribute('aria-busy'), 'true');
      assert.equal(await page.locator('.workout-entry').count(), 0);
      assert.match(await page.locator('#workout-status').textContent(), /yükleniyor/);
      await noPageScroll();
      await page.locator('#workout-prev').click();
      await ready();
      assert.match(await page.locator('#workout-status').textContent(), /Bu ay antrenman kaydı yok/);
      await release({json: [initial[3]]});
      assert.equal(await page.locator('#workout-month').inputValue(), '2025-11');
      assert.equal(await page.locator('.workout-entry').count(), 0);
      assert.equal(await page.locator('#workout-timeline').getAttribute('aria-busy'), 'false');
      assert.equal(await page.locator('#workout-retry').isHidden(), true);

      await page.locator('#workout-prev').click();
      await ready();
      assert.equal(await page.locator('#workout-month').inputValue(), '2025-10');
      assert.match(await page.locator('#workout-status').textContent(), /Antrenmanlar alınamadı/);
      await reachable('#workout-retry');
      await page.locator('#workout-retry').click();
      await ready();
      assert.equal(await page.locator('.workout-entry').count(), 1);
      assert.equal(await page.locator('#workout-retry').isHidden(), true);
      assert.equal(calls.filter(call => call.month === '2025-10').length, 2);
      // A stale failure must not clear the newer success or reveal its retry button.
      heldMonth = '2025-12';
      await choose('2025-12');
      await until(() => held, 'Second December request is held');
      await choose('2026-01'); await ready();
      await release({status: 503, json: {detail: 'Eski ay hatası'}});
      assert.equal(await page.locator('.workout-entry').count(), 75);
      assert.equal(await page.locator('#workout-retry').isHidden(), true);
      await noPageScroll();

      // Device entry times and the selected display zone differ. Saving chooses the start month.
      await page.locator('#workout-add').click();
      await noPageScroll();
      for (const selector of ['[name="exercise_type"]', '[name="start_time"]', '[name="end_time"]', '[name="calories_kcal"]', '[name="distance_km"]', '#workout-cancel', '#workout-save']) await reachable(selector);
      await page.locator('[name="exercise_type"]').fill('Gece bisikleti');
      await page.locator('[name="start_time"]').fill('2025-12-31T20:00');
      await page.locator('[name="end_time"]').fill('2025-12-31T21:00');
      await page.locator('[name="distance_km"]').fill('5');
      assert.match(await page.locator('#workout-timezone').textContent(), /America\/New_York/);
      // Navigation while the form is open retains the entered draft and keeps controls reachable.
      await reachable('#workout-prev'); await page.locator('#workout-prev').click(); await ready();
      assert.equal(await page.locator('[name="exercise_type"]').inputValue(), 'Gece bisikleti');
      await reachable('#workout-next'); await page.locator('#workout-next').click(); await ready();
      await reachable('#workout-prev'); await page.locator('#workout-prev').click(); await ready();
      assert.equal(await page.locator('#workout-month').inputValue(), '2025-12');
      await reachable('#workout-save'); await page.locator('#workout-save').click();
      await page.waitForFunction(() => !document.querySelector('#workout-editor').open && document.querySelector('[data-workout-id="saved"]'));
      assert.equal(saved.start_time, '2026-01-01T01:00:00.000Z');
      assert.equal(saved.calories_kcal, null); assert.equal(saved.distance_meters, 5000);
      assert.equal(await page.locator('#workout-month').inputValue(), '2026-01');
      assert.match(await page.locator('[data-workout-id="saved"] .workout-time').textContent(), /04:00.*05:00/);
      assert.match(await page.locator('[data-workout-id="saved"] .workout-source').textContent(), /Manuel/);
      await noPageScroll();
      await choose('2025-11'); await ready();
      await page.locator('#workout-add').click();
      await reachable('#workout-save'); await reachable('#workout-cancel');
      await page.locator('#workout-cancel').click();
      assert.equal(await page.locator('#workout-editor').getAttribute('open'), null);
      await choose(''); await ready();
      assert.match(await page.locator('#workout-status').textContent(), /Geçerli bir ay/);
      assert.equal(await page.locator('#workout-prev').isDisabled(), true);
      await choose('2026-02'); await ready();
      assert.match(await page.locator('#workout-status').textContent(), /Geçerli bir ay/);
      assert(calls.every(call => call.month <= '2026-01'));
      await choose('2026-01'); await ready();
      await page.screenshot({path: shot(`workout-timeline-${width}.png`)});
      assert(calls.every(call => call.zone === 'Europe/Istanbul'));
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height}: sources, local days, missing/zero values, year boundary, retry, stale responses, manual save, bounded scrolling`);
      await page.close();
    }
  } finally { await browser.close(); }
});
