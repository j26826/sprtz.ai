/**
 * Editor settings: app language, metadata language, theme.
 *
 * Two of these are per-device display preferences and live in localStorage; the
 * third is not. **Metadata language belongs to the job, not to the browser.**
 * The descriptions and summaries a match already carries were generated in one
 * language and stay in it, so a later reader switching their UI to German must
 * not make a job's stored English prose claim to be German. The setting here is
 * therefore what *new* analyses will be asked for, and it is copied onto each
 * job when it is registered.
 *
 * Themes are a token overlay rather than a stylesheet. `metro-light` is the
 * Modernist palette exactly as the design system ships it — an empty override —
 * so adding a theme is a matter of listing the tokens that differ, not of
 * duplicating a file that then drifts.
 */

import { LOCALES, localeName } from './i18n.js';

const STORAGE_KEY = 'sportscut.settings';

export const THEMES = {
  'metro-light': {
    name: 'Metro Light',
    // No overrides: this is the design system's own palette. A theme that
    // restated the tokens here would be a second source of truth for them.
    tokens: {},
  },
};

/**
 * Languages the analysis can be asked to write in.
 *
 * Deliberately not the same list as the UI's. The UI distinguishes en-GB from
 * en-US because button labels differ; asking a model for "English (UK)" prose
 * about a handball match is a distinction it cannot reliably hold, so both map
 * to English here.
 */
export const METADATA_LANGUAGES = [
  { code: 'en', name: 'English' },
  { code: 'de', name: 'Deutsch' },
  { code: 'it', name: 'Italiano' },
  { code: 'fr', name: 'Français' },
  { code: 'es', name: 'Español' },
];

const DEFAULTS = {
  locale: '',            // empty means "follow the browser"
  metadataLanguage: 'en',
  theme: 'metro-light',
};

let current = { ...DEFAULTS };

export function loadSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) current = { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    // A private window throws on access, and a corrupt value should not brick
    // the app. Defaults are a perfectly good answer.
    current = { ...DEFAULTS };
  }
  if (!THEMES[current.theme]) current.theme = DEFAULTS.theme;
  if (!METADATA_LANGUAGES.some((l) => l.code === current.metadataLanguage)) {
    current.metadataLanguage = DEFAULTS.metadataLanguage;
  }
  if (current.locale && !LOCALES.includes(current.locale)) current.locale = '';
  return current;
}

export function getSettings() {
  return { ...current };
}

export function saveSettings(patch) {
  current = { ...current, ...patch };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch { /* still applies for this tab */ }
  return { ...current };
}

/** Paint a theme by writing its overrides onto the document root. */
export function applyTheme(themeId) {
  const theme = THEMES[themeId] || THEMES[DEFAULTS.theme];
  const root = document.documentElement;
  root.dataset.theme = THEMES[themeId] ? themeId : DEFAULTS.theme;

  // Clear the previous theme's overrides before applying the new one, or
  // switching from a theme that sets a token to one that does not would leave
  // the old value behind.
  for (const id of Object.keys(THEMES)) {
    for (const token of Object.keys(THEMES[id].tokens)) {
      root.style.removeProperty(token);
    }
  }
  for (const [token, value] of Object.entries(theme.tokens)) {
    root.style.setProperty(token, value);
  }
}

export function themeOptions() {
  return Object.entries(THEMES).map(([id, theme]) => ({ id, name: theme.name }));
}

export function localeOptions() {
  return [
    { id: '', name: '' },   // caller fills the "follow the browser" label
    ...LOCALES.map((id) => ({ id, name: localeName(id) })),
  ];
}
