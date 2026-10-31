const states = {active: 'Sürüyor', completed: 'Tamamlandı', interrupted: 'Durduruldu',
  failed: 'Hata ile sonlandı', disconnected: 'Bağlantı kesildi', timed_out: 'Süre doldu'};
const actions = {inventory: 'Arşiv envanteri', daily: 'Günlük veri', records: 'Ham kayıt sayfası',
  coverage: 'Kapsama üstverisi', baseline: 'Kişisel taban', analyze: 'Kanıtlı yorum adayları',
  sleep: 'Uyku kanıtı', intraday: 'Gün içi dağılım', sessions: 'Antrenman oturumları',
  physiology: 'Fizyolojik ölçümler', longitudinal: 'Boylamsal ölçümler', relationship: 'Takvimli ilişki', invalid: 'Geçersiz sorgu'};
const counts = {raw_records: 'Ham kayıt', daily_summaries: 'Günlük özet',
  profile_measurements: 'Profil ölçümü', chart_points: 'Grafik noktası',
  derived_summaries: 'Türetilmiş özet', derived_observations: 'Türetilmiş gözlem',
  metadata_items: 'Üstveri satırı'};

function node(tag, text, parent, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function range(value) {
  return value ? `${value.first} – ${value.last}` : 'Yok';
}

function countText(values) {
  return Object.entries(counts).map(([key, label]) => `${label}: ${values[key] ?? 0}`).join(' · ');
}

// Kept beside this turn's question and answers; never stored or reused by a new socket.
export function createTransfer(container) {
  let receipt = null, details = null, title = null, body = null;
  let totals = null, notice = null, failure = null, shownCalls = 0;
  function update(event) {
    if (receipt && receipt.turn !== event.turn) return;
    receipt = event;
    if (!details) {
      details = node('details', '', container, 'chat-transfer');
      title = node('summary', '', details);
      body = node('div', '', details, 'chat-transfer-body');
      body.tabIndex = 0;
      body.setAttribute('role', 'region');
      body.setAttribute('aria-label', 'Bu yanıtın sağlık verisi aktarımı');
      totals = node('p', '', body);
      notice = node('p', '', body);
      failure = node('p', '', body);
      node('p', 'Alan adları: [] liste öğesi, * gizlenen dinamik anahtar, ** daha derin alanlar. Boş alanlar da listelenir.', body);
    }
    title.textContent = `Sağlık verisi aktarımı · ${states[event.status] ?? states.failed}`;
    totals.textContent = countText(event.counts);
    const healthSent = Object.keys(counts).some(key => key !== 'metadata_items' && event.counts[key] > 0);
    notice.textContent = healthSent ? 'Sayılar gönderilen gösterim adedidir; tekrarlar olabilir. Benzersiz kaynak kayıt sayısı bilinmiyor.' :
      event.calls.length ? 'Bu turda sağlık kaydı veya ölçüm değeri gönderilmedi; yalnız üstveri gönderildi.' :
        'Bu turda yeni sağlık verisi gönderilmedi.';
    failure.hidden = !event.failed_sends;
    failure.textContent = `${event.failed_sends} sonuç için köprüye gönderim tamamlanamadı; sayılara katılmadı.`;
    event.calls.slice(shownCalls).forEach((call, offset) => {
      const index = shownCalls + offset;
      node('h3', `${index + 1}. ${actions[call.action] ?? actions.invalid}${call.outcome === 'refused' ? ' · Reddedildi' : ''}`, body);
      node('p', countText(call.counts), body);
      node('p', `İstenen pencere: ${range(call.requested)} · Saat dilimi: ${call.timezone}`, body);
      node('p', `Gönderilen veride bulunan tarihler: ${range(call.data_dates)}`, body);
      if (call.profile_dates) node('p', `Profil ölçüm tarihleri (pencere öncesi olabilir): ${range(call.profile_dates)}`, body);
      if (call.metadata_dates) node('p', `Üstverinin anlattığı tarihler: ${range(call.metadata_dates)}`, body);
      if (call.source_timezones.length) node('p', `Kaynak saat dilimleri: ${call.source_timezones.join(', ')}`, body);
      const fields = node('details', '', body, 'chat-transfer-fields');
      node('summary', `Gönderilen alanlar (${call.fields.length})`, fields);
      const list = node('ul', '', fields);
      list.tabIndex = 0;
      list.setAttribute('role', 'region');
      list.setAttribute('aria-label', `${index + 1}. sorguda gönderilen alanlar`);
      call.fields.forEach(field => node('li', field, list));
    });
    shownCalls = event.calls.length;
  }
  return {
    update,
    disconnect() {
      if (receipt?.status === 'active') update({...receipt, status: 'disconnected'});
    },
  };
}
