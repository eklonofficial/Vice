import en from './en.json';
import pt_BR from './pt-BR.json';

/**
 * Every locale, imported statically.
 *
 * Static rather than dynamic because the build emits exactly two flat, unhashed
 * files and forbids code splitting (see vite.config.mts), and share.py's
 * _UI_ASSET_KINDS is exactly {fonts, styles, scripts}, so a locales/ asset kind
 * would mean widening the path-traversal guard that serves them. At roughly 5KB
 * gzipped per language that trade is not worth making yet. Revisit past about
 * eight languages.
 *
 * Adding a language is two lines here and one file next to this one. See
 * docs/TRANSLATING.md.
 */
export const LOCALES = {
  en,
  'pt-BR': pt_BR,
} as const;

export type LocaleName = keyof typeof LOCALES;

/** What the picker in Settings shows, in the language itself. */
export const LOCALE_LABELS: Record<LocaleName, string> = {
  en: 'English',
  'pt-BR': 'Português Brasileiro',
};
