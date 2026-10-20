import { enhanceMonthInput } from './calendar.js';

export function setupWorkouts({request,getZone,getToday,escape,num,duration,fullDate,timeOf}) {
  const $ = id => document.getElementById(id);
  const month = $('workout-month'), form = $('workout-form'), editor = $('workout-editor');
  let version = 0;
  month.value = month.max = getToday().slice(0,7);
  enhanceMonthInput(month);
  const panel = month.closest('.workouts-panel');
  async function update() {
    const token = ++version;
    month.max = getToday().slice(0,7);
    $('workout-status').textContent = 'Antrenmanlar yükleniyor…';
    $('workout-rows').replaceChildren();
    delete panel.dataset.empty;
    try {
      if (!month.value || month.value > month.max) throw new Error('Geçerli bir ay seç.');
      const rows = await request(`/api/dashboard/workouts?${new URLSearchParams({month:`${month.value}-01`,timezone:getZone()})}`);
      if (token !== version) return;
      panel.toggleAttribute('data-empty', !rows.length);
      $('workout-status').textContent = rows.length ? `${num(rows.length)} antrenman` : 'Bu ay antrenman kaydı yok. Fitbit’i eşitleyebilir veya antrenman ekleyebilirsin.';
      $('workout-rows').innerHTML = rows.map(row => `<tr><td>${escape(fullDate(row.start_time))}<small class="workout-time">${escape(timeOf(row.start_time))} – ${escape(timeOf(row.end_time))}</small></td><td>${escape(row.exercise_type || 'Antrenman')}</td><td>${duration(row.duration_seconds)}</td><td>${num(row.calories_kcal,1)}${row.calories_kcal == null ? '' : ' kcal'}</td><td>${num(row.distance_meters == null ? null : row.distance_meters/1000,2)}${row.distance_meters == null ? '' : ' km'}</td><td>${escape(row.source)}</td></tr>`).join('');
    } catch (error) { if (token === version) $('workout-status').textContent = `${error.message} Ayı yeniden seçerek tekrar dene.`; }
  }
  $('workout-add').addEventListener('click', () => { editor.open = true; form.elements.exercise_type.focus(); editor.scrollIntoView({block:'nearest'}); });
  $('workout-cancel').addEventListener('click', () => { editor.open = false; $('workout-add').focus(); });
  $('workout-timezone').textContent = `Girdiğin saatler ${Intl.DateTimeFormat().resolvedOptions().timeZone} saat diliminde kaydedilir.`;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    $('workout-error').textContent = ''; $('workout-save').disabled = true;
    try {
      const fields = form.elements;
      const start = new Date(fields.start_time.value), end = new Date(fields.end_time.value);
      if (end <= start || end > new Date()) throw new Error('Bitiş saati başlangıçtan sonra ve şu andan önce olmalı.');
      await request('/api/dashboard/workouts', {method:'POST',headers:{'Content-Type':'application/json','X-HealthOS-Request':'1'},body:JSON.stringify({
        exercise_type:fields.exercise_type.value,start_time:start.toISOString(),end_time:end.toISOString(),
        calories_kcal:fields.calories_kcal.value === '' ? null : Number(fields.calories_kcal.value),
        distance_meters:fields.distance_km.value === '' ? null : Number(fields.distance_km.value)*1000,
      })});
      // Locale field order is not guaranteed; derive the month explicitly.
      const parts = new Intl.DateTimeFormat('en-CA',{timeZone:getZone(),year:'numeric',month:'2-digit'}).formatToParts(start);
      month.value = `${parts.find(part=>part.type==='year').value}-${parts.find(part=>part.type==='month').value}`;
      form.reset(); editor.open = false; $('workout-add').focus(); await update();
    } catch (error) { $('workout-error').textContent = error.message; }
    finally { $('workout-save').disabled = false; }
  });
  month.addEventListener('change', update);
  return {update};
}
