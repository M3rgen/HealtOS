// SYNTHETIC preview fixtures. Every value here is invented; no account data.
// The clock is frozen at NOW so screenshots are deterministic.
const NOW = '2026-10-16T14:30:00Z';
const TODAY = NOW.slice(0, 10);

const iso = (date) => date.toISOString().slice(0, 10);
const civil = (value) => new Date(`${value}T12:00:00Z`);
const minus = (value, days) => { const d = civil(value); d.setUTCDate(d.getUTCDate() - days); return iso(d); };
// Small deterministic pseudo-random walk so charts look organic but stable.
const wave = (i, seed) => (Math.sin(i * 1.7 + seed) + Math.sin(i * 0.63 + seed * 2.1)) / 2;

const health = {
  status: 'healthy', database: 'healthy', scheduler: 'running', configuration: 'healthy',
  checked_at: '2026-10-16T14:28:00Z', last_sync_success: '2026-10-16T14:15:00Z',
  latest_health_write: '2026-10-16T14:15:00Z', last_webhook_received: '2026-10-16T13:58:00Z',
  data_freshness: { heart_rate: '2026-10-16T14:05:00Z', steps: '2026-10-16T14:10:00Z' },
  pending_notifications: 0, failed_notifications: 0, failed_syncs: 0, stale_streams: [],
};

function day(date, i) {
  const partial = date === TODAY;
  const missingSpo2 = i % 5 === 3;
  return {
    date,
    steps: Math.round((partial ? 6240 : 9100) + wave(i, 1) * 2600),
    sleep_seconds: Math.round(26400 + wave(i, 2) * 3600),
    resting_bpm: Math.round(58 + wave(i, 3) * 4),
    hrv_ms: 44 + Math.round(wave(i, 4) * 90) / 10,
    spo2: missingSpo2 ? null : 97 + Math.round(wave(i, 5) * 10) / 10,
    partial,
    status: partial ? 'partial' : missingSpo2 ? 'missing' : 'complete',
  };
}

function dashboard(params) {
  const days = Math.max(1, Math.min(31, Number(params.get('days') || 7)));
  const end = params.get('end_date') || TODAY;
  const rows = Array.from({ length: days }, (_, i) => day(minus(end, i), Number(end.slice(-2)) - i));
  const daytime = (h) => h >= 7 && h <= 22;
  return {
    start_date: minus(end, days - 1), end_date: end, timezone: params.get('timezone') || 'UTC',
    profile: {
      height: { value: 178, measured_at: '2026-05-29T08:00:00Z' },
      weight: { value: 74.6, measured_at: '2026-09-30T07:30:00Z' },
      body_fat: { value: 17.8, measured_at: '2026-09-30T07:30:00Z' },
    },
    days: rows,
    charts: {
      steps: Array.from({ length: 24 }, (_, h) => end === TODAY && h > 14 ? null
        : daytime(h) ? Math.round(420 + Math.max(0, wave(h, 7)) * 900 + (h === 8 || h === 18 ? 1100 : 0)) : (h === 6 ? 60 : 0)),
      heart_rate: Array.from({ length: 24 }, (_, h) => end === TODAY && h > 14 ? null
        : h === 3 ? null : Math.round((daytime(h) ? 72 : 57) + wave(h, 9) * 6 + (h === 18 ? 24 : 0))),
    },
  };
}

function history(params) {
  const metric = params.get('metric') || 'steps';
  const page = Number(params.get('page') || 1);
  const end = minus(TODAY, 30 * (page - 1));
  const days = Array.from({ length: 30 }, (_, i) => {
    const date = minus(end, 29 - i);
    const row = day(date, i + page * 30);
    const value = metric === 'steps' ? row.steps : metric === 'sleep' ? row.sleep_seconds : row.resting_bpm;
    return { date, value: i % 11 === 4 ? null : value, partial: row.partial };
  });
  return {
    metric, page, total_pages: 7, start_date: minus(end, 29), end_date: end,
    earliest_date: minus(TODAY, 200), latest_date: TODAY, timezone: 'UTC', days,
  };
}

const measurements = [
  ['weight', 74.6, '2026-09-30T07:30:00Z'], ['body_fat', 17.8, '2026-09-30T07:30:00Z'],
  ['weight', 75.1, '2026-09-23T07:40:00Z'], ['body_fat', 18.2, '2026-09-23T07:40:00Z'],
  ['weight', 75.4, '2026-09-16T07:20:00Z'], ['muscle', 41.2, '2026-09-16T07:20:00Z'],
  ['weight', 75.9, '2026-09-09T07:35:00Z'], ['body_fat', 18.9, '2026-09-09T07:35:00Z'],
  ['weight', 76.3, '2026-09-02T07:30:00Z'], ['weight', 76.8, '2026-08-26T07:30:00Z'],
  ['height', 178, '2026-05-29T08:00:00Z'], ['weight', 77.4, '2026-08-19T07:30:00Z'],
  ['weight', 77.9, '2026-08-12T07:30:00Z'], ['body_fat', 19.6, '2026-08-12T07:30:00Z'],
].map(([metric, value, measured_at], i) => ({ id: `m${i}`, metric, value, measured_at, source: 'Manuel' }));

