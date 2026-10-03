// Fixed SVG geometry keeps symbols consistent across fonts and platforms.
const paths = {
  close: '<path d="m12 12 16 16m0-16L12 28"/>',
  send: '<path d="M20 31V9m-9 10 9-10 9 10"/>',
  stop: '<rect x="12" y="12" width="16" height="16" rx="2"/>',
  'chat-new': '<path d="M20 7H9a3 3 0 0 0-3 3v17a3 3 0 0 0 3 3h5l6 5v-5h10a3 3 0 0 0 3-3V20M29 5v12m-6-6h12"/>',

  shoe: '<path d="M6 23c3-2 6-5 8-8l5-8 4 2 2-4 5 5c2 2 2 5 0 7l-3 3-1 5c-1 3-7 6-13 7-4 1-8 0-9-2-1-2 0-5 2-7Z"/><path d="m14 15 4 3m-7 1 4 3m4-11 4 3M5 28c7 2 17-2 21-5"/>',
  moon: '<path d="M29 26A15 15 0 0 1 15 5a15 15 0 1 0 14 21Z"/>',
  heart: '<path d="M20 34S4 23 4 13a8 8 0 0 1 16-2 8 8 0 0 1 16 2c0 10-16 21-16 21Z"/>',
  sync: '<path d="M32 15a13 13 0 0 0-23-5l-3 5m0-8v8h8M8 25a13 13 0 0 0 23 5l3-5m0 8v-8h-8"/>',
  plus: '<circle cx="20" cy="20" r="15"/><path d="M20 12v16M12 20h16"/>',
  download: '<path d="M20 5v20m-7-7 7 7 7-7M8 25v8h24v-8"/>',
  asclepius: '<path d="M20 3v34"/><path d="M25 6c-8-2-16 2-13 7 2 4 14 2 15 7 1 5-15 4-14 9 0 3 5 4 9 3"/><path d="m25 6 3 2-4 2"/>',
};

export function renderIcons() {
  document.querySelectorAll('[data-icon]').forEach((element) => {
    element.innerHTML = `<svg viewBox="0 0 40 40" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[element.dataset.icon] || ''}</svg>`;
  });
}
