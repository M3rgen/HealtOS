// Run with Playwright on NODE_PATH and the local site running.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    for (const width of [1280, 390, 320]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
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
            chunks.forEach((text, index) => setTimeout(() => this.onmessage?.({
              data: JSON.stringify({type: 'delta', id: 'answer', text}),
            }), 500 + 100 * index));
            setTimeout(() => this.onmessage?.({data: JSON.stringify({
              type: 'done', status: 'completed',
            })}), 1200);
          }
          close() {}
        };
      });
      await page.goto(process.env.UMAY_BASE_URL || 'http://localhost:8000');
      await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
      assert.equal(await page.locator('#assistant-dialog').isVisible(), false);
      assert.equal(await page.evaluate(() => document.activeElement.id === 'assistant-input'), false);
      await page.locator('#assistant-open').click();
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      async function checkGeometry() {
        const geometry = await page.evaluate(() => {
          const panel = document.querySelector('#assistant-dialog').getBoundingClientRect();
          const bubble = document.querySelector('#assistant-open').getBoundingClientRect();
          return {
            fits: panel.left >= 0 && panel.top >= 0 && panel.right <= innerWidth && panel.bottom <= innerHeight,
            attached: Math.abs(bubble.top - panel.bottom - 12) < 1 || Math.abs(panel.top - bubble.bottom - 12) < 1,
            overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight,
          };
        });
        assert.equal(geometry.fits, true);
        assert.equal(geometry.attached, true);
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
      await page.screenshot({path: `.test-tmp-umay-thinking-${width}.png`});
      await page.emulateMedia({reducedMotion: 'reduce'});
      assert.equal(await page.locator('#assistant-typing span').first().evaluate(el => getComputedStyle(el).animationName), 'none');
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
      await page.screenshot({path: `.test-tmp-umay-render-${width}.png`});
      await page.locator('#assistant-new').click();
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      assert.equal(await page.locator('#assistant-typing').count(), 1);
      assert.equal(await page.locator('.chat-message').count(), 0);
      await page.evaluate(() => window.umayTestSocket.onmessage({data: JSON.stringify({
        type: 'message', id: 'retained', text: 'Bağlantı kopsa da bu mesaj kalmalı.',
      })}));
      await page.evaluate(() => window.umayTestSocket.onclose());
      assert.equal(await page.locator('#assistant-status').textContent(), 'Çevrimdışı');
      assert.equal(await page.locator('#assistant-typing').isVisible(), false);
      await page.locator('#assistant-status').getByText('Aktif', {exact: true}).waitFor();
      assert.equal(await page.evaluate(() => window.umayConnections), 3);
      assert.equal(await page.locator('.chat-message').count(), 1);
      console.log(`PASS ${width}px: automatic connection/recovery, synchronized drag, resize, formatting, safe HTML, presence, typing dots, reduced motion, reset, no page overflow`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
