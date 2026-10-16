// HealthOS icon set (lane C, see public/assets/ASSETS.md): 24×24 grid, 1.75
// round stroke, currentColor. Inlined so CSS colours them and no request is
// needed. CSS sizes the wrapper (`.button-icon`, `.metric-icon`, …); the SVG
// fills it. An entry may also be a complete "<svg …>" string, inserted as-is.
const dot = (cx, cy) => `<circle cx="${cx}" cy="${cy}" r=".6" fill="currentColor" stroke="none"/>`;
const icons = {
  steps: '<path d="m3 14.5 2-8L9 9l2.5-1.5 3 5 5 1.5c1 .5 1.5 1 1.5 2.5v2H5a2 2 0 0 1-2-2z"/><path d="M3.5 15.5H20M10 11l2-1.5"/>',
  sleep: '<path d="M20.5 13.5a8.5 8.5 0 1 1-10-10 6.5 6.5 0 0 0 10 10Z"/>',
  heart: '<path d="M19.5 5.5C17.5 3.5 14 4 12 6.5 10 4 6.5 3.5 4.5 5.5S2.5 11 5 13.5L12 20l7-6.5c2.5-2.5 2.5-6 0-8Z"/>',
  hrv: '<path d="M3 11.5h2L7 4.5l2 9 2-2h4l2-7 2 9 2-2"/><path d="M7 19h10m-8-2-2 2 2 2m6-4 2 2-2 2"/>',
  spo2: '<path d="M10 3.5c-2 3-6.5 7.5-6.5 11a6.5 6.5 0 0 0 13 0c0-3.5-4.5-8-6.5-11Z"/><circle cx="18.5" cy="6.5" r="2"/><circle cx="19.5" cy="12.5" r="1.5"/>',
  workout: '<rect x="4.5" y="6" width="4" height="12" rx="1.5"/><rect x="15.5" y="6" width="4" height="12" rx="1.5"/><path d="M8.5 12h7M2.5 9v6M21.5 9v6"/>',
  height: '<circle cx="8" cy="5.5" r="2"/><path d="M8 8.5v6L5 20.5m3-6 3 6M4.5 12.5 8 9.5l3.5 3M15 3h6m-6 18h6M18 6.5v11m-1.5-9.5L18 6.5 19.5 8m-3 8L18 17.5l1.5-1.5"/>',
  weight: '<rect x="3.5" y="4.5" width="17" height="16" rx="2"/><path d="M7.5 11.5a4.5 4.5 0 0 1 9 0M12 11.5l2-2"/>',
  'body-fat': '<path d="M12 3.5c-2.5 3-7 7.5-7 10.5a7 7 0 0 0 14 0c0-3-4.5-7.5-7-10.5Z"/><path d="m9 17.5 6-7"/><circle cx="9" cy="12" r="1.25"/><circle cx="15" cy="16" r="1.25"/>',
  muscle: '<path d="M3 14v4c4 1.5 10.5 2 15.5 1.2 1.7-.4 2.5-1.6 2.5-3.2l-.5-7.5-.7-3.6c-.2-.8-.8-1.2-1.6-1.2h-1.2c-.8 0-1.3.5-1.5 1.2l-.4 1.7c-.2.7.1 1.2.7 1.5l1.4.7-1.7 5.1c-.7-3-2.6-4.7-4.7-4.7-2.9 0-4.7 2-5 4.8H3Z"/><path d="M9.5 14.2c1-1 2.5-1.2 3.8-.5"/>',
  sync: '<path d="M20.5 10A8.5 8.5 0 0 0 6 5.5L3.5 8m0-4v4h4M3.5 14A8.5 8.5 0 0 0 18 18.5l2.5-2.5m-4 0h4v4"/>',
  download: '<path d="M12 3.5v11m-4-4 4 4 4-4M4 15v4a1.5 1.5 0 0 0 1.5 1.5h13A1.5 1.5 0 0 0 20 19v-4"/>',
  plus: '<circle cx="12" cy="12" r="8.5"/><path d="M8 12h8m-4-4v8"/>',
  calendar: `<rect x="3.5" y="5.5" width="17" height="15" rx="2"/><path d="M7.5 3.5v4m9-4v4M3.5 10h17"/>${[7.5, 12, 16.5].map(x => dot(x, 13.5) + dot(x, 17)).join('')}`,
  status: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><path d="M6.5 12h3L11 8l3 8 1.5-4h2"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  send: '<path d="M12 19V5m-5.5 5.5L12 5l5.5 5.5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  'chat-new': '<path d="M20.5 6.5v9a2 2 0 0 1-2 2H8l-4.5 3v-14a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2Z"/><path d="M8 11h8m-4-4v8"/>',
  'chevron-down': '<path d="m7 9.5 5 5 5-5"/>',
  'chevron-up': '<path d="m7 14.5 5-5 5 5"/>',
  'chevron-left': '<path d="m14.5 7-5 5 5 5"/>',
  'chevron-right': '<path d="m9.5 7 5 5-5 5"/>',
  // Not in the lane C set; drawn on the same grid and stroke.
  external: '<path d="M14 4.5h5.5V10m0-5.5L11 13m6 1v4.5a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 4 18.5v-10A1.5 1.5 0 0 1 5.5 7H10"/>',
  umay: '<path d="m10.6 8.7-3.3-1-4.2.3 1.3 3.2 2-.6.3 2.4 2-.8.7 1.6 1.3-.6.3 1.9-2.1 2.5Q12 19.3 15.1 17.6L13 15.1l.3-1.9 1.3.6.7-1.6 2 .8.3-2.4 2 .6 1.3-3.2-4.2-.3-3.3 1V6.6Q12 3.3 10.6 6.6Z"/>',
};
// Data keys and earlier names that map onto the set.
const aliases = {shoe: 'steps', moon: 'sleep', body_fat: 'body-fat', asclepius: 'umay'};

export function registerIcons(map) { Object.assign(icons, map); }

export function iconMarkup(name) {
  const icon = icons[aliases[name] || name] || '';
  if (icon.trimStart().startsWith('<svg')) return icon;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${icon}</svg>`;
}

export function renderIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((element) => {
    element.innerHTML = iconMarkup(element.dataset.icon);
  });
}
