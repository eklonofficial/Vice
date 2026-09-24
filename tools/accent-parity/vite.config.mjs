// Bundles entry.ts so the runtime accent derivation can be run under Node and
// diffed against the generator. Driven by tools/verify-custom-accent.mjs, not
// by anything the app ships.
import {fileURLToPath} from 'node:url';
import {defineConfig} from 'vite';

export default defineConfig({
  build: {
    lib: {entry: fileURLToPath(new URL('./entry.ts', import.meta.url)), formats: ['es'], fileName: 'parity'},
    outDir: process.env.VICE_PARITY_OUT ?? fileURLToPath(new URL('./out', import.meta.url)),
    minify: false,
    emptyOutDir: false,
    target: 'node20',
  },
});
