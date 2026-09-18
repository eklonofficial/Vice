// Proves the runtime accent derivation agrees with the build-time generator.
//
// Run with: npm run accents:verify
//
// ui-src/theme/deriveAccent.ts builds a Material 3 scheme in the browser for a
// colour the user picked. tools/derive-accents.mjs builds the same thing at
// build time for the five shipped seeds. If the two ever drift, a custom
// accent stops being the same design system as the presets and nothing else
// would notice: both would keep producing plausible colours.
//
// So: run all five shipped seeds through the runtime code and diff every role
// against what the generator actually wrote. Then run a spread of arbitrary
// hues through it, because a user can pick any of them and none may produce a
// scheme that fails its own contrast checks.
//
// deriveAccent.ts is TypeScript and imports a bundler-only package, so it goes
// through Vite rather than being imported directly. That also proves it still
// bundles, which is the form it actually ships in.

import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));
const repo = fileURLToPath(new URL('..', import.meta.url));

const work = mkdtempSync(join(tmpdir(), 'vice-accent-parity-'));
try {
  // The config lives in the repo rather than in the temp dir: a config outside
  // the project cannot resolve vite itself. Only the build output is temporary.
  // vite's own entry run with this node, not `npx`: on Windows npx is a .cmd
  // shim, which execFileSync refuses to start without a shell.
  execFileSync(
    process.execPath,
    [join(repo, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--config', join(here, 'accent-parity', 'vite.config.mjs'), '--logLevel', 'error'],
    {cwd: repo, stdio: ['ignore', 'ignore', 'inherit'], env: {...process.env, VICE_PARITY_OUT: work}},
  );

  const raw = execFileSync('node', [join(work, 'parity.mjs')], {encoding: 'utf8'});
  const got = JSON.parse(raw);

  // What the generator actually wrote, parsed rather than imported so this
  // reads the shipped file exactly as the Python tests do.
  // CRLF when git checked it out with autocrlf (Windows); the patterns want LF.
  const accents = readFileSync(join(repo, 'ui-src', 'theme', 'accents.ts'), 'utf8').replace(/\r\n/g, '\n');
  const shipped = {};
  for (const m of accents.matchAll(/^ {2}(\w+): \{\n([\s\S]*?)\n {2}\},$/gm)) {
    shipped[m[1]] = Object.fromEntries(
      [...m[2].matchAll(/^ {4}(\w+): '(#[0-9a-f]{6})',$/gm)].map(r => [r[1], r[2]]),
    );
  }

  const names = Object.keys(shipped);
  if (names.length !== 5) {
    console.error(`Expected five accents in accents.ts, found ${names.length}.`);
    process.exit(1);
  }

  let problems = 0;
  for (const name of names) {
    const want = shipped[name];
    const {ramp, failures} = got[name];
    const diffs = Object.keys(want).filter(k => want[k] !== ramp[k]);
    if (diffs.length || failures.length) {
      problems++;
      console.error(`${name}: the runtime derivation does not match the generator`);
      for (const k of diffs) console.error(`   ${k}: runtime ${ramp[k]}, generator ${want[k]}`);
      for (const f of failures) console.error(`   check failed: ${f}`);
    } else {
      console.log(`ok  ${name.padEnd(7)} all ${Object.keys(want).length} roles identical`);
    }
  }

  console.log('\narbitrary seeds:');
  for (const [seed, {ramp, failures}] of Object.entries(got.__probes)) {
    if (failures.length) {
      problems++;
      console.error(`  ${seed} produces a scheme that fails: ${failures.join('; ')}`);
    } else {
      console.log(`  ok  ${seed} -> accent ${ramp.base}, background ${ramp.bg}`);
    }
  }

  if (problems) {
    console.error(
      '\nderiveAccent.ts and tools/derive-accents.mjs have drifted. ' +
      'A custom accent is no longer the same scheme as the presets.',
    );
    process.exit(1);
  }
  console.log('\nThe runtime derivation matches the generator exactly.');
} finally {
  rmSync(work, {recursive: true, force: true});
}
