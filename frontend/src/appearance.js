// Appearance: logo, Umay portrait and page background, chosen from presets
// (public/assets/presets/manifest.json) or uploaded by the user. Applied at
// runtime without a reload; any failure falls back to the built-in defaults.
//
// Persistence goes through a small store adapter with this API:
//   getSelection() → {logo?, umay?, background?}   setSelection(selection)
//   getUpload(kind) → record|null   putUpload(kind, record)   deleteUpload(kind)
// createSyncedAppearanceStore (the default) keeps the preset ids on the server
// (/api/settings/appearance) so every device shows the same choice, with
// localStorage as the offline copy. Uploaded images are never sent anywhere: they
// stay in this browser's IndexedDB; a device without the upload shows the default.
//
// Security: uploaded files are only ever shown through <img src> or CSS url()
// with a blob: URL. Uploaded SVG text is never parsed into the DOM.
import { request } from './api.js';
import { iconMarkup } from './icons.js';

const MANIFEST_URL = '/assets/presets/manifest.json';
const KINDS = ['logo', 'umay', 'background'];
const MAX_BYTES = 2 * 1024 * 1024;
const TYPES = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/svg+xml': 'svg' };
const MIN_SIDE = { logo: 16, umay: 64, background: 64 };
const MAX_SIDE = 8192;
const UPLOAD = 'upload';
const PRESET_ID = /^[a-z0-9][a-z0-9-]{0,47}$/;
const REMOTE_URL = '/api/settings/appearance';
const DEFAULT_GLYPH = '/assets/umay/umay-glyph.svg';
const TEXT = {
  logo: { legend: 'Logo', hint: 'Üst çubukta ve tarayıcı sekmesinde görünür.' },
  umay: { legend: 'Umay', hint: 'Sohbet penceresinde ve karşılama ekranında görünür.' },
  background: { legend: 'Arka plan', hint: 'Panellerin arkasındaki zemin; paneller her zaman okunaklı kalır.' },
};

// Used when the manifest cannot be loaded or lists nothing usable.
const FALLBACK = {
  logo: [{ id: 'yurt', label: 'Otağ', builtin: true, files: {
    mark: '/assets/presets/logo/yurt/mark.svg', markMono: '/assets/presets/logo/yurt/mark-mono.svg',
    // No SVG favicon on purpose: browsers prefer it over the pixel-snapped 16/32 PNGs.
    faviconIco: '/assets/presets/logo/yurt/favicon.ico',
    favicon16: '/assets/presets/logo/yurt/favicon-16.png', favicon32: '/assets/presets/logo/yurt/favicon-32.png',
    appleTouch: '/assets/presets/logo/yurt/apple-touch-icon.png' } },
  { id: 'healthos', label: 'HealthOS (klasik)', builtin: true, files: {
    mark: '/assets/brand/healthos-mark-amber.svg', markMono: '/assets/brand/healthos-mark.svg',
    favicon: '/assets/brand/favicon.svg', faviconIco: '/assets/brand/favicon.ico', appleTouch: '/assets/brand/apple-touch-icon.png' } }],
  umay: [{ id: 'umay', label: 'Umay', builtin: true, files: {
    src512: '/assets/umay/umay.webp', src256: '/assets/umay/umay-256.webp', src64: '/assets/umay/umay-64.webp',
    src64x2: '/assets/umay/umay-64@2x.webp', glyph: DEFAULT_GLYPH } }],
  background: [{ id: 'warm-grain', label: 'Sıcak doku', builtin: true, files: {
    tile: '/assets/bg/grain-tile.webp', vignette: '/assets/bg/vignette.webp' } }],
};
const REQUIRED = { logo: ['mark', 'markMono'], umay: ['src512', 'src256'], background: ['tile'] };

