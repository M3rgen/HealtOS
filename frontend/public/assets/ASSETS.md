# HealthOS custom assets (lane C)

Everything here is served by Vite from `/assets/...` (it lives in `frontend/public`). UI assets total about 190 KB. The `skin/` maps are a separate budget of 1.49 MB.
Palette: page #181814, surface #20201c, amber #f0c478, muted #b9b7ae, plus the existing metric tokens. Every SVG uses `currentColor`, so the existing CSS tokens color them.

Provenance key:
- **Codex SVG**: hand-authored SVG code written by OpenAI Codex CLI (`codex exec`, model gpt-6.1-sol). It was rendered and checked visually, iterated, then minified by a local script. No third-party icon set was copied. This is original work for the project.
- **gpt-image-2**: raster generated with Codex's built-in image tool (gpt-image-2), then resized and encoded to WebP locally with Pillow. Output is AI-generated and was made for this project; no third-party stock is included.
- **Procedural**: generated with numpy and Pillow (seeded noise and analytic gradients). Original.

## Icons: `icons/` (24×24, stroke 1.75, round caps/joins, `fill="none"`, `stroke="currentColor"`)

| Path | UI slot it replaces / targets | Size | Made with | Provenance |
|---|---|---|---|---|
| icons/steps.svg | `.steps-card .metric-icon` (`data-icon="shoe"`) | 286 B | Codex SVG | original |
| icons/sleep.svg | `.sleep-card .metric-icon` (`data-icon="moon"`) | 230 B | Codex SVG | original |
| icons/heart.svg | `.heart-card .metric-icon` (`data-icon="heart"`) | 275 B | Codex SVG | original |
| icons/hrv.svg | HRV column/metric (new; pulse with R-R interval arrow) | 261 B | Codex SVG | original |
| icons/spo2.svg | SpO₂ column/metric (new) | 316 B | Codex SVG | original |
| icons/workout.svg | Spor tab / workouts (new) | 310 B | Codex SVG | original |
| icons/height.svg | Profile "Boy" measurement (new) | 323 B | Codex SVG | original |
| icons/weight.svg | Profile "Kilo" measurement (new) | 271 B | Codex SVG | original |
| icons/body-fat.svg | Profile "Yağ oranı" (new) | 331 B | Codex SVG | original |
| icons/muscle.svg | Profile "Kas oranı" (new) | 416 B | Codex SVG | original |
| icons/sync.svg | `#sync-button`, `#assistant-reconnect` (`data-icon="sync"`) | 267 B | Codex SVG | original |
| icons/download.svg | `#export-button` "CSV indir" (`data-icon="download"`) | 257 B | Codex SVG | original |
| icons/csv.svg | Optional CSV file glyph (alternative to download) | 322 B | Codex SVG | original |
| icons/plus.svg | `#add-measurement` (`data-icon="plus"`) | 226 B | Codex SVG | original |
| icons/calendar.svg | `#header-date` / date picker trigger (new) | 672 B | Codex SVG | original |
| icons/status.svg | Sistem tab / service status (new) | 258 B | Codex SVG | original |
| icons/close.svg | `#assistant-close`, `#metric-history-close` (`data-icon="close"`) | 199 B | Codex SVG | original |
| icons/send.svg | `#assistant-send` (`data-icon="send"`) | 209 B | Codex SVG | original |
| icons/stop.svg | `#assistant-stop` (`data-icon="stop"`) | 216 B | Codex SVG | original |
| icons/chat-new.svg | `#assistant-new` (`data-icon="chat-new"`) | 275 B | Codex SVG | original |
| icons/chevron-{down,up,left,right}.svg | `.calendar-control` buttons (currently inline filled triangles) | ~194 B each | Codex SVG | original |
| icons/sprite.svg | All of the above as `<symbol id="i-<name>">`, for `<svg><use href="/assets/icons/sprite.svg#i-steps"/></svg>` | 4.1 KB | local script from the files above | original |

## Umay: `umay/`

