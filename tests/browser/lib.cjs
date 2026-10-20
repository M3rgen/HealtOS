// Shared helpers for tests/browser/*.cjs. Synthetic fixtures only; no live account records.
//
// Every check targets one frontend URL, HEALTHOS_TEST_URL (default http://127.0.0.1:5174, a
// Vite server you started yourself). `cd frontend && npm run test:browser` starts its own Vite
// server on a free port and sets it for you. Screenshots go to frontend/.test-output/ (gitignored)
// or HEALTHOS_TEST_OUTPUT. A check that cannot run here exits with SKIP_CODE after printing why.
const fs = require('node:fs');
const path = require('node:path');

const FRONTEND = path.join(__dirname, '../../frontend');

// Resolve Playwright from frontend/node_modules so a check also runs without NODE_PATH.
function requirePlaywright() {
  try { return require('playwright'); } catch {
    return require(require.resolve('playwright', { paths: [FRONTEND] }));
  }
}
const { chromium } = requirePlaywright();

const BASE = (process.env.HEALTHOS_TEST_URL || 'http://127.0.0.1:5174').replace(/\/$/, '');
const OUTPUT = process.env.HEALTHOS_TEST_OUTPUT || path.join(FRONTEND, '.test-output');
const SKIP_CODE = 77;

// Slow machines (CI runners have no GPU and few cores):
//   HEALTHOS_TEST_STEP_TIMEOUT_MS=60000  Playwright's per-action/wait timeout (default 30000)
// CI-like slowness on a fast machine (both optional):
//   HEALTHOS_TEST_CPU_THROTTLE=4   every page runs with Chromium CPU throttling (CDP rate 4)
//   HEALTHOS_TEST_SWIFTSHADER=1    software WebGL (ANGLE/SwiftShader), as on GPU-less CI runners
const STEP_TIMEOUT = Number(process.env.HEALTHOS_TEST_STEP_TIMEOUT_MS || 30_000);
const THROTTLE = Number(process.env.HEALTHOS_TEST_CPU_THROTTLE || 0);
const SWIFTSHADER = process.env.HEALTHOS_TEST_SWIFTSHADER === '1';
// The 3D body model loads and renders at its real size in every check, but its auto-rotation
// (on by default) redraws the WebGL scene every frame. Without a GPU that starves the page, and
// input events and rAF-polled waits queue behind each frame. No check tests the rotation, so it
// starts switched off, as a stored preference would; HEALTHOS_TEST_MODEL_MOTION=1 keeps it on.
const MODEL_MOTION = process.env.HEALTHOS_TEST_MODEL_MOTION === '1';

async function throttle(page) {
  if (THROTTLE <= 1) return;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: THROTTLE });
}

/** Playwright's bundled Chromium (headless shell); no system browser channel. */
async function launch(options = {}) {
  const args = [...(options.args || []), ...(SWIFTSHADER ? ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] : [])];
  const browser = await chromium.launch({ headless: true, ...options, args });
  // Apply per-page settings to every page, whether opened by browser.newPage or context.newPage.
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (contextOptions) => {
    const context = await newContext(contextOptions);
    context.setDefaultTimeout(STEP_TIMEOUT);
    if (!MODEL_MOTION) await context.addInitScript(() => {
      try { if (localStorage.getItem('healtos.motion') === null) localStorage.setItem('healtos.motion', 'false'); } catch { /* no storage */ }
    });
    const newPage = context.newPage.bind(context);
    context.newPage = async () => { const page = await newPage(); await throttle(page); return page; };
    return context;
  };
  browser.newPage = async (contextOptions) => {
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    // Like Playwright's own browser.newPage: closing the page closes its private context.
    const close = page.close.bind(page);
    page.close = async (closeOptions) => { await close(closeOptions); await context.close(); };
    return page;
  };
  return browser;
}

/** Path for a screenshot under the gitignored output directory. */
function shot(name) {
  fs.mkdirSync(OUTPUT, { recursive: true });
  return path.join(OUTPUT, name);
}

/** Polls a test-side condition (e.g. a mock's call counter) until it holds; fails after STEP_TIMEOUT. */
async function until(condition, message, timeout = STEP_TIMEOUT) {
  for (const deadline = Date.now() + timeout; !(await condition());) {
    if (Date.now() > deadline) throw new Error(`${message} (not met within ${timeout} ms)`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

function skip(reason) {
  console.log(`SKIP: ${reason}`);
  process.exit(SKIP_CODE);
}

/** Runs an async check; any thrown error fails the process. */
function run(main) {
  main().catch((error) => { console.error(error); process.exit(1); });
}

module.exports = { BASE, OUTPUT, SKIP_CODE, STEP_TIMEOUT, chromium, launch, shot, skip, run, until };