const syncStatus = ['steps', 'heart-rate', 'daily-resting-heart-rate', 'sleep', 'heart-rate-variability',
  'oxygen-saturation', 'exercise', 'weight'].map((data_type, i) => ({
  data_type, status: i === 6 ? 'running' : 'completed', last_error: null,
  last_requested_from: '2026-09-24T00:00:00Z', last_requested_to: NOW,
  last_successful_sync: `2026-10-16T14:${String(15 - i).padStart(2, '0')}:00Z`,
  records_downloaded: [18422, 96211, 214, 189, 2340, 1580, 61, 37][i],
}));

const sports = ['Yürüyüş', 'Koşu', 'Bisiklet', 'Yüzme', 'Ağırlık antrenmanı', 'Yoga'];
function workouts(params) {
  const month = (params.get('month') || `${TODAY.slice(0, 7)}-01`).slice(0, 7);
  const last = month === TODAY.slice(0, 7) ? Number(TODAY.slice(-2)) : 28;
  return Array.from({ length: 12 }, (_, i) => {
    const date = `${month}-${String(Math.max(1, Math.round(1 + i * (last - 1) / 11))).padStart(2, '0')}`;
    const minutes = 25 + ((i * 17) % 50);
    const start = new Date(`${date}T${String(6 + (i % 3) * 3).padStart(2, '0')}:15:00Z`);
    const type = sports[i % sports.length];
    return {
      id: `w${i}`, exercise_type: type, start_time: start.toISOString(),
      end_time: new Date(start.getTime() + minutes * 60000).toISOString(), duration_seconds: minutes * 60,
      calories_kcal: 140 + ((i * 53) % 380), distance_meters: ['Yoga', 'Ağırlık antrenmanı'].includes(type) ? null : 1800 + i * 730,
      source: i % 4 === 1 ? 'Manuel' : 'Fitbit',
    };
  }).reverse();
}

const chatAnswer = '**Merhaba!** Son 7 günün özeti:\n\n' +
  '- Ortalama adım: **9.120**\n- Ortalama uyku: **7 sa 18 dk**\n- Dinlenme nabzı istikrarlı (57–61 bpm).\n\n' +
  '| Gün | Adım | Uyku |\n|---|---:|---:|\n| Pzt | 10.240 | 7 sa 40 dk |\n| Sal | 8.910 | 6 sa 55 dk |\n\n' +
  'Uyku düzenini korumak için yatış saatini sabit tutmayı deneyebilirsin.';

// Alternate data sets for edge-case views. 'sync-error' shows the notice
// banner; 'empty' is a fresh install: Google not set up and no records yet.
function scenario(name) {
  if (name === 'sync-error') return {
    health: { ...health, failed_syncs: 1 },
    syncStatus: syncStatus.map((row, i) => i === 3 ? { ...row, status: 'failed', last_error: 'Google API zaman aşımı (synthetic).' } : row),
  };
  if (name === 'empty') {
    const blank = (params) => {
      const data = dashboard(params);
      return { ...data, profile: {}, charts: { steps: data.charts.steps.map(() => null), heart_rate: data.charts.heart_rate.map(() => null) },
        days: data.days.map((d) => ({ ...d, steps: null, sleep_seconds: null, resting_bpm: null, hrv_ms: null, spo2: null, status: d.partial ? 'partial' : 'empty' })) };
    };
    return {
      health: { ...health, configuration: 'degraded', last_sync_success: null, latest_health_write: null, last_webhook_received: null, data_freshness: {} },
      dashboard: blank, measurements: [], syncStatus: [], workouts: () => [],
      history: (params) => ({ ...history(params), total_pages: 1, earliest_date: null, days: history(params).days.map((d) => ({ ...d, value: null })) }),
    };
  }
  return {};
}

// Synthetic upload files for the appearance views/tests (generated, no assets).
const zlib = require('node:zlib');
function crc32(buffer) {
  let crc = ~0;
  for (const byte of buffer) { crc ^= byte; for (let k = 0; k < 8; k++) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1)); }
  return ~crc >>> 0;
}
function syntheticPng(width, height, paint) {
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const out = Buffer.alloc(12 + data.length);
    out.writeUInt32BE(data.length, 0); body.copy(out, 4); out.writeUInt32BE(crc32(body), 8 + data.length);
    return out;
  };
  const rows = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const [r, g, b] = paint(x / width, y / height);
    rows.set([r, g, b], y * (width * 3 + 1) + 1 + x * 3);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0))]);
}
// A teal disc on a dusk gradient: reads as a portrait-like avatar when cropped.
const uploadAvatarPng = () => syntheticPng(320, 240, (u, v) => {
  const d = Math.hypot(u - .5, (v - .45) * .75);
  return d < .2 ? [72, 216, 176] : [Math.round(40 + 60 * v), Math.round(30 + 30 * u), Math.round(70 + 80 * (1 - v))];
});
const uploadBackgroundPng = () => syntheticPng(640, 400, (u, v) => [Math.round(30 + 90 * u), Math.round(50 + 40 * v), Math.round(90 + 60 * (1 - u))]);
// Includes a script and an id marker: tests assert neither ever reaches the DOM.
const uploadLogoSvg = () => Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" id="uploaded-svg-marker"><script>window.uploadedSvgRan=true</script><circle cx="24" cy="24" r="18" fill="none" stroke="#48d88c" stroke-width="5"/><path d="M15 24h18" stroke="#48d88c" stroke-width="5" stroke-linecap="round"/></svg>');

module.exports = { NOW, TODAY, health, dashboard, history, measurements, syncStatus, workouts, chatAnswer, scenario, syntheticPng, uploadAvatarPng, uploadBackgroundPng, uploadLogoSvg };
