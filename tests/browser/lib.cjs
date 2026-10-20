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

/** Playwright's bundled Chromium (headless shell); no system browser channel. */
const launch = (options = {}) => chromium.launch({ headless: true, ...options });

/** Path for a screenshot under the gitignored output directory. */
function shot(name) {
  fs.mkdirSync(OUTPUT, { recursive: true });
  return path.join(OUTPUT, name);
}

function skip(reason) {
  console.log(`SKIP: ${reason}`);
  process.exit(SKIP_CODE);
}

/** Runs an async check; any thrown error fails the process. */
function run(main) {
  main().catch((error) => { console.error(error); process.exit(1); });
}

module.exports = { BASE, OUTPUT, SKIP_CODE, chromium, launch, shot, skip, run };
