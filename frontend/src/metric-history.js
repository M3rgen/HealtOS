import { handleUnauthorized } from './api.js';

const metrics = {
  steps: {title: 'Adım geçmişi', average: 'Günlük ortalama', color: 'var(--step-chart)'},
  sleep: {title: 'Uyku geçmişi', average: 'Ortalama uyku', color: 'var(--sleep-light)'},
  heart: {title: 'Dinlenme nabzı geçmişi', average: 'Ortalama dinlenme nabzı', color: 'var(--heart-chart)'},
};
const number = (value, digits = 0) => Number(value).toLocaleString('tr-TR', {maximumFractionDigits: digits});
const date = (value, long = false) => new Intl.DateTimeFormat('tr-TR', {
  timeZone: 'UTC', day: 'numeric', month: long ? 'long' : 'short', ...(long ? {year: 'numeric'} : {}),
}).format(new Date(`${value}T12:00:00Z`));
function valueLabel(metric, value) {
  if (value == null) return '—';
  if (metric === 'sleep') {
    const minutes = Math.round(value / 60);
    return `${Math.floor(minutes / 60)} sa ${minutes % 60} dk`;
  }
  return `${number(value, metric === 'heart' ? 1 : 0)} ${metric === 'heart' ? 'bpm' : 'adım'}`;
}

