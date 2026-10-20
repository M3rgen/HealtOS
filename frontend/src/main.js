import './style.css';
import { createBody } from './body.js';
import { renderIcons, iconMarkup } from './icons.js';
import { setupChat } from './chat.js';
import { setupMetricHistory } from './metric-history.js';
import { setupCalendar } from './calendar.js';
import { setupHistoryPanel } from './history-panel.js';
import { setupWorkouts } from './workouts.js';
import { setupAppearance } from './appearance.js';
import { request } from './api.js';

renderIcons();
setupAppearance();

const $ = id => document.getElementById(id);
const escape = value =>
  String(value ?? '').replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const num = (value, digits = 0) =>
  value == null ? '—' : Number(value).toLocaleString('tr-TR', { maximumFractionDigits: digits });
const duration = seconds =>
  seconds == null ? '—' : `${Math.floor(Math.round(seconds / 60) / 60)} sa ${Math.round(seconds / 60) % 60} dk`;
const metrics = {
  height: ['Boy', 'cm'],
  weight: ['Kilo', 'kg'],
  body_fat: ['Yağ oranı', '%'],
  muscle: ['Kas oranı', '%'],
};
const states = { empty: 'Kayıt yok', partial: 'Devam ediyor', complete: 'Kayıt var', missing: 'Eksik ölçüm' };
function readPreference(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function writePreference(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* Restricted browser storage still allows using the app. */
  }
}
const validZone = value => {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
    return true;
  } catch {
    return false;
  }
};
const deviceZone = (() => {
  try {
    const value = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return validZone(value) ? value : 'UTC';
  } catch {
    return 'UTC';
  }
})();
const resolveZone = choice => (choice === 'auto' ? deviceZone : choice);
let zoneChoice = readPreference('healtos.timezone.v2', 'auto');
if (zoneChoice !== 'auto' && !validZone(zoneChoice)) zoneChoice = 'auto';
let zone = resolveZone(zoneChoice);
let data = null,
  health = null,
  busy = false,
  pendingRefresh = false,
  activePage = 'overview',
  poll = null;
let selectedDate = null;
const motion = readPreference('healtos.motion', 'true') === 'true';
$('timezone').querySelector('option[value="auto"]').textContent = `Cihaz saati (${deviceZone})`;
if (![...$('timezone').options].some(option => option.value === zoneChoice))
  $('timezone').add(new Option(zoneChoice, zoneChoice));
$('timezone').value = zoneChoice;
$('model-motion').checked = motion;
const body = createBody($('body-model'), motion);
const shortDate = value =>
  new Intl.DateTimeFormat('tr-TR', { timeZone: zone, day: 'numeric', month: 'short' }).format(new Date(value));
const fullDate = value =>
  new Intl.DateTimeFormat('tr-TR', { timeZone: zone, day: 'numeric', month: 'long', year: 'numeric' }).format(
    new Date(value),
  );
const timeOf = value =>
  new Intl.DateTimeFormat('tr-TR', { timeZone: zone, hour: '2-digit', minute: '2-digit' }).format(new Date(value));
