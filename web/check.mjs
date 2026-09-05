/**
 * Static checks for the editor's source.
 *
 * `node --check` only parses, so everything here is a class of mistake that
 * parses perfectly and fails in a browser — where the symptom is usually a
 * blank screen or a dead button rather than anything naming the cause.
 *
 * Each check is exact rather than heuristic. An earlier version tried to find
 * calls to undefined functions by pattern, and could not tell a destructured
 * parameter or the word "the" in a comment from a function call; a check that
 * cries wolf is worse than no check, because it trains you to skip the output.
 *
 * Run with: node web/check.mjs
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';

const ROOT = new URL('./', import.meta.url);
const read = (name) => readFileSync(new URL(name, ROOT), 'utf8');

const failures = [];
const fail = (message) => failures.push(message);

/** Comments are prose: "the run that owned this" is not a call to the(). */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/^\s*\/\/.*$/gm, ' ');
}

const app = stripComments(read('src/app.js'));
const i18n = read('src/i18n.js');
const html = read('index.html');

/* ── every function called is a function that exists ──────────────────────── */

/**
 * This needs a real parser, and three hand-rolled attempts proved it.
 *
 * Pattern matching cannot tell a call from the word "the" in a comment, a
 * destructured parameter from an undeclared name, or where a template literal
 * ends. Each attempt produced more noise than signal, and a check that cries
 * wolf is one people stop reading — which is how a deleted playerMarkup and a
 * deleted renderSessions each reached production, both of them blanking the
 * whole screen because the throw happened inside render().
 *
 * acorn is a parser. If it is not installed the check is skipped and said to be
 * skipped, rather than quietly passing: CI installs it, so it always runs where
 * it matters.
 */
async function checkSymbols(source) {
  let acorn;
  try {
    acorn = await import('acorn');
  } catch {
    console.warn('  (symbol check skipped: acorn is not installed)');
    return;
  }

  const ast = acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'module' });
  const declared = new Set();
  const called = [];

  const bind = (node) => {
    if (!node) return;
    if (node.type === 'Identifier') declared.add(node.name);
    else if (node.type === 'ObjectPattern') node.properties.forEach((p) => bind(p.value ?? p.argument));
    else if (node.type === 'ArrayPattern') node.elements.forEach(bind);
    else if (node.type === 'AssignmentPattern') bind(node.left);
    else if (node.type === 'RestElement') bind(node.argument);
  };

  const walk = (node) => {
    if (!node || typeof node.type !== 'string') return;

    if (node.type === 'FunctionDeclaration' && node.id) declared.add(node.id.name);
    if (node.type === 'ClassDeclaration' && node.id) declared.add(node.id.name);
    if (node.type === 'VariableDeclarator') bind(node.id);
    if (node.type === 'ImportSpecifier') declared.add(node.local.name);
    if (node.type === 'ImportDefaultSpecifier') declared.add(node.local.name);
    if (node.type === 'ImportNamespaceSpecifier') declared.add(node.local.name);
    if (node.params) node.params.forEach(bind);
    if (node.type === 'CatchClause') bind(node.param);

    // A bare name being called. A method call has a MemberExpression callee and
    // is somebody else's problem.
    if (node.type === 'CallExpression' && node.callee.type === 'Identifier') {
      called.push(node.callee.name);
    }

    for (const key of Object.keys(node)) {
      const child = node[key];
      if (Array.isArray(child)) child.forEach(walk);
      else if (child && typeof child.type === 'string') walk(child);
    }
  };

  walk(ast);

  const GLOBALS = new Set([
    'String', 'Number', 'Boolean', 'Array', 'Object', 'Date', 'JSON', 'Promise',
    'Error', 'Set', 'Map', 'RegExp', 'parseInt', 'parseFloat', 'isNaN', 'fetch',
    'setTimeout', 'setInterval', 'clearInterval', 'clearTimeout', 'alert',
    'confirm', 'prompt', 'decodeURIComponent', 'encodeURIComponent', 'atob',
    'btoa', 'structuredClone', 'queueMicrotask', 'requestAnimationFrame',
  ]);

  for (const name of new Set(called)) {
    if (GLOBALS.has(name) || declared.has(name)) continue;
    fail(`app.js calls ${name}() but nothing declares, imports or binds it`);
  }
}

await checkSymbols(readFileSync(new URL('src/app.js', ROOT), 'utf8'));

/* ── every translation key resolves ───────────────────────────────────────── */