/* ------------------------------------------------------------------------ */
/* Storage adapter: localStorage for ids, IndexedDB for blobs, memory last.  */
/* ------------------------------------------------------------------------ */
export function createLocalAppearanceStore({ key = 'healtos.appearance', dbName = 'healtos-appearance' } = {}) {
  const memory = new Map();
  let database;
  const open = () => database ??= new Promise((resolve) => {
    try {
      const request = indexedDB.open(dbName, 1);
      request.onupgradeneeded = () => request.result.createObjectStore('uploads');
      request.onsuccess = () => resolve(request.result);
      request.onerror = request.onblocked = () => resolve(null);
    } catch { resolve(null); }
  });
  const run = async (mode, action, fallback) => {
    const db = await open();
    if (!db) return fallback();
    return new Promise((resolve) => {
      try {
        const transaction = db.transaction('uploads', mode);
        const request = action(transaction.objectStore('uploads'));
        transaction.oncomplete = () => resolve(request.result);
        transaction.onerror = transaction.onabort = () => resolve(fallback());
      } catch { resolve(fallback()); }
    });
  };
  return {
    async getSelection() {
      try { const value = JSON.parse(localStorage.getItem(key) || '{}'); return value && typeof value === 'object' ? value : {}; }
      catch { return {}; }
    },
    async setSelection(selection) {
      try { localStorage.setItem(key, JSON.stringify(selection)); } catch { /* Private mode: keep for this session only. */ }
    },
    async getUpload(kind) {
      return (await run('readonly', store => store.get(kind), () => memory.get(kind))) ?? memory.get(kind) ?? null;
    },
    async putUpload(kind, record) {
      memory.set(kind, record);
      await run('readwrite', store => store.put(record, kind), () => undefined);
    },
    async deleteUpload(kind) {
      memory.delete(kind);
      await run('readwrite', store => store.delete(kind), () => undefined);
    },
  };
}

// Shared selection. getSelection() answers from localStorage at once (first paint);
// sync() then asks the server, whose copy wins when reachable. Offline, the local
// copy stays in use and a write that could not reach the server is marked pending,
// then sent on the next load that reaches it (last write wins).
const cleanSelection = value => Object.fromEntries(KINDS.flatMap(kind => (
  typeof value?.[kind] === 'string' && PRESET_ID.test(value[kind]) ? [[kind, value[kind]]] : [])));
export function createSyncedAppearanceStore({ local = createLocalAppearanceStore(), url = REMOTE_URL, pendingKey = 'healtos.appearance.pending' } = {}) {
  const pending = (value) => {
    try {
      if (value === undefined) return localStorage.getItem(pendingKey) === '1';
      if (value) localStorage.setItem(pendingKey, '1'); else localStorage.removeItem(pendingKey);
    } catch { /* Private mode: nothing to resume later. */ }
    return false;
  };
  let latest = null, queue = Promise.resolve();
  const enqueue = (selection) => {
    latest = selection; pending(true);
    // One request at a time, newest last; superseded writes are skipped.
    queue = queue.then(() => (latest === selection ? push(selection) : undefined));
    return queue;
  };
  const push = async (selection) => {
    try {
      await request(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify(cleanSelection(selection)),
      });
      if (latest === selection) pending(false);
    } catch { /* Offline or rejected: stays pending; the local copy still applies. */ }
  };
  return {
    ...local,
    async getSelection() { return cleanSelection(await local.getSelection()); },
    // → the server's selection, or null when it is unreachable or this browser's
    // copy was sent instead (an unsent change, or a server with nothing stored yet).
    async sync() {
      const cached = cleanSelection(await local.getSelection());
      let remote;
      try { remote = await request(url); } catch { return null; }
      if (pending() || (!remote?.updated_at && Object.keys(cached).length)) { await enqueue(cached); return null; }
      const selection = cleanSelection(remote?.selection);
      await local.setSelection(selection);
      return selection;
    },
    async setSelection(selection) {
      await local.setSelection(selection);
      return enqueue(selection);
    },
  };
}

