import { enhanceMonthInput } from './calendar.js';

export function setupHistoryPanel({request, getZone, getToday, paint}) {
  const $ = id => document.getElementById(id);
  const panel = $('history-panel'), toggle = $('history-toggle'), month = $('history-month');
  const placeholder = document.createElement('div');
  placeholder.hidden = true;
  panel.before(placeholder);
  let expanded = false, weekly, version = 0, pointer, dragged = false, animation, sheet;
  month.value = month.max = getToday().slice(0, 7);
  enhanceMonthInput(month);

  async function update(data = weekly) {
    weekly = data;
    const token = ++version;
    month.max = getToday().slice(0, 7);
    if (!expanded) { $('history-status').hidden = true; if (weekly) paint(weekly); return; }
    $('history-status').hidden = false;
    $('history-status').textContent = 'Aylık veriler yükleniyor…';
    $('history-rows').replaceChildren();
    $('export-button').hidden = true;
    const selected = month.value;
    $('history-title').textContent = 'Aylık görünüm';
    $('range-caption').textContent = '';
    try {
      if (!/^\d{4}-\d{2}$/.test(selected) || selected > month.max) throw new Error('Geçerli bir ay seç.');
      $('history-title').textContent = new Intl.DateTimeFormat('tr-TR', {month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(`${selected}-01T12:00:00Z`));
      const [year, number] = selected.split('-').map(Number);
      const last = `${selected}-${String(new Date(Date.UTC(year, number, 0)).getUTCDate()).padStart(2,'0')}`;
      const end = last > getToday() ? getToday() : last;
      const days = Number(end.slice(-2));
      const result = await request(`/api/dashboard?${new URLSearchParams({days,end_date:end,timezone:getZone()})}`);
      if (token !== version) return;
      paint(result);
      $('history-title').textContent = new Intl.DateTimeFormat('tr-TR', {month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(`${selected}-01T12:00:00Z`));
      $('history-status').hidden = true;
      $('export-button').hidden = false;
    } catch (error) {
      if (token === version) $('history-status').textContent = `${error.message} Ayı yeniden seçerek tekrar dene.`;
    }
  }

  function setExpanded(value, animate = true) {
    if (expanded === value) return;
    animation?.cancel();
    const before = panel.getBoundingClientRect();
    if (value) { placeholder.style.height = `${before.height}px`; placeholder.hidden = false; month.value = getToday().slice(0,7); }
    expanded = value;
    panel.classList.toggle('expanded', value);
    $('history-months').hidden = !value;
    toggle.setAttribute('aria-expanded', String(value));
    toggle.setAttribute('aria-label', value ? '7 günlük görünüme dön' : 'Aylık görünümü aç');
    if (!value) {
      placeholder.hidden = true; $('export-button').hidden = false;
      for (const property of ['top', 'bottom', 'left', 'right']) panel.style.removeProperty(property);
      sheet = null;
    }
    update();
    const after = panel.getBoundingClientRect();
    if (animate && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      animation = panel.animate([
        {transformOrigin:'top left',transform:`translate(${before.left-after.left}px,${before.top-after.top}px) scale(${before.width/after.width},${before.height/after.height})`},
        {transformOrigin:'top left',transform:'none'},
      ], {duration:380,easing:'cubic-bezier(.16,1,.3,1)'});
    }
  }
  toggle.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    animation?.cancel();
    const bounds = panel.getBoundingClientRect();
    pointer = {id:event.pointerId,y:event.clientY,top:bounds.top,bounds}; dragged = false;
    toggle.setPointerCapture(event.pointerId);
  });
  toggle.addEventListener('pointermove', event => {
    if (pointer?.id !== event.pointerId) return;
    const delta = event.clientY-pointer.y;
    if (!dragged && Math.abs(delta) < 4) return;
    if (!expanded && delta >= 0) return;
    dragged = true;
    if (!expanded) {
      sheet = {closedTop:pointer.top,bottom:Math.max(16,innerHeight-pointer.bounds.bottom),
        left:pointer.bounds.left,right:innerWidth-pointer.bounds.right};
      setExpanded(true, false);
    }
    if (!sheet) {
      // A panel opened by clicking can also be resized by its handle.
      const original = placeholder.getBoundingClientRect();
      sheet = {closedTop:original.top,bottom:Math.max(16,innerHeight-pointer.bounds.bottom),
        left:pointer.bounds.left,right:innerWidth-pointer.bounds.right};
    }
    const upperLimit = Math.min(innerWidth <= 700 ? 84 : 90, sheet.closedTop);
    const top = Math.max(upperLimit, Math.min(sheet.closedTop, pointer.top + delta));
    Object.assign(panel.style, {top:`${top}px`,bottom:`${sheet.bottom}px`,
      left:`${sheet.left}px`,right:`${sheet.right}px`});
  });
  function finishDrag() {
    if (dragged && sheet && panel.getBoundingClientRect().top >= sheet.closedTop - 2) setExpanded(false, false);
    pointer = null;
  }
  toggle.addEventListener('pointerup', finishDrag);
  toggle.addEventListener('pointercancel', finishDrag);
  toggle.addEventListener('click', event => { if (dragged && event.detail !== 0) { dragged = false; return; } setExpanded(!expanded); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && expanded && !document.querySelector('dialog[open]')) { setExpanded(false); toggle.focus(); } });
  month.addEventListener('change', () => update());
  $('history-current').addEventListener('click', () => { month.value = getToday().slice(0,7); update(); });
  return {update};
}
