const metrics = {
  steps: {title: 'Adım geçmişi', average: 'Günlük ortalama', color: 'var(--step-chart)'},
  sleep: {title: 'Uyku geçmişi', average: 'Ortalama uyku', color: 'var(--sleep-light)'},
  heart: {title: 'Dinlenik nabız geçmişi', average: 'Ortalama dinlenik nabız', color: 'var(--heart-chart)'},
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

function chart(metric, days) {
  const values = days.filter(day => day.value != null).map(day => day.value);
  if (!values.length) return '<p class="history-empty">Bu dönemde kayıt yok.</p>';
  const high = Math.max(...values, 1) * 1.12;
  const low = metric === 'heart' ? Math.max(0, Math.min(...values) - 8) : 0;
  const max = metric === 'heart' ? Math.max(...values) + 8 : high;
  const x = (i) => 58 + i * 918 / (days.length - 1 || 1);
  const y = (v) => 188 - (v - low) / (max - low) * 160;
  const title = (day) => `${date(day.date, true)}: ${valueLabel(metric, day.value)}${day.partial ? ' · Gün devam ediyor' : ''}`;
  let marks = '';
  if (metric === 'heart') {
    let points = [];
    const flush = () => {
      if (points.length > 1) marks += `<polyline points="${points.join(' ')}" fill="none" stroke="${metrics[metric].color}" stroke-width="3"/>`;
      points = [];
    };
    days.forEach((day, i) => {
      if (day.value == null) { flush(); return; }
      points.push(`${x(i)},${y(day.value)}`);
      marks += `<circle cx="${x(i)}" cy="${y(day.value)}" r="4" fill="${metrics[metric].color}"><title>${title(day)}</title></circle>`;
    });
    flush();
  } else {
    days.forEach((day, i) => {
      if (day.value == null) return;
      marks += `<rect x="${x(i) - 10}" y="${y(day.value)}" width="20" height="${Math.max(1, 188 - y(day.value))}" rx="3" fill="${metrics[metric].color}" opacity="${day.partial ? '.55' : '1'}"><title>${title(day)}</title></rect>`;
    });
  }
  const axis = [low, (max + low) / 2, max].map(v => `<path d="M48 ${y(v)}H990" stroke="var(--line)"/><text x="38" y="${y(v) + 4}" text-anchor="end">${metric === 'sleep' ? `${number(v / 3600, 1)} sa` : number(v)}</text>`).join('');
  const labels = [0, 6, 12, 18, 24, days.length - 1].filter((i, at, all) => i < days.length && all.indexOf(i) === at).map(i => `<text x="${x(i)}" y="218" text-anchor="middle">${date(days[i].date)}</text>`).join('');
  return `<svg viewBox="0 0 1020 236" preserveAspectRatio="none" aria-hidden="true">${axis}${marks}${labels}</svg>`;
}

export function setupMetricHistory(getTimezone) {
  const $ = id => document.getElementById(id);
  const dialog = $('metric-history-dialog');
  let currentMetric = 'steps', currentPage = 1, totalPages = 1, controller = null, opener = null;

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
      plot.innerHTML = chart(metric, days);
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

  document.querySelectorAll('[data-history-metric]').forEach(card => {
    function open() {
      opener = card;
      if (!dialog.open) dialog.showModal();
      document.body.classList.add('metric-history-open');
      load(card.dataset.historyMetric);
    }
    card.addEventListener('click', open);
    card.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
    });
  });
  document.querySelectorAll('[data-history-tab]').forEach(el => el.addEventListener('click', () => load(el.dataset.historyTab)));
  $('metric-history-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    controller?.abort(); controller = null;
    document.body.classList.remove('metric-history-open'); opener?.focus();
  });
}