/* ------------------------------------------------------------------------ */
/* Manifest                                                                  */
/* ------------------------------------------------------------------------ */
function assetPath(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith('/assets/') && !/["'()\\\s]/.test(url.pathname) ? url.pathname : null;
  } catch { return null; }
}
function normalize(raw) {
  const out = {};
  for (const kind of KINDS) {
    const seen = new Set();
    out[kind] = (Array.isArray(raw?.[kind]) ? raw[kind] : []).flatMap((entry) => {
      const id = String(entry?.id ?? '');
      if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(id) || id === UPLOAD || seen.has(id)) return [];
      const files = {};
      for (const [name, path] of Object.entries(entry.files ?? {})) { const safe = assetPath(path); if (safe) files[name] = safe; }
      if (!REQUIRED[kind].some(name => files[name])) return [];
      seen.add(id);
      return [{ id, label: String(entry.label ?? id).slice(0, 40), builtin: entry.builtin !== false, files }];
    });
    if (!out[kind].length) out[kind] = FALLBACK[kind];
  }
  return out;
}
async function loadManifest() {
  try {
    const response = await fetch(MANIFEST_URL, { cache: 'no-cache', signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(String(response.status));
    return normalize(await response.json());
  } catch { return normalize(FALLBACK); }
}

/* ------------------------------------------------------------------------ */
/* Upload validation and derived sizes                                       */
/* ------------------------------------------------------------------------ */
function decode(blob) {
  const url = URL.createObjectURL(blob);
  const image = new Image();
  image.decoding = 'async';
  image.src = url;
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 8000));
  return Promise.race([image.decode(), timeout]).then(() => ({ image, url }), (error) => { URL.revokeObjectURL(url); throw error; });
}
function canvasBlob(canvas, type = 'image/png', quality) {
  return new Promise((resolve, reject) => {
    try { canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('encode')), type, quality); } catch (error) { reject(error); }
  });
}
// Square render with stepwise halving so small sizes stay sharp, not aliased.
async function square(image, size, fit, background) {
  const width = image.naturalWidth || 512, height = image.naturalHeight || 512;
  let source = image, sw = width, sh = height, sx = 0, sy = 0;
  if (fit === 'cover') { const side = Math.min(width, height); sx = (width - side) / 2; sy = (height - side) / 2; sw = sh = side; }
  while (Math.max(sw, sh) > size * 2) {
    const step = document.createElement('canvas');
    step.width = Math.max(1, Math.round(sw / 2)); step.height = Math.max(1, Math.round(sh / 2));
    const context = step.getContext('2d');
    context.imageSmoothingQuality = 'high';
    context.drawImage(source, sx, sy, sw, sh, 0, 0, step.width, step.height);
    source = step; sx = sy = 0; sw = step.width; sh = step.height;
  }
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const context = canvas.getContext('2d');
  context.imageSmoothingQuality = 'high';
  if (background) { context.fillStyle = background; context.fillRect(0, 0, size, size); }
  const scale = fit === 'cover' ? size / Math.max(sw, sh) : Math.min(size / sw, size / sh) * (background ? .78 : 1);
  const dw = sw * scale, dh = sh * scale;
  context.drawImage(source, sx, sy, sw, sh, (size - dw) / 2, (size - dh) / 2, dw, dh);
  return canvasBlob(canvas);
}
function fileType(file) {
  if (TYPES[file.type]) return file.type;
  const extension = /\.([a-z0-9]+)$/i.exec(file.name || '')?.[1]?.toLowerCase();
  return Object.keys(TYPES).find(type => TYPES[type] === extension || (extension === 'jpeg' && type === 'image/jpeg')) ?? null;
}
export async function prepareUpload(kind, file) {
  const type = file && fileType(file);
  if (!type) throw new Error('Bu dosya türü desteklenmiyor. PNG, JPEG, WebP veya SVG yükle.');
  if (!file.size) throw new Error('Dosya boş görünüyor.');
  if (file.size > MAX_BYTES) throw new Error('Dosya 2 MB’tan büyük. Daha küçük bir görsel seç.');
  const vector = type === 'image/svg+xml';
  // Only a sanity check; the markup itself is never inserted into the page.
  if (vector && !/<svg[\s>]/i.test(await file.slice(0, 4096).text())) throw new Error('SVG dosyası okunamadı.');
  const original = file.slice(0, file.size, type);
  let decoded;
  try { decoded = await decode(original); } catch { throw new Error('Görsel açılamadı. Dosya bozuk olabilir.'); }
  const { image, url } = decoded;
  try {
    const width = image.naturalWidth, height = image.naturalHeight;
    if (!vector) {
      if (width < MIN_SIDE[kind] || height < MIN_SIDE[kind]) throw new Error(`Görsel çok küçük; en az ${MIN_SIDE[kind]}×${MIN_SIDE[kind]} piksel olmalı.`);
      if (width > MAX_SIDE || height > MAX_SIDE) throw new Error(`Görsel çok büyük; kenarlar en fazla ${MAX_SIDE} piksel olabilir.`);
    }
    // Canvas output can fail (e.g. an SVG the browser refuses to rasterize); keep the original then.
    const derive = (size, fit, background) => square(image, size, fit, background).catch(() => original);
    const files = {};
    if (kind === 'logo') {
      files.mark = original;
      [files.favicon32, files.favicon16, files.appleTouch] = await Promise.all([derive(32, 'contain'), derive(16, 'contain'), derive(180, 'contain', '#181814')]);
    } else if (kind === 'umay') {
      [files.src512, files.src256, files.src64, files.src64x2] = await Promise.all([derive(512, 'cover'), derive(256, 'cover'), derive(64, 'cover'), derive(128, 'cover')]);
    } else {
      files.image = original;
    }
    const mode = kind === 'background' && !vector && Math.max(width, height) < 512 ? 'tile' : 'cover';
    return { kind, name: String(file.name || '').slice(0, 80), type, width, height, mode, files, createdAt: Date.now() };
  } finally { URL.revokeObjectURL(url); }
}

