// Reports translation coverage, and finds copy that never made it into a
// locale file.
//
// Run with: npm run i18n:check
//
// Two jobs. For a translator it answers "what is left for my language", which
// is the question that otherwise means diffing JSON by hand. For Vice it
// answers "did somebody add a hardcoded string again", which is the way this
// rots: the plumbing lands, and six months later half the new copy is English
// only and nobody noticed.

import {readFileSync, readdirSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const LOCALES_DIR = new URL('../ui-src/locales/', import.meta.url);
const UI_SRC = new URL('../ui-src/', import.meta.url);

const read = url => JSON.parse(readFileSync(url, 'utf8'));

/** Every leaf path in a locale tree. A plural object counts as one leaf. */
function leaves(node, prefix = '') {
  if (typeof node === 'string') return [prefix];
  if (node && typeof node === 'object' && 'other' in node) return [prefix];
  const out = [];
  for (const [key, value] of Object.entries(node ?? {})) {
    out.push(...leaves(value, prefix ? `${prefix}.${key}` : key));
  }
  return out;
}

function valueAt(tree, path) {
  return path.split('.').reduce((n, p) => (n == null ? undefined : n[p]), tree);
}

const en = read(new URL('en.json', LOCALES_DIR));
const enKeys = leaves(en);

const localeFiles = readdirSync(LOCALES_DIR)
  .filter(f => f.endsWith('.json'))
  .sort();

console.log(`English source: ${enKeys.length} keys\n`);

let problems = 0;
for (const file of localeFiles) {
  const name = file.replace(/\.json$/, '');
  if (name === 'en') continue;
  const locale = read(new URL(file, LOCALES_DIR));
  const keys = new Set(leaves(locale));

  // A key the source does not have means English was renamed and this locale
  // was not. It is the one case worth failing on: the string is dead weight and
  // whatever replaced it is silently untranslated.
  const stale = [...keys].filter(k => !enKeys.includes(k));
  // Present but still holding the English text: scaffolded and not yet done.
  const untouched = enKeys.filter(k => {
    const mine = valueAt(locale, k);
    return mine !== undefined && JSON.stringify(mine) === JSON.stringify(valueAt(en, k));
  });
  const missing = enKeys.filter(k => valueAt(locale, k) === undefined);
  const done = enKeys.length - missing.length - untouched.length;
  const pct = Math.round((done / enKeys.length) * 100);

  console.log(`${name}: ${pct}% (${done}/${enKeys.length})`);
  if (missing.length) console.log(`  ${missing.length} missing, falls back to English`);
  if (untouched.length) console.log(`  ${untouched.length} still English`);
  if (stale.length) {
    problems += stale.length;
    console.log(`  ${stale.length} no longer in en.json: ${stale.slice(0, 5).join(', ')}`);
  }
}

// ── copy that never reached a locale file ──────────────────────────────
// Heuristic by necessity: prose is a quoted string with a space in it, sitting
// where a person would read it. Anything it flags is either a string to
// translate or something to add to IGNORE with a reason.
const IGNORE = [
  /^[a-z-]+$/,                       // css classes, ids, keys
  /^[A-Z_]+$/,                       // constants
  /\{|\}|=>|\$\{/,                   // code fragments
  /^(GET|POST|PUT|DELETE|PATCH)\b/,  // http verbs
  /^\//,                             // paths
  /^https?:/,                        // urls
  /^[\d.]+$/,                        // numbers
  /^[MmLlHhVvCcSsQqTtAaZz][\d\s.,-]/, // svg path data
  /^Andrew Marin$/,                  // a person's name, not copy
  /^JetBrains Mono$/,                // a typeface name, the same in every language
  /^GPL-[\d.]+$/,                    // an SPDX licence id
  /^-{1,2}[a-z]/,                    // command-line flags shown as placeholders
];

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(path));
    // .ts as well as .tsx: the editor engine builds its timeline with template
    // strings rather than JSX, and that is where its copy lives.
    else if (entry.name.endsWith('.tsx') || entry.name.endsWith('.ts')) out.push(path);
  }
  return out;
}

