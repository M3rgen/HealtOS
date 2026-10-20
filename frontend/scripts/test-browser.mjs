#!/usr/bin/env node
// Runs every browser check in tests/browser against a Vite server this script starts on a free
// port. All backend routes are answered by synthetic fixtures inside each check; the Vite proxy
// points at a closed local port, so a running HealthOS backend (and its archive) is never reached.
//
//   npm run test:browser                      # build into .test-output/dist, serve with vite preview
//   npm run test:browser -- --dev             # vite dev server instead (no build step)
//   npm run test:browser -- --only job-csrf,appearance
//   HEALTHOS_TEST_URL=http://127.0.0.1:5174 npm run test:browser   # use a server you started
//
// Each check prints PASS/INFO lines; exit code 0 = pass, 77 = skip, anything else = fail.
// Screenshots land in frontend/.test-output/ (gitignored). Exits non-zero if any check fails.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TESTS = path.resolve(FRONTEND, '../tests/browser');
const OUTPUT = process.env.HEALTHOS_TEST_OUTPUT || path.join(FRONTEND, '.test-output');
const SKIP_CODE = 77;
const TIMEOUT_MS = 240_000;

function options() {
  const out = { dev: false, only: null };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const [key, inline] = argv[i].split('=');
    if (key === '--dev') out.dev = true;
    else if (key === '--preview') out.dev = false;
    else if (key === '--only') out.only = (inline ?? argv[++i]).split(',').map((name) => name.replace(/\.cjs$/, ''));
    else if (key === '--help' || key === '-h') {
      console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('\n').slice(1, 12).join('\n'));
      process.exit(0);
    } else throw new Error(`Unknown argument ${argv[i]}`);
  }
  return out;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function stubBackend() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((request, response) => {
      response.writeHead(503, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ detail: `browser test: ${request.method} ${request.url} is not mocked` }));
    });
    server.on('upgrade', (request, socket) => socket.destroy());
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

async function startServer(dev) {
  const vite = await import('vite');
  const port = await freePort();
  // Stand-in backend: unmocked API calls get a quick 503 instead of reaching a real backend.
  const backend = await stubBackend();
  const dead = `http://127.0.0.1:${backend.address().port}`;
  const proxy = {
    '/api': { target: dead, ws: true },
    '/health': dead,
    '/sync': dead,
    '/google-health': dead,
  };
  const common = {
    configFile: path.join(FRONTEND, 'vite.config.js'),
    root: FRONTEND,
    cacheDir: path.join(OUTPUT, 'vite-cache'),
    logLevel: 'warn',
  };
  const listen = { host: '127.0.0.1', port, strictPort: true, proxy };
  if (dev) {
    const server = await vite.createServer({ ...common, server: listen });
    await server.listen();
    return { url: `http://127.0.0.1:${port}`, close: () => server.close(), mode: 'vite dev' };
  }
  const outDir = path.join(OUTPUT, 'dist');
  const started = Date.now();
  await vite.build({ ...common, build: { outDir, emptyOutDir: true } });
  console.log(`built ${path.relative(FRONTEND, outDir)} in ${((Date.now() - started) / 1000).toFixed(1)} s`);
  const server = await vite.preview({ ...common, build: { outDir }, preview: listen });
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((resolve) => server.httpServer.close(resolve)),
    mode: 'vite preview',
  };
}

function runCheck(file, url) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [file], {
      cwd: FRONTEND,
      env: { ...process.env, HEALTHOS_TEST_URL: url, HEALTHOS_TEST_OUTPUT: OUTPUT },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const name = path.basename(file, '.cjs');
    let reason = '';
    const relay = (stream, target) => {
      let rest = '';
      stream.on('data', (chunk) => {
        const lines = (rest + chunk).split('\n');
        rest = lines.pop();
        for (const line of lines) {
          if (line.startsWith('SKIP: ')) reason = line.slice(6);
          target.write(`  [${name}] ${line}\n`);
        }
      });
      stream.on('end', () => { if (rest) target.write(`  [${name}] ${rest}\n`); });
    };
    relay(child.stdout, process.stdout);
    relay(child.stderr, process.stderr);
    const timer = setTimeout(() => { reason = `timed out after ${TIMEOUT_MS / 1000} s`; child.kill('SIGKILL'); }, TIMEOUT_MS);
    child.on('close', (code) => {
      clearTimeout(timer);
      const result = code === 0 ? 'pass' : code === SKIP_CODE ? 'skip' : 'fail';
      resolve({ name, result, reason, seconds: (Date.now() - started) / 1000 });
    });
  });
}

const opt = options();
const files = fs.readdirSync(TESTS)
  .filter((name) => name.endsWith('.cjs') && name !== 'lib.cjs')
  .filter((name) => !opt.only || opt.only.includes(name.replace(/\.cjs$/, '')))
  .sort()
  .map((name) => path.join(TESTS, name));
if (opt.only) {
  const missing = opt.only.filter((name) => !files.some((file) => path.basename(file, '.cjs') === name));
  if (missing.length) throw new Error(`Unknown checks: ${missing.join(', ')}`);
}
fs.mkdirSync(OUTPUT, { recursive: true });

const external = process.env.HEALTHOS_TEST_URL;
const server = external ? { url: external, close: async () => {}, mode: 'HEALTHOS_TEST_URL' } : await startServer(opt.dev);
console.log(`browser checks against ${server.url} (${server.mode})`);
const results = [];
try {
  for (const file of files) {
    console.log(`\n▶ ${path.basename(file)}`);
    results.push(await runCheck(file, server.url));
  }
} finally {
  await server.close();
}

console.log('\nbrowser checks:');
for (const r of results) {
  console.log(`  ${r.result.toUpperCase().padEnd(5)} ${r.name.padEnd(22)} ${r.seconds.toFixed(1).padStart(6)} s${r.reason ? `  ${r.reason}` : ''}`);
}
const count = (result) => results.filter((r) => r.result === result).length;
console.log(`${count('pass')} passed, ${count('fail')} failed, ${count('skip')} skipped · screenshots: ${OUTPUT}`);
process.exit(count('fail') ? 1 : 0);