function today() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const get = type => parts.find(x => x.type === type).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function civilDate(value) {
  return new Date(`${value}T12:00:00Z`);
}
function notice(message, error = false, source = 'general') {
  $('notice').hidden = false;
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
  $('notice').dataset.source = source;
}
function clearSyncNotice(includeStart = false) {
  if (
    $('notice').dataset.source === 'sync' ||
    (includeStart && ['sync-start', 'sync-result'].includes($('notice').dataset.source))
  ) {
    $('notice').hidden = true;
    $('notice').textContent = '';
  }
}
function query() {
  return new URLSearchParams({ days: 7, timezone: zone, ...(selectedDate ? { end_date: selectedDate } : {}) });
}
function timeHtml(value) {
  return value ? `${escape(timeOf(value))}<small>${escape(fullDate(value))}</small>` : '—';
}
function setState(id, text, good) {
  $(id).textContent = text;
  $(id).className = good ? 'good' : 'warning';
}
function paintHealth() {
  if (!health) return;
  const status = health.status;
  $('service-status').textContent =
    status === 'healthy' ? 'Servis çalışıyor' : status === 'failed' ? 'Servis hatası' : 'Kontrol gerekiyor';
  $('service-status').className =
    `status-text ${status === 'healthy' ? 'good' : status === 'failed' ? 'bad' : 'warning'}`;
  $('last-sync').innerHTML = timeHtml(health.last_sync_success);
  $('last-heart').innerHTML = timeHtml(health.data_freshness?.heart_rate);
  $('last-steps').innerHTML = timeHtml(health.data_freshness?.steps);
  setState(
    'database-state',
    health.database === 'healthy' ? 'Çalışıyor' : 'Ulaşılamıyor',
    health.database === 'healthy',
  );
  setState(
    'scheduler-state',
    health.scheduler === 'running' ? 'Çalışıyor' : health.scheduler === 'disabled' ? 'Kapalı' : 'Durdu',
    health.scheduler === 'running',
  );
  $('webhook-info').innerHTML = health.latest_health_write
    ? `Son veri güncellemesi: ${escape(fullDate(health.latest_health_write))} · ${escape(timeOf(health.latest_health_write))}<small>Google’dan alınan verilerin veritabanına son kaydedildiği zaman.</small>`
    : 'Henüz veri kaydedilmedi.<small>Verileri almak için Şimdi eşitle düğmesini kullan.</small>';
  $('queue-state').textContent =
    `${num(health.pending_notifications ?? 0)} bekleyen · ${num((health.failed_notifications ?? 0) + (health.failed_syncs ?? 0))} hata`;
  const details = [
    ['Servis', status === 'healthy' ? 'Çalışıyor' : status === 'failed' ? 'Hata' : 'Kontrol gerekiyor'],
    ['Google yapılandırması', health.configuration === 'healthy' ? 'Hazır' : 'Eksik'],
    ['Son kontrol', health.checked_at ? `${shortDate(health.checked_at)} ${timeOf(health.checked_at)}` : '—'],
    [
      'Son veri yazımı',
      health.latest_health_write
        ? `${shortDate(health.latest_health_write)} ${timeOf(health.latest_health_write)}`
        : '—',
    ],
    [
      'Son Google bildirimi',
      health.last_webhook_received
        ? `${fullDate(health.last_webhook_received)} ${timeOf(health.last_webhook_received)}`
        : 'Henüz bildirim alınmadı',
    ],
    ['Bildirim hataları', num(health.failed_notifications ?? 0)],
    ['Eşitleme hataları', num(health.failed_syncs ?? 0)],
    ['Geciken veri türleri', num(health.stale_streams?.length ?? 0)],
  ];
  $('google-empty').hidden = health.configuration === 'healthy';
  $('system-details').innerHTML = details
    .map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`)
    .join('');
}
// Card sparklines: gradient bars with rounded tops and a smooth line with a soft area.
// preserveAspectRatio="none" stretches x, so strokes and dots use non-scaling-stroke.
const smooth = p =>
  p
    .map((pt, i) => {
      if (!i) return `M${pt[0]},${pt[1]}`;
      const a = p[i - 2] || p[i - 1],
        b = p[i - 1],
        d = p[i + 1] || pt;
      return `C${b[0] + (pt[0] - a[0]) / 6},${b[1] + (pt[1] - a[1]) / 6} ${pt[0] - (d[0] - b[0]) / 6},${pt[1] - (d[1] - b[1]) / 6} ${pt[0]},${pt[1]}`;
    })
    .join('');
function bars(values, color, alternate = color) {
  if (!values.some(v => v != null)) return '<p class="empty-chart">Bu dönemde veri yok</p>';
  const max = Math.max(...values.filter(v => v != null), 1),
    width = 240 / values.length,
    id = `bar-${color.replace(/\W/g, '')}`;
  const fill = (c, key) =>
    `<linearGradient id="${id}-${key}" gradientUnits="userSpaceOnUse" x1="0" y1="4" x2="0" y2="41"><stop offset="0" style="stop-color:${c}"/><stop offset="1" style="stop-color:${c};stop-opacity:.38"/></linearGradient>`;
  const bar = (v, i) => {
    const w = Math.max(width * (values.length > 12 ? 0.62 : 0.7), 1),
      x = i * width + (width - w) / 2,
      h = Math.max((v / max) * 35, 0),
      y = 41 - h,
      r = Math.min(1.6, w / 2, h);
    if (h < 1)
      return `<rect x="${x}" y="40" width="${w}" height="1" fill="${i % 2 ? alternate : color}" opacity=".35"><title>${i}: ${num(v, 1)}</title></rect>`;
    return `<path d="M${x},41V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V41Z" fill="url(#${id}-${i % 2 ? 'b' : 'a'})"><title>${i}: ${num(v, 1)}</title></path>`;
  };
  return `<svg viewBox="0 0 240 42" preserveAspectRatio="none" aria-hidden="true"><defs>${fill(color, 'a')}${fill(alternate, 'b')}</defs><path d="M0,41.5H240" stroke="var(--line)" vector-effect="non-scaling-stroke"/>${values.map((v, i) => (v == null ? '' : bar(v, i))).join('')}</svg>`;
}
function line(values) {
  const present = values.filter(v => v != null);
  if (!present.length) return '<p class="empty-chart">Bu dönemde veri yok</p>';
  const min = Math.min(...present) - 3,
    max = Math.max(...present) + 3;
  let paths = [],
    points = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (points.length) paths.push(points);
      points = [];
    } else points.push([(i / 23) * 240, 38 - ((v - min) / (max - min)) * 32]);
  });
  if (points.length) paths.push(points);
  const last = paths.at(-1).at(-1),
    stroke = 'stroke="var(--heart-chart)" vector-effect="non-scaling-stroke" stroke-linecap="round"';
  return `<svg viewBox="0 0 240 42" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="line-heart-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--heart-chart);stop-opacity:.26"/><stop offset="1" style="stop-color:var(--heart-chart);stop-opacity:0"/></linearGradient></defs><path d="M0,41.5H240" stroke="var(--line)" vector-effect="non-scaling-stroke"/>${paths.map(p => (p.length === 1 ? '' : `<path d="${smooth(p)}V42H${p[0][0]}Z" fill="url(#line-heart-area)"/><path d="${smooth(p)}" fill="none" ${stroke} stroke-linejoin="round" stroke-width="2"/>`)).join('')}${paths
    .filter(p => p.length === 1)
    .map(p => `<path d="M${p[0][0]},${p[0][1]}h0" ${stroke} stroke-width="4"/>`)
    .join(
      '',
    )}<path d="M${last[0]},${last[1]}h0" ${stroke} stroke-width="11" opacity=".22"/><path d="M${last[0]},${last[1]}h0" ${stroke} stroke-width="5.5"/></svg>`;
}
function paintData() {
  const first = data.days[0];
  $('profile-values').innerHTML = Object.entries(metrics)
    .map(([key, [label, unit]]) => {
      const item = data.profile[key];
      return `<div class="measurement"><label><span class="measure-icon">${iconMarkup(key)}</span>${label}</label><button data-metric="${key}" aria-label="${label} için manuel ölçüm ekle">${item ? `${num(item.value, 1)} ${unit}` : '—'}</button><small>${item ? escape(shortDate(item.measured_at)) : key === 'muscle' ? 'Manuel ölçüm' : 'Henüz ölçüm yok'}</small></div>`;
    })
    .join('');
  // Drive the 3D body's morphs from the latest profile measurements (missing values keep the basis).
  body.setBodyMetrics({
    heightCm: data.profile.height?.value,
    weightKg: data.profile.weight?.value,
    bodyFatPct: data.profile.body_fat?.value,
    musclePct: data.profile.muscle?.value,
  });
  $('steps-label').textContent = first.partial ? 'Bugünkü adım' : 'Seçilen gün adım';
  $('sleep-label').textContent = first.partial ? 'Son gece uyku' : 'Seçilen gün uyku';
  $('steps-value').textContent = num(first.steps);
  $('sleep-value').textContent = duration(first.sleep_seconds);
  $('heart-value').textContent = first.resting_bpm == null ? '—' : `${num(first.resting_bpm)} bpm`;
  $('steps-note').textContent =
    first.steps == null ? 'Adım kaydı yok' : first.partial ? 'Gün devam ediyor' : shortDate(civilDate(first.date));
  $('steps-note').classList.toggle('ongoing-badge', first.partial && first.steps != null);
  $('sleep-note').textContent = first.sleep_seconds == null ? 'Uyku kaydı yok' : shortDate(civilDate(first.date));
  $('heart-note').textContent = first.resting_bpm == null ? 'Günlük özet yok' : shortDate(civilDate(first.date));
  $('steps-chart').innerHTML = bars(data.charts.steps, 'var(--step-chart)');
  $('sleep-chart').innerHTML = bars(
    [...data.days].reverse().map(r => r.sleep_seconds),
    'var(--sleep-light)',
    'var(--sleep-dark)',
  );
  $('sleep-axis').textContent = `${data.days.length} gün`;
  $('heart-chart').innerHTML = line(data.charts.heart_rate);
  $('steps-chart').setAttribute(
    'aria-label',
    `Seçilen günün saatlik adımları: ${data.charts.steps.map((v, i) => `${i}:00 ${num(v)}`).join(', ')}`,
  );
  $('heart-chart').setAttribute(
    'aria-label',
    `Seçilen günün saatlik ortalama nabzı: ${data.charts.heart_rate.map((v, i) => `${i}:00 ${num(v, 1)}`).join(', ')}`,
  );
  $('sleep-chart').setAttribute('aria-label', data.days.map(r => `${r.date}: ${duration(r.sleep_seconds)}`).join(', '));
  historyPanel.update(data);
}
function paintHistory(data) {
  $('history-title').textContent = 'Son 7 gün';
  $('range-caption').textContent = `${shortDate(civilDate(data.start_date))} – ${fullDate(civilDate(data.end_date))}`;
  $('export-button').href =
    `/api/dashboard/export.csv?${new URLSearchParams({ days: data.days.length, end_date: data.end_date, timezone: data.timezone })}`;
  $('history-rows').innerHTML = data.days
    .map(
      row =>
        `<tr class="${row.partial ? 'today-row' : ''}"><td>${escape(shortDate(civilDate(row.date)))}</td><td>${num(row.steps)}</td><td>${duration(row.sleep_seconds)}</td><td>${row.resting_bpm == null ? '—' : `${num(row.resting_bpm)} bpm`}</td><td>${row.hrv_ms == null ? '—' : `${num(row.hrv_ms, 1)} ms`}</td><td>${row.spo2 == null ? '—' : `%${num(row.spo2, 1)}`}</td><td><span class="dot ${row.status === 'complete' || row.status === 'partial' ? 'good' : 'warning'}">${states[row.status]}</span></td></tr>`,
    )
    .join('');
}
async function refresh() {
  if (busy) {
    pendingRefresh = true;
    return;
  }
  busy = true;
  try {
    const results = await Promise.allSettled([
      request(`/api/dashboard?${query()}`),
      fetch('/health', { cache: 'no-store', signal: AbortSignal.timeout(15000) }).then(r => r.json()),
    ]);
    if (results[0].status === 'fulfilled') {
      data = results[0].value;
      paintData();
    } else notice('Veriler alınamadı. Bağlantını kontrol edip tekrar dene.', true);
    if (results[1].status === 'fulfilled') {
      health = results[1].value;
      paintHealth();
    } else {
      health = null;
      $('service-status').textContent = 'Servise ulaşılamıyor';
      $('service-status').className = 'status-text bad';
      $('system-details').textContent = 'Servise ulaşılamıyor. Durumu yenilemeyi dene.';
    }
    if (health) {
      if (health.failed_syncs > 0) {
        try {
          const rows = await request('/sync/status');
          const failed = rows.filter(row => row.status === 'failed' || row.last_error);
          if (failed.length)
            notice(
              `Eşitleme hatası: ${failed.map(row => `${row.data_type}: ${row.last_error || 'Eşitleme tamamlanamadı.'}`).join(' · ')}`,
              true,
              'sync',
            );
        } catch {
          notice('Eşitlemede hata oluştu. Hata ayrıntıları alınamadı; Sistem bölümünü kontrol et.', true, 'sync');
        }
      } else clearSyncNotice();
    }
    calendar.update();
  } finally {
    busy = false;
    if (pendingRefresh) {
      pendingRefresh = false;
      refresh();
    }
  }
}
async function loadMeasurements() {
  try {
    const rows = await request('/api/dashboard/measurements');
    $('measurement-rows').innerHTML = rows.length
      ? rows
          .map(
            row => {
              const [label, unit] = metrics[row.metric] ?? [row.metric ?? '?', ''];
              return `<tr><td>${escape(fullDate(row.measured_at))} ${escape(timeOf(row.measured_at))}</td><td><span class="cell-icon">${iconMarkup(row.metric)}</span>${escape(label)}</td><td>${num(row.value, 2)} ${escape(unit)}</td><td>Manuel</td></tr>`;
            },
          )
          .join('')
      : '<tr><td colspan="4">Henüz manuel ölçüm yok. Ölçüm ekleyerek başlayabilirsin.</td></tr>';
  } catch {
    notice('Ölçüm geçmişi alınamadı. Tekrar dene.', true);
  }
}
async function loadSystem() {
  try {
    const rows = await request('/sync/status');
    $('sync-rows').innerHTML = rows.length
      ? rows
          .map(
            row =>
              `<tr><td>${escape(row.data_type)}</td><td>${row.last_successful_sync ? `${escape(shortDate(row.last_successful_sync))} ${escape(timeOf(row.last_successful_sync))}` : '—'}</td><td>${escape({ completed: 'Tamamlandı', running: 'Çalışıyor', pending: 'Bekliyor', failed: 'Başarısız' }[row.status] ?? row.status)}</td><td>${num(row.records_downloaded)}</td></tr>`,
          )
          .join('')
      : '<tr><td colspan="4">Henüz eşitleme yapılmadı.</td></tr>';
  } catch {
    notice('Eşitleme durumu alınamadı. Tekrar dene.', true);
  }
}
function setPage(page) {
  activePage = page;
  document.querySelectorAll('.page').forEach(el => (el.hidden = el.id !== page));
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
    if (el.dataset.page === page) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  if (page === 'measurements') loadMeasurements();
  if (page === 'system') loadSystem();
  if (page === 'sports') workouts.update();
}
document.querySelectorAll('[data-page]').forEach(el => el.addEventListener('click', () => setPage(el.dataset.page)));
function setBounds() {
  const key = $('measurement-metric').value;
  const [min, max] = { height: [30, 260], weight: [1, 500], body_fat: [0, 100], muscle: [0, 100] }[key];
  $('measurement-value').min = min;
  $('measurement-value').max = max;
}
function openMeasurement(metric = 'weight') {
  $('measurement-form').reset();
  $('form-error').textContent = '';
  $('measurement-metric').value = metric;
  const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $('measurement-date').value = local;
  $('measurement-date').max = local;
  $('input-timezone').textContent = `Bu formun saat dilimi: ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`;
  if (data?.profile[metric]) $('measurement-value').value = data.profile[metric].value;
  setBounds();
  $('measurement-dialog').showModal();
  $('measurement-value').focus();
}
$('add-measurement').addEventListener('click', () => openMeasurement());
$('measurements-add').addEventListener('click', () => openMeasurement());
$('profile-values').addEventListener('click', event => {
  const el = event.target.closest('[data-metric]');
  if (el) openMeasurement(el.dataset.metric);
});
['measurement-close', 'measurement-cancel'].forEach(id =>
  $(id).addEventListener('click', () => $('measurement-dialog').close()),
);
$('measurement-metric').addEventListener('change', () => {
  setBounds();
  $('measurement-value').value = data?.profile[$('measurement-metric').value]?.value ?? '';
});
$('measurement-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('measurement-save').disabled = true;
  $('form-error').textContent = '';
  try {
    await request('/api/dashboard/measurements', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
      body: JSON.stringify({
        metric: $('measurement-metric').value,
        value: $('measurement-value').value,
        measured_at: new Date($('measurement-date').value).toISOString(),
      }),
    });
    $('measurement-dialog').close();
    notice('Manuel ölçüm kaydedildi.');
    await refresh();
    if (activePage === 'measurements') await loadMeasurements();
  } catch (error) {
    $('form-error').textContent = error.message;
  } finally {
    $('measurement-save').disabled = false;
  }
});
let syncing = false;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function syncBusy(on) {
  syncing = on;
  const button = $('sync-button');
  button.disabled = on;
  button.classList.toggle('syncing', on);
  button.setAttribute('aria-busy', String(on));
  button.lastElementChild.textContent = on ? 'Eşitleniyor…' : 'Şimdi eşitle';
}
async function watchSync() {
  try {
    const times = (await request('/google-health/devices')).devices
      .map(device => device.last_sync_time)
      .filter(Boolean)
      .sort((a, b) => new Date(b) - new Date(a));
    if (!times[0]) return '';
    const day = fullDate(times[0]) === fullDate(new Date()) ? '' : `${shortDate(times[0])} `;
    return ` Saatin son eşitlemesi: ${day}${timeOf(times[0])}`;
  } catch {
    return '';
  }
}
async function waitForSync({ data_types: types, sequence }) {
  // A run counts once its in-memory outcome has a sequence above the baseline returned by POST /sync/all.
  for (const deadline = Date.now() + 90000; Date.now() < deadline;) {
    await wait(2000);
    try {
      const rows = await request('/sync/status');
      const runs = types.map(type => {
        const row = rows.find(item => item.data_type === type);
        return { type, error: row?.last_error, ...row?.last_run };
      });
      if (runs.every(run => run.sequence > sequence)) return runs;
    } catch {
      /* Keep polling until the deadline. */
    }
  }
  return null;
}
$('sync-button').addEventListener('click', async () => {
  if (syncing) return;
  syncBusy(true);
  clearSyncNotice(true);
  try {
    const runs = await waitForSync(
      await request('/sync/all', { method: 'POST', headers: { 'X-HealthOS-Request': '1' } }),
    );
    const added = runs?.reduce((sum, run) => sum + run.inserted, 0),
      changed = runs?.reduce((sum, run) => sum + run.updated, 0),
      failed = runs?.filter(run => run.failed).length,
      counts = [added && `${num(added)} yeni kayıt`, changed && `${num(changed)} kayıt güncellendi`]
        .filter(Boolean)
        .join(' · ');
    const message = !runs
      ? 'Eşitleme arka planda sürüyor; tamamlanınca panel kendiliğinden güncellenecek.'
      : failed
        ? `Eşitleme ${num(failed)}/${num(runs.length)} veri türünde başarısız oldu · ${runs
            .filter(run => run.failed)
            .map(run => `${run.type}: ${run.error || 'Eşitleme tamamlanamadı.'}`)
            .join(' · ')}${counts ? ` · ${counts}` : ''}`
        : `Eşitleme tamamlandı · ${counts || `Google’da yeni veri yok.${await watchSync()}`}`;
    await refresh();
    notice(message, Boolean(failed), failed ? 'sync' : 'sync-result');
    if (!failed)
      setTimeout(() => {
        if ($('notice').textContent === message) clearSyncNotice(true);
      }, 20000);
  } catch (error) {
    notice(`Eşitleme başlatılamadı: ${error.message}`, true, 'sync-start');
  } finally {
    syncBusy(false);
  }
});
$('refresh-system').addEventListener('click', async () => {
  await refresh();
  await loadSystem();
});
$('timezone').addEventListener('change', async () => {
  zoneChoice = $('timezone').value;
  zone = resolveZone(zoneChoice);
  writePreference('healtos.timezone.v2', zoneChoice);
  await refresh();
  if (activePage === 'system') await loadSystem();
  if (activePage === 'sports') await workouts.update();
});
function updateMotion() {
  body.setMotion($('model-motion').checked);
}
$('model-motion').addEventListener('change', () => {
  writePreference('healtos.motion', String($('model-motion').checked));
  updateMotion();
});
matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', updateMotion);
updateMotion();
setupChat(() => zone);
setupMetricHistory(() => zone);
const calendar = setupCalendar({
  getToday: today,
  getSelected: () => selectedDate,
  onSelect: date => {
    selectedDate = date === today() ? null : date;
    refresh();
  },
});
const historyPanel = setupHistoryPanel({ request, getZone: () => zone, getToday: today, paint: paintHistory });
const workouts = setupWorkouts({
  request,
  getZone: () => zone,
  getToday: today,
  escape,
  num,
  duration,
  fullDate,
  timeOf,
});
await refresh();
poll = setInterval(async () => {
  if (!document.hidden && !$('measurement-dialog').open) {
    await refresh();
    if (activePage === 'system') await loadSystem();
    if (activePage === 'sports') await workouts.update();
  }
}, 60000);
window.addEventListener('pagehide', () => clearInterval(poll));