const suspects = [];
for (const file of walk(fileURLToPath(new URL('.', UI_SRC)))) {
  const src = readFileSync(file, 'utf8');
  const seen = new Set();
  const add = (text, {oneWordOk = false} = {}) => {
    const clean = text.replace(/\s+/g, ' ').trim();
    if (oneWordOk) {
      if (clean.length < 3 || !/^[A-Z]/.test(clean)) return;
    } else if (clean.length < 7 || !clean.includes(' ')) return;
    if (IGNORE.some(re => re.test(clean))) return;
    if (seen.has(clean)) return;
    seen.add(clean);
    suspects.push(`${file.split('/ui-src/')[1]}: ${clean}`);
  };

  // Comments describe the code, they are not shown to anyone. A doc comment
  // quoting a default label would otherwise register as untranslated copy.
  const withoutComments = src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

  // console.* is developer output. It goes in the log a maintainer reads, not
  // on screen, and translating it would make searching the issue tracker
  // harder rather than easier.
  const withoutLogging = withoutComments.replace(/console\.(debug|log|warn|error|info)\([^;]*?\);/gs, '');

  // JSX attribute values: label="...", help="...", title="...". These sit
  // after an = and so are invisible to the pass below, which is most of what a
  // settings screen is made of.
  for (const [, text] of withoutLogging.matchAll(
    /\b(?:label|help|title|placeholder|aria-label|note|detail|heading|emptyLabel)=["']([^"']{6,140})["']/g,
  )) {
    add(text, {oneWordOk: true});
  }

  // Template literals. Prose hides in these because interpolation is how a
  // sentence gets a number in it, which is exactly the copy most likely to be
  // wrong in another language.
  for (const [, text] of withoutLogging.matchAll(/`([^`\\]{6,200}?)`/gs)) {
    if (!/[A-Za-z]{3}\s+[A-Za-z]{3}/.test(text)) continue;
    add(text.replace(/\$\{[^}]*\}/g, '{}'));
  }

  // Quoted prose: labels, titles, notify() text.
  // The quote character is excluded from the run by backreference, or two
  // adjacent short strings on one line read as one sentence: ['Geist', stack:
  // was reported as untranslated copy.
  for (const [, , text] of withoutLogging.matchAll(
    /(?:^|[\s({[,])(["'])((?:(?!\1)[A-Za-z0-9 ,.:;!?'’“”()%·-]){6,90})\1/gm,
  )) {
    if (!/^[A-Z]/.test(text)) continue;
    add(text);
  }
  // Text sitting directly in JSX, which the quoted pass cannot see. The
  // closing </ is required: without it every TypeScript generic reads as copy,
  // because `: Promise<void>` puts a word between a > and a <.
  for (const [, text] of withoutLogging.matchAll(/>\s*([A-Z][A-Za-z0-9 ,.:;!?'’“”()%·-]{2,120}?)\s*<\//gs)) {
    add(text, {oneWordOk: true});
  }

  // The same thing across lines, which is where Prettier puts anything longer
  // than a word or two.
  const lines = withoutLogging.split('\n');
  for (let i = 1; i < lines.length - 1; i++) {
    const word = lines[i].trim();
    if (!/^[A-Z][A-Za-z0-9 ,.:;!?'’“”()%·-]{2,120}$/.test(word)) continue;
    if (!lines[i - 1].trimEnd().endsWith('>')) continue;
    if (!lines[i + 1].trimStart().startsWith('<')) continue;
    add(word, {oneWordOk: true});
  }
}

if (suspects.length) {
  const byFile = new Map();
  for (const s of suspects) {
    const [file] = s.split(': ');
    byFile.set(file, (byFile.get(file) ?? 0) + 1);
  }
  console.log(`\n${suspects.length} string(s) look like copy but do not go through t():`);
  for (const [file, n] of [...byFile].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(4)}  ${file}`);
  }
  if (process.argv.includes('--list')) {
    console.log('');
    for (const s of suspects) console.log(`  ${s}`);
  } else {
    console.log('\n  Run with --list to see them.');
  }
}

process.exit(problems ? 1 : 0);
