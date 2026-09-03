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
    logo: '/assets/logo-full-black.png',
    // No overrides: this is the design system's own palette. A theme that
    // restated the tokens here would be a second source of truth for them.
    tokens: {},
  },

  /**
   * Skyline Dark — the Scanline palette on the Modernist structure.
   *
   * Near-black cool ground, a violet accent in place of the red, type from IBM
   * Plex, 14px cards and 8px pills on hairlines rather than 2px rules.
   *
   * Structure is a theme's business as much as its palette is, so radius and
   * rule weight are tokens like any other and app.css names them everywhere it
   * draws a border. Modernist keeps its square corners by leaving them at
   * their shipped values, which is what an empty override means.
   *
   * **The ramps are read by step, not by lightness.** Each rung of the neutral
   * and accent scales has a fixed job in app.css, and the theme answers the
   * job rather than preserving the order: 100 is a surface, 300-500 are rules,
   * 600-800 are text, and 900 is the ground a picture sits on — so 900 is dark
   * here while 800 is nearly white. Sorting these into a monotonic ramp would
   * put a white background behind every video.
   */
  'skyline-dark': {
    name: 'Skyline Dark',
    // The wordmark is drawn in dark ink and disappears on this ground.
    logo: '/assets/icon-full-white.png',
    fontUrl: 'https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800'
      + '&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap',
    tokens: {
      '--color-bg': '#0a0b0f',
      '--color-surface': '#14161f',
      '--color-text': '#f3f4f7',
      // The darker end of the reference's own accent gradient, not the
      // #7c6cff base. Solid buttons put white on this, and white on #7c6cff is
      // 3.9:1 where this is 4.8:1 — the base stays as accent-500, which is
      // where the ramp wants it and where nothing carries text.
      '--color-accent': '#6a5cf0',
      // Danger, kept separate from the accent. In the Modernist palette the
      // two are both red and nothing depended on the difference; here the
      // accent is violet, and a failed job printed in it reads as a highlight.
      '--color-accent-2': '#ff6b6b',
      '--color-divider': 'rgba(255, 255, 255, 0.18)',

      // 100 surface · 300-500 rules · 600-800 text · 900 the video ground.
      '--color-neutral-100': '#14161f',
      '--color-neutral-200': '#1b1e2a',
      '--color-neutral-300': 'rgba(255, 255, 255, 0.10)',
      '--color-neutral-400': 'rgba(255, 255, 255, 0.18)',
      '--color-neutral-500': 'rgba(255, 255, 255, 0.26)',
      '--color-neutral-600': '#7d8294',
      '--color-neutral-700': '#9ca1b3',
      '--color-neutral-800': '#d5d8e2',
      '--color-neutral-900': '#0e0f15',

      // 100-300 are tints behind hover and pressed states; 700-900 are the
      // accent as text, which has to be light enough to read on the ground.
      '--color-accent-100': 'rgba(124, 108, 255, 0.14)',
      '--color-accent-200': 'rgba(124, 108, 255, 0.22)',
      '--color-accent-300': 'rgba(124, 108, 255, 0.32)',
      '--color-accent-400': '#6a5cf0',
      '--color-accent-500': '#7c6cff',
      '--color-accent-600': '#8f7fff',
      '--color-accent-700': '#b3adff',
      '--color-accent-800': '#c9c2ff',
      '--color-accent-900': '#ded9ff',

      '--color-accent-2-100': 'rgba(255, 107, 107, 0.13)',
      '--color-accent-2-200': 'rgba(255, 107, 107, 0.20)',
      '--color-accent-2-300': 'rgba(255, 107, 107, 0.30)',
      '--color-accent-2-400': '#e85f5f',
      '--color-accent-2-500': '#ff6b6b',
      '--color-accent-2-600': '#ff8080',
      '--color-accent-2-700': '#ff9b9b',
      '--color-accent-2-800': '#ffbcbc',
      '--color-accent-2-900': '#ffd9d9',

      '--font-heading': "'Bricolage Grotesque', 'Archivo', system-ui, sans-serif",
      '--font-heading-weight': '700',
      '--font-body': "'IBM Plex Sans', 'Archivo', system-ui, sans-serif",

      // Ink-tinted shadows are invisible on a dark ground; depth here is
      // ambient black under the element rather than a tint over it.
      '--shadow-sm': '0 1px 2px rgba(0, 0, 0, 0.5)',
      '--shadow-md': '0 6px 18px -6px rgba(0, 0, 0, 0.6)',
      '--shadow-lg': '0 30px 80px -30px rgba(0, 0, 0, 0.7)',

      // 14px cards, 8px pills, and hairlines rather than 2px rules. On a
      // near-black ground a 2px rule in the text colour is a bright line
      // across the screen — the reference draws its structure with white at
      // one tenth opacity instead, and the weight has to come down with it.
      '--radius-lg': '14px',
      '--radius-md': '8px',
      '--radius-sm': '6px',
      '--rule': '1px',
      '--rule-hair': '1px',
    },
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

/** Paint a theme: its tokens, the fonts they name, and the wordmark. */
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

  applyThemeFont(theme.fontUrl || '');
  applyThemeLogo(theme.logo || THEMES[DEFAULTS.theme].logo);
}


/**
 * Load the webfont a theme's type tokens name.
 *
 * `--font-body: 'IBM Plex Sans'` with nothing fetching IBM Plex is a token
 * that quietly means system-ui — the theme looks applied, reads wrong, and
 * nothing says why. One <link> whose href is rewritten, rather than one per
 * theme: switching back and forth would otherwise stack them up.
 */
function applyThemeFont(url) {
  let link = document.getElementById('theme-font');
  if (!url) {
    link?.remove();
    return;
  }
  if (!link) {
    link = document.createElement('link');
    link.id = 'theme-font';
    link.rel = 'stylesheet';
    document.head.appendChild(link);
  }
  if (link.href !== url) link.href = url;
}


/**
 * Swap the wordmark for one that can be seen on this theme's ground.
 *
 * The logo carries its own colour rather than taking the accent, so a dark
 * theme cannot tint it — it needs the other file. An <img> rather than a
 * background image, so it keeps its alt text.
 */
function applyThemeLogo(src) {
  for (const img of document.querySelectorAll('.brand-logo, .signin-logo')) {
    if (img.getAttribute('src') !== src) img.setAttribute('src', src);
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
