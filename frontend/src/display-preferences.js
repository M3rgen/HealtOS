// Display preferences (time zone choice, body model rotation), shared by every device
// through /api/settings/display. localStorage keeps the offline copy under the keys the
// dashboard used before, so the first paint never waits for the server. Like the
// appearance selection (appearance.js): the server's copy wins when it is reachable; a
// change that could not reach it is marked pending and sent by the next sync that does
// (last write wins).
import { request } from './api.js';

export const SCHEMA_VERSION = 1;
const REMOTE_URL = '/api/settings/display';
const KEYS = { timezone: 'healtos.timezone.v2', motion: 'healtos.motion' };
// Same rule as TimeZoneChoice in app/api/settings.py; Intl then decides if the zone exists.
const ZONE = /^(auto|[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+){0,2})$/;

function stored(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* Restricted browser storage still allows using the app. */
  }
  return null;
}

/** Keeps only preferences this version understands: {timezone?, motion?}. */
export function cleanPreferences(value, validZone) {
  const out = {};
  const zone = value?.timezone;
  if (typeof zone === 'string' && zone.length <= 64 && ZONE.test(zone) && (zone === 'auto' || validZone(zone)))
    out.timezone = zone;
  if (typeof value?.motion === 'boolean') out.motion = value.motion;
  return out;
}

/**
 * read() → this browser's copy, at once. write(preferences) stores it here and sends it.
 * sync() → the server's preferences, or null when it is unreachable or this browser's copy
 * was sent instead (an unsent change, or a server with nothing stored yet).
 * onStatus('saving' | 'saved' | 'offline') reports each send.
 */
export function createDisplayPreferences({
  validZone,
  url = REMOTE_URL,
  pendingKey = 'healtos.display.pending',
  onStatus = () => {},
}) {
  const read = () => {
    const motion = stored(KEYS.motion);
    return cleanPreferences(
      { timezone: stored(KEYS.timezone), motion: motion === null ? undefined : motion === 'true' },
      validZone,
    );
  };
  const keep = preferences => {
    stored(KEYS.timezone, preferences.timezone ?? null);
    stored(KEYS.motion, preferences.motion === undefined ? null : String(preferences.motion));
  };
  const pending = value => (value === undefined ? stored(pendingKey) === '1' : stored(pendingKey, value ? '1' : null));
  let latest = null,
    queue = Promise.resolve();
  const push = async preferences => {
    onStatus('saving');
    try {
      await request(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify({ schema_version: SCHEMA_VERSION, preferences }),
      });
      if (latest === preferences) {
        pending(false);
        onStatus('saved');
      }
    } catch {
      onStatus('offline'); // Offline or rejected: stays pending; the local copy still applies.
    }
  };
  const send = preferences => {
    latest = preferences;
    pending(true);
    // One request at a time, newest last; superseded writes are skipped.
    queue = queue.then(() => (latest === preferences ? push(preferences) : undefined));
    return queue;
  };
  return {
    read,
    write(preferences) {
      const clean = cleanPreferences(preferences, validZone);
      keep(clean);
      return send(clean);
    },
    async sync() {
      let remote;
      try {
        remote = await request(url);
      } catch {
        return null;
      }
      const local = read(); // Read after the request: a change made meanwhile wins.
      if (pending() || (!remote?.updated_at && Object.keys(local).length)) {
        await send(local);
        return null;
      }
      if (remote?.schema_version !== SCHEMA_VERSION) return null;
      const preferences = cleanPreferences(remote.preferences, validZone);
      keep(preferences);
      return preferences;
    },
  };
}