/* ------------------------------------------------------------------------ */
/* Apply                                                                     */
/* ------------------------------------------------------------------------ */
const cssUrl = (url) => url ? `url("${url}")` : 'none';
const live = { logo: [], umay: [], background: [] };      // blob URLs currently shown
function blobUrls(kind, record) {
  const urls = {};
  for (const [name, blob] of Object.entries(record.files)) if (blob instanceof Blob) urls[name] = URL.createObjectURL(blob);
  return urls;
}
function resolveEntry(kind, entry) {
  const f = entry.files;
  if (kind === 'umay') {
    const big = f.src512 || f.src256;
    return { ...entry, files: { ...f, src512: big, src256: f.src256 || big, src64: f.src64 || f.src256 || big, src64x2: f.src64x2 || f.src256 || big } };
  }
  return entry;
}
function renderMark(container, entry, inline) {
  container.replaceChildren();
  container.className = 'appearance-mark';
  if (inline) { container.append(inline.cloneNode(true)); return; }
  // Full-colour marks render as designed (two-tone accents); a mono-only mark is tinted amber.
  if (entry.files.mark) {
    const image = new Image();
    image.alt = ''; image.decoding = 'async'; image.src = entry.files.mark;
    container.append(image);
  } else {
    container.classList.add('is-mask');
    container.style.setProperty('--mark', cssUrl(entry.files.markMono));
  }
}
function setLink(id, rel, href, type, sizes) {
  const old = document.getElementById(id);
  if (!href) { old?.remove(); return; }
  // Replacing the element (not just href) makes browsers refresh the tab icon.
  const link = document.createElement('link');
  link.id = id; link.rel = rel; link.href = href;
  if (type) link.type = type;
  if (sizes) link.setAttribute('sizes', sizes);
  if (old) old.replaceWith(link); else document.head.append(link);
}

