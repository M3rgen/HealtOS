# Appearance presets (lane C)

These are drop-in entries for `presets/manifest.json` (lane D owns that file; lane C did not edit it). The schema matches what is already there: `{ id, label, builtin, files }`. File keys reuse lane D's names (`mark`, `markMono`, `favicon`, `src512`, `src256`, `src64`, `src64x2`, `glyph`, `tile`, `vignette`). The new keys are:
- logos: `lockup`, `lockupMono` (mark plus the "HealthOS" wordmark, 48 units tall), `favicon16`, `favicon32` (PNG test renders);
- backgrounds: `tileSize` and `css` (hints).

All SVGs are hand-authored, minified, and contain no rasters or `<text>`. The wordmark is Inter 4.1 (SIL OFL 1.1) converted to paths. Mono files use `currentColor`. Lockups follow the current header style: "Health" in Inter Bold #f8f8f8 and "OS" in Inter Light #f0c478 (the monogram uses SemiBold for both). Size them by height: 42px matches the current `.brand-mark`.

Provenance: concept boards were made with gpt-image-2 through Codex. Each chosen mark was then drawn by hand as SVG with `codex exec` (gpt-6.1-sol) and checked visually at 16, 32 and 256 px. There is no auto-tracing. Umay styles were made with gpt-image-2 using `umay/umay.webp`'s source as the identity reference. Backgrounds are procedural (numpy) and original. Everything is AI- or code-generated for HealthOS; there is no third-party art.

## Logos: recommended order (best first)

`current` is the same art as the existing builtin `healthos` entry. Keep `healthos` and optionally add its new lockup files; the `current/` folder exists so every concept has the same file set.

```json
[
  {"id":"dial","label":"Kadran","builtin":true,"files":{"mark":"/assets/presets/logo/dial/mark.svg","markMono":"/assets/presets/logo/dial/mark-mono.svg","lockup":"/assets/presets/logo/dial/lockup.svg","lockupMono":"/assets/presets/logo/dial/lockup-mono.svg","favicon":"/assets/presets/logo/dial/favicon.svg","favicon16":"/assets/presets/logo/dial/favicon-16.png","favicon32":"/assets/presets/logo/dial/favicon-32.png"}},
  {"id":"monogram-h","label":"Monogram","builtin":true,"files":{"mark":"/assets/presets/logo/monogram-h/mark.svg","markMono":"/assets/presets/logo/monogram-h/mark-mono.svg","lockup":"/assets/presets/logo/monogram-h/lockup.svg","lockupMono":"/assets/presets/logo/monogram-h/lockup-mono.svg","favicon":"/assets/presets/logo/monogram-h/favicon.svg","favicon16":"/assets/presets/logo/monogram-h/favicon-16.png","favicon32":"/assets/presets/logo/monogram-h/favicon-32.png"}},
  {"id":"umay-wings","label":"Umay kanatları","builtin":true,"files":{"mark":"/assets/presets/logo/umay-wings/mark.svg","markMono":"/assets/presets/logo/umay-wings/mark-mono.svg","lockup":"/assets/presets/logo/umay-wings/lockup.svg","lockupMono":"/assets/presets/logo/umay-wings/lockup-mono.svg","favicon":"/assets/presets/logo/umay-wings/favicon.svg","favicon16":"/assets/presets/logo/umay-wings/favicon-16.png","favicon32":"/assets/presets/logo/umay-wings/favicon-32.png"}},
  {"id":"yurt","label":"Otağ","builtin":true,"files":{"mark":"/assets/presets/logo/yurt/mark.svg","markMono":"/assets/presets/logo/yurt/mark-mono.svg","lockup":"/assets/presets/logo/yurt/lockup.svg","lockupMono":"/assets/presets/logo/yurt/lockup-mono.svg","favicon":"/assets/presets/logo/yurt/favicon.svg","favicon16Simple":"/assets/presets/logo/yurt/favicon-16-simple.svg","favicon16":"/assets/presets/logo/yurt/favicon-16.png","favicon32":"/assets/presets/logo/yurt/favicon-32.png","faviconIco":"/assets/presets/logo/yurt/favicon.ico","appleTouch":"/assets/presets/logo/yurt/apple-touch-icon.png"}},
  {"id":"current","label":"Nabız H (mevcut)","builtin":true,"files":{"mark":"/assets/presets/logo/current/mark.svg","markMono":"/assets/presets/logo/current/mark-mono.svg","lockup":"/assets/presets/logo/current/lockup.svg","lockupMono":"/assets/presets/logo/current/lockup-mono.svg","favicon":"/assets/presets/logo/current/favicon.svg","favicon16":"/assets/presets/logo/current/favicon-16.png","favicon32":"/assets/presets/logo/current/favicon-32.png"}},
  {"id":"pulse-h","label":"Canlı H","builtin":true,"files":{"mark":"/assets/presets/logo/pulse-h/mark.svg","markMono":"/assets/presets/logo/pulse-h/mark-mono.svg","lockup":"/assets/presets/logo/pulse-h/lockup.svg","lockupMono":"/assets/presets/logo/pulse-h/lockup-mono.svg","favicon":"/assets/presets/logo/pulse-h/favicon.svg","favicon16":"/assets/presets/logo/pulse-h/favicon-16.png","favicon32":"/assets/presets/logo/pulse-h/favicon-32.png"}},
  {"id":"balance","label":"Denge","builtin":true,"files":{"mark":"/assets/presets/logo/balance/mark.svg","markMono":"/assets/presets/logo/balance/mark-mono.svg","lockup":"/assets/presets/logo/balance/lockup.svg","lockupMono":"/assets/presets/logo/balance/lockup-mono.svg","favicon":"/assets/presets/logo/balance/favicon.svg","favicon16":"/assets/presets/logo/balance/favicon-16.png","favicon32":"/assets/presets/logo/balance/favicon-32.png"}}
]
```

