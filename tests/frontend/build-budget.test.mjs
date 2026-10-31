import test from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { randomBytes } from 'node:crypto';
import { measureBudget, enforceBudget, performanceBudget, limits } from '../../frontend/build-budget.js';

const asset = source => ({ type: 'asset', source });
const chunk = (code, imports = [], modules = {}, css = [], dynamicImports = []) => ({
  type: 'chunk', code, imports, modules, dynamicImports, viteMetadata: { importedCss: new Set(css) },
});
function fixture() {
  return {
    'index.html': asset(`<script type="module" src="/assets/entry.js"></script>
      <link rel="modulepreload" href="/assets/left.js">
      <link rel="stylesheet" href="/assets/entry.css">
      <link rel="preload" href="/assets/font.woff2" as="font">`),
    'assets/entry.js': chunk('console.log("entry");', ['assets/left.js', 'assets/right.js'], {}, ['assets/entry.css'], ['assets/viewer.js']),
    'assets/left.js': chunk('export const left = 1;', ['assets/shared.js'], {}, ['assets/shared.css']),
    'assets/right.js': chunk('export const right = 2;', ['assets/shared.js'], {}, ['assets/shared.css']),
    'assets/shared.js': chunk('export const shared = "Türkçe";'),
    'assets/entry.css': asset('@import "./shared.css"; body { color: red; }'),
    'assets/shared.css': asset('html { background: url("image.webp"); }'),
    'assets/viewer.js': chunk('export const createBody = () => {};', ['assets/engine.js', 'assets/shared.js'],
      { '/project/frontend/src/body.js': {} }, ['assets/viewer.css']),
    'assets/engine.js': chunk('export const THREE = {};', [], { '/project/frontend/node_modules/three/build/three.module.js': {} }),
    'assets/viewer.css': asset('canvas { opacity: 1; }'),
    'assets/font.woff2': asset(Buffer.alloc(100000)),
    'assets/image.webp': asset(Buffer.alloc(200000)),
    'models/body.glb': asset(Buffer.alloc(300000)),
  };
}
const sum = (bundle, files, gzip = false) => files.reduce((total, file) => {
  const value = Buffer.from(bundle[file].code ?? bundle[file].source);
  return total + (gzip ? gzipSync(value, { level: 9 }).length : value.length);
}, 0);

test('static imports, modulepreloads and CSS form a transitive deduplicated graph', () => {
  const bundle = fixture(), report = measureBudget(bundle);
  assert.deepEqual(report.initial.js.files, ['assets/entry.js', 'assets/left.js', 'assets/right.js', 'assets/shared.js']);
  assert.deepEqual(report.initial.css.files, ['assets/entry.css', 'assets/shared.css']);
  for (const kind of ['js', 'css']) {
    const { files, raw, gzip } = report.initial[kind];
    assert.equal(raw, sum(bundle, files)); assert.equal(gzip, sum(bundle, files, true));
  }
  assert.equal(report.initial.js.files.filter(file => file === 'assets/shared.js').length, 1);
  for (const extension of ['woff2', 'webp', 'glb']) assert.ok(!JSON.stringify(report).includes(extension));
});

test('dynamic body and three.js are reported apart from initial and shared dependencies', () => {
  const bundle = fixture(), report = measureBudget(bundle);
  assert.deepEqual(report.deferred.js.files, ['assets/engine.js', 'assets/viewer.js']);
  assert.deepEqual(report.deferred.css.files, ['assets/viewer.css']);
  assert.equal(report.deferred.js.raw, sum(bundle, report.deferred.js.files)); assert.equal(report.deferred.js.gzip, sum(bundle, report.deferred.js.files, true));
  bundle['assets/viewer.js'].dynamicImports.push('assets/detail.js');
  bundle['assets/detail.js'] = chunk('console.log("detail");');
  assert.ok(measureBudget(bundle).deferred.js.files.includes('assets/detail.js'));
});

test('CSS url imports with quotes, whitespace and media conditions retain the same dependency', () => {
  for (const reference of ['url(shared.css)', 'url( "shared.css" )', "url('shared.css')"]) {
    const bundle = fixture();
    bundle['assets/entry.css'].source = `@import ${reference} screen;`;
    assert.deepEqual(measureBudget(bundle).initial.css.files, ['assets/entry.css', 'assets/shared.css']);
  }
});

test('exact gzip limits pass; one byte over JS or CSS fails with measurement and limit', () => {
  const report = measureBudget(fixture());
  const exact = { js: report.initial.js.gzip, css: report.initial.css.gzip };
  const messages = [];
  assert.doesNotThrow(() => enforceBudget(report, exact, message => messages.push(message)));
  assert.match(messages.join('\n'), /raw \d+ B; gzip \d+ B; gzip limit \d+ B \(enforced\)/);
  assert.match(messages.join('\n'), /body\/three.js \(excluded\)/);
  for (const kind of ['js', 'css']) {
    const budget = { ...exact, [kind]: exact[kind] - 1 };
    assert.throws(() => enforceBudget(report, budget, () => {}),
      new RegExp(`${kind.toUpperCase()} gzip budget exceeded: measured ${exact[kind]} B > limit ${budget[kind]} B`));
  }
});

