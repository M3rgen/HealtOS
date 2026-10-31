import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setupWorkouts } from '../../frontend/src/workouts.js';

// A small DOM boundary for the controller; layout and the native picker are covered in Playwright.
// type=text exercises the supported fallback when a browser has no native month picker.
class Element {
  value = ''; type = 'text'; textContent = ''; innerHTML = ''; hidden = false;
  disabled = false; dataset = {}; attributes = {}; handlers = {};
  addEventListener(name, handler) { this.handlers[name] = handler; }
  emit(name) { return this.handlers[name]({preventDefault() {}}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  toggleAttribute(name, active) { if (active) this.attributes[name] = ''; else delete this.attributes[name]; }
  replaceChildren() { this.innerHTML = ''; }
  focus() {}
}
function setup() {
  const elements = new Map(), pending = [];
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  globalThis.document = {getElementById: $};
  $('workout-month').closest = () => $('panel');
  let zone = 'Europe/Istanbul';
  const form = $('workout-form');
  form.elements = Object.fromEntries(['exercise_type','start_time','end_time','calories_kcal','distance_km'].map(key => [key,new Element()]));
  form.reset = () => Object.values(form.elements).forEach(field => { field.value = ''; });
  const controller = setupWorkouts({
    request: (url, options) => new Promise((resolve, reject) => pending.push({url,options,resolve,reject})),
    getZone: () => zone, getToday: () => '2026-01-15',
    escape: value => String(value).replaceAll('<','&lt;').replaceAll('"','&quot;'),
    num: value => value == null ? '—' : String(value), duration: value => value == null ? '—' : `${value} s`,
    fullDate: value => new Intl.DateTimeFormat('tr', {timeZone: zone, dateStyle: 'long'}).format(new Date(value)),
    timeOf: value => new Intl.DateTimeFormat('tr', {timeZone: zone, timeStyle: 'short'}).format(new Date(value)),
  });
  return {$,pending,controller,setZone: value => { zone = value; }};
}
const rows = [
  {id:'shared',source:'Fitbit',exercise_type:'<Koşu>',start_time:'2025-12-31T22:30:00Z',end_time:'2026-01-01T22:00:00Z',duration_seconds:3500,calories_kcal:null,distance_meters:null},
  {id:'shared',source:'Manuel',exercise_type:'Yürüyüş',start_time:'2025-12-31T22:30:00Z',end_time:'2025-12-31T23:30:00Z',duration_seconds:3600,calories_kcal:0,distance_meters:0},
];

test('year controls, future boundary, invalid selection and same-month retry', async () => {
  const {$,pending,controller} = setup();
  assert.equal($('workout-next').disabled, true);
  const december = $('workout-prev').emit('click');
  assert.equal($('workout-month').value, '2025-12');
  assert.match(pending[0].url, /month=2025-12-01/);
  pending[0].resolve([]); await december;
  const january = $('workout-next').emit('click');
  assert.equal($('workout-month').value, '2026-01');
  pending[1].reject(new Error('Bağlantı hatası')); await january;
  assert.equal($('workout-retry').hidden, false);
  const retry = $('workout-retry').emit('click');
  assert.equal(pending[2].url, pending[1].url);
  pending[2].resolve([]); await retry;
  assert.match($('workout-status').textContent, /Bu ay antrenman kaydı yok/);
  assert.equal($('workout-retry').hidden, true);
  for (const value of ['', '2026-02', '2025-13', '2025-00', '0000-01']) {
    $('workout-month').value = value; await controller.update();
    assert.equal(pending.length, 3);
    assert.equal($('workout-timeline').attributes['aria-busy'], 'false');
    assert.equal($('workout-next').disabled, true);
  }
});

test('local start day, distinct source/id pairs, cross-day end, null and measured zero', async () => {
  const {$,pending,controller} = setup();
  const loading = controller.update(); pending[0].resolve(rows); await loading;
  const html = $('workout-rows').innerHTML;
  assert.equal((html.match(/class="workout-day"/g) || []).length, 1);
  assert.match(html, /data-date="2026-01-01"/);
  assert.equal((html.match(/data-workout-id="shared"/g) || []).length, 2);
  assert.match(html, /Google · Fitbit/); assert.match(html, /workout-manual/);
  assert.match(html, /01:30/); assert.match(html, /2 Ocak 2026 · /);
  assert.match(html, /3500 s/); assert.match(html, /3600 s/);
  assert.equal((html.match(/<dd>—<\/dd>/g) || []).length, 2);
  assert.match(html, /<dd>0 kcal<\/dd>/); assert.match(html, /<dd>0 km<\/dd>/);
  assert.match(html, /&lt;Koşu>/);
});

test('reverse month and timezone responses cannot overwrite newer content, status or busy state', async () => {
  const {$,pending,controller,setZone} = setup();
  const january = controller.update();
  $('workout-month').value = '2025-12'; const december = controller.update();
  assert.equal($('workout-rows').innerHTML, '');
  pending[1].resolve([]); await december;
  pending[0].resolve(rows); await january;
  assert.equal($('workout-month').value, '2025-12');
  assert.equal($('workout-rows').innerHTML, '');
  assert.match($('workout-status').textContent, /Bu ay antrenman kaydı yok/);
  const oldZone = controller.update(); setZone('UTC'); const newZone = controller.update();
  pending[3].resolve(rows); await newZone;
  const html = $('workout-rows').innerHTML, status = $('workout-status').textContent;
  pending[2].reject(new Error('Eski istek hatası')); await oldZone;
  assert.equal($('workout-rows').innerHTML, html);
  assert.equal($('workout-status').textContent, status);
  assert.equal($('workout-retry').hidden, true);
  assert.equal($('workout-timeline').attributes['aria-busy'], 'false');
  assert.match(pending[3].url, /timezone=UTC/);
  const older = controller.update(), newer = controller.update();
  pending[4].resolve([]); await older;
  assert.equal($('workout-timeline').attributes['aria-busy'], 'true');
  pending[5].resolve(rows); await newer;
});

test('manual save refreshes the selected-zone start month and preserves optional empty values', async () => {
  const {$,pending} = setup();
  const fields = $('workout-form').elements;
  fields.exercise_type.value = 'Gece yürüyüşü';
  fields.start_time.value = '2025-12-31T22:30:00Z'; fields.end_time.value = '2025-12-31T23:30:00Z';
  fields.distance_km.value = '5';
  $('workout-month').value = '2025-11';
  $('workout-add').emit('click'); assert.equal($('workout-editor').open, true);
  const save = $('workout-form').emit('submit');
  const body = JSON.parse(pending[0].options.body);
  assert.equal(body.calories_kcal, null); assert.equal(body.distance_meters, 5000);
  assert.equal($('workout-save').getAttribute('aria-disabled'), 'true');
  pending[0].resolve({...body,id:'saved',source:'Manuel'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal($('workout-month').value, '2026-01');
  assert.equal($('workout-editor').open, false);
  assert.match(pending[1].url, /month=2026-01-01/);
  pending[1].resolve([{...rows[1],id:'saved'}]); await save;
  assert.equal($('workout-save').getAttribute('aria-disabled'), 'false');
  assert.match($('workout-rows').innerHTML, /data-workout-id="saved"/);
});
