// Shared fetch helpers for the app's own endpoints.

const TIMEOUT_MESSAGE = 'İstek zaman aşımına uğradı. Biraz sonra tekrar dene.';
const UNREACHABLE_MESSAGE = 'Sunucuya ulaşılamadı. Bağlantını kontrol edip tekrar dene.';

/** Sends the browser to the login page and brings it back to the current page afterwards. */
let redirecting = false;
export function redirectToLogin() {
  if (redirecting) return;
  redirecting = true;
  location.assign(`/login?${new URLSearchParams({ next: `${location.pathname}${location.search}` })}`);
}

/** Returns true (and redirects) when the server asks for a login. */
export function handleUnauthorized(response) {
  if (response.status !== 401) return false;
  redirectToLogin();
  return true;
}

/** Turkish message for a fetch that failed before a usable response arrived. */
export function failureMessage(error) {
  return error?.name === 'TimeoutError' || error?.name === 'AbortError' ? TIMEOUT_MESSAGE : UNREACHABLE_MESSAGE;
}

export async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, cache: 'no-store', signal: AbortSignal.timeout(20000) });
  } catch (error) {
    throw new Error(failureMessage(error));
  }
  if (handleUnauthorized(response)) throw new Error('Oturumun sona erdi. Giriş sayfasına yönlendiriliyorsun.');
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    // A proxy error page (for example an HTML 502) or a body cut off by the timeout.
    if (error?.name === 'TimeoutError' || error?.name === 'AbortError') throw new Error(TIMEOUT_MESSAGE);
    throw new Error(response.ok ? 'Sunucudan beklenmeyen bir yanıt geldi. Tekrar dene.' : UNREACHABLE_MESSAGE);
  }
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map(x => x.msg).join(' ')
          : 'İşlem tamamlanamadı.',
    );
  }
  return payload;
}