test('fixed repository limits are numeric and enforcement uses gzip rather than raw bytes', () => {
  assert.deepEqual(limits, { js: 60000, css: 20000 });
  const bundle = fixture();
  bundle['assets/entry.js'].code = 'a'.repeat(1000000);
  const report = measureBudget(bundle);
  assert.ok(report.initial.js.raw > limits.js); assert.ok(report.initial.js.gzip < limits.js);
  assert.doesNotThrow(() => enforceBudget(report, limits, () => {}));
  for (const invalid of [undefined, null, '60000', -1, 0, 1.5, NaN, Infinity]) {
    assert.throws(() => enforceBudget(report, { ...limits, js: invalid }, () => {}), /positive integer/);
    assert.throws(() => enforceBudget(report, { ...limits, css: invalid }, () => {}), /positive integer/);
  }
});

test('body or three.js anywhere in the static closure fails even under budget', () => {
  for (const file of ['assets/viewer.js', 'assets/engine.js']) {
    const bundle = fixture();
    bundle['assets/shared.js'].imports.push(file);
    assert.throws(() => measureBudget(bundle), /body\/three.js entered the initial static graph/);
  }
  const bundle = fixture();
  bundle['index.html'].source += '<link rel="modulepreload" href="/assets/viewer.js">';
  assert.throws(() => measureBudget(bundle), /initial static graph/);
});

test('module identities detect a body merged into entry, without depending on chunk names', () => {
  const bundle = fixture();
  bundle['assets/entry.js'].modules['C:\\project\\frontend\\src\\body.js'] = {};
  assert.throws(() => measureBudget(bundle), /initial static graph: assets\/entry.js/);
  delete bundle['assets/entry.js'].modules['C:\\project\\frontend\\src\\body.js'];
  bundle['assets/entry.js'].modules['/project/node_modules/three/src/Three.js'] = {};
  assert.throws(() => measureBudget(bundle), /initial static graph/);
});

test('missing protected output, HTML or dependencies fail closed', () => {
  for (const file of ['index.html', 'assets/engine.js', 'assets/shared.js', 'assets/shared.css']) {
    const bundle = fixture(); delete bundle[file];
    assert.throws(() => measureBudget(bundle), /Budget:/);
  }
  const bundle = fixture();
  bundle['assets/engine.js'].modules = {};
  assert.throws(() => measureBudget(bundle), /output is missing/);
});

test('HTML attribute order, comments, repeated roots and circular imports do not alter totals', () => {
  const bundle = fixture(), expected = measureBudget(bundle);
  bundle['index.html'].source += `<!-- <script type="module" src="missing.js"></script> -->
    <LINK href="/assets/shared.js" rel="modulepreload">
    <SCRIPT src=/assets/entry.js type=module></SCRIPT>`;
  bundle['assets/shared.js'].imports.push('assets/entry.js');
  assert.deepEqual(measureBudget(bundle), expected);
});

test('external or missing emitted HTML dependencies cannot silently bypass the check', () => {
  for (const path of ['https://external.invalid/app.js', '/assets/missing.js']) {
    const bundle = fixture();
    bundle['index.html'].source += `<script type="module" src="${path}"></script>`;
    assert.throws(() => measureBudget(bundle), /Budget: (external|missing) dependency/);
  }
});

test('production plugin runs late and does not depend on the output directory', () => {
  for (const outDir of ['../app/static', '/tmp/alternative-output']) {
    const plugin = performanceBudget();
    assert.equal(plugin.apply, 'build'); assert.equal(plugin.enforce, 'post'); assert.equal(plugin.generateBundle.order, 'post');
    plugin.configResolved({ base: '/', build: { outDir } });
    const original = console.log;
    console.log = () => {};
    const emitted = [];
    try { plugin.generateBundle.handler.call({ emitFile: value => emitted.push(value) }, {}, fixture()); }
    finally { console.log = original; }
    assert.equal(emitted[0].fileName, 'performance-budget-report.json');
    assert.deepEqual(JSON.parse(emitted[0].source), { gzipLevel: 9, limits, ...measureBudget(fixture()) });
    const oversized = fixture();
    oversized['assets/entry.js'].code = randomBytes(100000).toString('hex');
    console.log = () => {};
    try { assert.throws(() => plugin.generateBundle.handler({}, oversized), /measured \d+ B > limit 60000 B/); }
    finally { console.log = original; }
  }
});
