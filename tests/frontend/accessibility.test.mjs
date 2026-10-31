import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setupDialog, replaceWithFocus } from '../../frontend/src/accessibility.js';
import { setupProfileValues } from '../../frontend/src/profile-values.js';

// Controller boundary only: native modal trapping, layout and colors are tested
// in the browser. These objects model disconnection and asynchronous ownership.
class Control {
  isConnected = true; disabled = false; hidden = false; attrs = {}; children = []; handlers = {};
  addEventListener(type, listener) { this.handlers[type] = listener; }
  getClientRects() { return this.hidden ? [] : [{}]; }
  closest() { return this.hidden ? this : null; }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  focus() { document.activeElement = this; }
  setAttribute(key, value) { this.attrs[key] = value; }
  append(child) { this.children.push(child); }
  set innerHTML(value) { this.markup = value; this.children = [new Control(), new Control()]; }
  querySelector(selector) { return this.children[selector === 'button' ? 0 : 1]; }
}
function environment() {
  const menu = new Control(), outside = new Control(), container = new Control();
  const doc = {activeElement: outside, modal: null,
    querySelector: selector => selector === 'dialog:modal' ? doc.modal : menu,
    getElementById: () => container, createElement: () => new Control()};
  globalThis.document = doc;
  globalThis.getComputedStyle = () => ({visibility: 'visible'});
  return {menu, outside, container, doc};
}

test('native dialog opener policy falls back to the visible current menu after a view change', () => {
  const {menu, outside, doc} = environment();
  const dialog = new Control(), initial = new Control(); dialog.children.push(initial);
  dialog.showModal = () => { dialog.open = true; doc.modal = dialog; };
  const controller = setupDialog(dialog);
  controller.open({initial});
  assert.equal(doc.activeElement, initial);
  assert.equal(doc.modal, dialog, 'open through native showModal');
  outside.hidden = true; doc.modal = null; dialog.open = false;
  dialog.handlers.close();
  assert.equal(doc.activeElement, menu, 'never return focus into the hidden view');
  outside.hidden = false; outside.focus(); controller.open({initial});
  doc.modal = null; dialog.open = false; dialog.handlers.close();
  assert.equal(doc.activeElement, outside, 'connected visible opener is reused');
});

test('DOM replacement keeps owned focus but never takes it from a user who left', () => {
  const {container, outside, doc} = environment();
  const old = new Control(), replacement = new Control(); container.append(old); old.focus();
  replaceWithFocus(container, () => { old.isConnected = false; container.children = [replacement]; }, () => replacement);
  assert.equal(doc.activeElement, replacement);
  outside.focus();
  replaceWithFocus(container, () => { replacement.isConnected = false; container.children = []; }, () => new Control());
  assert.equal(doc.activeElement, outside);
  container.children = [old]; old.isConnected = true; old.focus();
  replaceWithFocus(container, () => { old.hidden = true; }, () => outside);
  assert.equal(doc.activeElement, outside, 'a hidden control also needs a visible fallback');
});

test('profile refresh and save preserve all four measurement controls and their visible names', () => {
  const {container, doc} = environment();
  const metrics = {height: ['Boy', 'cm'], weight: ['Kilo', 'kg'], body_fat: ['Yağ oranı', '%'], muscle: ['Kas oranı', '%']};
  const profile = setupProfileValues({metrics, iconMarkup: () => '', num: value => String(value), shortDate: value => value});
  profile.update({weight: {value: 70, measured_at: '2026-10-01'}});
  const controls = container.children.map(row => row.querySelector('button'));
  controls[1].focus();
  profile.update({weight: {value: 71, measured_at: '2026-10-02'}});
  assert.equal(doc.activeElement, controls[1]);
  assert.equal(controls[1].textContent, '71 kg');
  assert.deepEqual(container.children.map(row => row.querySelector('button')), controls);
  for (const control of controls) assert(control.attrs['aria-label'].includes(control.textContent));
  profile.update({});
  assert.equal(controls[1].textContent, '—');
  assert.equal(doc.activeElement, controls[1], 'missing profile data must not remove its focused button');
});
