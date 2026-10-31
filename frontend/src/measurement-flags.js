// Test-entry flags on manual measurements. A flagged entry stays in the list with its badge and
// is left out of the profile and the 3D body; the server only appends flag events, so marking
// and clearing never change or delete the entry itself.
import './measurement-flags.css';

export const isTest = row => (row.flags ?? []).includes('test');

/** The source cell (with the badge) and the action cell of one measurement row. */
export function flagCells(row, label, when, escape) {
  const test = isTest(row);
  const action = test ? 'İşareti kaldır' : 'Test olarak işaretle';
  // The accessible name starts with the visible text (WCAG 2.5.3, label in name).
  return `<td>Manuel${test ? ' <span class="dot warning">Test kaydı</span>' : ''}</td><td><button type="button" class="row-action" data-flag-id="${escape(row.id)}" data-test="${test ? 1 : 0}" aria-label="${escape(`${action}: ${label}, ${when}`)}">${action}</button></td>`;
}

export function setupMeasurementFlags({ request, notice, reload }) {
  document.getElementById('measurement-rows').addEventListener('click', async event => {
    const button = event.target.closest('[data-flag-id]');
    if (!button || button.getAttribute('aria-disabled') === 'true') return;
    const marking = button.dataset.test !== '1';
    button.setAttribute('aria-disabled', 'true');
    try {
      await request(`/api/dashboard/measurements/${encodeURIComponent(button.dataset.flagId)}/${marking ? 'flag' : 'unflag'}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify({ flag: 'test' }),
      });
      notice(
        marking
          ? 'Ölçüm test kaydı olarak işaretlendi; profilde ve beden modelinde sayılmaz. Kayıt silinmedi.'
          : 'Test işareti kaldırıldı; ölçüm yeniden sayılıyor.',
      );
      await reload();
    } catch (error) {
      notice(`İşaret değiştirilemedi: ${error.message}`, true);
    } finally {
      button.removeAttribute('aria-disabled');
    }
  });
}
