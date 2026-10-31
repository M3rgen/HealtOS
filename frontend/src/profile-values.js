export function setupProfileValues({ metrics, iconMarkup, num, shortDate }) {
  const container = document.getElementById('profile-values');
  const rows = Object.entries(metrics).map(([key, [label, unit]]) => {
    const row = document.createElement('div');
    row.className = 'measurement';
    // These controls retain their identity across polling and modal saves, so
    // native dialog return focus still has its original, connected opener.
    row.innerHTML = `<label for="profile-${key}"><span class="measure-icon">${iconMarkup(key)}</span>${label}</label><button type="button" id="profile-${key}" data-metric="${key}" aria-haspopup="dialog"></button><small></small>`;
    container.append(row);
    return { key, label, unit, button: row.querySelector('button'), note: row.querySelector('small') };
  });
  return {
    update(profile) {
      for (const { key, label, unit, button, note } of rows) {
        const item = profile[key];
        const value = item ? `${num(item.value, 1)} ${unit}` : '—';
        button.textContent = value;
        button.setAttribute('aria-label', `${label}: ${value}. Manuel ölçüm ekle`);
        note.textContent = item ? shortDate(item.measured_at) : key === 'muscle' ? 'Yalnız elle girilir' : 'Henüz ölçüm yok';
      }
    },
  };
}