| id | direction | idea | notes |
|---|---|---|---|
| dial | (f) wildcard | Amber core inside a three-arc ring; the top-right arc is mint (#48f8b0, the steps tint) | Reads best at 16px and looks like the dashboard itself |
| monogram-h | (d) monogram | An "H" made of two parenthesis-like curved stems and a bar: a protected centre, a breathing chest | Very legible at 16px. Uses a wordmark-led lockup (SemiBold) |
| umay-wings | (b) Umay | Two sheltering wings around a mint "life" dot | Most on-story (it ties to Umay). Slightly busier at 16px |
| yurt | (e) hearth/tent | Yurt with a crown ring (tunduk), radiating roof poles, a lattice wall and a mint door light | **Default ("Otağ").** Unmistakably Turkic. At 16px the lattice blurred, so there is now a dedicated small-size variant (below) |

**yurt small-size variant:** `favicon-16-simple.svg` is drawn on a 16-unit grid (`viewBox 0 0 16 16`). It keeps the dome roof outline, the hollow crown ring and the mint door, and drops the lattice X's and roof poles.
- **Pixel snapping:** 1px strokes sit on half-pixel centres, so the eave, walls and base land exactly on pixels. The crown ring is filled pixels and the door is 2×3 px; only the dome curve antialiases.
- **`favicon-16.png`:** now rendered from this variant.
- **`favicon.ico`:** rebuilt from the new 16px image plus the existing `favicon-32.png` (both frames verified pixel-identical to their sources).
- **`favicon.svg`:** unchanged, the detailed 32px+ version.
- **`apple-touch-icon.png`:** lane D's, untouched.

Suggested tags: `<link rel="icon" href="/assets/presets/logo/yurt/favicon.ico" sizes="16x16 32x32">` plus `<link rel="icon" type="image/svg+xml" href="/assets/presets/logo/yurt/favicon.svg">`. Browsers that support SVG favicons generally use the SVG when it is linked, so leave the SVG link out if you want the crisp 16px frame in tabs.
| current | — | The existing H stems with an ECG crossbar | Baseline for comparison |
| pulse-h | (a) evolution | Bolder H-pulse with a mint "live" dot | At 16px it can read as a "W" |
| balance | (c) body/vitality | Head over a cradle arc, with a violet (#827aff) sleep arc | Can read as a smiley. Ranked last |

Rejected during exploration: a tunduk lattice ring (read as a basketball), a first yurt (read as a food cloche), a hearth flame (weak at 16px) and a sun over pulse (generic). The exploration boards are in lane C's scratchpad.

## Umay avatar styles (same character: diadem bird, bird earrings, wings)

`painterly` is the same art as the builtin `umay` entry; it is copied here so all three styles share one layout. Each entry reuses the shared launcher glyph.

```json
[
  {"id":"painterly","label":"Umay · resim","builtin":true,"files":{"src512":"/assets/presets/umay/painterly/umay.webp","src256":"/assets/presets/umay/painterly/umay-256.webp","src64":"/assets/presets/umay/painterly/umay-64.webp","src64x2":"/assets/presets/umay/painterly/umay-64@2x.webp","glyph":"/assets/umay/umay-glyph.svg"}},
  {"id":"flat","label":"Umay · sade amblem","builtin":true,"files":{"src512":"/assets/presets/umay/flat/umay.webp","src256":"/assets/presets/umay/flat/umay-256.webp","src64":"/assets/presets/umay/flat/umay-64.webp","src64x2":"/assets/presets/umay/flat/umay-64@2x.webp","glyph":"/assets/umay/umay-glyph.svg"}},
  {"id":"engraved","label":"Umay · gravür madalyon","builtin":true,"files":{"src512":"/assets/presets/umay/engraved/umay.webp","src256":"/assets/presets/umay/engraved/umay-256.webp","src64":"/assets/presets/umay/engraved/umay-64.webp","src64x2":"/assets/presets/umay/engraved/umay-64@2x.webp","glyph":"/assets/umay/umay-glyph.svg"}}
]
```

- **Sizes:** src512 is 31–86 KB (the engraved hatching is the largest); src64 is about 2 KB.
- **Crops:** `src64`/`src64x2` are face-centred crops made for the 44px circle. The engraved medallion's ring fills the square edge to edge, so `src512`/`src256` look best with `border-radius:50%`.
- **Backgrounds:** painterly and flat sit on about #201f1a, which blends with `--surface`.

## Backgrounds (seamless tiles; the mean colour of each is exactly #181814 to within 0.3 of a level; contrast is 6–7 levels, about 2.5 %)

```json
[
  {"id":"topo","label":"Topografik çizgiler","builtin":true,"files":{"tile":"/assets/presets/background/topo/tile.webp","vignette":"/assets/bg/vignette.webp"},"tileSize":512,"css":"background:#181814 url(/assets/presets/background/topo/tile.webp) repeat"},
  {"id":"warm-mesh","label":"Sıcak degrade","builtin":true,"files":{"tile":"/assets/presets/background/warm-mesh/tile.webp","vignette":"/assets/bg/vignette.webp"},"tileSize":768,"css":"background:#181814 url(/assets/presets/background/warm-mesh/tile.webp) repeat"},
  {"id":"kilim","label":"Kilim deseni","builtin":true,"files":{"tile":"/assets/presets/background/kilim/tile.webp","vignette":"/assets/bg/vignette.webp"},"tileSize":240,"css":"background:#181814 url(/assets/presets/background/kilim/tile.webp) repeat"}
]
```

| id | what | size |
|---|---|---|
| topo | Faint warm contour lines from periodic noise; every 4th line is slightly brighter | 28.7 KB |
| warm-mesh | Very low-frequency warm/cool pools, Bayer-dithered so 1-level steps don't band. Lossless | 42.9 KB |
| kilim | Lozenge lattice with small ram's-horn hooks and dots (kilim/carpet geometry) | 1.4 KB |

`vignette` is optional; it is the existing `/assets/bg/vignette.webp`, applied on a fixed `body::before`. Render tiles at their natural pixel size (`background-size:auto`) so the 1-level detail stays crisp.
