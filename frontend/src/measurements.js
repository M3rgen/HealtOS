import { setupDialog } from './accessibility.js';

export function setupMeasurements({ request, getProfile, notice, onSaved }) {
  const $ = id => document.getElementById(id);
  const modal = setupDialog($('measurement-dialog'));
  function setBounds() {
    const key = $('measurement-metric').value;
    const [min, max] = { height: [30, 260], weight: [1, 500], body_fat: [0, 100], muscle: [0, 100] }[key];
    $('measurement-value').min = min;
    $('measurement-value').max = max;
  }
  function openMeasurement(metric = 'weight') {
    $('measurement-form').reset();
    $('form-error').textContent = '';
    $('measurement-metric').value = metric;
    const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    $('measurement-date').value = local;
    $('measurement-date').max = local;
    $('input-timezone').textContent = `Girdiğin saat ${Intl.DateTimeFormat().resolvedOptions().timeZone} saat diliminde kaydedilir.`;
    if (getProfile()?.[metric]) $('measurement-value').value = getProfile()[metric].value;
    $('measurement-form').querySelectorAll('[aria-invalid]').forEach(field => field.removeAttribute('aria-invalid'));
    setBounds();
    modal.open({ initial: $('measurement-value') });
  }
  $('add-measurement').addEventListener('click', () => openMeasurement());
  $('measurements-add').addEventListener('click', () => openMeasurement());
  $('profile-values').addEventListener('click', event => {
    const el = event.target.closest('[data-metric]');
    if (el) openMeasurement(el.dataset.metric);
  });
  ['measurement-close', 'measurement-cancel'].forEach(id =>
    $(id).addEventListener('click', () => $('measurement-dialog').close()),
  );
  $('measurement-metric').addEventListener('change', () => {
    setBounds();
    $('measurement-value').value = getProfile()?.[$('measurement-metric').value]?.value ?? '';
  });
  $('measurement-form').addEventListener('submit', async event => {
    event.preventDefault();
    if ($('measurement-save').getAttribute('aria-disabled') === 'true') return;
    $('measurement-save').setAttribute('aria-disabled', 'true');
    $('form-error').textContent = '';
    try {
      await request('/api/dashboard/measurements', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify({
          metric: $('measurement-metric').value,
          value: $('measurement-value').value,
          measured_at: new Date($('measurement-date').value).toISOString(),
        }),
      });
      $('measurement-dialog').close();
      notice('Ölçüm kaydedildi.');
      await onSaved();
    } catch (error) {
      $('form-error').textContent = error.message;
    } finally {
      $('measurement-save').removeAttribute('aria-disabled');
    }
  });
  $('measurement-form').addEventListener('invalid', event => event.target.setAttribute('aria-invalid', 'true'), true);
  $('measurement-form').addEventListener('input', event => event.target.removeAttribute('aria-invalid'));
}
