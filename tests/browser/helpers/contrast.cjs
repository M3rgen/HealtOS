// Read Chromium's 8-bit RGB/RGBA PNG output with Node's standard library.
// Sampling the painted page includes gradients, uploaded images and alpha layers.
const assert = require('node:assert/strict');
const { inflateSync } = require('node:zlib');
function pixels(png) {
  let width, height, channels, raw; const chunks = [];
  for (let at = 8; at < png.length;) {
    const length = png.readUInt32BE(at), type = png.toString('ascii', at + 4, at + 8), data = png.subarray(at + 8, at + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      assert.equal(data[8], 8); assert.equal(data[12], 0);
      assert([2, 6].includes(data[9])); channels = data[9] === 2 ? 3 : 4;
    }
    if (type === 'IDAT') chunks.push(data);
    at += length + 12;
  }
  raw = inflateSync(Buffer.concat(chunks));
  const stride = width * channels, decoded = Buffer.alloc(stride * height);
  const paeth = (a, b, c) => {
    const p = a + b - c, x = Math.abs(p - a), y = Math.abs(p - b), z = Math.abs(p - c);
    return x <= y && x <= z ? a : y <= z ? b : c;
  };
  for (let y = 0; y < height; y++) {
    const source = y * (stride + 1), target = y * stride, filter = raw[source];
    assert(filter <= 4);
    for (let x = 0; x < stride; x++) {
      const left = x >= channels ? decoded[target + x - channels] : 0;
      const above = y ? decoded[target + x - stride] : 0;
      const diagonal = y && x >= channels ? decoded[target + x - stride - channels] : 0;
      const predict = [0, left, above, Math.floor((left + above) / 2), paeth(left, above, diagonal)][filter];
      decoded[target + x] = (raw[source + x + 1] + predict) & 255;
    }
  }
  return (x, y) => {
    assert(x >= 0 && x < width && y >= 0 && y < height, `pixel outside viewport: ${x},${y}`);
    const at = (Math.floor(y) * width + Math.floor(x)) * channels;
    return [...decoded.subarray(at, at + 3)];
  };
}
const luminance = rgb => rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
  .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
const ratio = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
const composite = (rgba, bg) => bg.map((v, i) => rgba[i] * rgba[3] + v * (1 - rgba[3]));

// Let pending layout work (ResizeObserver redraws, e.g. the metric history chart) finish so
// the sampled geometry and the screenshot describe the same frame.
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function auditText(page, label) {
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
  const samples = await page.evaluate(() => {
    const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d');
    const color = value => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1);
      const data = [...ctx.getImageData(0, 0, 1, 1).data]; return [...data.slice(0, 3), data[3] / 255]; };
    const samples = [];
    const add = (element, rects, text, value, threshold, extra = {}) => {
      if (element.closest('[hidden], [inert], :disabled, [aria-disabled="true"], .sr-only')) return;
      const style = getComputedStyle(element);
      if (style.visibility !== 'visible') return;
      const fg = color(value);
      for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) fg[3] *= Number(getComputedStyle(ancestor).opacity);
      if (!fg[3]) return;
      const owner = element.closest('.metric-card') || element;
      const clip = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
      for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
        const s = getComputedStyle(ancestor), r = ancestor.getBoundingClientRect();
        if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) {
          clip.left = Math.max(clip.left, r.left + ancestor.clientLeft);
          clip.right = Math.min(clip.right, r.left + ancestor.clientLeft + ancestor.clientWidth);
        }
        if (/(auto|scroll|hidden|clip)/.test(s.overflowY)) {
          clip.top = Math.max(clip.top, r.top + ancestor.clientTop);
          clip.bottom = Math.min(clip.bottom, r.top + ancestor.clientTop + ancestor.clientHeight);
        }
      }
      const points = [];
      for (const rect of rects) {
        for (const part of [.25, .5, .75]) {
          const x = Math.floor(rect.left + rect.width * part), y = Math.floor(rect.top + rect.height / 2);
          if (x < clip.left || y < clip.top || x >= clip.right || y >= clip.bottom) continue;
          // Glyphs partly covered by another layer (e.g. a word half under the floating Umay
          // panel and its shadow) are occluded, not low-contrast: the column must be clear.
          const clear = [y, rect.top + rect.height / 4, rect.top + rect.height * 3 / 4].every(at => {
            const hit = document.elementFromPoint(x, at);
            return hit && (owner.contains(hit) || hit.contains(element));
          });
          if (clear) points.push([x, y]);
        }
      }
      if (points.length) samples.push({text: text.trim().slice(0, 70), fg, threshold, points, ...extra});
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode, el = node.parentElement;
      if (!node.textContent.trim() || el.closest('script, style, option, svg')) continue;
      const style = getComputedStyle(el), size = parseFloat(style.fontSize);
      const range = document.createRange(); range.selectNodeContents(node);
      add(el, [...range.getClientRects()], node.textContent, style.color,
        size >= 24 || (size >= 18.667 && Number(style.fontWeight) >= 700) ? 3 : 4.5);
    }
    for (const text of document.querySelectorAll('svg text'))
      add(text, [...text.getClientRects()], text.textContent, getComputedStyle(text).fill, 4.5);
    for (const input of document.querySelectorAll('input:not(.tile-input), select, textarea')) {
      const style = getComputedStyle(input), rect = input.getBoundingClientRect();
      // Native checkboxes have no text; transparent month text is represented by its overlay label.
      if (['checkbox', 'file', 'radio'].includes(input.type)) continue;
      const placeholder = input.placeholder && !input.value;
      // Chromium paints the keyboard-selected segment of a focused date/time field with the
      // system SelectedItemText on SelectedItem (authors cannot restyle it), not the field colour.
      let segment;
      if (input === document.activeElement && /^(date|datetime-local|month|time|week)$/.test(input.type)) {
        const probe = document.body.appendChild(document.createElement('span'));
        probe.style.cssText = 'color:SelectedItemText;background-color:SelectedItem';
        segment = {fg: color(getComputedStyle(probe).color), bg: color(getComputedStyle(probe).backgroundColor)};
        probe.remove();
      }
      add(input, [{left: rect.left + 12, top: rect.top, width: Math.min(30, rect.width - 24), height: rect.height}],
        input.id || input.name, placeholder ? getComputedStyle(input, '::placeholder').color : style.color, 4.5, {segment});
    }
    return samples;
  });
  // Hide only glyph paint. currentColor backgrounds/borders, layout, images and
  // transparency retain the original CSS cascade. No approximation of a flat background.
  const hide = await page.addStyleTag({content: '*{-webkit-text-fill-color:transparent!important;text-shadow:none!important}svg text{fill:transparent!important}'});
  let painted;
  try { painted = pixels(await page.screenshot()); } finally { await hide.evaluate(el => el.remove()); }
  assert(samples.length > 5, `${label}: no visible text audited`);
  const failures = [], ratios = [];
  for (const sample of samples) {
    const minimum = Math.min(...sample.points.map(([x, y]) => {
      const bg = painted(x, y);
      const selected = sample.segment && bg.every((v, i) => Math.abs(v - sample.segment.bg[i]) < 3);
      const fg = selected ? sample.segment.fg : sample.fg;
      return ratio(composite(fg, bg), bg);
    }));
    ratios.push(minimum);
    if (minimum < sample.threshold) failures.push(`${sample.text}: ${minimum.toFixed(2)} < ${sample.threshold}`);
  }
  assert.deepEqual(failures, [], `${label}: painted text contrast`);
  console.log(`PASS ${label}: ${samples.length} text samples, minimum ${Math.min(...ratios).toFixed(2)}:1`);
}

