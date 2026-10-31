import { gzipSync } from 'node:zlib';
import { readFileSync } from 'node:fs';

export const limits = JSON.parse(readFileSync(new URL('./performance-budget.json', import.meta.url)));
const bytes = item => Buffer.from(item.type === 'chunk' ? item.code : item.source);
const bodyModule = id => /\/src\/body\.js$/.test(id.replaceAll('\\', '/'));
const threeModule = id => /\/node_modules\/three\//.test(id.replaceAll('\\', '/'));

// Inspect emitted HTML and the bundler's import metadata, never hashed name patterns or outDir.
export function measureBudget(bundle, base = '/') {
  const origin = 'https://build.invalid';
  const baseURL = new URL(base, origin);
  const resolve = (reference, from) => {
    const url = new URL(reference, new URL(from, baseURL));
    if (url.origin !== baseURL.origin || !url.pathname.startsWith(baseURL.pathname))
      throw new Error(`Budget: external dependency ${reference}`);
    const file = decodeURIComponent(url.pathname.slice(baseURL.pathname.length));
    if (!bundle[file]) throw new Error(`Budget: missing dependency ${file}`);
    return file;
  };
  const visit = (roots, dynamic = false) => {
    const seen = new Set();
    function walk(file) {
      if (seen.has(file)) return;
      const item = bundle[file];
      if (!item) throw new Error(`Budget: missing dependency ${file}`);
      seen.add(file);
      if (item.type === 'chunk') {
        for (const dependency of item.imports) walk(dependency);
        for (const css of item.viteMetadata?.importedCss ?? []) walk(css);
        if (dynamic) for (const dependency of item.dynamicImports) walk(dependency);
      } else if (file.endsWith('.css')) {
        const css = bytes(item).toString().replace(/\/\*[\s\S]*?\*\//g, '');
        const imports = /@import\s+(?:url\(\s*(?:"([^"]+)"|'([^']+)'|([^\s)]+))\s*\)|"([^"]+)"|'([^']+)')/gi;
        for (const match of css.matchAll(imports)) {
          const reference = match.slice(1).find(value => value !== undefined);
          walk(resolve(reference, file));
        }
      }
    }
    for (const root of roots) walk(root);
    return seen;
  };
  const roots = [];
  for (const [file, item] of Object.entries(bundle)) {
    if (!file.endsWith('.html')) continue;
    const html = bytes(item).toString().replace(/<!--[\s\S]*?-->/g, '');
    for (const match of html.matchAll(/<(script|link)\b([^>]+)>/gi)) {
      const attributes = Object.fromEntries([...match[2].matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)]
        .map(([, key, double, single, bare]) => [key.toLowerCase(), double ?? single ?? bare]));
      const script = match[1].toLowerCase() === 'script' && attributes.type === 'module';
      const link = match[1].toLowerCase() === 'link' && /\b(modulepreload|stylesheet)\b/.test(attributes.rel);
      const reference = script ? attributes.src : link ? attributes.href : null;
      if (reference) roots.push(resolve(reference, file));
    }
  }
  if (!roots.length) throw new Error('Budget: no entry HTML dependencies');
  const initial = visit(roots);
  const chunks = Object.entries(bundle).filter(([, item]) => item.type === 'chunk');
  const protectedChunks = chunks.filter(([, item]) => Object.keys(item.modules).some(id => bodyModule(id) || threeModule(id)));
  for (const identify of [bodyModule, threeModule]) {
    if (!chunks.some(([, item]) => Object.keys(item.modules).some(identify)))
      throw new Error('Budget: body/three.js output is missing');
  }
  for (const [file] of protectedChunks) {
    if (initial.has(file)) throw new Error(`Budget: body/three.js entered the initial static graph: ${file}`);
  }
  const deferred = visit(protectedChunks.map(([file]) => file), true).difference(initial);
  function totals(files) {
    const result = { js: { raw: 0, gzip: 0, files: [] }, css: { raw: 0, gzip: 0, files: [] } };
    for (const file of [...files].sort()) {
      const kind = /\.(?:m?js)$/.test(file) ? 'js' : file.endsWith('.css') ? 'css' : null;
      if (!kind) continue; // Models, images, fonts and maps are outside the JS/CSS budget.
      const content = bytes(bundle[file]);
      result[kind].raw += content.length;
      result[kind].gzip += gzipSync(content, { level: 9 }).length;
      result[kind].files.push(file);
    }
    return result;
  }
  return { initial: totals(initial), deferred: totals(deferred) };
}

export function enforceBudget(report, budget = limits, log = console.log) {
  for (const kind of ['js', 'css']) {
    if (!Number.isSafeInteger(budget[kind]) || budget[kind] <= 0)
      throw new Error(`Budget: ${kind.toUpperCase()} gzip limit must be a positive integer`);
  }
  for (const [group, values] of Object.entries(report)) {
    for (const [kind, value] of Object.entries(values)) {
      const limit = group === 'initial' ? `; gzip limit ${budget[kind]} B (enforced)` : '; body/three.js (excluded)';
      log(`[budget] ${group} ${kind.toUpperCase()}: raw ${value.raw} B; gzip ${value.gzip} B${limit}`);
      log(`[budget]   ${value.files.join(', ') || '(none)'}`);
    }
  }
  for (const kind of ['js', 'css']) {
    if (report.initial[kind].gzip > budget[kind])
      throw new Error(`Initial ${kind.toUpperCase()} gzip budget exceeded: measured ${report.initial[kind].gzip} B > limit ${budget[kind]} B`);
  }
}

export function performanceBudget() {
  let base;
  return {
    name: 'healthos-performance-budget',
    apply: 'build',
    enforce: 'post',
    configResolved(config) { base = config.base; },
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const report = measureBudget(bundle, base);
        enforceBudget(report);
        this.emitFile({
          type: 'asset', fileName: 'performance-budget-report.json',
          source: JSON.stringify({ gzipLevel: 9, limits, ...report }, null, 2),
        });
      },
    },
  };
}
