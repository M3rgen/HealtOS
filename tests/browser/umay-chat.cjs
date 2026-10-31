// Synthetic fixtures; no live account records.
// Umay chat: streaming, Markdown rendering, unsafe HTML, presence, drag/resize, no page overflow.
// The chat socket is replaced in the page, so no Codex bridge is needed. Targets the shared
// HEALTHOS_TEST_URL (Vite) by default; set UMAY_BASE_URL to run it against the built app served
// by the backend instead (for example http://127.0.0.1:8000, to exercise its real CSP). With
// UMAY_BASE_URL set and nothing serving the app there, the check is skipped.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, shot, skip, run } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const TARGET = (process.env.UMAY_BASE_URL || BASE).replace(/\/$/, '');

async function reachable(url) {
  try {
    const response = await fetch(url, {signal: AbortSignal.timeout(3000), redirect: 'manual'});
    if (response.status !== 200) return `answered HTTP ${response.status}`;
    return (await response.text()).includes('assistant-open') ? null : 'did not serve the HealthOS page';
  } catch (error) {
    return `is not reachable (${error.cause?.code ?? error.name})`;
  }
}

// Dashboard data is always synthetic, even against a backend that has a real archive.
async function mock(page) {
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== new URL(TARGET).origin) return route.abort();
    const p = url.pathname;
    if (p === '/health') return route.fulfill({json: fx.health});
    if (p === '/api/dashboard') return route.fulfill({json: fx.dashboard(url.searchParams)});
    if (p === '/sync/status') return route.fulfill({json: fx.syncStatus});
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({status: 404, json: {}});
    return route.continue();
  });
}