export function setupAppearance({ store = createSyncedAppearanceStore() } = {}) {
  const $ = id => document.getElementById(id);
  const root = document.documentElement;
  const brand = document.querySelector('.topbar .brand');
  const inlineMark = brand?.querySelector('.brand-mark');
  const headerMark = document.createElement('span');
  headerMark.hidden = true;
  inlineMark?.after(headerMark);
  const dialog = $('appearance-dialog');
  let manifest = normalize(FALLBACK), selection = {}, uploads = {}, current = {};
  // What is stored (and shared): like `selection`, but an `upload` choice made on a
  // device whose image this browser lacks is kept, not replaced by the fallback.
  let saved = {}, edits = 0;
  const save = (changes) => { edits += 1; saved = { ...saved, ...changes }; return store.setSelection(saved); };
  const resolveSelection = () => Object.fromEntries(KINDS.map((kind) => {
    const id = saved[kind];
    const valid = id === UPLOAD ? Boolean(uploads[kind]) : manifest[kind].some(entry => entry.id === id);
    return [kind, valid ? id : manifest[kind][0].id];
  }));

  const defaults = () => Object.fromEntries(KINDS.map(kind => [kind, manifest[kind][0].id]));
  function entryFor(kind, id) {
    if (id === UPLOAD && uploads[kind]) return { id: UPLOAD, label: 'Yüklenen görsel', upload: uploads[kind] };
    return resolveEntry(kind, manifest[kind].find(entry => entry.id === id) ?? manifest[kind][0]);
  }

  function applyKind(kind) {
    const entry = entryFor(kind, selection[kind]);
    const previous = live[kind];
    let files = entry.files, upload = null;
    if (entry.upload) { upload = entry.upload; files = blobUrls(kind, upload); live[kind] = Object.values(files); }
    else live[kind] = [];
    current[kind] = entry.id;
    if (kind === 'logo') applyLogo(entry, files, upload);
    else if (kind === 'umay') applyUmay(files);
    else applyBackground(files, upload);
    // Release the previous blob URLs once the new images have had a chance to load.
    if (previous.length) setTimeout(() => previous.forEach(url => URL.revokeObjectURL(url)), 1500);
  }
  function applyLogo(entry, files, upload) {
    // index.html paints the default preset's mark inline (data-preset) for first paint.
    const isDefault = !upload && Boolean(inlineMark) && entry.id === inlineMark.dataset.preset && entry.files.mark === FALLBACK.logo[0].files.mark;
    inlineMark?.toggleAttribute('hidden', !isDefault);   // SVGElement has no .hidden property
    headerMark.hidden = isDefault;
    const shown = upload ? { files: { mark: files.mark } } : entry;
    if (!isDefault) { renderMark(headerMark, shown); headerMark.classList.add('brand-mark-custom'); }
    document.querySelectorAll('[data-appearance-preview="logo"]').forEach(el => renderMark(el, shown, isDefault ? inlineMark : null));
    if (upload) {
      const svg = upload.type === 'image/svg+xml';
      setLink('favicon-primary', 'icon', svg ? files.mark : files.favicon32, svg ? 'image/svg+xml' : 'image/png', svg ? null : '32x32');
      setLink('favicon-fallback', 'icon', files.favicon32, 'image/png', '32x32');
      setLink('favicon-small', 'icon', files.favicon16, 'image/png', '16x16');
      setLink('apple-touch', 'apple-touch-icon', files.appleTouch);
    } else {
      // Presets with an SVG favicon use it; presets without one (e.g. the yurt, whose
      // pixel-snapped PNGs beat its detailed SVG at 16px) lead with the 32px PNG.
      const f = entry.files, svg = f.favicon && /\.svg$/i.test(f.favicon);
      const primary = f.favicon || f.favicon32 || f.mark;
      setLink('favicon-primary', 'icon', primary, svg ? 'image/svg+xml' : /\.png$/i.test(primary) ? 'image/png' : null, !f.favicon && f.favicon32 ? '32x32' : null);
      setLink('favicon-small', 'icon', f.favicon16 || null, 'image/png', '16x16');
      setLink('favicon-fallback', 'icon', f.faviconIco || (f.favicon ? f.favicon32 : null) || null, f.faviconIco ? null : 'image/png', f.faviconIco ? 'any' : '32x32');
      setLink('apple-touch', 'apple-touch-icon', f.appleTouch || FALLBACK.logo[0].files.appleTouch);
    }
  }
  function applyUmay(files) {
    document.querySelectorAll('.assistant-avatar, [data-appearance-preview="avatar"]').forEach((image) => {
      image.src = files.src64; image.srcset = `${files.src64} 1x, ${files.src64x2} 2x`;
    });
    document.querySelectorAll('.assistant-intro img').forEach((image) => {
      image.src = files.src256; image.srcset = `${files.src256} 1x, ${files.src512} 2x`;
    });
    const glyph = files.glyph && files.glyph !== DEFAULT_GLYPH ? files.glyph : null;
    document.querySelectorAll('#assistant-open > [data-icon], [data-appearance-preview="glyph"]').forEach((slot) => {
      slot.classList.toggle('glyph-mask', Boolean(glyph));
      if (glyph) { slot.replaceChildren(); slot.style.setProperty('--glyph', cssUrl(glyph)); }
      else { slot.style.removeProperty('--glyph'); slot.innerHTML = iconMarkup('umay'); }
    });
  }
  function applyBackground(files, upload) {
    const set = (name, value) => root.style.setProperty(name, value);
    if (upload) {
      const tile = upload.mode === 'tile';
      set('--bg-tile', cssUrl(files.image)); set('--bg-repeat', tile ? 'repeat' : 'no-repeat');
      set('--bg-size', tile ? 'auto' : 'cover'); set('--bg-position', tile ? '0 0' : 'center');
      set('--bg-vignette', cssUrl(FALLBACK.background[0].files.vignette));
      set('--bg-dim', tile ? 'rgb(24 24 20 / .6)' : 'rgb(24 24 20 / .72)');
    } else {
      set('--bg-tile', cssUrl(files.tile)); set('--bg-repeat', 'repeat'); set('--bg-size', 'auto'); set('--bg-position', '0 0');
      set('--bg-vignette', cssUrl(files.vignette)); set('--bg-dim', 'transparent');
    }
  }
  function applyAll() {
    for (const kind of KINDS) {
      try { applyKind(kind); }
      catch { selection[kind] = manifest[kind][0].id; try { applyKind(kind); } catch { /* Static defaults in index.html/CSS remain. */ } }
    }
    updateForm();
  }

  /* ---------------- Dialog UI ---------------- */
  function thumb(kind, entry) {
    const box = document.createElement('span');
    box.className = `preset-thumb thumb-${kind}`;
    const upload = entry.upload;
    const urls = upload ? blobUrls(kind, upload) : null;
    if (urls) box.dataset.blobs = Object.values(urls).join(' ');
    if (kind === 'logo') {
      const mark = document.createElement('span');
      renderMark(mark, upload ? { files: { mark: urls.mark } } : entry);
      box.append(mark);
    } else if (kind === 'umay') {
      const image = new Image();
      image.alt = ''; image.decoding = 'async'; image.loading = 'lazy';
      image.src = upload ? urls.src256 : entry.files.src256;
      box.append(image);
    } else {
      const tile = upload ? urls.image : entry.files.tile;
      box.style.setProperty('--thumb-tile', cssUrl(tile));
      box.style.setProperty('--thumb-size', upload && upload.mode !== 'tile' ? 'cover' : 'auto');
      box.style.setProperty('--thumb-vignette', cssUrl(upload ? FALLBACK.background[0].files.vignette : entry.files.vignette));
    }
    return box;
  }
  function tile(kind, entry) {
    const label = document.createElement('label');
    label.className = 'preset-tile';
    const input = document.createElement('input');
    input.type = 'radio'; input.name = `appearance-${kind}`; input.value = entry.id; input.className = 'tile-input';
    input.addEventListener('change', () => { if (input.checked) choose(kind, entry.id); });
    const caption = document.createElement('span');
    caption.className = 'preset-label';
    caption.textContent = entry.label;
    label.title = entry.upload ? entry.upload.name : entry.label;
    label.append(input, thumb(kind, entry), caption);
    return label;
  }
  function uploadTile(kind) {
    const label = document.createElement('label');
    label.className = 'preset-tile upload-tile';
    const input = document.createElement('input');
    input.type = 'file'; input.className = 'tile-input'; input.dataset.upload = kind;
    input.accept = 'image/png,image/jpeg,image/webp,image/svg+xml,.png,.jpg,.jpeg,.webp,.svg';
    input.setAttribute('aria-describedby', `appearance-${kind}-hint`);
    input.addEventListener('change', () => { const file = input.files?.[0]; input.value = ''; if (file) upload(kind, file); });
    const box = document.createElement('span');
    box.className = 'preset-thumb thumb-upload';
    box.innerHTML = iconMarkup('plus');
    const caption = document.createElement('span');
    caption.className = 'preset-label';
    caption.textContent = uploads[kind] ? 'Değiştir' : 'Yükle';
    label.append(input, box, caption);
    return label;
  }
  function renderGroup(kind) {
    const group = dialog?.querySelector(`[data-appearance-kind="${kind}"]`);
    if (!group) return;
    const grid = group.querySelector('.preset-grid');
    grid.querySelectorAll('[data-blobs]').forEach(el => el.dataset.blobs.split(' ').forEach(url => URL.revokeObjectURL(url)));
    const entries = manifest[kind].map(entry => resolveEntry(kind, entry));
    if (uploads[kind]) entries.push(entryFor(kind, UPLOAD));
    grid.replaceChildren(...entries.map(entry => tile(kind, entry)), uploadTile(kind));
    group.querySelector('[data-remove]').hidden = !uploads[kind];
  }
  function updateForm() {
    if (!dialog) return;
    for (const kind of KINDS) {
      dialog.querySelectorAll(`input[name="appearance-${kind}"]`).forEach((input) => { input.checked = input.value === current[kind]; });
    }
  }
  function error(kind, message = '') {
    const node = dialog?.querySelector(`[data-appearance-kind="${kind}"] .appearance-error`);
    if (node) { node.textContent = message; node.hidden = !message; }
  }
  async function choose(kind, id) {
    error(kind);
    selection = { ...selection, [kind]: id };
    applyKind(kind); updateForm();
    await save({ [kind]: id });
  }
  async function upload(kind, file) {
    const group = dialog.querySelector(`[data-appearance-kind="${kind}"]`);
    error(kind); group.setAttribute('aria-busy', 'true');
    try {
      const record = await prepareUpload(kind, file);
      await store.putUpload(kind, record);
      uploads[kind] = record;
      renderGroup(kind);
      await choose(kind, UPLOAD);
      dialog.querySelector(`input[name="appearance-${kind}"][value="${UPLOAD}"]`)?.focus();
    } catch (problem) {
      error(kind, problem?.message || 'Görsel yüklenemedi.');
    } finally { group.removeAttribute('aria-busy'); }
  }
  async function removeUpload(kind) {
    await store.deleteUpload(kind);
    delete uploads[kind];
    if (selection[kind] === UPLOAD) { selection = { ...selection, [kind]: manifest[kind][0].id }; await save({ [kind]: selection[kind] }); }
    applyKind(kind); renderGroup(kind); updateForm();
  }
  async function reset() {
    selection = defaults();
    KINDS.forEach(error);
    applyAll();
    await save(selection);
  }

  if (dialog) {
    dialog.querySelectorAll('[data-appearance-kind]').forEach((group) => {
      const kind = group.dataset.appearanceKind;
      group.querySelector('legend').textContent = TEXT[kind].legend;
      group.querySelector('.appearance-hint').textContent = TEXT[kind].hint;
      group.querySelector('.appearance-hint').id = `appearance-${kind}-hint`;
      group.querySelector('[data-remove]').addEventListener('click', () => removeUpload(kind));
    });
    $('appearance-open')?.addEventListener('click', () => { dialog.showModal(); dialog.querySelector('input:checked')?.focus(); });
    $('appearance-close')?.addEventListener('click', () => dialog.close());
    $('appearance-done')?.addEventListener('click', () => dialog.close());
    $('appearance-reset')?.addEventListener('click', reset);
  }

  const ready = (async () => {
    const [loaded, remembered] = await Promise.all([loadManifest(), store.getSelection().catch(() => ({}))]);
    manifest = loaded;
    saved = cleanSelection(remembered);
    const stored = await Promise.all(KINDS.map(kind => store.getUpload(kind).catch(() => null)));
    KINDS.forEach((kind, i) => { const record = stored[i]; if (record?.files && record.kind === kind) uploads[kind] = record; });
    selection = resolveSelection();
    KINDS.forEach(renderGroup);
    applyAll();
    root.dataset.appearanceReady = '';
  })().catch(() => { root.dataset.appearanceReady = 'fallback'; });
  // Then the shared copy from the server, unless the user already changed something.
  const synced = ready.then(async () => {
    const before = edits;
    const remote = store.sync ? await store.sync().catch(() => null) : null;
    if (remote && edits === before && JSON.stringify(remote) !== JSON.stringify(saved)) {
      saved = remote; selection = resolveSelection(); applyAll();
    }
    root.dataset.appearanceSynced = remote ? '' : 'local';
  });
  return { ready, synced, choose, reset };
}
