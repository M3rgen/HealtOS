import './styles/webhook-status.css';
import { setupDialog } from './accessibility.js';

const reasons = {
  authorization_rejected: 'Yetkilendirme reddi', receiver_unconfigured: 'Alıcı yapılandırılmamış',
  missing_signature: 'İmza eksik', malformed_signature: 'İmza biçimi bozuk',
  invalid_signature: 'İmza geçersiz', key_download_failed: 'İmza anahtarları alınamadı',
  signature_valid: 'İmza geçerli', challenge: 'Alıcı doğrulama isteği (challenge)',
  invalid_json: 'JSON geçersiz', body_rejected: 'İstek boyutu veya uzunluğu reddedildi',
  notification_received: 'Doğrulanmış teslim', duplicate: 'Yinelenen teslim',
  expired_replay: '24 saatten eski tekrar teslimi', ingestion_failed: 'Arşive alma hatası',
  notification_rejected: 'Bildirim içeriği reddedildi', processing_failed: 'İşleme hatası',
  batch_processing_failed: 'Toplu bildirim işleme hatası',
  processing_interrupted: 'Yarım kalan işleme kurtarıldı',
};

export function setupWebhookStatus({ request, getZone }) {
  const $ = id => document.getElementById(id);
  const dialog = $('webhook-status-dialog');
  const modal = setupDialog(dialog);
  let loading = false;
  const date = value => value ? new Date(value).toLocaleString('tr-TR', { timeZone: getZone() }) : 'Yok';

  function message(text) {
    $('webhook-status-summary').textContent = text;
    $('webhook-status-message').textContent = text;
  }

  function row(label, value) {
    const group = document.createElement('div');
    const term = document.createElement('dt');
    const description = document.createElement('dd');
    term.textContent = label;
    description.textContent = value;
    group.append(term, description);
    $('webhook-status-details').append(group);
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    message('Bildirim durumu yükleniyor…');
    $('webhook-status-summary').classList.remove('warning');
    $('webhook-status-details').replaceChildren();
    $('webhook-status-details').setAttribute('aria-busy', 'true');
    $('webhook-status-retry').setAttribute('aria-disabled', 'true');
    try {
      const status = await request('/api/webhooks/status');
      const silence = status.silence;
      const summaries = {
        unconfigured: 'Bildirim alıcısı yapılandırılmamış.',
        never_received: 'Henüz doğrulanmış bildirim alınmadı; izleme sürüyor.',
        receiving: `Son doğrulanmış bildirim: ${date(status.last_notification_at)}.`,
        silent: silence.never_received
          ? 'Bildirim gelmiyor: izleme başladığından beri en az 7 gün geçti; henüz bildirim alınmadı.'
          : 'Bildirim gelmiyor: en az 7 gündür doğrulanmış teslim yok.',
      };
      message(summaries[silence.state] ?? 'Bildirim durumu bilinmiyor.');
      $('webhook-status-summary').classList.toggle('warning', silence.warning);
      row('Son doğrulanmış bildirim', date(status.last_notification_at));
      row('Sessizlik başlangıcı', date(silence.since));
      row('Uyarı eşiği', `${silence.threshold_seconds / 86400} gün`);
      row('Abonelik / yenileme', 'Bilinmiyor. Abonelik istemcisi veya yenileme kanıtı yok; yapılandırma başarı kanıtı değildir.');
      row('Sayaç kapsamı', `Kalıcı tanılama olayları; başlangıç: ${date(status.counters.since)}. Yeniden başlatmada korunur. Eski arşivden sayaç üretilmez; veritabanı kesintisinde gözlemler eksik kalabilir.`);
      row('Sayaçların anlamı', 'Bir isteğin birden fazla olayı olabilir. Geçerli imza işleme başarısı değildir; eski tekrar tesliminin imzası geçerli olabilir.');
      row('Geçerli / geçersiz / süresi dolmuş', `${status.counters.valid} / ${status.counters.invalid} / ${status.counters.expired}`);
      row('Süresi dolmuş kategorisi', 'İmzada süre sonu alanı yok. Yerel 24 saatlik tekrar teslimi kuralı, ilk doğrulaması 24 saatten eski arşivlenmiş gövdenin yeniden işlenmesini engeller.');
      for (const [code, label] of Object.entries(reasons)) row(label, status.counters.reasons[code] ?? 0);
      const error = status.last_error;
      row('Son hata', error
        ? `${reasons[error.code] ?? 'Bilinmeyen hata'} · ${error.stage === 'processing' ? 'İşleme' : 'Doğrulama'} · ${date(error.at)}`
        : 'Kayıtlı hata yok.');
      row('Teslim ve veri güncellemesi', 'Bu zaman, geçerli imza ve en az bir geçerli bildirim öğesiyle alınan teslimi gösterir. Son veri kaydı ve eşitleme zamanı ayrı bilgilerdir. Challenge, geçersiz trafik ve eski tekrar teslimi sessizlik süresini sıfırlamaz.');
    } catch {
      message('Bildirim durumu alınamadı. Yeniden dene.');
    } finally {
      loading = false;
      $('webhook-status-details').setAttribute('aria-busy', 'false');
      $('webhook-status-retry').removeAttribute('aria-disabled');
    }
  }
  $('webhook-status-open').addEventListener('click', () => modal.open());
  $('webhook-status-close').addEventListener('click', () => dialog.close());
  $('webhook-status-retry').addEventListener('click', refresh);
  return { refresh };
}