async function retryScenarios(browser, width, height) {
  const page = await browser.newPage({viewport: {width, height}});
  await mock(page);
  await page.addInitScript(() => {
    const OriginalSocket = window.WebSocket;
    window.umayMode = 'down';
    window.umayConnections = 0;
    window.umayLiveSockets = 0;
    window.umayPeakSockets = 0;
    window.WebSocket = class {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
      constructor(url) {
        if (!url.endsWith('/api/codex/chat')) return new OriginalSocket(url);
        this.readyState = 0;
        window.umayTestSocket = this;
        window.umayConnections++;
        window.umayLiveSockets++;
        window.umayPeakSockets = Math.max(window.umayPeakSockets, window.umayLiveSockets);
        setTimeout(() => {
          if (this.readyState !== 0 || window.umayMode === 'hold') return;
          if (window.umayMode === 'up') return this.ready();
          const errors = [
            'Umay bağlantısı henüz yapılandırılmamış. Bağlantı ayarlarını kontrol et.',
            'Umay’a şu an ulaşılamıyor. Bağlantı yeniden denenecek.',
            'Umay’a bağlanırken zaman aşımı oldu. Bağlantı yeniden denenecek.',
            'Umay’ın hazırlanması uzun sürdü. Bağlantı yeniden denenecek.',
          ];
          this.onmessage?.({data: JSON.stringify({type: 'error',
            text: errors[Math.min(window.umayConnections - 1, errors.length - 1)]})});
          this.close();
        }, 20);
      }
      ready() {
        this.readyState = 1;
        this.onmessage?.({data: JSON.stringify({type: 'ready', model: 'synthetic', effort: 'medium'})});
      }
      close() {
        if (this.readyState === 3) return;
        this.readyState = 3;
        window.umayLiveSockets--;
        this.onclose?.();
      }
    };
  });
  await page.goto(TARGET);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  await page.clock.install();
  // The installed clock flows until paused and its first real-time sync can run it a little ahead
  // of Node's Date.now(), so pausing at new Date() can land in the clock's past ("Cannot
  // fast-forward to the past"). Pause just ahead of the page's own clock instead.
  await page.clock.pauseAt(await page.evaluate(() => Date.now()) + 1000);
  await page.locator('#assistant-open').click({force: true});
  await page.clock.runFor(25);

  async function offline(expected) {
    assert.match(await page.locator('#assistant-error').textContent(), expected);
    assert.equal(await page.locator('#assistant-error').getAttribute('role'), 'alert');
    assert.equal(await page.getByRole('button', {name: 'Yeniden bağlan', exact: true}).isVisible(), true);
    for (const id of ['assistant-input', 'assistant-send']) assert.equal(await page.locator(`#${id}`).isDisabled(), true);
    assert.equal(await page.evaluate(() => {
      const fits = ['assistant-dialog', 'assistant-error', 'assistant-reconnect'].every(id => {
        const r = document.getElementById(id).getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
      });
      return fits && document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight;
    }), true);
    assert.doesNotMatch(await page.locator('#assistant-error').textContent(), /codex|token|scripts\//i);
  }

  await offline(/yapılandırılmamış/);
  // Five retries after the initial attempt, with the existing 1/2/4/8/16 s backoff.
  for (const [index, delay] of [1000, 2000, 4000, 8000, 16000].entries()) {
    await page.clock.runFor(delay - 30);
    assert.equal(await page.evaluate(() => window.umayConnections), index + 1);
    await page.clock.runFor(55);
    assert.equal(await page.evaluate(() => window.umayConnections), index + 2);
    await offline(index === 0 ? /ulaşılamıyor/ : index === 1 ? /bağlanırken zaman aşımı/ : index === 2 ? /hazırlanması/ : /Umay/);
  }
  await page.clock.runFor(60000);
  assert.equal(await page.evaluate(() => window.umayConnections), 6);
  await offline(/şu an çevrimdışı/);

  // Manual recovery after budget exhaustion; online/visibility must not duplicate a handshake.
  await page.evaluate(() => { window.umayMode = 'hold'; });
  await page.getByRole('button', {name: 'Yeniden bağlan', exact: true}).click({force: true});
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  assert.equal(await page.evaluate(() => window.umayConnections), 7);
  assert.equal(await page.locator('#assistant-send').isDisabled(), true);
  await page.evaluate(() => window.umayTestSocket.ready());
  assert.equal(await page.locator('#assistant-input').isDisabled(), false);
  assert.equal(await page.locator('#assistant-error').isVisible(), false);

  // Recovery within the retry budget, with a manual retry cancelling an already scheduled timer.
  await page.evaluate(() => window.umayTestSocket.close());
  await page.clock.runFor(1000);
  await page.clock.runFor(25);
  assert.equal(await page.evaluate(() => window.umayConnections), 8);
  await page.evaluate(() => window.umayTestSocket.close());
  await page.evaluate(() => { window.umayMode = 'up'; });
  await page.getByRole('button', {name: 'Yeniden bağlan', exact: true}).click({force: true});
  await page.clock.runFor(25);
  await page.clock.runFor(30000);
  assert.equal(await page.evaluate(() => window.umayConnections), 9);
  assert.equal(await page.evaluate(() => window.umayPeakSockets), 1);
  assert.equal(await page.locator('#assistant-input').isDisabled(), false);
  await page.evaluate(() => window.umayTestSocket.close());
  await page.clock.runFor(1025);
  assert.equal(await page.evaluate(() => window.umayConnections), 10);
  assert.equal(await page.locator('#assistant-input').isDisabled(), false);
  // Leaving while a retry is pending cancels it, including visibility/online events.
  await page.evaluate(() => {
    window.umayMode = 'down';
    window.umayTestSocket.close();
    window.dispatchEvent(new Event('pagehide'));
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.clock.runFor(60000);
  assert.equal(await page.evaluate(() => window.umayConnections), 10);
  console.log(`PASS ${width}x${height}: safe errors, bounded backoff, manual/automatic recovery, disabled sending, one socket, no page overflow`);
  await page.close();
}

run(async () => {
  const problem = await reachable(TARGET);
  if (problem) {
    if (process.env.UMAY_BASE_URL) skip(`no backend: ${TARGET} ${problem}`);
    throw new Error(`${TARGET} ${problem}`);
  }
  const browser = await launch();
  try {
    for (const width of [1280, 390, 320]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      await mock(page);
      await page.addInitScript(() => {
        const OriginalSocket = window.WebSocket;
        window.WebSocket = class {
          constructor(url) {
            if (!url.endsWith('/api/codex/chat')) return new OriginalSocket(url);
            window.umayTestSocket = this;
            window.umayConnections = (window.umayConnections ?? 0) + 1;
            setTimeout(() => this.onmessage?.({data: JSON.stringify({
              type: 'ready', model: 'test', effort: 'medium',
            })}), 20);
          }
          send(data) {
            if (!JSON.parse(data).text) return;
            const chunks = ['**Mer', 'haba**.\n\n',
              '| Dönem | Uyku |\n|---|---|\n| Eylül | 8 saat |\n\n',
              '<img src=x onerror="window.umayUnsafe=true">',
              '<script>window.umayUnsafe=true</script>',
              '[zararlı](javascript:alert(1))\n\nSon cümle.'];
            // The answer streams only when the test calls umayAnswer(), so the "thinking" state
            // can be checked however slowly the page runs.
            window.umayAnswer = () => {
              chunks.forEach((text, index) => setTimeout(() => this.onmessage?.({
                data: JSON.stringify({type: 'delta', id: 'answer', text}),
              }), 100 * index));
              setTimeout(() => this.onmessage?.({data: JSON.stringify({
                type: 'done', status: 'completed',
              })}), 100 * chunks.length);
            };
          }
          close() {}
        };
      });
      await page.goto(TARGET);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(await page.locator('#assistant-dialog').isVisible(), false);
      assert.equal(await page.evaluate(() => document.activeElement.id === 'assistant-input'), false);
      // The chat socket opens only when Umay is first opened.
      assert.equal(await page.evaluate(() => window.umayConnections ?? 0), 0);
      await page.locator('#assistant-open').click();
      await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      async function checkGeometry() {
        // Move off the bubble so its :hover scale does not shift the measured box.
        await page.mouse.move(1, 1);
        await page.waitForFunction(() => !document.getElementById('assistant-open').getAnimations().length);
        const geometry = await page.evaluate(() => {
          const panel = document.querySelector('#assistant-dialog').getBoundingClientRect();
          const bubble = document.querySelector('#assistant-open').getBoundingClientRect();
          return {
            fits: panel.left >= 0 && panel.top >= 0 && panel.right <= innerWidth && panel.bottom <= innerHeight,
            attached: Math.abs(bubble.top - panel.bottom - 12) < 1 || Math.abs(panel.top - bubble.bottom - 12) < 1,
            overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight,
            panel: [panel.top, panel.bottom], bubble: [bubble.top, bubble.bottom],
          };
        });
        assert.equal(geometry.fits, true);
        assert.equal(geometry.attached, true, JSON.stringify(geometry));
        assert.equal(geometry.overflow, false);
      }
      await checkGeometry();
      const bubble = await page.locator('#assistant-open').boundingBox();
      await page.mouse.move(bubble.x + bubble.width / 2, bubble.y + bubble.height / 2);
      await page.mouse.down();
      await page.mouse.move(width / 2, 420, {steps: 10});
      await page.mouse.up();
      assert.equal(await page.locator('#assistant-dialog').isVisible(), true);
      await checkGeometry();
      await page.setViewportSize({width, height: 568});
      await checkGeometry();
      await page.setViewportSize({width, height: 900});
      await page.locator('#assistant-close').click();
      await page.locator('#assistant-open').click();
      await checkGeometry();
      await page.evaluate(() => {
        Object.defineProperty(window.visualViewport, 'height', {value: 350, configurable: true});
        window.visualViewport.dispatchEvent(new Event('resize'));
      });
      assert.equal(await page.evaluate(() => ['assistant-dialog', 'assistant-open'].every(id => {
        const rect = document.getElementById(id).getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= 350;
      })), true);
      await checkGeometry();
      await page.evaluate(() => {
        delete window.visualViewport.height;
        window.visualViewport.dispatchEvent(new Event('resize'));
      });
      const userText = '**Aynen kalsın** <img src=x onerror=alert(1)>';
      await page.locator('#assistant-input').fill(userText);
      await page.locator('#assistant-send').click();
      assert.equal(await page.locator('#assistant-status').textContent(), 'Yazıyor');
      assert.equal(await page.locator('#assistant-typing').isVisible(), true);
      await page.screenshot({path: shot(`umay-thinking-${width}.png`)});
      await page.emulateMedia({reducedMotion: 'reduce'});
      assert.equal(await page.locator('#assistant-typing span').first().evaluate(el => getComputedStyle(el).animationName), 'none');
      await page.evaluate(() => window.umayAnswer());
      await page.locator('.chat-assistant .chat-content').getByText('Son cümle.', {exact: false}).waitFor();
      await page.waitForFunction(() => document.querySelector('#assistant-messages').getAttribute('aria-busy') === 'false');
      const result = await page.evaluate(() => {
        const content = document.querySelector('.chat-assistant .chat-content');
        const dialog = document.querySelector('#assistant-dialog');
        return {
          bold: content.querySelector('strong')?.textContent,
          cells: [...content.querySelectorAll('td')].map(el => el.textContent),
          text: content.textContent,
          unsafe: !!window.umayUnsafe,
          unsafeNodes: content.querySelectorAll('script,img,iframe,input,[onerror],a[href^="javascript:"]').length,
          user: document.querySelector('.chat-user .chat-content').textContent,
          userTags: document.querySelector('.chat-user .chat-content').children.length,
          overflow: dialog.scrollWidth > dialog.clientWidth,
        };
      });
      assert.equal(result.bold, 'Merhaba');
      assert.deepEqual(result.cells, ['Eylül', '8 saat']);
      assert.ok(result.text.includes('Son cümle.'));
      assert.ok(!result.text.includes('**'));
      assert.equal(result.unsafe, false);
      assert.equal(result.unsafeNodes, 0);
      assert.equal(result.user, userText);
      assert.equal(result.userTags, 0);
      assert.equal(result.overflow, false);
      assert.equal(await page.locator('#assistant-status').textContent(), 'Aktif');
      assert.equal(await page.locator('#assistant-typing').isVisible(), false);
      await page.screenshot({path: shot(`umay-render-${width}.png`)});
      await page.locator('#assistant-new').click();
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      assert.equal(await page.locator('#assistant-typing').count(), 1);
      assert.equal(await page.locator('.chat-message').count(), 0);
      await page.evaluate(() => window.umayTestSocket.onmessage({data: JSON.stringify({
        type: 'message', id: 'retained', text: 'Bağlantı kopsa da bu mesaj kalmalı.',
      })}));
      // Read the offline state in the same task as the close: the automatic retry follows 1 s later.
      const offline = await page.evaluate(() => {
        window.umayTestSocket.onclose();
        return {status: document.querySelector('#assistant-status').textContent,
          typing: !!document.querySelector('#assistant-typing').checkVisibility()};
      });
      assert.deepEqual(offline, {status: 'Çevrimdışı', typing: false});
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      assert.equal(await page.evaluate(() => window.umayConnections), 3);
      assert.equal(await page.locator('.chat-message').count(), 1);
      console.log(`PASS ${width}px: automatic connection/recovery, synchronized drag, resize, formatting, safe HTML, presence, typing dots, reduced motion, reset, no page overflow`);
      await page.close();
    }
    for (const [width, height] of [[1280, 900], [390, 844], [320, 568]]) {
      await retryScenarios(browser, width, height);
    }
  } finally {
    await browser.close();
  }
});