| Path | UI slot | Size | Made with | Provenance |
|---|---|---|---|---|
| umay/umay.webp | 512×512 full emblem (face plus protective wings and a bird diadem). Use for the chat header or any large image | 39 KB | gpt-image-2. The prompt used the earlier `src/assets/umay.png` avatar (since removed) as a character reference, so identity stays the same | AI-generated for HealthOS |
| umay/umay-256.webp | 256×256 version of the above | 14 KB | same | same |
| umay/umay-64.webp | `.assistant-avatar` (44px circle, replaces `/src/assets/umay.png`). Face-centred crop so it reads at small size | 2 KB | same, cropped | same |
| umay/umay-64@2x.webp | 128px retina version of the 44px avatar (`srcset`) | 4.8 KB | same, cropped | same |
| umay/umay-alt.webp | Alternate 512px emblem: flatter, more poster-like style, braids, large gold wings. Kept as an option only | 30 KB | gpt-image-2, no reference | AI-generated for HealthOS |
| umay/umay-glyph.svg | `.assistant-launcher` glyph (replaces `data-icon="asclepius"`): a spread-wing heraldic bird, Turkic-ornament feel, drawn in currentColor (it renders in `--button-ink` on the amber button) | 354 B | Codex SVG (3 design rounds, variant J) | original |

## Brand: `brand/`

| Path | UI slot | Size | Made with | Provenance |
|---|---|---|---|---|
| brand/healthos-mark.svg | `.brand .brand-mark` inline mark (48 viewBox, stroke currentColor, 3.5). A refined, symmetric pulse: same H plus ECG concept | 241 B | Codex SVG | original, derived from the existing mark |
| brand/healthos-mark-amber.svg | Same geometry, hard-coded #f0c478 (for `<img>` / README) | 236 B | Codex SVG | same |
| brand/favicon.svg | `<link rel="icon" type="image/svg+xml">` (replaces `/healthos-mark.svg`) | 315 B | Codex SVG | same |
| brand/favicon-small.svg | Small-size tuned geometry (stroke 5.5), the source for 16/32 PNG and ICO | 312 B | Codex SVG | same |
| brand/favicon-16.png, favicon-32.png | PNG fallbacks | 0.4 / 0.7 KB | resvg render | same |
| brand/favicon.ico | 16/32/48 multi-size ICO | 3 KB | resvg plus Pillow | same |
| brand/apple-touch-icon.png | `<link rel="apple-touch-icon">` 180px, full-bleed (iOS masks corners itself) | 1.9 KB | resvg | same |
| brand/icon-192.png, icon-512.png | Web manifest icons | 2.8 / 8.8 KB | resvg | same |

## Ambient and 3D: `bg/`, `3d/`

| Path | UI slot | Size | Made with | Provenance |
|---|---|---|---|---|
| bg/grain-tile.webp | `body` background: an opaque 256px seamless tile. Mean colour is exactly #181814 (±2–4 levels of warm grain), so text contrast is unchanged | 31 KB | Procedural (periodic FFT-filtered noise), lossless WebP | original |
| bg/grain-overlay.webp | Alternative: a transparent 256px grain to layer over any colour (alpha ≤ 18/255) | 23 KB | Procedural | original |
| bg/vignette.webp | Soft edge darkening, 320×200 RGBA. Stretch it over the viewport (`background-size:100% 100%`) | 4.4 KB | Procedural | original |
| 3d/pedestal-glow.webp | 512×256 transparent elliptical amber glow with a faint rim, placed under the body model in `#body-model` | 4.8 KB | Procedural | original |

## Empty states: `empty/` (viewBox 160×120, stroke 1.75, currentColor; secondary lines use `stroke-opacity=.45`)

| Path | UI slot | Size | Made with | Provenance |
|---|---|---|---|---|
| empty/empty-no-data.svg | "Bu gün için veri yok": `.chart .empty-chart`, `.history-empty`, a day with no data | 500 B | Codex SVG | original |
| empty/empty-no-workouts.svg | "Bu ay antrenman yok": empty Spor/workouts month | 621 B | Codex SVG | original |
| empty/empty-google-disconnected.svg | "Google bağlı değil": unplugged connector, no Google branding | 613 B | Codex SVG | original |

## Skin micro-detail: `skin/` (tiling detail layer on the body material; seamless; one tile is about 2 cm of real skin)

