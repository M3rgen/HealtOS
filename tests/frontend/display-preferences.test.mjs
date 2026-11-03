import test from 'node:test';
import assert from 'node:assert/strict';
import { cleanPreferences, createDisplayPreferences } from '../../frontend/src/display-preferences.js';

const validZone = zone => ['Europe/Istanbul', 'UTC', 'America/Argentina/Buenos_Aires'].includes(zone);

// A fake server and storage; `online` false makes every request fail like a dropped tunnel.
function environment(t, { remote = null, online = true, local = {} } = {}) {
  const saved = ['localStorage', 'fetch'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
  t.after(() => saved.forEach(([key, descriptor]) => {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }));
  const items = new Map(Object.entries(local));
  const state = { remote, online, puts: [] };
  const storage = {
    getItem: key => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: key => items.delete(key),
  };
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  const fetch = async (url, options = {}) => {
    assert.equal(url, '/api/settings/display');
    if (!state.online) throw new TypeError('Failed to fetch');
    if (options.method === 'PUT') {
      assert.equal(options.headers['X-HealthOS-Request'], '1');
      const body = JSON.parse(options.body);
      state.puts.push(body);
      state.remote = { ...body, updated_at: '2026-11-02T10:00:00Z' };
      return json(200, state.remote);
    }
    return json(200, state.remote ?? { schema_version: 1, preferences: {}, updated_at: null });
  };
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true });
  Object.defineProperty(globalThis, 'fetch', { value: fetch, configurable: true, writable: true });
  return { items, state };
}

test('cleanPreferences keeps only known, well-formed values', () => {
  assert.deepEqual(cleanPreferences({ timezone: 'Europe/Istanbul', motion: false, theme: 'dark' }, validZone),
    { timezone: 'Europe/Istanbul', motion: false });
  assert.deepEqual(cleanPreferences({ timezone: 'auto', motion: true }, validZone), { timezone: 'auto', motion: true });
  for (const timezone of ['Mars/Olympus', '../x', 'a;b', 'Europe/Istanbul'.padEnd(70, 'x'), 3, null])
    assert.deepEqual(cleanPreferences({ timezone }, validZone), {});
  assert.deepEqual(cleanPreferences({ motion: 'true' }, validZone), {});
  assert.deepEqual(cleanPreferences(null, validZone), {});
});

test('reads the keys the dashboard used before, so existing choices survive', t => {
  environment(t, { local: { 'healtos.timezone.v2': 'UTC', 'healtos.motion': 'false' } });
  assert.deepEqual(createDisplayPreferences({ validZone }).read(), { timezone: 'UTC', motion: false });
});

test('a server copy wins and replaces this browser copy', async t => {
  const { items } = environment(t, {
    remote: { schema_version: 1, preferences: { timezone: 'Europe/Istanbul' }, updated_at: '2026-11-01T08:00:00Z' },
    local: { 'healtos.timezone.v2': 'UTC', 'healtos.motion': 'false' },
  });
  const display = createDisplayPreferences({ validZone });
  assert.deepEqual(await display.sync(), { timezone: 'Europe/Istanbul' });
  assert.equal(items.get('healtos.timezone.v2'), 'Europe/Istanbul');
  assert.equal(items.has('healtos.motion'), false);
});

test('a server with nothing stored receives this browser copy', async t => {
  const { state } = environment(t, { local: { 'healtos.motion': 'false' } });
  assert.equal(await createDisplayPreferences({ validZone }).sync(), null);
  assert.deepEqual(state.puts, [{ schema_version: 1, preferences: { motion: false } }]);
});

test('offline: the change applies here, stays pending, and is sent once the server answers', async t => {
  const { items, state } = environment(t, {
    online: false,
    remote: { schema_version: 1, preferences: { timezone: 'UTC' }, updated_at: '2026-11-01T08:00:00Z' },
  });
  const statuses = [];
  const display = createDisplayPreferences({ validZone, onStatus: status => statuses.push(status) });
  await display.write({ timezone: 'Europe/Istanbul', motion: false });
  assert.deepEqual(display.read(), { timezone: 'Europe/Istanbul', motion: false });
  assert.equal(items.get('healtos.display.pending'), '1');
  assert.equal(await display.sync(), null);
  assert.deepEqual(statuses, ['saving', 'offline']);

  state.online = true; // Next load: the unsent change beats the older server copy.
  assert.equal(await createDisplayPreferences({ validZone }).sync(), null);
  assert.deepEqual(state.puts, [{ schema_version: 1, preferences: { timezone: 'Europe/Istanbul', motion: false } }]);
  assert.equal(items.has('healtos.display.pending'), false);
});

test('a server copy in a newer schema version is not applied', async t => {
  environment(t, {
    remote: { schema_version: 2, preferences: { timezone: { name: 'UTC' } }, updated_at: '2026-11-01T08:00:00Z' },
    local: { 'healtos.timezone.v2': 'UTC' },
  });
  const display = createDisplayPreferences({ validZone });
  assert.equal(await display.sync(), null);
  assert.deepEqual(display.read(), { timezone: 'UTC' });
});
