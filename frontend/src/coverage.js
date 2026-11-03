import './styles/coverage.css';
import { setupDialog, focusVisible } from './accessibility.js';

const labels = {
  completed_with_records: 'Kayıtla tamamlandı',
  completed_empty: 'Google boş döndü',
  incomplete: 'Çekim yarım',
  not_attempted: 'Hiç çekilmedi',
  unknown: 'Çekimin tamamlandığı bilinmiyor',
};
const reasons = {
  no_time_filter: 'Bu türün zaman filtresi yok; günlük kanıt üretilemez.',
  returned_dates_unknown: 'Dönen bazı kayıtların günü belirlenemiyor; boş dönüş sayılamaz.',
  civil_start_offset_envelope: 'Filtre yerel başlangıç saatine göre; fiziksel gün için saat dilimi farkı kadar pay bırakılır.',
  lower_bound_filter: 'Google filtresinin yalnız başlangıç sınırı var.',
  open_day: 'Gün henüz kapanmadı; tam gün sayılmaz.',
};
const shift = (day, days) => {
  const value = new Date(`${day}T12:00:00Z`);
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
};

export function setupCoverage({ request, getZone, getToday, escape }) {
  const $ = id => document.getElementById(id);
  const dialog = $('coverage-dialog');
  const modal = setupDialog(dialog);
  let sequence = 0;
  let initialized = false;
  let loadingKey = null;
  let knownTypes = [];

  function initialize() {
    if (initialized) return;
    initialized = true;
    $('coverage-end').value = shift(getToday(), -1);
    $('coverage-start').value = shift($('coverage-end').value, -6);
  }

  function render(types) {
    $('coverage-results').innerHTML = types.map(type => {
      const sources = type.source_regimes.map(source =>
        `<li>${escape(source.source)} · ${source.value_days} değeri olan gün · ${source.record_count} kayıt</li>`
      ).join('');
      const exclusions = type.exclusions
        ? Object.entries(type.exclusions).filter(([, value]) => value > 0)
          .map(([key, value]) => `${escape({ quarantined: 'karantina', without_raw_record: 'ham kaydı yok',
            source_deleted: 'kaynakta silinmiş', superseded: 'başka kayıt temsil ediyor',
            invalid_shape: 'değer biçimi geçersiz' }[key] ?? key)}: ${value}`).join(' · ')
        : '';
      return `<section class="coverage-type"><h3>${escape(type.data_type)}</h3>
        <p class="fine-print">${type.presence_basis === 'eligible_record'
          ? 'Değer varlığı: dışlanmamış kayıt. Ölçüm sıklığı gösterilmez.'
          : 'Değer varlığı mevcut ölçüm kurallarıyla hesaplanır.'}</p>
        ${sources ? `<ul class="coverage-sources" aria-label="Ayrı kaynak akışları">${sources}</ul>` : ''}
        ${exclusions ? `<p class="fine-print">Dışlanan kayıtlar · ${exclusions}</p>` : ''}
        <ul class="coverage-days">${type.days.map(day => `<li>
          <time datetime="${escape(day.date)}">${escape(day.date)}</time>
          <span>${day.has_value == null ? 'Günlük değer belirlenemiyor' : day.has_value ? 'Değer var' : 'Değer yok'}</span>
          <span>${escape(labels[day.retrieval] ?? labels.unknown)}${day.returned_records != null
            ? ` · ${day.returned_records} dönen kayıt` : ''}</span>
          ${day.reason ? `<small>${escape(reasons[day.reason] ?? day.reason)}</small>` : ''}
        </li>`).join('')}</ul></section>`;
    }).join('');
  }

  async function refresh() {
    initialize();
    const start = $('coverage-start').value;
    const end = $('coverage-end').value;
    const type = $('coverage-type').value;
    const zone = getZone();
    const query = new URLSearchParams({ start_date: start, end_date: end, timezone: zone, limit: 4 });
    if (type) query.set('data_type', type);
    const key = query.toString();
    if (loadingKey === key) return;
    loadingKey = key;
    const current = ++sequence;
    $('coverage-period').textContent = `${start} – ${end} · ${zone}`;
    $('coverage-summary').textContent = 'Kapsama yükleniyor…';
    $('coverage-status').textContent = 'Kapsama yükleniyor…';
    $('coverage-results').replaceChildren();
    if (document.activeElement === $('coverage-retry')) focusVisible($('coverage-form').querySelector('button'));
    $('coverage-retry').hidden = true;
    $('coverage-results').setAttribute('aria-busy', 'true');
    const all = [];
    const totals = { days_without_value: 0, unknown_days: 0, incomplete_days: 0, not_attempted_days: 0 };
    try {
      let offset = 0;
      do {
        query.set('offset', offset);
        const result = await request(`/api/sync/coverage?${query}`);
        if (current !== sequence) return;
        knownTypes = result.supported_data_types;
        all.push(...result.types);
        for (const key of Object.keys(totals)) totals[key] += result.summary[key] ?? 0;
        offset = result.next_offset;
        $('coverage-status').textContent = `${all.length} veri türü alındı…`;
      } while (offset != null);
      const selection = $('coverage-type').value;
      $('coverage-type').replaceChildren(new Option('Tüm türler', ''),
        ...knownTypes.map(name => new Option(name, name)));
      $('coverage-type').value = selection;
      // The report covers only the selected type (steps by default); say which.
      const summary = `${type || 'Tüm türler'}: ${totals.days_without_value} değeri olmayan · ${totals.unknown_days} bilinmeyen · ${totals.incomplete_days} yarım · ${totals.not_attempted_days} hiç çekilmemiş tür/gün`;
      $('coverage-summary').textContent = summary;
      $('coverage-status').textContent = `${start} – ${end} · ${zone} · ${summary}`;
      render(all);
    } catch (error) {
      if (current !== sequence) return;
      $('coverage-summary').textContent = 'Kapsama alınamadı.';
      $('coverage-status').textContent = `Kapsama alınamadı. ${error.message} Dönemi kısaltmayı veya tek veri türü seçmeyi dene (en çok 31 gün).`;
      $('coverage-retry').hidden = false;
    } finally {
      if (current === sequence) {
        loadingKey = null;
        $('coverage-results').setAttribute('aria-busy', 'false');
      }
    }
  }
  $('coverage-open').addEventListener('click', () => modal.open({ initial: $('coverage-start') }));
  $('coverage-close').addEventListener('click', () => dialog.close());
  $('coverage-retry').addEventListener('click', refresh);
  $('coverage-form').addEventListener('submit', event => { event.preventDefault(); refresh(); });
  return { refresh };
}
