import { enhanceMonthInput } from './calendar.js';

export function setupWorkouts({request,getZone,getToday,escape,num,duration,fullDate,timeOf}) {
  const $ = id => document.getElementById(id);
  const month = $('workout-month'), form = $('workout-form'), editor = $('workout-editor');
  const panel = month.closest('.workouts-panel'), timeline = $('workout-timeline');
  const rowsElement = $('workout-rows'), status = $('workout-status'), retry = $('workout-retry');
  let version = 0;
  month.value = month.max = getToday().slice(0,7);
  month.min = '0001-01';
  enhanceMonthInput(month);

  const validMonth = () => /^\d{4}-(0[1-9]|1[0-2])$/.test(month.value) && month.value >= month.min && month.value <= month.max;
  function controls() {
    const focused = document.activeElement;
    month.max = getToday().slice(0,7);
    $('workout-prev').disabled = !validMonth() || month.value === month.min;
    $('workout-next').disabled = !validMonth() || month.value === month.max;
    if ((focused === $('workout-prev') || focused === $('workout-next')) && focused.disabled) month.focus();
  }
  function dateKey(value, zone) {
    const parts = new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value));
    return ['year','month','day'].map(type => parts.find(part => part.type === type).value).join('-');
  }
  function time(value) {
    return `<time datetime="${escape(value)}">${escape(timeOf(value))}</time>`;
  }
  function entry(row, day, zone) {
    // Keep every source/id pair, even when sessions overlap or share timestamps.
    const manual = row.source === 'Manuel';
    const endDate = dateKey(row.end_time, zone) === day ? '' : `${escape(fullDate(row.end_time))} · `;
    return `<li class="workout-entry" data-source="${escape(row.source)}" data-workout-id="${escape(row.id)}">
      <article><div class="workout-entry-heading"><h4>${escape(row.exercise_type || 'Antrenman')}</h4><span class="workout-source ${manual ? 'workout-manual' : 'workout-google'}">${manual ? 'Manuel' : `Google · ${escape(row.source)}`}</span></div>
      <p class="workout-time">${time(row.start_time)} – ${endDate}${time(row.end_time)}</p>
      <dl class="workout-values"><div><dt>Süre</dt><dd>${duration(row.duration_seconds)}</dd></div><div><dt>Kalori</dt><dd>${num(row.calories_kcal,1)}${row.calories_kcal == null ? '' : ' kcal'}</dd></div><div><dt>Mesafe</dt><dd>${num(row.distance_meters == null ? null : row.distance_meters/1000,2)}${row.distance_meters == null ? '' : ' km'}</dd></div></dl></article></li>`;
  }
  function render(rows, zone) {
    const days = new Map();
    // Newest first, with stable source/id tie-breakers; never infer continuous exercise.
    const sorted = [...rows].sort((a,b) => new Date(b.start_time) - new Date(a.start_time) || `${a.source}:${a.id}`.localeCompare(`${b.source}:${b.id}`));
    for (const row of sorted) {
      const day = dateKey(row.start_time, zone);
      if (!days.has(day)) days.set(day, []);
      days.get(day).push(row);
    }
    rowsElement.innerHTML = [...days].map(([day, entries]) => `<li class="workout-day" data-date="${day}"><h3><time datetime="${day}">${escape(fullDate(entries[0].start_time))}</time></h3><ol aria-label="${escape(fullDate(entries[0].start_time))} antrenmanları">${entries.map(row => entry(row,day,zone)).join('')}</ol></li>`).join('');
    timeline.scrollTop = 0;
  }
  async function update() {
    const token = ++version;
    controls();
    const selectedMonth = month.value, zone = getZone();
    const current = () => token === version && month.value === selectedMonth && getZone() === zone;
    status.textContent = 'Antrenmanlar yükleniyor…';
    rowsElement.replaceChildren();
    timeline.setAttribute('aria-busy','true');
    if (document.activeElement === retry) month.focus();
    retry.hidden = true;
    delete panel.dataset.empty;
    $('workout-zone').textContent = `Saat dilimi: ${zone} · Kayıtlar başlangıç gününde gösterilir; kaynaklar ayrı tutulur.`;
    try {
      if (!validMonth()) throw new Error('Geçerli bir ay seç.');
      const rows = await request(`/api/dashboard/workouts?${new URLSearchParams({month:`${selectedMonth}-01`,timezone:zone})}`);
      if (!current()) return;
      render(rows,zone);
      panel.toggleAttribute('data-empty', !rows.length);
      status.textContent = rows.length ? `${num(rows.length)} antrenman` : 'Bu ay antrenman kaydı yok. Fitbit’i eşitleyebilir veya antrenman ekleyebilirsin.';
    } catch (error) {
      if (current()) {
        status.textContent = error.message;
        retry.hidden = false;
      }
    } finally {
      if (current()) timeline.setAttribute('aria-busy','false');
    }
  }
  for (const [id, offset] of [['workout-prev',-1],['workout-next',1]]) {
    $(id).addEventListener('click', () => {
      if (!validMonth()) return;
      const date = new Date(`${month.value}-01T12:00:00Z`);
      date.setUTCMonth(date.getUTCMonth()+offset);
      month.value = date.toISOString().slice(0,7);
      update();
    });
  }
  retry.addEventListener('click', update);
  $('workout-add').addEventListener('click', () => { editor.open = true; form.elements.exercise_type.focus(); });
  $('workout-cancel').addEventListener('click', () => { editor.open = false; $('workout-add').focus(); });
  $('workout-timezone').textContent = `Girdiğin saatler ${Intl.DateTimeFormat().resolvedOptions().timeZone} saat diliminde kaydedilir.`;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if ($('workout-save').getAttribute('aria-disabled') === 'true') return;
    $('workout-error').textContent = ''; $('workout-save').setAttribute('aria-disabled', 'true');
    try {
      const fields = form.elements;
      const start = new Date(fields.start_time.value), end = new Date(fields.end_time.value);
      if (end <= start || end > new Date()) throw new Error('Bitiş saati başlangıçtan sonra ve şu andan önce olmalı.');
      await request('/api/dashboard/workouts', {method:'POST',headers:{'Content-Type':'application/json','X-HealthOS-Request':'1'},body:JSON.stringify({
        exercise_type:fields.exercise_type.value,start_time:start.toISOString(),end_time:end.toISOString(),
        calories_kcal:fields.calories_kcal.value === '' ? null : Number(fields.calories_kcal.value),
        distance_meters:fields.distance_km.value === '' ? null : Number(fields.distance_km.value)*1000,
      })});
      month.value = dateKey(start,getZone()).slice(0,7);
      form.reset(); editor.open = false; $('workout-add').focus(); await update();
    } catch (error) { $('workout-error').textContent = error.message; }
    finally { $('workout-save').setAttribute('aria-disabled', 'false'); }
  });
  month.addEventListener('change', update);
  controls();
  return {update};
}
