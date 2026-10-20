// Turkish wording for the short error codes in /sync/status `last_error`
// (e.g. "GoogleHealthError (http_429)", "TimeoutException", "process restarted").

const FALLBACK = 'Eşitleme tamamlanamadı.';
// "ErrorClass" or "ErrorClass (short_code)".
const CODE = /^[A-Za-z_][\w.]*(?: \([\w.:-]+\))?$/;

export function describeSyncError(code) {
  const raw = String(code ?? '').trim();
  if (!raw) return FALLBACK;
  const status = Number(/\bhttp_(\d{3})\b/i.exec(raw)?.[1]);
  if (status === 401 || status === 403) return 'Google erişim izni reddedildi; hesabını yeniden bağla.';
  if (status === 429) return 'Google istek sınırına ulaşıldı; biraz sonra yeniden denenecek.';
  if (status >= 500 && status <= 599) return 'Google tarafında geçici bir sorun var.';
  if (/timeout/i.test(raw)) return 'Google’a ulaşılamadı (zaman aşımı).';
  if (/^process restarted$/i.test(raw)) return 'Eşitleme yarıda kaldı; uygulama yeniden başlatıldı.';
  // Older rows may still hold a readable sentence instead of a code; show those unchanged.
  if (!CODE.test(raw)) return raw;
  // Unknown code: keep it visible so it can still be looked up.
  return `Eşitleme tamamlanamadı (${raw}).`;
}
