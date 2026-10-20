import { formatAssistantMessage } from './chat-format.js';

export function setupChat(getTimezone) {
  const $ = (id) => document.getElementById(id);
  let socket = null, connected = false, busy = false;
  let waiting = false;
  const items = new Map();
  const rawText = new WeakMap();
  const launcher = $('assistant-open');
  const dialog = $('assistant-dialog');
  let drag = null, suppressClick = false, movedLauncher = false;
  let retryTimer = null, retryDelay = 1000, pageLeaving = false;
  // The socket opens only once Umay is first opened; automatic retries stop after MAX_RETRIES.
  const MAX_RETRIES = 5;
  let started = false, retries = 0;

  // The bubble's layout box: its hover/press scale must not shift the panel or the drag origin.
  function launcherBox() {
    const left = launcher.offsetLeft, top = launcher.offsetTop;
    return {left, top, right: left + launcher.offsetWidth, bottom: top + launcher.offsetHeight};
  }

  function placeDialog() {
    if (dialog.hidden) return;
    const edge = 8, gap = 12;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0, top = viewport?.offsetTop ?? 0;
    const width = viewport?.width ?? document.documentElement.clientWidth;
    const height = viewport?.height ?? window.innerHeight;
    const rect = launcherBox();
    const above = rect.top - top - edge - gap;
    const below = top + height - edge - rect.bottom - gap;
    const useAbove = above >= 300 || above >= below;
    const panelHeight = Math.min(width <= 540 ? 700 : 740, Math.max(0, useAbove ? above : below));
    const panelWidth = Math.min(560, width - edge * 2);
    dialog.style.width = `${panelWidth}px`;
    dialog.style.height = `${panelHeight}px`;
    dialog.style.left = `${Math.max(left + edge, Math.min(rect.right - panelWidth, left + width - edge - panelWidth))}px`;
    dialog.style.top = `${useAbove ? rect.top - gap - panelHeight : rect.bottom + gap}px`;
    dialog.style.right = 'auto';
    dialog.style.bottom = 'auto';
  }

  function placeLauncher(x, y) {
    const edge = 12;
    const viewport = window.visualViewport;
    const minX = (viewport?.offsetLeft ?? 0) + edge;
    const minY = (viewport?.offsetTop ?? 0) + edge;
    const maxX = Math.max(minX, minX + (viewport?.width ?? document.documentElement.clientWidth) - launcher.offsetWidth - edge * 2);
    const maxY = Math.max(minY, minY + (viewport?.height ?? window.innerHeight) - launcher.offsetHeight - edge * 2);
    launcher.style.left = `${Math.max(minX, Math.min(x, maxX))}px`;
    launcher.style.top = `${Math.max(minY, Math.min(y, maxY))}px`;
    launcher.style.right = 'auto';
    launcher.style.bottom = 'auto';
    placeDialog();
  }

  launcher.addEventListener('pointerdown', (event) => {
    if (!event.isPrimary || event.button !== 0) return;
    const rect = launcherBox();
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, left: rect.left, top: rect.top, moved: false};
    suppressClick = false;
    launcher.setPointerCapture(event.pointerId);
  });
  launcher.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 6) return;
    drag.moved = true;
    movedLauncher = true;
    launcher.classList.add('is-dragging');
    placeLauncher(drag.left + dx, drag.top + dy);
  });
  function finishDrag(event) {
    if (!drag || drag.id !== event.pointerId) return;
    suppressClick = event.type === 'pointerup' && drag.moved;
    drag = null;
    launcher.classList.remove('is-dragging');
    if (launcher.hasPointerCapture(event.pointerId)) launcher.releasePointerCapture(event.pointerId);
  }
  launcher.addEventListener('pointerup', finishDrag);
  launcher.addEventListener('pointercancel', finishDrag);
  launcher.addEventListener('lostpointercapture', finishDrag);
  function fitViewport() {
    if (movedLauncher || (!dialog.hidden && window.visualViewport?.height < window.innerHeight)) {
      movedLauncher = true;
      const rect = launcherBox();
      placeLauncher(rect.left, rect.top);
    } else placeDialog();
  }
  window.addEventListener('resize', fitViewport);
  window.visualViewport?.addEventListener('resize', fitViewport);
  window.visualViewport?.addEventListener('scroll', fitViewport);

  function state(text) {
    $('assistant-status').textContent = connected ? (busy ? 'Yazıyor' : 'Aktif') : text;
    $('assistant-status').dataset.presence = connected ? 'online' : 'offline';
    $('assistant-typing').hidden = !connected || !busy || !waiting;
    $('assistant-send').disabled = !connected || busy;
    $('assistant-input').disabled = !connected;
    $('assistant-stop').hidden = !busy;
    $('assistant-new').disabled = busy;
    $('assistant-messages').setAttribute('aria-busy', String(busy));
  }

  function error(text) {
    $('assistant-error').textContent = text;
    $('assistant-error').hidden = !text;
  }

  function append(role, text, id) {
    let content = id && items.get(id);
    if (!content) {
      const message = document.createElement('article');
      message.className = `chat-message chat-${role}`;
      const label = document.createElement('span');
      label.className = 'chat-sender';
      label.textContent = role === 'user' ? 'Sen' : 'Umay';
      content = document.createElement('div');
      content.className = 'chat-content';
      message.append(label, content);
      $('assistant-messages').insertBefore(message, $('assistant-typing'));
      if (id) items.set(id, content);
    }
    rawText.set(content, text);
    if (role === 'assistant') content.innerHTML = formatAssistantMessage(text);
    else content.textContent = text;
    $('assistant-messages').scrollTop = $('assistant-messages').scrollHeight;
    return content;
  }

  function connect({ automatic = false } = {}) {
    started = true;
    if (!automatic) {
      retries = 0;
      retryDelay = 1000;
    }
    clearTimeout(retryTimer);
    retryTimer = null;
    if (socket) {
      socket.onclose = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.close();
    }
    connected = false;
    busy = false;
    waiting = false;
    error('');
    $('assistant-reconnect').hidden = true;
    state('Bağlanıyor…');
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/codex/chat`);
    socket.onmessage = ({data}) => {
      const event = JSON.parse(data);
      if (event.type === 'ready') {
        connected = true;
        retries = 0;
        retryDelay = 1000;
        error('');
        state('');
        if (!dialog.hidden) $('assistant-input').focus();
      } else if (event.type === 'working') {
        busy = true;
        waiting = true;
        state(event.text);
        $('assistant-messages').scrollTop = $('assistant-messages').scrollHeight;
      } else if (event.type === 'delta') {
        waiting = false;
        $('assistant-typing').hidden = true;
        const text = (rawText.get(items.get(event.id)) ?? '') + event.text;
        append('assistant', text, event.id);
      } else if (event.type === 'message') {
        waiting = false;
        $('assistant-typing').hidden = true;
        append('assistant', event.text, event.id);
      } else if (event.type === 'done') {
        busy = false;
        state(event.status === 'interrupted' ? 'Yanıt durduruldu' : event.status === 'failed' ? 'Yanıt tamamlanamadı' : 'Yeni sorunu yazabilirsin');
      } else if (event.type === 'error') {
        error(event.text);
        busy = false;
        state(connected ? 'Yanıt tamamlanamadı' : 'Bağlantı kurulamadı');
      } else if (event.type === 'notice') {
        error(event.text);
      }
    };
    socket.onclose = () => {
      connected = false;
      busy = false;
      waiting = false;
      state('Çevrimdışı');
      $('assistant-reconnect').hidden = false;
      if (pageLeaving) return;
      if (retries >= MAX_RETRIES) {
        error('Umay şu an çevrimdışı. Biraz sonra yeniden bağlanmayı dene.');
        return;
      }
      retries += 1;
      if (!$('assistant-error').textContent) error('Bağlantı kesildi. Umay yeniden bağlanmayı deniyor…');
      retryTimer = setTimeout(() => connect({ automatic: true }), retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    };
    socket.onerror = () => error('Umay’a bağlanılamadı; yeniden denenecek.');
  }

  function reset() {
    document.querySelectorAll('.chat-message').forEach((message) => message.remove());
    items.clear();
    $('assistant-input').value = '';
    connect();
  }

  function send(text) {
    if (!connected || busy || !text.trim()) return;
    error('');
    append('user', text.trim());
    socket.send(JSON.stringify({text: text.trim(), timezone: getTimezone()}));
    $('assistant-input').value = '';
    busy = true;
    waiting = true;
    state('Umay düşünüyor…');
    $('assistant-messages').scrollTop = $('assistant-messages').scrollHeight;
  }

  launcher.addEventListener('click', (event) => {
    if (suppressClick && event.detail !== 0) {
      suppressClick = false;
      return;
    }
    $('assistant-dialog').hidden = false;
    placeDialog();
    $('assistant-open').setAttribute('aria-expanded', 'true');
    if (connected) $('assistant-input').focus();
    else if (!socket || socket.readyState >= WebSocket.CLOSING) connect();
  });
  $('assistant-close').addEventListener('click', () => {
    $('assistant-dialog').hidden = true;
    $('assistant-open').setAttribute('aria-expanded', 'false');
    $('assistant-open').focus();
  });
  $('assistant-dialog').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') $('assistant-close').click();
  });
  $('assistant-form').addEventListener('submit', (event) => {
    event.preventDefault();
    send($('assistant-input').value);
  });
  $('assistant-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send($('assistant-input').value);
    }
  });
  $('assistant-stop').addEventListener('click', () => {
    if (connected && busy) socket.send(JSON.stringify({type: 'cancel'}));
  });
  $('assistant-new').addEventListener('click', reset);
  $('assistant-reconnect').addEventListener('click', () => connect());
  window.addEventListener('online', () => { if (started && !connected) connect(); });
  document.addEventListener('visibilitychange', () => {
    // Coming back to the tab skips the wait of a pending retry; it does not restart a stopped one.
    if (!document.hidden && started && !connected && retryTimer) connect({ automatic: true });
  });
  window.addEventListener('pagehide', () => {
    pageLeaving = true;
    clearTimeout(retryTimer);
    socket?.close();
  });
  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    pageLeaving = false;
    if (started) connect();
  });
}