| Path | UI slot | Size | Made with | Provenance |
|---|---|---|---|---|
| skin/skin-detail-normal.png | three.js detail **normalMap** on the body material (tangent space, **OpenGL +Y up**, three.js default). 1024², RGB. Encoded RMS tilt is about 12°. RG keep 7 bits and B 6 bits; error after renormalisation is under 1° | 989 KB | gpt-image-2 source (a flat-lit, top-down macro of skin), then a local numpy pipeline: high-pass, local-contrast equalisation, separable variance-preserving half-offset blend for seamlessness, then periodic central-difference normals | AI-generated source made for HealthOS. No scanned or third-party data |
| skin/skin-detail-roughness.webp | three.js detail **roughnessMap** (G channel is read; the image is grayscale). 512², mean exactly 0.50 (range ≈0.43–0.57). Furrows and pores are slightly rougher. Lossless WebP | 129 KB | Derived from the height map plus a soft periodic noise | same |
| skin/skin-pores-height.png | Height/bump reference (`bumpMap` fallback, or for Blender). 1024² grayscale, mean 0.5, pores and furrows dark. Quantised to 39 levels to fit the budget; the normal map was computed from full precision | 370 KB | same pipeline | same |

**Seamlessness:** the wrap-edge pixel difference matches the interior neighbour difference on both axes (normal: 3.23 vs 3.25 in x, 4.09 vs 4.06 in y). The 3×3 tiling and 1:1 seam-crossing crops were checked visually.

**Recommended three.js use:** `wrapS = wrapT = RepeatWrapping`. Set `colorSpace = NoColorSpace` on all three textures (they are data, not colour). Keep mipmaps on, with anisotropy of 4–8.
- **Repeat:** physically, one tile is about 2 cm, so a 1.75 m body on a single 0–1 (2048²) UV atlas would need about 90 repeats. At the dashboard's model size (about 350–700 px tall) that is sub-pixel and mips away to nothing. Use **about 24–32 repeats per UV unit**, which puts one tile at about 6–7 cm on the body. Go up to about 48 only for close-up zoom.
- **Normal strength:** `normalScale ≈ (0.35, 0.35)` for a subtle look, up to 0.6 for a visible skin grain. The detail must be combined with lane A's baked normal map (for example a UDN/whiteout blend in `onBeforeCompile`, or a second UV set), not substituted for it.
- **Roughness:** multiply the detail roughness by about 2× around the base value (detail 0.5 means "no change") so the base ORM roughness stays authoritative.

## Integration notes

```css
/* Ambient page background: keeps #181814 as the fallback colour */
body{background:#181814 url(/assets/bg/grain-tile.webp) repeat}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:url(/assets/bg/vignette.webp) center/100% 100% no-repeat}
/* The pseudo-element is position:fixed, so it can never add page scroll (AGENTS.md: no page scrolling). */

/* Empty states */
.empty-illustration{width:160px;height:120px;color:var(--amber)}
```

- **Icons:** the viewBox changes from the current `0 0 40 40` / stroke 2.2 in `icons.js` to `0 0 24 24` / stroke 1.75. Existing CSS sizes (`.metric-icon` 34px, `.button-icon` 20px, `.assistant-icon` padding 8px) work unchanged. `.metric-top svg{stroke-width:1.8}` overrides the attribute in CSS. Drop that rule, or set it to 1.75, so the set stays consistent. Inner markup can be pasted into the `paths` map in `icons.js` after changing the wrapper viewBox to 24.
- **Launcher:** `umay-glyph.svg` replaces `asclepius`. The launcher has `color:var(--button-ink)`, so the glyph renders dark on amber.
- **Avatar:** `<img class="assistant-avatar" src="/assets/umay/umay-64.webp" srcset="/assets/umay/umay-64.webp 1x, /assets/umay/umay-64@2x.webp 2x" width="44" height="44" alt="">`. Use `umay-256.webp` / `umay.webp` for any larger header use. The WebP background is ~#201f1a, which blends with the `--surface` (#20201c) dialog.
- **Favicons:** `<link rel="icon" href="/assets/brand/favicon.svg" type="image/svg+xml"><link rel="icon" href="/assets/brand/favicon.ico" sizes="any"><link rel="apple-touch-icon" href="/assets/brand/apple-touch-icon.png">`.
- **Pedestal:** absolutely position it under the model's feet inside `#body-model` (for example `bottom:4%; width:70%; left:15%`) with `pointer-events:none`. It is decorative only.