// Drawn at the plot's real pixel size so text and strokes are never stretched.
function chart(metric, days, width = 1020, height = 236) {
  const values = days.filter(day => day.value != null).map(day => day.value);
  if (!values.length) return '<p class="history-empty">Bu dönemde kayıt yok.</p>';
  const W = Math.max(280, Math.round(width)), H = Math.max(140, Math.round(height));
  const compact = W < 560;
  const high = Math.max(...values, 1) * 1.12;
  const low = metric === 'heart' ? Math.max(0, Math.min(...values) - 8) : 0;
  const max = metric === 'heart' ? Math.max(...values) + 8 : high;
  const tick = (v) => metric === 'sleep' ? `${number(v / 3600, 1)} sa` : number(v);
  const left = Math.max(36, Math.max(...[low, max].map(v => tick(v).length)) * 6.6 + 16), right = W - 8, top = 12, bottom = H - 28;
  const step = (right - left - 16) / (days.length - 1 || 1);
  const x = (i) => left + 8 + i * step;
  const y = (v) => bottom - (v - low) / (max - low) * (bottom - top);
  const color = metrics[metric].color;
  const title = (day) => `${date(day.date, true)}: ${valueLabel(metric, day.value)}${day.partial ? ' · Gün devam ediyor' : ''}`;
  const defs = `<defs><linearGradient id="history-fill" gradientUnits="userSpaceOnUse" x1="0" y1="${top}" x2="0" y2="${bottom}"><stop offset="0" style="stop-color:${color}"/><stop offset="1" style="stop-color:${color};stop-opacity:${metric === 'heart' ? 0 : .42}"/></linearGradient></defs>`;
  let marks = '';
  if (metric === 'heart') {
    let points = [];
    const smooth = (p) => p.map((pt, i) => {
      if (!i) return `M${pt[0]},${pt[1]}`;
      const a = p[i - 2] || p[i - 1], b = p[i - 1], d = p[i + 1] || pt;
      return `C${b[0] + (pt[0] - a[0]) / 6},${b[1] + (pt[1] - a[1]) / 6} ${pt[0] - (d[0] - b[0]) / 6},${pt[1] - (d[1] - b[1]) / 6} ${pt[0]},${pt[1]}`;
    }).join('');
    const flush = () => {
      if (points.length > 1) marks = `<path d="${smooth(points)}V${bottom}H${points[0][0]}Z" fill="url(#history-fill)" opacity=".5"/><path d="${smooth(points)}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` + marks;
      points = [];
    };
    days.forEach((day, i) => {
      if (day.value == null) { flush(); return; }
      points.push([x(i), y(day.value)]);
      marks += `<circle cx="${x(i)}" cy="${y(day.value)}" r="${compact ? 3 : 3.5}" fill="var(--surface-2)" stroke="${color}" stroke-width="2"${day.partial ? ' stroke-dasharray="2 2"' : ''}><title>${title(day)}</title></circle>`;
    });
    flush();
  } else {
    const w = Math.max(3, Math.min(22, step * .62)), r = Math.min(4, w / 2);
    days.forEach((day, i) => {
      if (day.value == null) return;
      const top = y(day.value), h = Math.max(1, bottom - top), bx = x(i) - w / 2, rr = Math.min(r, h);
      marks += `<path d="M${bx},${bottom}V${top + rr}Q${bx},${top} ${bx + rr},${top}H${bx + w - rr}Q${bx + w},${top} ${bx + w},${top + rr}V${bottom}Z" fill="url(#history-fill)" opacity="${day.partial ? '.5' : '1'}"><title>${title(day)}</title></path>`;
    });
  }
  const axis = [low, (max + low) / 2, max].map((v, i) => `<path d="M${left} ${y(v)}H${right}" stroke="var(--line)" ${i ? 'stroke-dasharray="2 4" opacity=".8"' : ''}/><text x="${left - 10}" y="${y(v) + 4}" text-anchor="end">${tick(v)}</text>`).join('');
  const ticks = compact ? [0, 10, 20, days.length - 1] : [0, 6, 12, 18, 24, days.length - 1];
  const labels = ticks.filter((i, at, all) => i < days.length && all.indexOf(i) === at).map(i => `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${date(days[i].date)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">${defs}${axis}${marks}${labels}</svg>`;
}

export function setupMetricHistory(getTimezone) {
  const $ = id => document.getElementById(id);
  const dialog = $('metric-history-dialog');
  let currentMetric = 'steps', currentPage = 1, totalPages = 1, controller = null, opener = null, plotted = null;
  // Redraw at the new pixel size when the dialog or viewport resizes.
  new ResizeObserver(() => {
    const plot = $('metric-history-chart');
    if (plotted && dialog.open && plot.clientWidth) plot.innerHTML = chart(plotted.metric, plotted.days, plot.clientWidth, plot.clientHeight);
  }).observe($('metric-history-chart'));

  function pagination() {
    const nav = $('metric-history-pages');
    nav.replaceChildren();
    const pages = [...new Set([1, totalPages, currentPage - 2, currentPage - 1, currentPage, currentPage + 1, currentPage + 2])]
      .filter(page => page >= 1 && page <= totalPages).sort((a, b) => a - b);
    const button = (label, page, disabled = false) => {
      const el = document.createElement('button');
      el.type = 'button'; el.textContent = label; el.disabled = disabled;
      el.setAttribute('aria-label', label === String(page) ? `${page}. sayfa` : label);
      if (page === currentPage && label === String(page)) el.setAttribute('aria-current', 'page');
      el.addEventListener('click', () => load(currentMetric, page));
      nav.append(el);
    };
    button('Önceki', currentPage - 1, currentPage === 1);
    pages.forEach((page, i) => {
      if (i && page - pages[i - 1] > 1) { const gap = document.createElement('span'); gap.textContent = '…'; nav.append(gap); }
      button(String(page), page);
    });
    button('Sonraki', currentPage + 1, currentPage === totalPages);
    $('metric-history-page-note').textContent = `${currentPage} / ${totalPages} · Her sayfa 30 gün`;
  }

  async function load(metric, page = 1) {
    controller?.abort();
    const pending = new AbortController(); controller = pending;
    currentMetric = metric; currentPage = page;
    $('metric-history-title').textContent = metrics[metric].title;
    dialog.style.setProperty('--metric-accent', metrics[metric].color);
    document.querySelectorAll('[data-history-tab]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.historyTab === metric)));
    $('metric-history-content').hidden = true;
    $('metric-history-status').hidden = false;
    $('metric-history-status').textContent = 'Geçmiş yükleniyor…';
    $('metric-history-pages').replaceChildren();
    $('metric-history-page-note').textContent = '';
    $('metric-history-range').textContent = '';
    try {
      const query = new URLSearchParams({metric, page, timezone: getTimezone()});
      const response = await fetch(`/api/dashboard/history?${query}`, {cache: 'no-store', signal: AbortSignal.any([pending.signal, AbortSignal.timeout(20000)])});
      if (handleUnauthorized(response)) return;
      if (!response.ok) throw new Error('Geçmiş alınamadı. Tekrar dene.');
      const result = await response.json();
      if (controller !== pending || !dialog.open) return;
      const days = [...result.days].sort((a, b) => a.date.localeCompare(b.date));
      const present = days.filter(day => day.value != null);
      totalPages = result.total_pages; currentPage = result.page;
      $('metric-history-range').textContent = `${date(result.start_date, true)} – ${date(result.end_date, true)}`;
      $('metric-history-average-label').textContent = metrics[metric].average;
      $('metric-history-average').textContent = valueLabel(metric, present.length ? present.reduce((sum, day) => sum + day.value, 0) / present.length : null);
      $('metric-history-coverage').textContent = `${present.length} / ${days.length} gün kayıtlı`;
      $('metric-history-archive').textContent = result.earliest_date ? `Arşiv başlangıcı: ${date(result.earliest_date, true)}` : 'Arşivde henüz kayıt yok';
      const plot = $('metric-history-chart');
      plotted = {metric, days};
      $('metric-history-content').hidden = false;
      plot.innerHTML = chart(metric, days, plot.clientWidth, plot.clientHeight);
      plot.setAttribute('aria-label', `${metrics[metric].title}, ${$('metric-history-range').textContent}`);
      const grid = $('metric-history-days'); grid.replaceChildren();
      days.forEach(day => {
        const item = document.createElement('li');
        if (day.value == null) item.className = 'missing-day';
        if (day.partial) item.classList.add('partial-day');
        const label = document.createElement('span'); label.textContent = date(day.date);
        const value = document.createElement('strong'); value.textContent = valueLabel(metric, day.value);
        item.title = `${date(day.date, true)}${day.partial ? ' · Gün devam ediyor' : ''}${day.value == null ? ' · Kayıt yok' : ''}`;
        item.append(label, value); grid.append(item);
      });
      $('metric-history-content').hidden = false;
      $('metric-history-status').hidden = true;
      pagination();
    } catch (error) {
      if (controller !== pending || !dialog.open || pending.signal.aborted) return;
      $('metric-history-status').textContent = error.message === 'Geçmiş alınamadı. Tekrar dene.' ? error.message : 'Geçmiş yüklenemedi. Tekrar dene.';
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'button outline'; retry.textContent = 'Tekrar dene';
      retry.addEventListener('click', () => load(metric, page)); $('metric-history-status').append(' ', retry);
    }
  }

  document.querySelectorAll('[data-history-metric]').forEach(button => {
    button.addEventListener('click', () => {
      opener = button;
      if (!dialog.open) dialog.showModal();
      document.body.classList.add('metric-history-open');
      load(button.dataset.historyMetric);
    });
  });
  document.querySelectorAll('[data-history-tab]').forEach(el => el.addEventListener('click', () => load(el.dataset.historyTab)));
  $('metric-history-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    controller?.abort(); controller = null;
    document.body.classList.remove('metric-history-open'); opener?.focus();
  });
}
