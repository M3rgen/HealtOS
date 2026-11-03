// Display preferences shared across devices: synthetic server state, two browser contexts
// (desktop and phone) and an offline phone that falls back to its own copy. No live records.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const overflow = page => page.evaluate(() => ({
  x: document.documentElement.scrollWidth > innerWidth,
  y: document.documentElement.scrollHeight > innerHeight,
}));
const note = page => page.locator('#display-preferences-note').textContent();
const DEFAULT_NOTE = 'Saat dilimi ve dönüş tercihi paneli açtığın her cihazda geçerli olur.';

async function device(browser, server, viewport, local = {}) {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(values => {
    if (!sessionStorage.getItem('seeded')) {
      for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
      sessionStorage.setItem('seeded', '1');
    }
  }, local);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/health', route => route.fulfill({ json: fx.health }));
  await page.route('**/api/dashboard?**', route => {
    server.zones.push(new URL(route.request().url()).searchParams.get('timezone'));
    return route.fulfill({ json: fx.dashboard(new URL(route.request().url()).searchParams) });
  });
  await page.route('**/sync/status', route => route.fulfill({ json: fx.syncStatus }));
  await page.route('**/api/settings/display', async route => {
    if (!server.online) return route.fulfill({ status: 503, json: { detail: 'Sentetik bağlantı hatası' } });
    const request = route.request();
    if (request.method() === 'PUT') {
      assert.equal((await request.allHeaders())['x-healthos-request'], '1');
      const body = request.postDataJSON();
      assert.equal(body.schema_version, 1);
      server.puts.push(body.preferences);
      server.stored = { ...body, updated_at: new Date().toISOString() };
      return route.fulfill({ json: server.stored });
    }
    return route.fulfill({ json: server.stored ?? { schema_version: 1, preferences: {}, updated_at: null } });
  });
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelector('#steps-value').textContent !== '—');
  await page.locator('[data-page="system"]').click();
  await page.locator('#timezone').waitFor({ state: 'visible' });
  return { context, page, errors };
}

run(async () => {
  const browser = await launch();
  const server = { online: true, stored: null, puts: [], zones: [] };
  try {
    // Desktop: an existing local choice reaches an empty server, then a new choice replaces it.
    const desk = await device(browser, server, { width: 1280, height: 720 }, { 'healtos.timezone.v2': 'UTC' });
    await until(() => server.puts.length === 1, 'the local copy must reach an empty server');
    assert.deepEqual(server.puts[0], { timezone: 'UTC', motion: false });
    await desk.page.locator('#timezone').selectOption('Europe/Istanbul');
    await until(() => server.puts.length === 2, 'a changed time zone must be sent');
    assert.deepEqual(server.puts[1], { timezone: 'Europe/Istanbul', motion: false });
    await desk.page.waitForFunction(text => document.querySelector('#display-preferences-note').textContent.startsWith(text), DEFAULT_NOTE);
    assert.deepEqual(await overflow(desk.page), { x: false, y: false });
    assert.deepEqual(desk.errors, []);
    await desk.context.close();

    // Phone: its own older choice gives way to the server copy, and the dashboard follows it.
    const phone = await device(browser, server, { width: 390, height: 844 }, { 'healtos.timezone.v2': 'UTC' });
    await phone.page.waitForFunction(() => document.querySelector('#timezone').value === 'Europe/Istanbul');
    await until(() => server.zones.at(-1) === 'Europe/Istanbul', 'the dashboard must reload in the shared zone');
    assert.equal(await phone.page.evaluate(() => localStorage.getItem('healtos.timezone.v2')), 'Europe/Istanbul');
    assert.equal(server.puts.length, 2, 'reading the shared copy must not write it back');
    assert.equal(await phone.page.locator('#system').isHidden(), false, 'the System page must stay open');
    assert.deepEqual(await overflow(phone.page), { x: false, y: false });
    await phone.page.screenshot({ path: shot('display-preferences-390x844.png') });

    // Offline: the change still applies on this device and is sent once the server answers.
    server.online = false;
    await phone.page.locator('#timezone').selectOption('UTC');
    await phone.page.waitForFunction(() => document.querySelector('#display-preferences-note').textContent.startsWith('Sunucuya ulaşılamadı'));
    await until(() => server.zones.at(-1) === 'UTC', 'the offline change must apply here');
    assert.deepEqual(await overflow(phone.page), { x: false, y: false });
    server.online = true;
    await phone.page.reload();
    await until(() => server.puts.length === 3, 'the pending change must be sent after reconnecting');
    assert.deepEqual(server.puts[2], { timezone: 'UTC', motion: false });
    assert.deepEqual(phone.errors, []);
    await phone.context.close();
    console.log('PASS 1280x720/390x844: shared display preferences, server copy wins, offline fallback and resend');
  } finally { await browser.close(); }
});
