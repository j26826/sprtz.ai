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
 * Themes are a token overlay rather than a stylesheet. `arenos-dark` is the
 * Arenos design system exactly as ds/styles.css ships it — an empty override —
 * because dark is the product's own default surface, not a fallback from
 * light. `arenos-light` is the full peer, applied as an overlay so both modes
 * stay one system rather than two stylesheets that can drift apart.
 */

import { LOCALES, localeName } from './i18n.js';

const STORAGE_KEY = 'arenos.settings';

export const THEMES = {
  /**
   * Arenos Dark — the product default. Ground #0F1113, Arenos Amber as the
   * only accent, Geist for brand and product, Geist Mono for anything a model
   * produced. No overrides: this is ds/styles.css's own palette, so a theme
   * that restated the tokens here would be a second source of truth for them.
   */
  'arenos-dark': {
    name: 'Arenos Dark',
    logo: '/assets/arenos-lockup-notag-on-dark.svg',
    logoSignin: '/assets/arenos-lockup-tagline-on-dark.svg',
    tokens: {},
  },

  /**
   * Arenos Light — Arena Ivory ground (#F6F4EF), a full peer to dark rather
   * than a fallback. Same amber, same type, same radii and spacing; only the
   * neutral ramp and the two accent ramps' contrast direction change.
   *
   * **The ramps are read by step, not by lightness**, same rule as any other
   * theme here: neutral 100 is a surface, 300-500 are rules, 600-800 are
   * text, 900 is the ground a picture sits on — so 900 stays near-black even
   * in light mode, because a thumbnail's video ground does not follow the
   * page. accent-700..900 are amber *as text*, which on a light ground amber
   * itself cannot be (2.86:1, fails AA) — semantic.css's light-mode
   * accent-text substitute, #9C6211, takes over there instead.
   */
  'arenos-light': {
    name: 'Arenos Light',
    logo: '/assets/arenos-lockup-notag-on-light.svg',
    logoSignin: '/assets/arenos-lockup-tagline-on-light.svg',
    tokens: {
      '--color-bg': '#F6F4EF',
      '--color-surface': '#FFFFFF',
      '--color-text': '#191816',
      '--color-accent': '#D4881A',
      '--color-accent-2': '#B23D38',
      '--color-divider': '#E3E1D8',
      // Amber as a fill still takes ink text (semantic.css: --on-accent is
      // #111111 in both themes) — this token is not overridden here on
      // purpose, ds/styles.css's value already applies to both.

      // The brand's 7-step light scale (neutrals.css --light-0..6).
      '--color-neutral-100': '#FFFFFF',
      '--color-neutral-200': '#F9F7F4',
      '--color-neutral-300': '#E3E1D8',
      '--color-neutral-400': '#D3D0C5',
      '--color-neutral-500': '#B7B6B0',
      '--color-neutral-600': '#8A8985',
      '--color-neutral-700': '#595854',
      '--color-neutral-800': '#191816',
      '--color-neutral-900': '#0F1113',

      // 100-300 are --accent-soft at rising strength (light: rgba(212,136,26,
      // 0.12) is the brand's own mid-point, landing at 200). 700-900 are
      // amber-as-text: #9C6211 (AA) stands in for amber itself, which fails
      // AA as light-mode type, then steps toward #774B0D (AAA, small type).
      '--color-accent-100': 'rgba(212, 136, 26, 0.08)',
      '--color-accent-200': 'rgba(212, 136, 26, 0.12)',
      '--color-accent-300': 'rgba(212, 136, 26, 0.20)',
      '--color-accent-400': '#DE9736',
      '--color-accent-500': '#D4881A',
      '--color-accent-600': '#B87415',
      '--color-accent-700': '#9C6211',
      '--color-accent-800': '#774B0D',
      '--color-accent-900': '#5C3A0A',

      // Brick, light-mode values direct from colors.css/semantic.css.
      '--color-accent-2-100': 'rgba(178, 61, 56, 0.08)',
      '--color-accent-2-200': 'rgba(178, 61, 56, 0.12)',
      '--color-accent-2-300': 'rgba(178, 61, 56, 0.20)',
      '--color-accent-2-400': '#B23D38',
      '--color-accent-2-500': '#9A3430',
      '--color-accent-2-600': '#7D2A27',
      '--color-accent-2-700': '#611F1D',
      '--color-accent-2-800': '#451614',
      '--color-accent-2-900': '#2E0E0D',

      // Elevation reads as soft lift on light rather than an ambient edge.
      '--shadow-sm': '0 1px 2px rgba(17, 17, 17, 0.06)',
      '--shadow-md': '0 2px 8px rgba(17, 17, 17, 0.08)',
      '--shadow-lg': '0 8px 34px rgba(17, 17, 17, 0.12)',
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
  theme: 'arenos-dark',
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
  applyThemeLogo(theme);
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
 * Swap the wordmark for the file drawn for this theme's ground.
 *
 * The artwork carries its own ink rather than taking the accent, so a dark
 * theme cannot tint it — it needs the other file. Two files, not one: the
 * header runs tight on space and uses the no-tagline cut, while the sign-in
 * screen has room for the full lockup with the tagline. An <img> rather than
 * a background image, so it keeps its alt text.
 */
function applyThemeLogo(theme) {
  const fallback = THEMES[DEFAULTS.theme];
  const header = theme.logo || fallback.logo;
  const signin = theme.logoSignin || theme.logo || fallback.logoSignin || fallback.logo;
  for (const img of document.querySelectorAll('.brand-logo')) {
    if (img.getAttribute('src') !== header) img.setAttribute('src', header);
  }
  for (const img of document.querySelectorAll('.signin-logo')) {
    if (img.getAttribute('src') !== signin) img.setAttribute('src', signin);
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