async function auditFocus(page, label) {
  const focus = await page.evaluate(() => {
    const active = document.activeElement;
    const el = active.matches('.tile-input') ? active.closest('.preset-tile') : active;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.fillStyle = s.outlineColor; ctx.fillRect(0, 0, 1, 1);
    const inset = -parseFloat(s.outlineOffset) - parseFloat(s.outlineWidth) / 2;
    return {color: [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3),
      width: parseFloat(s.outlineWidth), visible: active.matches(':focus-visible'),
      points: [[r.left + inset, r.top + r.height / 2], [r.right - inset, r.top + r.height / 2],
        [r.left + r.width / 2, r.top + inset], [r.left + r.width / 2, r.bottom - inset]]};
  });
  assert(focus.visible && focus.width >= 2, `${label}: keyboard focus must have a visible indicator`);
  const shown = pixels(await page.screenshot());
  const hide = await page.addStyleTag({content: '*{outline:none!important}'});
  let beneath;
  try { beneath = pixels(await page.screenshot()); } finally { await hide.evaluate(el => el.remove()); }
  for (const [x, y] of focus.points) {
    assert(shown(x, y).every((v, i) => Math.abs(v - focus.color[i]) < 25), `${label}: ring clipped/covered at ${x},${y}`);
    assert(ratio(focus.color, beneath(x, y)) >= 3, `${label}: focus indicator contrast < 3:1`);
  }
}

async function auditBorders(page, selector) {
  const borders = await page.locator(selector).evaluateAll(elements => elements.filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 10 && r.height > 10 && r.left >= 2 && r.right < innerWidth - 2 && r.top >= 0 && r.bottom <= innerHeight;
  }).map(el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.fillStyle = s.borderLeftColor; ctx.fillRect(0, 0, 1, 1);
    const color = [...ctx.getImageData(0, 0, 1, 1).data];
    return {name: el.id || el.className, width: parseFloat(s.borderLeftWidth),
      color: [...color.slice(0, 3), color[3] / 255], x: r.left, y: r.top + r.height / 2};
  }));
  assert(borders.length, `no control borders measured: ${selector}`);
  const painted = pixels(await page.screenshot());
  for (const border of borders) {
    const inside = painted(border.x + border.width + 2, border.y), outside = painted(border.x - 2, border.y);
    const stroke = composite(border.color, inside);
    // WCAG 1.4.11: an outlined control is identified by its stroke, which must stand out from
    // both its own field and the surroundings. A filled control is identified by its fill, so
    // there the fill (and the stroke painted at its edge) must stand out from the surroundings;
    // a stroke matching its own fill is part of that boundary, not a separate indicator.
    const outlined = Math.min(ratio(stroke, inside), ratio(stroke, outside));
    const filled = Math.min(ratio(inside, outside), ratio(stroke, outside));
    assert(border.width >= 1 && Math.max(outlined, filled) >= 3,
      `${border.name}: control boundary contrast < 3:1`);
  }
}

module.exports = { auditText, auditFocus, auditBorders, ratio, pixels };
