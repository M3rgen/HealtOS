// Turn receipts stay with their answers; long metadata scrolls inside the Umay bubble.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');
const { scrollKey } = require('./helpers/accessibility.cjs');

const zero = {raw_records: 0, daily_summaries: 0, profile_measurements: 0, chart_points: 0,
  derived_summaries: 0, derived_observations: 0, metadata_items: 0};
const call = {action: 'daily', outcome: 'sent', counts: {...zero, daily_summaries: 1,
  profile_measurements: 1, derived_observations: 3}, unique_source_records: null,
requested: {first: '2026-09-29', last: '2026-09-29'}, timezone: 'Europe/Paris',
source_timezones: ['UTC'], data_dates: {first: '2026-09-01', last: '2026-09-29'},
profile_dates: {first: '2026-09-01', last: '2026-09-01'}, metadata_dates: null,
fields: Array.from({length: 160}, (_, i) => `derived.days[].result.streams[].observations[].field_${i}`)};

async function emit(page, message) {
  await page.evaluate(message => window.transferSocket.emit(message), message);
}

async function geometry(page, visibleHeight) {
  const result = await page.evaluate(visibleHeight => {
    const viewportTop = visualViewport.offsetTop;
    const height = visibleHeight ?? innerHeight;
    const fits = ['assistant-dialog', 'assistant-form', 'assistant-input', 'assistant-send', 'assistant-stop']
      .every(id => {
        const element = document.getElementById(id);
        if (element.hidden) return true;
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height >= 30 && rect.left >= 0 && rect.right <= innerWidth + 1 &&
          rect.top >= viewportTop && rect.bottom <= viewportTop + height + 1;
      });
    const dialog = document.querySelector('#assistant-dialog');
    const messages = document.querySelector('#assistant-messages');
    return {fits, pageFits: document.documentElement.scrollWidth <= innerWidth &&
      document.documentElement.scrollHeight <= innerHeight,
    dialogFits: dialog.scrollHeight <= dialog.clientHeight + 1 && dialog.scrollWidth <= dialog.clientWidth + 1,
    reachable: messages.clientHeight >= 40 && getComputedStyle(messages).overflowY === 'auto'};
  }, visibleHeight);
  assert.deepEqual(result, {fits: true, pageFits: true, dialogFits: true, reachable: true});
}

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 900], [1280, 480], [390, 844], [320, 568], [320, 360]]) {
      const page = await browser.newPage({viewport: {width, height}});
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== new URL(BASE).origin) return route.abort();
        if (url.pathname === '/health') return route.fulfill({json: fx.health});
        if (url.pathname === '/api/dashboard') return route.fulfill({json: fx.dashboard(url.searchParams)});
        if (url.pathname === '/sync/status') return route.fulfill({json: fx.syncStatus});
        if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({status: 404, json: {}});
        return route.continue();
      });
      await page.addInitScript(() => {
        window.WebSocket = class {
          static CLOSING = 2; static CLOSED = 3;
          constructor() {
            window.transferSocket = this;
            window.transferConnections = (window.transferConnections ?? 0) + 1;
            this.readyState = 1;
            setTimeout(() => this.emit({type: 'ready'}), 20);
          }
          emit(message) { this.onmessage?.({data: JSON.stringify(message)}); }
          send(text) { window.transferRequest = JSON.parse(text); }
          close() { this.readyState = 3; this.onclose?.(); }
        };
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.locator('#assistant-open').click();
      await page.waitForFunction(() => !document.querySelector('#assistant-input').disabled);
      assert.match(await page.locator('#assistant-data-notice').textContent(), /OpenAI’ye gönderilir/);
      assert.match(await page.locator('#assistant-data-notice').textContent(), /aldığını doğrulamaz/);
      assert.match(await page.locator('#assistant-data-notice').textContent(), /bağlamında kalabilir/);

      async function question(text, turn) {
        await page.locator('#assistant-input').fill(text);
        await page.locator('#assistant-send').click();
        await emit(page, {type: 'health_transfer', turn, status: 'active', calls: [], counts: zero, failed_sends: 0});
      }
      await question('İlk sorum', 'first');
      const first = page.locator('.chat-turn').first();
      await first.locator('.chat-transfer > summary').click();
      assert.match(await first.locator('.chat-transfer-body').textContent(), /yeni sağlık verisi gönderilmedi/);
      const receipt = {type: 'health_transfer', turn: 'first', status: 'active', calls: [call],
        counts: call.counts, failed_sends: 0};
      await emit(page, receipt);
      assert.match(await first.locator('.chat-transfer-body').textContent(), /Günlük özet: 1/);
      assert.match(await first.locator('.chat-transfer-body').textContent(), /Benzersiz kaynak kayıt sayısı bilinmiyor/);
      assert.match(await first.locator('.chat-transfer-body').textContent(), /2026-09-01/);
      assert.match(await first.locator('.chat-transfer-body').textContent(), /Europe\/Paris/);
      await first.locator('.chat-transfer-fields > summary').click();
      const fields = first.getByRole('region', {name: '1. sorguda gönderilen alanlar'});
      assert.equal(await fields.locator('li').count(), 160);
      await fields.focus();
      // Wait for the animated keyboard scroll to land; a mid-animation position is not the one to keep.
      await scrollKey(page, '.chat-transfer-fields ul', 'End');
      const scroll = await fields.evaluate(el => ({top: el.scrollTop, height: el.clientHeight,
        content: el.scrollHeight}));
      assert.ok(scroll.content > scroll.height && scroll.top > 0);
      // New calls and terminal events keep the opened list, scroll position and keyboard focus.
      const updated = {...receipt, calls: [call, {...call, fields: ['records[].payload.*']}],
        counts: {...call.counts, derived_observations: 6}};
      await emit(page, updated);
      assert.ok(await fields.evaluate(el => el.scrollTop >= 1 && document.activeElement === el));
      await emit(page, {type: 'delta', id: 'answer', text: 'İlk yanıt'});
      await emit(page, {...updated, status: 'completed'});
      await emit(page, {type: 'done', status: 'completed'});
      assert.equal(await first.locator('.chat-assistant').count(), 1);
      assert.match(await first.locator('.chat-transfer > summary').textContent(), /Tamamlandı/);
      assert.equal(await fields.evaluate(el => el.scrollTop), scroll.top);
      await geometry(page);

      await question('Takip sorum', 'second');
      const second = page.locator('.chat-turn').nth(1);
      await second.locator('.chat-transfer > summary').click();
      assert.match(await second.locator('.chat-transfer-body').textContent(), /Ham kayıt: 0/);
      assert.equal(await second.locator('.chat-transfer-fields').count(), 0);
      await emit(page, {...receipt, turn: 'first'}); // stale turn cannot enter the follow-up
      assert.equal(await second.locator('.chat-transfer-fields').count(), 0);
      await emit(page, {type: 'delta', id: 'answer', text: 'İkinci yanıt'});
      // Rendered Markdown ends with a newline text node after </p>; the words are what must not grow.
      assert.equal((await first.locator('.chat-assistant .chat-content').textContent()).trim(), 'İlk yanıt');
      await geometry(page);

      // Virtual keyboard: visual viewport shrinks and pans while the layout viewport stays put.
      await page.evaluate(() => {
        Object.defineProperty(visualViewport, 'height', {value: 250, configurable: true});
        Object.defineProperty(visualViewport, 'offsetTop', {value: 40, configurable: true});
        visualViewport.dispatchEvent(new Event('resize'));
      });
      await geometry(page, 250);
      await page.locator('#assistant-input').fill('Klavye açıkken erişilebilir');
      await page.locator('#assistant-stop').click();
      assert.deepEqual(await page.evaluate(() => window.transferRequest), {type: 'cancel'});
      await emit(page, {type: 'health_transfer', turn: 'second', status: 'interrupted',
        calls: [], counts: zero, failed_sends: 0});
      await emit(page, {type: 'done', status: 'interrupted'});
      await geometry(page, 250);
      await page.evaluate(() => {
        delete visualViewport.height; delete visualViewport.offsetTop;
        visualViewport.dispatchEvent(new Event('resize'));
      });

      await question('Bağlantı öncesi', 'third');
      await page.evaluate(() => window.transferSocket.close());
      assert.match(await page.locator('.chat-turn').nth(2).locator('.chat-transfer > summary').textContent(), /Bağlantı kesildi/);
      await page.locator('#assistant-reconnect').click();
      await page.waitForFunction(() => !document.querySelector('#assistant-input').disabled);
      const boundary = page.locator('.chat-boundary');
      assert.equal(await boundary.count(), 1);
      assert.match(await boundary.textContent(), /Yeni sohbet başladı/);
      assert.match(await boundary.textContent(), /Umay bu yeni sohbet içinde onları görmüyor/);
      assert.equal(await page.locator('.chat-turn').count(), 3);
      await question('Yeni bağlantıda sorum', 'fourth');
      assert.equal(await page.locator('.chat-turn').nth(3).locator('.chat-transfer-fields').count(), 0);
      const interpretation = {...call, action: 'analyze', counts: {...zero, derived_summaries: 2,
        derived_observations: 29}, data_dates: {first: '2026-09-01', last: '2026-09-29'},
        fields: ['summaries[].statistics.median', 'summaries[].evidence[].value', 'candidates[].priority_score']};
      await emit(page, {type: 'health_transfer', turn: 'fourth', status: 'active', calls: [interpretation],
        counts: interpretation.counts, failed_sends: 0});
      const fourth = page.locator('.chat-turn').nth(3);
      await fourth.locator('.chat-transfer > summary').click();
      assert.match(await fourth.locator('.chat-transfer-body').textContent(), /Kanıtlı yorum adayları/);
      assert.match(await fourth.locator('.chat-transfer-body').textContent(), /Türetilmiş özet: 2/);
      assert.doesNotMatch(await fourth.locator('.chat-transfer-body').textContent(), /yalnız üstveri gönderildi/);
      // Reused bridge item id must produce a new answer, leaving the old answer intact.
      await emit(page, {type: 'message', id: 'answer', text: 'Yeni sohbet yanıtı\n' + 'Uzun araç kanıtı ve yorum.\n'.repeat(180)});
      await page.locator('#assistant-messages').evaluate(el => { el.scrollTop = el.scrollHeight; });
      assert.ok(await page.locator('#assistant-messages').evaluate(el =>
        el.scrollHeight > el.clientHeight && el.scrollTop + el.clientHeight >= el.scrollHeight - 2));
      await geometry(page);
      // Rendered Markdown ends with a newline text node after </p>; the words are what must not grow.
      assert.equal((await first.locator('.chat-assistant .chat-content').textContent()).trim(), 'İlk yanıt');
      assert.equal(await page.locator('.chat-assistant').count(), 3);
      await emit(page, {type: 'health_transfer', turn: 'fourth', status: 'completed', calls: [interpretation], counts: interpretation.counts, failed_sends: 0});
      await emit(page, {type: 'done', status: 'completed'});
      await page.locator('#assistant-new').click();
      await page.waitForFunction(() => !document.querySelector('#assistant-input').disabled);
      assert.equal(await page.locator('.chat-turn, .chat-transfer, .chat-boundary').count(), 0);
      await question('Sıfırdan', 'fifth');
      assert.equal(await page.locator('.chat-transfer-fields').count(), 0);
      await geometry(page);
      console.log(`PASS ${width}x${height}: turn receipts, long accessible fields, reset, reconnect, keyboard and no page overflow`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
