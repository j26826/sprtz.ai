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

import { readFileSync } from 'node:fs';

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

if (failures.length) {
  console.error(`web/check.mjs found ${failures.length} problem(s):`);
  for (const message of failures) console.error(`  - ${message}`);
  process.exit(1);
}
console.log('web/check.mjs: ok');
