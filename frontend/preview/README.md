# Preview harness (dev only)

Screenshots every HealthOS view at the target viewports against a running Vite
dev server, with every backend route answered by **synthetic** fixtures
(`fixtures.cjs`; no account data). It fails when the main page can scroll,
which is the top rule in `AGENTS.md`. Nothing here is part of `npm run build`.

## Run

```sh
cd frontend
npm install
npx playwright install chromium        # once per machine
npx vite --host 127.0.0.1 --port 5174  # in another terminal; no backend needed
node preview/shoot.cjs --out /tmp/healthos-shots
```

Options:

| flag | default | |
|---|---|---|
| `--out <dir>` | required | PNGs are written as `<W>x<H>-<view>.png` |
| `--base <url>` | `http://127.0.0.1:5174` | dev server to target |
| `--viewports a,b` | `1920x976,1440x900,1280x720,1024x650,390x844,320x700` | |
| `--views a,b` | all | `overview, history-drag, history-open, metric-history, calendar, umay-intro, umay, measurements, measurement-dialog, system, sports, sports-editor`, plus edge cases `appearance`, `appearance-preset` (lane C presets on the dashboard), `appearance-uploaded` (synthetic SVG logo + PNG avatar/background uploads), `notice` (sync error banner), `empty-overview`, `empty-history`, `empty-system`, `empty-sports` |
| `--reduced-motion` | off | emulates `prefers-reduced-motion: reduce` |

## Output

A table per view with `document.scrollingElement` `scrollHeight/innerHeight`
and `scrollWidth/innerWidth`. `scrollHeight` counts clipped content too, so an
`overflow:hidden` body does not hide a failure. The exit code is `1` if any
view overflows, a page error is thrown, a backend route is not mocked, or the
page tries to load anything from another origin (remote fonts/CDNs are blocked
and reported). `history-drag` drags the history handle part-way and keeps the
released height; `calendar` is reported as skipped where the date button is
hidden.

## Notes

- The clock is frozen at `NOW` in `fixtures.cjs` (2026-10-16 14:30 UTC), so
  the dates in the screenshots stay the same between runs.
- Umay runs on a mocked WebSocket that streams one Markdown answer.
- Uses Playwright's bundled Chromium in new-headless mode (`channel: 'chromium'`)
  so the WebGL body model renders on the GPU.
- If the frontend starts calling a new endpoint, add it to `mock()` in
  `shoot.cjs` and give it a fixture.
