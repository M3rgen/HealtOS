// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    for (const [width, height] of [[1920, 976], [1920, 900], [1280, 720], [1024, 650], [1280, 600], [390, 844], [320, 700]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/health', route => route.fulfill({ json: {
        status: 'healthy', database: 'healthy', scheduler: 'running', configuration: 'healthy',
        checked_at: '2026-10-01T12:00:00Z', last_sync_success: '2026-10-01T12:00:00Z',
        latest_health_write: '2026-10-01T12:00:00Z',
        data_freshness: { heart_rate: '2026-10-01T12:00:00Z', steps: '2026-10-01T12:00:00Z' },
      } }));
      await page.route('**/api/dashboard?**', route => route.fulfill({ json: {
        start_date: '2026-09-25', end_date: '2026-10-01', timezone: 'UTC',
        profile: {height:{value:175,measured_at:'2026-05-29T12:00:00Z'},
          weight:{value:70,measured_at:'2026-09-30T12:00:00Z'},
          body_fat:{value:20,measured_at:'2026-09-30T12:00:00Z'}},
        days: Array.from({ length: 7 }, (_, i) => ({
          date: new Date(Date.UTC(2026, 9, 1 - i)).toISOString().slice(0, 10),
          steps: 1516, sleep_seconds: 21960, resting_bpm: 70, hrv_ms: 42, spo2: 98,
          partial: i === 0, status: i === 0 ? 'partial' : 'complete',
        })),
        charts: { steps: Array.from({ length: 24 }, (_, i) => i === 8 ? 1000 : 20),
          heart_rate: Array.from({ length: 24 }, (_, i) => i === 9 ? 90 : 65 + i % 5) },
      } }));
      await page.goto('http://127.0.0.1:5174');
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      async function assertNoPageScroll() {
        const size = await page.evaluate(() => ({height:innerHeight,width:innerWidth,
          scrollHeight:document.documentElement.scrollHeight,scrollWidth:document.documentElement.scrollWidth}));
        assert(size.scrollHeight <= size.height && size.scrollWidth <= size.width, JSON.stringify(size));
      }
      await assertNoPageScroll();
      const cards = await page.locator('.metric-card').evaluateAll(elements => elements.map(card => {
        const bounds = card.getBoundingClientRect();
        const chart = card.querySelector('.chart').getBoundingClientRect();
        const axis = card.querySelector('.chart-axis').getBoundingClientRect();
        return { height: bounds.height, chartHeight: chart.height,
          contained: chart.top >= bounds.top && chart.bottom <= bounds.bottom && axis.bottom <= bounds.bottom };
      }));
      assert(cards.every(card => card.contained && card.chartHeight >= 24), JSON.stringify({ width, height, cards }));
      const toggle = page.locator('#history-toggle');
      assert.equal((await toggle.textContent()).trim(), '');
      assert.equal(await toggle.getAttribute('aria-label'), 'Aylık görünümü aç');
      await toggle.click();
      assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      await assertNoPageScroll();
      assert.equal(await toggle.getAttribute('aria-label'), '7 günlük görünüme dön');
      await page.keyboard.press('Escape');
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      await page.waitForFunction(() => !document.querySelector('#history-panel').getAnimations().length);
      if (width === 1920) await page.locator('.metric-cards').screenshot({ path: '.test-tmp-chart-fixed.png' });
      await toggle.scrollIntoViewIfNeeded();
      const panel = page.locator('#history-panel');
      const initial = await panel.boundingBox();
      async function drag(distance) {
        const handle = await toggle.boundingBox();
        const x = handle.x + handle.width / 2, y = handle.y + handle.height / 2;
        await page.mouse.move(x, y);
        await page.mouse.down();
        await page.mouse.move(x, y + distance, { steps: 8 });
        const moving = await panel.boundingBox();
        await page.mouse.up();
        const released = await panel.boundingBox();
        assert(Math.abs(moving.y - released.y) < 2, 'Panel must stay at the release position');
        return released;
      }
      const partial = await drag(-60);
      await assertNoPageScroll();
      assert(Math.abs(partial.y - (initial.y - 60)) < 2, 'Drag must follow the pointer instead of fully opening');
      const further = await drag(-40);
      assert(Math.abs(further.y - (partial.y - 40)) < 2, 'Repeated dragging must continue from the current position');
      await drag(100);
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      if (width === 390) {
        const handle = await toggle.boundingBox();
        const x = handle.x + handle.width / 2, y = handle.y + handle.height / 2;
        const before = await panel.boundingBox();
        const cdp = await page.context().newCDPSession(page);
        await cdp.send('Input.dispatchTouchEvent', {type:'touchStart',touchPoints:[{x,y}]});
        await cdp.send('Input.dispatchTouchEvent', {type:'touchMove',touchPoints:[{x,y:y-55}]});
        await cdp.send('Input.dispatchTouchEvent', {type:'touchEnd',touchPoints:[]});
        assert(Math.abs((await panel.boundingBox()).y - (before.y - 55)) < 2, 'Touch drag must retain the chosen height');
        await cdp.detach();
      }
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height}: charts fit; click, proportional drag, release and collapse work`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