const base = i18n.slice(i18n.indexOf("'en-GB': {"), i18n.indexOf("'en-US': {"));
const keys = new Set([...base.matchAll(/^\s{4}'([^']+)':/gm)].map((m) => m[1]));

for (const [, key] of app.matchAll(/\bt\('([^']+)'\)/g)) {
  if (!keys.has(key)) fail(`app.js uses t('${key}') which en-GB does not define`);
}
for (const [, key] of html.matchAll(/data-i18n="([^"]+)"/g)) {
  if (!keys.has(key)) fail(`index.html marks data-i18n="${key}" which en-GB does not define`);
}

/* ── every element the script reaches for exists in the document ──────────── */

const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
for (const [, id] of app.matchAll(/\$\('([^']+)'\)/g)) {
  if (!ids.has(id)) fail(`app.js reads $('${id}') but index.html has no such id`);
}

/* ── every button is reachable by a handler ───────────────────────────────── */

// A button whose data attribute is not in the delegated selector, and which has
// no id of its own, is a button nothing listens to. That is how Settings, Sign
// out and Prepare playback each shipped inert.
const selector = app.match(/event\.target\.closest\(([\s\S]*?)\);/)?.[1] ?? '';
const boundIds = new Set(
  [...app.matchAll(/\$\('([^']+)'\)\??\.addEventListener/g)].map((m) => m[1]),
);

for (const source of [app, html]) {
  for (const [, tag] of source.matchAll(/<button\b([^>]*)>/g)) {
    const attrs = [...tag.matchAll(/\bdata-([a-z-]+)=/g)].map((m) => `data-${m[1]}`);
    const id = tag.match(/\bid="([^"]+)"/)?.[1];

    if (/type="submit"/.test(tag)) continue;   // the form's submit handler drives it
    if (id && boundIds.has(id)) continue;
    if (id && app.includes(`'${id}'`)) continue;
    if (attrs.some((a) => selector.includes(a))) continue;
    if (attrs.length === 0 && !id) continue;   // decorative or form-submit

    fail(`a <button> with ${attrs.join(' ') || `id="${id}"`} has no handler: `
      + 'it is not in the click selector and nothing binds it directly');
  }
}

/* ── every module imported is a module that exists ────────────────────────── */

// A relative import that resolves to nothing is a 404 on a bare `import`: the
// script never runs and the screen is blank, with the console naming a URL
// rather than the file that asked for it. This catches a typo; the Dockerfile
// copying src/ wholesale is what stops a real file being left out of the image.
// Discovered rather than listed. A named list is a second place to remember
// when a module is added, and that is the place that gets forgotten — the same
// mistake the Dockerfile made by copying files one by one.
const modules = readdirSync(new URL('src/', ROOT))
  .filter((f) => f.endsWith('.js'))
  .map((f) => `src/${f}`);

for (const name of modules) {
  const source = read(name);
  for (const [, spec] of source.matchAll(/\bfrom\s+'(\.[^']+)'/g)) {
    const resolved = new URL(spec, new URL(name, ROOT));
    if (!existsSync(resolved)) {
      fail(`${name} imports '${spec}', which does not exist`);
    }
  }
}

/* ── every api() call is addressed to the API ─────────────────────────────── */

// The load balancer routes /api/* to the API and /jobs/* to the HLS bucket, so
// a path missing its /api prefix does not 404 — it reaches a private bucket and
// comes back 403, which reads as an authorisation problem in a completely
// different service. That is how the moment thumbnails shipped unable to load.
for (const [, path] of app.matchAll(/\bapi\(\s*[`'"]([^`'"]+)/g)) {
  if (!path.startsWith('/api/')) {
    fail(`app.js calls api('${path}'), which is not under /api/ — `
      + 'the load balancer sends that to a bucket, not to the API');
  }
}

/* ── every theme's wordmark is a file that is actually there ──────────────── */

// The logo carries its own colour, so a dark theme swaps the file rather than
// tinting it. A path that 404s is a missing image with nothing naming the
// cause — the same class of failure as a t() key that does not resolve.
const settings = read('src/settings.js');
for (const [, src] of settings.matchAll(/logo(?:Signin)?: '([^']+)'/g)) {
  const file = src.replace(/^\//, 'src/');
  if (!existsSync(new URL(file, ROOT))) {
    fail(`settings.js names the logo ${src}, which web/${file} does not exist for`);
  }
}

if (failures.length) {
  console.error(`web/check.mjs found ${failures.length} problem(s):`);
  for (const message of failures) console.error(`  - ${message}`);
  process.exit(1);
}
console.log('web/check.mjs: ok');
