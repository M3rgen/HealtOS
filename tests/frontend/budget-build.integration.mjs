// Real Vite builds exercise the same config used by npm, direct CLI, CI and preview tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, openSync, closeSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { build } from '../../frontend/node_modules/vite/dist/node/index.js';

const frontend = fileURLToPath(new URL('../../frontend/', import.meta.url));
const productionConfig = path.join(frontend, 'vite.config.js');
const viteCLI = path.join(frontend, 'node_modules/vite/bin/vite.js');
function fixture(t) {
  const output = path.join(frontend, '.test-output');
  mkdirSync(output, { recursive: true });
  const root = mkdtempSync(path.join(output, 'budget-build-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(path.join(root, 'src'));
  writeFileSync(path.join(root, 'index.html'), '<script type="module" src="/src/main.js"></script>');
  writeFileSync(path.join(root, 'src/main.js'), 'import "./style.css"; document.onclick = () => import("./body.js");');
  writeFileSync(path.join(root, 'src/style.css'), 'body { color: red; }');
  writeFileSync(path.join(root, 'src/body.js'), 'import { Vector3 } from "three"; console.log(new Vector3(1, 2, 3));');
  return root;
}
function cli(root, outDir) {
  const logfile = path.join(root, 'build.log'), fd = openSync(logfile, 'w');
  let result;
  try {
    result = spawnSync(process.execPath, [viteCLI, 'build', root, '--config', productionConfig,
      '--outDir', outDir, '--emptyOutDir'], {
      cwd: frontend, stdio: ['ignore', fd, fd], timeout: 120000,
    });
  } finally { closeSync(fd); }
  assert.ifError(result.error);
  return { ...result, log: readFileSync(logfile, 'utf8') };
}

test('programmatic build and direct CLI enforce production config in alternative output directories', async t => {
  const root = fixture(t);
  const result = await build({ root, configFile: productionConfig, logLevel: 'silent',
    build: { outDir: path.join(root, 'programmatic-dist'), emptyOutDir: true } });
  assert.ok(result.output.some(item => item.fileName === 'index.html'));
  const compiled = cli(root, path.join(root, 'ci-alternative-dist'));
  assert.equal(compiled.status, 0, compiled.log);
  assert.match(compiled.log, /initial JS: raw \d+ B; gzip \d+ B; gzip limit 60000 B \(enforced\)/);
  assert.match(compiled.log, /initial CSS: raw \d+ B; gzip \d+ B; gzip limit 20000 B \(enforced\)/);
  assert.match(compiled.log, /deferred JS:.*body\/three.js \(excluded\)/);
});

for (const kind of ['JS', 'CSS']) {
  test(`controlled ${kind} overflow fails a real production CLI build with alternative outDir`, t => {
    const root = fixture(t), noise = randomBytes(100000).toString('hex');
    const file = path.join(root, kind === 'JS' ? 'src/main.js' : 'src/style.css');
    const padding = kind === 'JS' ? `\nconsole.log(${JSON.stringify(noise)});` : `\nbody { --budget: ${noise}; }`;
    writeFileSync(file, readFileSync(file, 'utf8') + padding);
    const result = cli(root, path.join(root, 'oversized-dist'));
    assert.notEqual(result.status, 0, result.log);
    assert.match(result.log, new RegExp(`Initial ${kind} gzip budget exceeded: measured \\d+ B > limit ${kind === 'JS' ? 60000 : 20000} B`));
  });
}

test('a real production build rejects a static import of body and three.js', t => {
  const root = fixture(t);
  writeFileSync(path.join(root, 'src/main.js'), 'import "./body.js"; import "./style.css";');
  const result = cli(root, path.join(root, 'static-dist'));
  assert.notEqual(result.status, 0, result.log);
  assert.match(result.log, /body\/three.js entered the initial static graph/);
});
