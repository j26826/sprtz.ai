/**
 * Arenos — chat-first sports video agent.
 *
 * Originally implemented `SPRTZ AI Chat.dc.html` on the Modernist design
 * system; re-skinned onto the Arenos design system (brand standards v1.0) with
 * the same structure. This is the interface driven by the real system:
 *
 *   - the transcript and its inline cards render from Firestore, which the
 *     agents write through the catalog MCP server, so the moment list and the
 *     moments update live while an analysis runs;
 *   - the composer talks to the deployed ADK agent over SSE;
 *   - a moment plays from the job's HLS stream behind the CDN, seeking to its
 *     in point and stopping at its out point — no timeline, as the footer says.
 *
 * Where the backend genuinely cannot do something the prototype mocks (post to
 * a platform, report view counts), the UI says so rather than showing a
 * plausible number.
 */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js';
import {
  getAuth, signInWithPopup, GoogleAuthProvider, onAuthStateChanged, getIdToken,
  signInWithEmailAndPassword, signOut, setPersistence, browserLocalPersistence,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import {
  getFirestore, collection, doc, query, orderBy, limit, onSnapshot,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

import { LOCALES, detectLocale, getLocale, localeName, setLocale, t } from './i18n.js';
import { chooseCard, wantsDetail } from './cards.js';
import { currentTurn } from './transcript.js';
import { liveStageFills, liveSummary, validateLiveEvent } from './live.js';
import {
  disciplinesFor, findGames, gamesInScope, scopeContextLine, scopeFilters, scopeTitle,
  sportsAvailable,
} from './scope.js';
import {
  filterAsked, gameNamedIn as namedGame, selectGames, selectMoments,
} from './search.js';
import {
  MAX_CUT_SEC, canTrim, clampTo, nextSpeed, playRange, playerTimeline, rangeBand,
  shortClock, trim, widen,
} from './player.js';
import {
  countTypesIn, filterByTypes, groupByRide, momentTypesIn,
  rideNamedIn, rideRank, ridesAsked, sortRideGroups,
} from './ridegroups.js';
import {
  METADATA_LANGUAGES, applyTheme, clampStallMinutes, getSettings, loadSettings, saveSettings,
  themeOptions,
} from './settings.js';
import {
  createSession, listSessions, loadSessions, removeSession, updateSession,
} from './sessions.js';

const CONFIG = window.SPRTZ_CONFIG || {};
// Empty means same-origin, which is how the load balancer serves it: `/` is
// the SPA and `/api/*` is the API on one hostname. That removes CORS entirely
// and lets the Identity Platform token travel on a plain relative fetch.
const API = (CONFIG.apiBaseUrl || '').replace(/\/$/, '');
const $ = (id) => document.getElementById(id);

/* ─────────────────────────────────────────────────────────── state ── */

const state = {
  reanalyse: null,   // { jobId, urls } while the analyse-again panel is open
  user: null,
  msgs: [],
  jobs: [],
  game: null,          // the match-level record for the selected job
  eventTree: null,     // { jobId, event } — the selected job's rides and their moments
  gameFor: null,       // the job whose game listener has answered, so "none" is not said too early
  games: [],           // every match with a game record, for the games list
  sessions: [],
  sessionKey: null,    // the open session, which may not have a job yet
  scope: null,         // what this session is about — see scope.js
  jobId: null,
  job: null,
  moments: [],
  events: [],
  thinking: false,
  sessionId: null,
  sports: ['handball'],
  // The publish panel, when the popup's record column has been swapped for
  // it: { momentId, title, description, privacy, status, error, url }. It
  // lives here rather than on a message because the popup is not a message.
  share: null,
  // What the server says is configured for YouTube, and what someone has typed
  // into the settings panel but not saved yet.
  youtube: null,
  youtubeForm: null,
  // What the player is on: { key, momentId, rideOrder, start, end, label,
  // full, loop, rate }. key names the popup's player slot and stays put while
  // the range changes — a chip or Widen re-aims the same video.
  playing: null,
  upload: {
    file: null, sport: 'handball', status: 'idle', pct: 0, name: '', size: '', gcsUri: '',
    // What the match is called. Empty means "take it from the file or the
    // URL", which is what this did before there was anywhere to type one.
    title: '',
    src: 'file',              // 'file' | 'path' | 'stream' — one source, not three stacked
    hlsUrl: '',               // a VOD playlist to download
    live: { title: '', hlsUrl: '', start: '', end: '' },
  },
  pendingUploads: [],     // uploaded to GCS but never registered as a job
  thumbs: { urls: {}, asked: new Set() },  // momentId -> signed URL for its still
  details: null,          // the moment whose popup is open, and playing inside it
  // The title being edited: { jobId, value, at }. The transcript re-renders on
  // every Firestore write, so a half-typed name has to live here rather than in
  // the field, the same reason the ingest panel's inputs do.
  renaming: null,
  unsubscribe: [],
};

/* ─────────────────────────────────────────────────────────── utils ── */

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function clock(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '00:00';
  const t = Math.floor(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function dur(sec) {
  const m = Math.floor(sec / 60);
  return `${m}:${String(Math.round(sec % 60)).padStart(2, '0')}`;
}

function bytes(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

/**
 * Call the API with a live credential.
 *
 * An ID token lasts an hour and the SDK only renews it when something asks, so
 * a tab left open overnight sends a stale one and gets a 401 that reads as
 * "logged out". The retry below is the actual fix: on a 401 the token is force
 * refreshed once and the call repeated, which turns an expiry into a pause
 * nobody notices. Once only — a second 401 is a real authentication failure and
 * retrying it forever would hide that.
 */
async function api(path, options = {}) {
  const send = async (forceRefresh) => {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (state.user) {
      headers.Authorization = `Bearer ${await getIdToken(state.user, forceRefresh)}`;
    }
    return fetch(`${API}${path}`, { ...options, headers, credentials: 'include' });
  };

  let res = await send(false);
  if (res.status === 401 && state.user) res = await send(true);

  if (!res.ok) {
    // A 401 that survives a forced refresh is a session that has genuinely
    // ended — the token could not be renewed, or the account no longer exists.
    // Leaving the desk on screen under a dead credential is the state this
    // used to sit in: every card failing, nothing saying why. Signing out
    // hands the editor the sign-in screen, which is the honest answer and the
    // only way back.
    if (res.status === 401) sessionExpired();
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}


/**
 * End the session and show the sign-in screen.
 *
 * Called from two places, and only ever when the credential itself is the
 * problem: an api() 401 that a forced refresh did not fix, and a scheduled
 * refresh that Firebase refuses. Once, however many requests fail at the same
 * moment — a page mid-analysis has several in flight, and each of them
 * signing out would be several sign-outs and several re-renders.
 */
let sessionEnding = false;

function sessionExpired() {
  if (sessionEnding || !state.user) return;
  sessionEnding = true;
  signOutNow().finally(() => { sessionEnding = false; });
}


// Renewed well inside the hour an ID token lasts, so a long analysis never
// crosses an expiry with a stale credential. Firebase caches aggressively, so
// this is cheap: a refresh that is not needed does not go to the network.
const TOKEN_REFRESH_MS = 45 * 60 * 1000;
let refreshTimer = null;

/**
 * Apply the current locale to everything on the page.
 *
 * Static chrome carries `data-i18n`; the chat and its cards are re-rendered,
 * because they are built from templates that call t() as they run. Messages
 * already on screen keep their text — rewriting something the editor has
 * read would be worse than leaving it in the previous language.
 */
function applyLocale() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  if (!$('settings')?.classList.contains('hidden')) renderSettings();
  render();
}


function fillSelect(id, options, selected) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = options.map(
    (o) => `<option value="${esc(o.id)}"${o.id === selected ? ' selected' : ''}>${esc(o.name)}</option>`,
  ).join('');
}

/** Repaint the settings controls. Called on open and after a language change. */
function renderSettings() {
  const s = getSettings();
  fillSelect('set-locale', [
    { id: '', name: t('settings.followBrowser') },
    ...LOCALES.map((id) => ({ id, name: localeName(id) })),
  ], s.locale);
  fillSelect('set-metadata-language',
    METADATA_LANGUAGES.map((l) => ({ id: l.code, name: l.name })), s.metadataLanguage);
  fillSelect('set-theme', themeOptions(), s.theme);
  const stall = $('set-live-stall');
  if (stall) stall.value = String(s.liveStallMinutes);
  // Re-drawn here too so switching language relabels it; its own state comes
  // from the server and is not re-fetched by a repaint.
  renderYouTube();
}

function openSettings() {
  renderSettings();
  refreshYouTube();
  $('settings').classList.remove('hidden');
}


/* ── The channel moments are published to ─────────────────────────────────────

   Unlike everything else in Settings, this is not a per-device preference: it
   is one channel for the whole desk, and the refresh token behind it is a
   standing permission to post there. So it lives on the server and this only
   ever asks what is set — never what any of it is — and sends what someone
   typed. */

async function refreshYouTube() {
  try {
    state.youtube = await api('/api/integrations/youtube');
  } catch (err) {
    state.youtube = { error: err.message };
  }
  renderYouTube();
}


function renderYouTube() {
  const box = $('youtube-state');
  if (!box) return;
  const y = state.youtube;
  if (!y) { box.innerHTML = `<div class="setting-hint">${esc(t('youtube.loading'))}</div>`; return; }
  if (y.error) { box.innerHTML = `<div class="share-error">${esc(y.error)}</div>`; return; }

  const form = state.youtubeForm || {};
  const client = y.client_configured
    ? `<div class="yt-line is-on">${esc(y.client_from_deployment
      ? t('youtube.clientFromDeployment') : t('youtube.clientSet'))}</div>`
    : `<div class="yt-line">${esc(t('youtube.noClient'))}</div>`;

  // The client is normally Terraform's, so its two boxes stay folded away
  // until someone opens them: a deployment that already supplies a client
  // should not greet everyone with two empty secret fields.
  const clientFields = `
    <details class="yt-details" ${y.client_configured ? '' : 'open'}>
      <summary>${esc(t('youtube.clientFields'))}</summary>
      <label class="field-label" for="yt-client-id">${esc(t('youtube.clientId'))}</label>
      <input class="input" id="yt-client-id" data-youtube-field="clientId"
             autocomplete="off" value="${esc(form.clientId || '')}" />
      <label class="field-label" for="yt-client-secret">${esc(t('youtube.clientSecret'))}</label>
      <input class="input" id="yt-client-secret" type="password" autocomplete="off"
             data-youtube-field="clientSecret" value="${esc(form.clientSecret || '')}" />
      <label class="field-label" for="yt-refresh">${esc(t('youtube.refreshToken'))}</label>
      <input class="input" id="yt-refresh" type="password" autocomplete="off"
             data-youtube-field="refreshToken" value="${esc(form.refreshToken || '')}" />
      <div class="setting-hint">${esc(t('youtube.refreshHint'))}</div>
      <div class="yt-actions">
        <button class="btn-outline" data-youtube-act="save">${esc(t('youtube.save'))}</button>
      </div>
      ${y.redirect_uri ? `<div class="setting-hint">${esc(t('youtube.redirectUri'))}
        <code class="yt-uri">${esc(y.redirect_uri)}</code></div>` : ''}
    </details>`;

  const channel = y.connected
    ? `<div class="yt-line is-on">${esc(y.channel_title
        ? `${t('youtube.connectedTo')} ${y.channel_title}` : t('youtube.connected'))}</div>`
    : `<div class="yt-line">${esc(t('youtube.notConnected'))}</div>`;

  box.innerHTML = `
    ${client}
    ${channel}
    <div class="yt-actions">
      <button class="btn-accent" data-youtube-act="connect" ${y.can_connect ? '' : 'disabled'}>${
        esc(y.connected ? t('youtube.reconnect') : t('youtube.connect'))}</button>
      ${y.connected ? `<button class="link-btn" data-youtube-act="disconnect">${
        esc(t('youtube.disconnect'))}</button>` : ''}
    </div>
    ${y.can_connect ? '' : `<div class="setting-hint">${esc(t('youtube.cannotConnect'))}</div>`}
    ${clientFields}`;
}


async function onYouTubeAction(action) {
  if (action === 'connect') {
    try {
      const out = await api('/api/integrations/youtube/auth-url');
      // A new tab, not this one: coming back lands on the callback page, and
      // replacing the desk with it would lose an open session.
      window.open(out.url, '_blank', 'noopener');
    } catch (err) {
      state.youtube = { ...(state.youtube || {}), error: err.message };
      renderYouTube();
    }
    return;
  }
  if (action === 'disconnect') {
    if (!window.confirm(t('youtube.disconnectConfirm'))) return;
    try {
      await api('/api/integrations/youtube', { method: 'DELETE' });
    } catch (err) {
      state.youtube = { ...(state.youtube || {}), error: err.message };
    }
    await refreshYouTube();
    return;
  }
  if (action === 'save') {
    const form = state.youtubeForm || {};
    const body = {};
    // Only what was typed: an empty box means "leave it alone", because the
    // fields never show what is stored and blanking one would be a way to
    // wipe the client by opening the panel.
    if (form.clientId) body.client_id = form.clientId;
    if (form.clientSecret) body.client_secret = form.clientSecret;
    if (form.refreshToken) body.refresh_token = form.refreshToken;
    try {
      await api('/api/integrations/youtube', { method: 'PUT', body: JSON.stringify(body) });
      state.youtubeForm = null;
    } catch (err) {
      state.youtube = { ...(state.youtube || {}), error: err.message };
    }
    await refreshYouTube();
  }
}

function closeSettings() {
  $('settings').classList.add('hidden');
}

function mountSettings() {
  $('set-locale')?.addEventListener('change', (e) => {
    // An empty value means "follow the browser", which is a real choice rather
    // than an absent one — storing it lets a browser-language change take
    // effect later instead of pinning whatever it happened to be today.
    const chosen = e.target.value;
    saveSettings({ locale: chosen });
    setLocale(chosen || detectLocale());
    applyLocale();
    renderSettings();
  });

  $('set-metadata-language')?.addEventListener('change', (e) => {
    saveSettings({ metadataLanguage: e.target.value });
  });

  // Clamped on the way in rather than refused: the API holds the same bounds,
  // and a value it would reject is better corrected where it was typed than
  // discovered as a failed booking an hour before the event.
  $('set-live-stall')?.addEventListener('change', (e) => {
    const minutes = clampStallMinutes(e.target.value);
    saveSettings({ liveStallMinutes: minutes });
    e.target.value = String(minutes);
  });

  $('set-theme')?.addEventListener('change', (e) => {
    saveSettings({ theme: e.target.value });
    applyTheme(e.target.value);
  });

  // Clicking the backdrop closes; clicking the card must not.
  $('settings')?.addEventListener('click', (e) => {
    if (e.target.id === 'settings') closeSettings();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeSettings(); closeDetails(); toggleAccountMenu(false);
      if (closeTypeMenus()) render();
    }
  });
}


async function signOutNow() {
  try {
    // Drop the listeners before the credential goes, or Firestore reports a
    // permission error on the way out that looks like a bug.
    state.unsubscribe.forEach((off) => off());
    state.unsubscribe = [];
    destroyPlayer();
    state.sessionId = null;
    await signOut(auth);
  } catch (err) {
    console.warn('sign out failed', err);
  }
}


function keepSessionAlive(user) {
  clearInterval(refreshTimer);
  if (!user) return;
  refreshTimer = setInterval(() => {
    getIdToken(user, true).catch((err) => {
      // Firebase renews a token from a credential it still holds, so a refusal
      // here is that credential being gone — the account disabled, the session
      // revoked, the password changed on another device. A network blip is not
      // this: it resolves as a failed fetch inside the SDK and comes back on
      // the next tick, which is why only auth/* errors end the session.
      console.warn('token refresh failed', err);
      if (String(err?.code || '').startsWith('auth/')) sessionExpired();
    });
  }, TOKEN_REFRESH_MS);
}

/* ──────────────────────────────────────────────────────────── auth ── */

const fb = initializeApp({
  apiKey: CONFIG.firebaseApiKey,
  authDomain: CONFIG.firebaseAuthDomain,
  projectId: CONFIG.projectId,
});
const auth = getAuth(fb);
const db = getFirestore(fb);

/**
 * Sign-in.
 *
 * Email/password is the primary path because it is what the Identity Platform
 * tenant actually has enabled. A federated button is only shown when a provider
 * is configured — offering one that is not enabled just yields
 * auth/operation-not-allowed, which reads as a bug rather than a setting.
 */
function signinError(err) {
  const box = $('signin-error');
  const code = err?.code || '';
  const message = {
    'auth/invalid-credential': t('auth.badCredentials'),
    'auth/wrong-password': t('auth.badCredentials'),
    'auth/user-not-found': t('auth.noAccount'),
    'auth/unauthorized-domain': t('auth.unauthorizedDomain'),
    'auth/operation-not-allowed': t('auth.notEnabled'),
  }[code] || err?.message || t('auth.failed');
  box.textContent = message;
  box.classList.remove('hidden');
}

function busy(on) {
  $('signin-submit').disabled = on;
}

$('signin-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('signin-error').classList.add('hidden');
  busy(true);
  try {
    await signInWithEmailAndPassword(
      auth, $('signin-email').value.trim(), $('signin-password').value,
    );
  } catch (err) {
    signinError(err);
  } finally {
    busy(false);
  }
});

$('google-btn').addEventListener('click', async () => {
  $('signin-error').classList.add('hidden');
  try {
    await signInWithPopup(auth, new GoogleAuthProvider());
  } catch (err) {
    signinError(err);
  }
});

// Reveal the federated button only if the project has a provider configured.
(async () => {
  try {
    const cfg = await fetch(`${API}/api/config`, { credentials: 'include' });
    const { federated_providers: providers = [] } = await cfg.json();
    if (providers.length) {
      $('signin-alt').classList.remove('hidden');
      $('google-btn').classList.remove('hidden');
    }
  } catch { /* leave it hidden */ }
})();

// Survive a closed tab. The SDK defaults to local persistence, but saying so
// makes it a decision rather than a default someone can change underneath us —
// and a session that silently became in-memory would look exactly like the
// expiry complaint this is meant to fix.
const initialSettings = loadSettings();
state.sessions = loadSessions();
applyTheme(initialSettings.theme);
setLocale(initialSettings.locale || detectLocale());
mountSettings();
applyLocale();

setPersistence(auth, browserLocalPersistence).catch((err) => {
  console.warn('could not set auth persistence', err);
});

onAuthStateChanged(auth, async (user) => {
  state.user = user;
  renderAccount();
  keepSessionAlive(user);
  // Restoring a stored session is asynchronous and fires null first. Showing
  // the sign-in card in that gap makes every reload look like a logout.
  document.body.dataset.authResolved = '1';
  $('signin').classList.toggle('hidden', !!user);
  $('app').classList.toggle('hidden', !user);
  if (!user) {
    // Nothing of the previous session stays on screen behind the sign-in card:
    // it is another person's desk until someone signs in again, and a popup
    // left open over it would still be playing their match.
    closeDetails();
    closeSettings();
    state.msgs = [];
    state.jobs = [];
    state.jobId = null;
    state.job = null;
    state.moments = [];
    state.events = [];
    state.game = null;
    state.share = null;
    state.youtube = null;
    state.youtubeForm = null;
    render();
    return;
  }

  state.msgs = [];
  render();
  watchJobs();
  watchGames();
  // An empty screen is not a starting point. With nothing stored this used to
  // paint a blank transcript and wait for the editor to find the + in the
  // rail; the opener is the thing that says what this desk can do, and it only
  // exists inside a session.
  if (state.sessions.length) openSession(state.sessions[0].id);
  else startSession();
  try {
    const cfg = await api('/api/config');
    if (cfg.supported_sports?.length) {
      state.sports = cfg.supported_sports;
      state.upload.sport = cfg.supported_sports[0];
    }
  } catch { /* the sport list falls back to the default */ }
  refreshPendingUploads();
});


/**
 * Look for uploads that reached the bucket but never became a job, so a match
 * that failed to register can be picked up instead of sent again.
 */
async function refreshPendingUploads() {
  try {
    const { uploads = [] } = await api('/api/jobs/pending-uploads');
    state.pendingUploads = uploads;
    if (uploads.length) render();
  } catch { /* the button simply does not appear */ }
}

/* ────────────────────────────────────────────────────── realtime ── */

function watchGames() {
  // Every game record on the desk. Separate from watchJobs because a job has a
  // game record only once its analysis has finished, and the two lists answer
  // different questions: what is being worked on, and what has been done.
  onSnapshot(
    query(collection(db, 'games'), limit(200)),
    (snap) => {
      state.games = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      render();
    },
    (err) => console.error('games listener', err),
  );
}


function watchJobs() {
  // Every job, not just this user's. Matches are shared across the desk, and
  // ordering by createdAt alone needs no composite index.
  onSnapshot(
    query(collection(db, 'jobs'), orderBy('createdAt', 'desc'), limit(50)),
    (snap) => {
      state.jobs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      // No session is created per job — the sidebar lists conversations, and a
      // match exists perfectly well without anyone having talked about it. But
      // something has to be selected or the per-job listeners never start and
      // every card that reads moments or the game record is empty
      // whatever Firestore holds.
      ensureJobContext();
      render();
    },
    (err) => console.error('jobs listener', err),
  );
}

/**
 * Make sure some match is in context.
 *
 * The moments, events and game record are all read through listeners
 * opened by selectJob, so with nothing selected the cards are empty however
 * much has been analysed — which reads as "the analysis found nothing" rather
 * than "no match is open". The most recent one is the useful default: it is
 * what someone just uploaded, or what the desk is working on.
 *
 * A session that names its own match wins, and this never overrides it.
 */
function ensureJobContext() {
  if (state.jobId || !state.jobs.length) return;
  const session = state.sessionKey
    ? listSessions().find((s) => s.id === state.sessionKey)
    : null;
  if (session?.jobId) return;
  const inScope = gamesInScope(state.scope, state.games);
  selectJob(inScope[0] ? (inScope[0].jobId || inScope[0].id) : state.jobs[0].id);
}


function selectJob(jobId) {
  state.unsubscribe.forEach((fn) => fn());
  state.unsubscribe = [];
  state.jobId = jobId;
  state.moments = [];
  state.game = null;
  state.gameFor = null;
  state.eventTree = null;
  eventTreeKey = '';
  clearTimeout(eventTreeTimer);
  state.events = [];
  state.playing = null;
  // Signed per job and per moment id, so nothing here survives the switch.
  state.thumbs = { urls: {}, asked: new Set() };
  playbackUrl = null;
  destroyPlayer();

  state.unsubscribe.push(onSnapshot(doc(db, 'jobs', jobId), (snap) => {
    if (snap.exists()) { state.job = { id: snap.id, ...snap.data() }; render(); }
  }));

  // The game record lives in its own top-level collection, keyed by job id, so
  // it is a separate listener rather than part of the job document.
  state.unsubscribe.push(onSnapshot(doc(db, 'games', jobId), (snap) => {
    state.game = snap.exists() ? snap.data() : null;
    state.gameFor = jobId;
    refreshEventTree();
    render();
  }, () => { state.game = null; state.gameFor = jobId; }));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'moments'), orderBy('startSec', 'asc'), limit(500)),
    (snap) => {
      state.moments = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      refreshEventTree();
      render();
    },
  ));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'events'), orderBy('ts', 'desc'), limit(80)),
    (snap) => { state.events = snap.docs.map((d) => ({ id: d.id, ...d.data() })); render(); },
  ));
}


/**
 * Fetch the open match's event tree — its rides and the moments in each — when
 * it has rides.
 *
 * Which ride a moment belongs to is the catalog's decision, not the browser's
 * (GET /api/jobs/{id}/event), so this asks rather than works it out. It asks
 * again only when something the answer depends on changed: the rides, or which
 * ride any moment is joined to. Debounced, because an analysis writing moments
 * fires the listener many times in a row.
 */
let eventTreeTimer = null;
let eventTreeKey = '';

function refreshEventTree() {
  clearTimeout(eventTreeTimer);
  const jobId = state.jobId;
  const rides = Array.isArray(state.game?.rides) ? state.game.rides : [];
  if (!jobId || !rides.length) return;

  const key = [
    jobId,
    rides.map((r) => `${r.order}@${r.start_sec}-${r.end_sec}`).join(','),
    state.moments.map((m) => `${m.momentId}:${m.rideOrder ?? ''}`).join(','),
  ].join('|');
  if (key === eventTreeKey) return;

  eventTreeTimer = setTimeout(async () => {
    eventTreeKey = key;
    try {
      const { event } = await api(`/api/jobs/${encodeURIComponent(jobId)}/event`);
      if (state.jobId !== jobId) return;
      state.eventTree = { jobId, event };
      render();
    } catch (err) {
      // The flat list still stands; the next change asks again.
      eventTreeKey = '';
      console.warn('event tree', err);
    }
  }, 400);
}

/* ────────────────────────────────────────────────────── transcript ── */

function push(msg) {
  state.msgs.push(msg);
  persistTranscript();
  render();
  scrollDown();
}

/**
 * Keep the conversation on its session, so switching away and back finds it.
 *
 * The last eighty turns, with the agent's session id beside them so the
 * agent's own memory of the conversation continues too. Big search results
 * are dropped: they are re-run in a click and they are the bulk of the bytes.
 */
function persistTranscript() {
  if (!state.sessionKey) return;
  const msgs = state.msgs.slice(-80).map((m) => {
    const copy = { ...m };
    if (Array.isArray(copy.searchResults) && copy.searchResults.length > 20) copy.searchResults = null;
    // A menu open when the session was saved is not one to reopen on load.
    delete copy.typesOpen;
    return copy;
  });
  updateSession(state.sessionKey, { msgs, agentSessionId: state.sessionId });
}

function say(text, extra = {}) { push({ who: 'agent', text, ...extra }); }

function scrollDown() {
  [30, 140, 340].forEach((d) => setTimeout(() => {
    const el = $('scroll');
    if (el) el.scrollTop = el.scrollHeight;
  }, d));
}

function momentById(id) {
  return state.moments.find((m) => m.momentId === id || m.id === id);
}

/* ──────────────────────────────────────────────────── card markup ── */

/**
 * A card that says why it is empty.
 *
 * Returning '' renders nothing, which is indistinguishable from a card that
 * failed to render — and the two have very different answers. "No moments yet"
 * is information; a blank space is a bug report.
 */
/**
 * The sessions list down the left.
 *
 * One entry per conversation, newest first. A session notes which job it is
 * about, so the row shows that job's live status through the same Firestore
 * listener the cards use — it follows an analysis rather than needing a
 * refresh of its own.
 */
/**
 * The signed-in account, at the foot of the rail.
 *
 * Firebase gives a displayName only when something set one, and these accounts
 * are provisioned by hand in Identity Platform — so the email is the name in
 * practice, and the part before the @ is what a person recognises. The avatar
 * is its first letter rather than a photo: there is no photo to have.
 */
function renderAccount() {
  const name = state.user?.displayName
    || (state.user?.email || '').split('@')[0]
    || '';
  $('account-name').textContent = name;
  $('account-initial').textContent = name.slice(0, 1);
  $('account-btn').title = state.user?.email || name;
}


/** Open or shut the account menu, and say which it is for a screen reader. */
function toggleAccountMenu(open) {
  const menu = $('account-menu');
  const shut = open === undefined ? !menu.classList.contains('hidden') : !open;
  menu.classList.toggle('hidden', shut);
  $('account-btn').setAttribute('aria-expanded', String(!shut));
}


function renderSessions() {
  const list = $('sessions-list');
  if (!list) return;

  if (!state.sessions.length) {
    list.innerHTML = `<div class="sessions-empty">${esc(t('sessions.empty'))}</div>`;
    return;
  }

  const jobsById = new Map(state.jobs.map((j) => [j.id, j]));

  // Sessions arrive newest first, so "today" is a prefix of the list and the
  // separator goes in where the day changes. Rendering the label from inside
  // the map — rather than as two pre-built lists — is what keeps a heading
  // from appearing with nothing under it.
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  let group = '';

  list.innerHTML = state.sessions.map((session) => {
    const job = session.jobId ? jobsById.get(session.jobId) : null;
    const running = job && ['analyzing', 'transcoding', 'uploaded'].includes(job.status);
    const failed = job && (job.status === 'failed' || job.status === 'rejected');
    const tone = failed ? 'failed' : running ? 'running' : 'idle';

    // A session with no job yet is a conversation waiting for a match. Saying
    // so is better than showing it blank, which reads as a broken row.
    const meta = !job ? esc(t('sessions.noMatch'))
      : running && !isStalled(job)
        ? `${esc(job.stage || job.status)} · ${Math.round(job.progress || 0)}%`
        : esc(job.status || '');
    const stamp = new Date(session.createdAt || Date.now());
    const bucket = stamp >= startOfToday ? 'today' : 'earlier';
    const heading = bucket === group ? '' : `
      <div class="session-group">${esc(t(`sessions.${bucket}`))}</div>`;
    group = bucket;

    return `${heading}
      <div class="session-row">
        <button class="session" data-session="${esc(session.id)}" data-tone="${tone}"
                aria-current="${session.id === state.sessionKey}">
          <div class="session-name">${esc(
            session.title || job?.title || t('sessions.untitled'))}</div>
          <div class="session-meta">${meta} · ${
            stamp.toLocaleDateString(getLocale())}</div>
        </button>
        <button class="session-delete" data-delete-session="${esc(session.id)}"
                title="${esc(t('jobs.delete'))}" aria-label="${esc(t('jobs.delete'))}">&times;</button>
      </div>`;
  }).join('');
}


// Ten rows is about a screen. A match yields a couple of hundred moments, and
// a card that showed all of them would bury the conversation it is part of —
// while one that silently stopped at six looked like the analysis found six.
// Paging is how both are avoided: everything is reachable, a screen at a time.
const PAGE_SIZE = 10;


function pageOf(items, page, size = PAGE_SIZE) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(0, page || 0), pages - 1);
  const from = current * size;
  return {
    slice: items.slice(from, from + size),
    current,
    pages,
    from: from + 1,
    to: Math.min(from + size, items.length),
    total: items.length,
    size,
  };
}


function pagerRow(view, index) {
  if (view.total <= view.size) return '';
  return `
    <div class="pager">
      <button class="link-btn" data-page="${index}:${view.current - 1}"
              ${view.current === 0 ? 'disabled' : ''}>${esc(t('pager.previous'))}</button>
      <div class="pager-count">${view.from}–${view.to} ${esc(t('pager.of'))} ${view.total}</div>
      <button class="link-btn" data-page="${index}:${view.current + 1}"
              ${view.current >= view.pages - 1 ? 'disabled' : ''}>${esc(t('pager.next'))}</button>
    </div>`;
}


function emptyCard(message) {
  return `<div class="panel-light"><div class="job">
    <div class="job-stage">${esc(message)}</div></div></div>`;
}


/** The match a question names, resolved against the titles already loaded. */
function gameNamedIn(question) {
  return namedGame(question, state.games, gameHeadline);
}


/**
 * The moments a message is about: which ones, and in what order.
 *
 * Ids are held on the message so scrolling back to an earlier answer finds
 * what it said. A question naming another match has no ids yet — selectJob's
 * listener has not answered — so it reads the live list instead, and only
 * while that match is still the open one.
 */
function momentsFor(msg) {
  const all = msg.momentIds
    ? msg.momentIds.map(momentById).filter(Boolean)
    : (!msg.jobId || msg.jobId === state.jobId ? [...state.moments] : []);

  return selectMoments(all, {
    terms: msg.showAll ? [] : (msg.terms || []),
    half: msg.showAll ? null : msg.half,
    sort: msg.sort,
  });
}


/**
 * What the list is showing, and how it is ordered.
 *
 * The filter has to be visible or it cannot be trusted: 42 rows where there
 * were 346 is indistinguishable from an analysis that found 42, and a word the
 * taxonomy does not use silently doing nothing is worse still.
 */
/**
 * What a filtered list is showing, in the three states it can be in.
 *
 * Narrowed, nothing matched, or no filter at all. The distinction has to be on
 * screen or the filter cannot be trusted: 2 rows where there were 40 is
 * indistinguishable from a desk with 2 games, and a word the vocabulary does
 * not use silently doing nothing is worse still.
 */
function filterTitle(view, index, fallback) {
  const asked = [
    ...view.terms,
    ...(view.half ? [t(`moments.half.${view.half}`)] : []),
  ].join(', ');

  if (view.narrowed) {
    return `
      <div class="panel-head-title">
        ${esc(t('list.filtered'))} ${esc(asked)}
        · ${view.list.length} ${esc(t('pager.of'))} ${view.total}
        <button class="link-btn" data-show-all="${index}">${esc(t('list.showAll'))}</button>
      </div>`;
  }
  if (view.missed) {
    return `<div class="panel-head-title">${esc(t('list.noMatch'))} ${esc(asked)}</div>`;
  }
  return `<div class="panel-head-title">${esc(fallback)}</div>`;
}



/**
 * A list panel's head: what it is showing, how many, and any controls.
 *
 * filterTitle carries the three filter states — narrowed, nothing matched, or
 * no filter at all — and the count sits opposite it, because "42 of 346" and
 * "42" answer different questions and both are worth having on screen.
 */
function listHead(view, index, fallback, extra = '') {
  return `
    <div class="list-head">
      ${filterTitle(view, index, fallback)}
      <div class="panel-head-meta">
        ${view.narrowed || view.missed ? '' : `<span class="list-count">${view.list.length}</span>`}
        ${extra}
      </div>
    </div>`;
}


function momentsHead(view, index) {
  const sort = view.sort === 'time' ? 'time' : 'score';
  return listHead(view, index, t('moments.title'), ['score', 'time'].map((key) => `
    <button class="link-btn" data-sort="${index}:${key}"
            aria-pressed="${key === sort}">${esc(t(`moments.sort.${key}`))}</button>`).join(''));
}


/**
 * One moment as a tile.
 *
 * The picture is the way in: a play button sits on it, and the whole frame
 * opens the moment in the player, where its record, its ride and the
 * download and publish buttons are. The buttons that used to sit at the foot
 * of every tile are gone — a row of them repeated down a page was most of what
 * the page said, and the player answers all of it.
 *
 * The summary is still the headline — the line an editor scans by is who did
 * what, not what the taxonomy calls it — clamped rather than cut at a
 * character count, because where a sentence can lose words without losing its
 * subject depends on the sentence. The foot holds the two readings an editor
 * weighs a moment by: what kind of moment the model called it, and how sure it
 * was. Both below the picture rather than on it: a still from a match is not a
 * flat colour, and text laid straight onto it is legible against a dark crowd
 * and gone against a bright floor. The star and the play button are the
 * exceptions, because each carries its own ground wherever it lands.
 */
function momentTile(m, opts = {}) {
  // A moment from another game cannot be played through the open match's
  // listeners, which do not hold it. opts.open routes the picture through the
  // row payload instead, and opts.game puts the match on the tile — across the
  // desk a moment without its match is a sentence without a subject.
  const meta = [
    // H.No and rider first: on a competition day that is what a tile is
    // scanned for. A schedule-inferred name is marked with a tilde. Not inside
    // a ride group, whose heading already says it once for every tile.
    m.rider && !opts.inRide
      ? `${m.startNumber ? `#${m.startNumber} ` : ''}${m.identitySource === 'schedule' ? '~' : ''}${m.rider}` : '',
    `${Math.round(m.endSec - m.startSec)}s`,
  ].filter(Boolean).join(' \u00b7 ');
  const kind = m.label || m.momentType || '';
  const sure = m.confidence == null ? null : Math.round(Math.min(Math.max(Number(m.confidence), 0), 1) * 100);

  return `
    <div class="tile">
      <button class="thumb" ${opts.open ? `data-search-open="${esc(opts.open)}"` : `data-play="${esc(m.momentId)}"`}
              title="${esc(t('moment.play'))}" aria-label="${esc(`${t('moment.play')}: ${m.summary || kind}`)}"
              ${m.thumbUri && !state.thumbs.urls[m.momentId]
                ? `data-thumb="${esc(m.momentId)}"${m.jobId && m.jobId !== state.jobId ? ` data-thumb-job="${esc(m.jobId)}"` : ''}`
                : ''}>
        ${state.thumbs.urls[m.momentId]
          ? `<img src="${esc(state.thumbs.urls[m.momentId])}" alt="" loading="lazy">`
          : '<span class="thumb-stripes"></span>'}
        <span class="thumb-play" aria-hidden="true"></span>
        <span class="thumb-clock">${clock(m.startSec)}</span>
      </button>
      ${opts.game ? `<div class="tile-game">${esc(opts.game.title || m.jobId || '')}${
        opts.game.discipline || opts.game.sport ? ` · ${esc(opts.game.discipline || opts.game.sport)}` : ''}</div>` : ''}
      <div class="tile-name">${esc(m.summary || kind)}</div>
      <div class="tile-meta">${esc(meta)}</div>
      ${m.rerankReason ? `<div class="tile-why">${esc(m.rerankReason)}</div>` : ''}
      <dl class="tile-facts">
        <div class="tile-fact">
          <dt>${esc(t('moment.type'))}</dt>
          <dd class="tile-kind">${esc(kind || '—')}</dd>
        </div>
        <div class="tile-fact">
          <dt>${esc(t('moment.confidence'))}</dt>
          <dd class="tile-sure">${sure == null ? '—' : `${sure}%`}
            ${sure == null ? '' : `<span class="sure-meter" aria-hidden="true"><span style="width:${sure}%"></span></span>`}</dd>
        </div>
      </dl>
    </div>`;
}

function momentsCard(msg, index) {
  const found = momentsFor(msg);
  if (!found.list.length) return emptyCard(t('moments.none'));

  const event = eventFor(msg);
  if (event) return rideGroupsCard(msg, index, found, event);

  const view = pageOf(found.list, msg.page);

  // The row scrolls sideways within a page rather than instead of one. A match
  // yields a couple of hundred moments, and one scroller holding all of them is
  // the truncation problem in the other axis: everything present, nothing
  // findable, and no way to tell how much is left.
  return `
    <div class="list">
      ${momentsHead({ ...found, sort: msg.sort }, index)}
      <div class="tile-row">${view.slice.map(momentTile).join('')}</div>
      ${pagerRow(view, index)}
    </div>`;
}


/**
 * The open event's tree, when this message is about it and it has rides.
 *
 * Only for the open job: the tree is fetched for the match whose listeners are
 * running, and an earlier answer about another match keeps the flat list it
 * was given rather than borrowing this one's rides.
 */
function eventFor(msg) {
  const tree = state.eventTree;
  const jobId = msg.jobId || state.jobId;
  if (!tree || tree.jobId !== jobId) return null;
  return tree.event?.riders?.length ? tree.event : null;
}


// Rides per page. A ride is a heading and a row of tiles, so a page of them is
// already long; a class of forty is forty headings, which is what the pager is for.
const RIDES_PER_PAGE = 4;


/**
 * A competition day as event, then rides, then moments.
 *
 * The same filter, sort and count as the flat list — the head is shared — but
 * the tiles sit under the ride they happened in, each ride headed by who rode
 * it and how it scored. The event's own details stay a click away in the game
 * popup; the heading names the event so the groups have something to belong to.
 */
function rideGroupsCard(msg, index, found, event) {
  const groups = groupByRide(event, found.list, { filtered: found.narrowed });
  const view = pageOf(groups, msg.page, RIDES_PER_PAGE);

  return `
    <div class="list">
      ${momentsHead({ ...found, sort: msg.sort }, index)}
      ${eventHead(event)}
      ${view.slice.map(rideGroup).join('')}
      ${pagerRow(view, index)}
    </div>`;
}


/**
 * Which event a question about rides is about.
 *
 * A named match first, when it has rides. Then the event that ran the rider
 * or horse the question names — in the session's scope before the whole desk,
 * and the open event whenever it is one of them, so asking about someone who
 * rode here does not jump elsewhere. With no name, the open event if it has
 * rides, else the first in scope that does.
 */
function rideJobFor(question) {
  const idOf = (g) => g.jobId || g.id;
  const hasRides = (g) => Array.isArray(g.rides) && g.rides.length > 0;
  const named = gameNamedIn(question);
  if (named && hasRides(named)) return idOf(named);

  const inScope = gamesInScope(state.scope, state.games).filter(hasRides);
  const everywhere = state.games.filter(hasRides);
  const riding = (games) => games.filter((g) => g.rides.some((r) => rideNamedIn(question, r)));
  const byName = riding(inScope).length ? riding(inScope) : riding(everywhere);
  const pool = byName.length ? byName : inScope;
  const open = pool.find((g) => idOf(g) === state.jobId);
  if (open) return state.jobId;
  if (!byName.length && hasRides(state.game || {})) return state.jobId;
  return pool[0] ? idOf(pool[0]) : state.jobId;
}


/**
 * What a rides card has to show, or why it has nothing.
 *
 * `asked` only when there are rides to show — cardAnswersIt reads it, so the
 * agent's reply stays visible behind every empty state.
 */
function ridesFor(msg) {
  const jobId = msg.jobId || state.jobId;
  // The tree is the open event's; an earlier answer about another event says
  // so rather than borrowing this one's rides.
  if (!jobId || jobId !== state.jobId) return { empty: t('rides.elsewhere') };
  if (state.gameFor !== jobId) return { empty: t('rides.loading'), loading: true };
  const stored = Array.isArray(state.game?.rides) ? state.game.rides : [];
  if (!stored.length) return { empty: t('rides.none') };
  const tree = state.eventTree;
  if (!tree || tree.jobId !== jobId) return { empty: t('rides.loading'), loading: true };

  const moments = selectMoments(state.moments, { sort: msg.sort }).list;
  const all = groupByRide(tree.event, moments).filter((g) => g.ride);
  if (!all.length) return { empty: t('rides.none') };
  const asked = ridesAsked(all, msg.rideQuery || '');
  if (!asked.groups.length) return { empty: t('rides.noMatch'), unchecked: asked.unchecked };
  return { event: tree.event, total: all.length, asked };
}


/**
 * The rides board while an event's rides are still arriving.
 *
 * Opening an event starts its listeners and asks the catalog for its tree,
 * which takes a moment on a day of forty rides — and a line of grey text in an
 * empty card read as "nothing here" rather than "coming". So the wait says
 * what it is doing, with something moving, over an outline of the board it is
 * about to become: riders on the left, moments on the right. Announced as a
 * status for screen readers; still, not spinning, for anyone who has asked
 * for less motion.
 */
function ridesLoading() {
  const rows = Array.from({ length: 5 }, () => `
    <div class="skel-row"><span class="skel skel-name"></span><span class="skel skel-line"></span></div>`).join('');
  const tiles = Array.from({ length: 3 }, () => `
    <div class="skel-tile"><span class="skel skel-frame"></span>
      <span class="skel skel-line"></span><span class="skel skel-line skel-short"></span></div>`).join('');
  return `
    <div class="rides-loading" role="status" aria-live="polite">
      <div class="rides-loading-head">
        <span class="loading-ring" aria-hidden="true"></span>
        <div>
          <div class="rides-loading-title">${esc(t('rides.loading'))}</div>
          <div class="rides-loading-hint">${esc(t('rides.loadingHint'))}</div>
        </div>
      </div>
      <div class="rides-loading-board" aria-hidden="true">
        <div class="skel-rail">${rows}</div>
        <div class="skel-pane">${tiles}</div>
      </div>
    </div>`;
}


/**
 * The rides a question asked for, as a board: who on the left, what on top.
 *
 * A competition day asks two questions at once — which round, and which
 * movement — and one column of rides answers only the first. Finding the
 * half-pass somebody asked about meant opening forty headings and reading
 * every tile under each of them. So the two axes are separated: the rail is
 * the running order and stays where it is while the pane changes, the type
 * filter crosses every ride at once, and the pane is one ride's moments.
 *
 * The count is moments rather than rides, because the pane is what it
 * describes: this ride's visible moments against everything the event holds.
 */
function ridesCard(msg, index) {
  const found = ridesFor(msg);
  if (found.loading) return ridesLoading();
  if (!found.asked) {
    const note = found.unchecked
      ? ` ${t('rides.unchecked').replace('{n}', String(found.unchecked))}` : '';
    return emptyCard(`${found.empty}${note}`);
  }

  const { asked, event } = found;
  // A type the moments no longer carry is dropped rather than left selected:
  // the filter lives on the message and outlives a re-render, while an
  // analysis still running changes what there is to choose from.
  const types = momentTypesIn(asked.groups);
  const picked = (msg.types || []).filter((key) => types.some((ty) => ty.key === key));
  const sort = msg.sort === 'time' ? 'time' : 'score';
  // A score bar already ranked these by total, and that is the order a question
  // about scores is asking to see — the board's sort does not overrule it.
  const shown = asked.score
    ? filterByTypes(asked.groups, picked)
    : sortRideGroups(filterByTypes(asked.groups, picked), sort);
  const current = shown.find((g) => String(g.ride.order) === String(msg.ride)) || shown[0];
  if (!current) return emptyCard(t('rides.noMatch'));
  // The type counts are of this ride before the type filter — of the live
  // moments the tiles are drawn from, not the tree's own list, or a count of
  // three beside a filter that yields two tiles reads as a broken card.
  const unfiltered = asked.groups.find((g) => g.ride === current.ride) || current;

  const all = asked.groups.reduce((n, g) => n + g.moments.length, 0);
  const bar = asked.score ? `${asked.score.inclusive ? '≥' : '>'} ${asked.score.min}%` : '';
  const title = [t('rides.title'), ...asked.names, bar].filter(Boolean).join(' · ');

  return `
    <div class="list rides-board-card">
      <div class="list-head">
        <div class="panel-head-title">${esc(title)}</div>
        <div class="panel-head-meta">
          <span class="list-count">${current.moments.length} ${esc(t('pager.of'))} ${all}</span>
          ${segmentedSort('sort', index, sort, t('rides.sortEvent'))}
        </div>
      </div>
      ${eventHead(event, { prominent: true })}
      ${asked.unchecked
        ? `<div class="ride-group-empty">${esc(t('rides.unchecked').replace('{n}', String(asked.unchecked)))}</div>`
        : ''}
      <div class="ride-board">
        <div class="ride-rail">
          <div class="ride-tabs-head">${esc(t('rides.riderHorseTiming'))}</div>
          <div class="ride-tabs" role="tablist" aria-label="${esc(t('rides.riderHorseTiming'))}">
            ${shown.map((group) => rideTab(group, index, current)).join('')}
          </div>
        </div>
        ${ridePane(current, index, msg, types, picked, unfiltered.moments, sort)}
      </div>
    </div>`;
}


/**
 * The moment-type axis: one dropdown, many choices at once.
 *
 * A dropdown rather than a row of chips because a class contains a dozen
 * movements and a row of them becomes a second navigation competing with the
 * rail — the rail is what the eye follows here, and the filter should stay one
 * control. What is chosen comes back out as chips beside it, so a narrowed
 * board says what narrowed it without being opened.
 *
 * The list is the whole event's types (`momentTypesIn`), so it does not
 * reshuffle when another rider is opened; the number beside each is the open
 * ride's (`countTypesIn`), because that is the ride being looked at. They are
 * the types the moments actually carry rather than the sport's catalogue: a
 * dressage catalogue is thirty movements where a class holds six, and a filter
 * offering two dozen choices that match nothing is one nobody trusts.
 *
 * Open state lives on the message, as the page and the sort do: render()
 * rebuilds the transcript, so anything the DOM would have held is lost.
 */
function typeFilter(types, picked, index, open, moments) {
  if (!types.length) return '';
  const counts = countTypesIn(moments);
  const chosen = types.filter((ty) => picked.includes(ty.key));
  let label = t('rides.allTypes');
  if (chosen.length === 1) label = chosen[0].label;
  else if (chosen.length) label = t('rides.typesSelected').replace('{n}', String(chosen.length));

  return `
    <div class="ride-filter">
      <div class="type-filter">
        <button class="type-filter-toggle" data-type-menu="${index}" aria-expanded="${open}">
          <span class="type-filter-label">${esc(label)}</span>
          <span class="type-filter-caret" aria-hidden="true">${open ? '▲' : '▼'}</span>
        </button>
        ${open ? `
          <div class="type-menu" role="group" aria-label="${esc(t('rides.types'))}">
            <div class="type-menu-head">
              <span class="type-menu-title">${esc(t('rides.types'))}</span>
              <button class="link-btn" data-type-pick="${index}:">${esc(t('rides.clear'))}</button>
            </div>
            <div class="type-menu-list">
              ${types.map((ty) => `
                <button class="type-opt" data-type-pick="${esc(`${index}:${ty.key}`)}"
                        aria-pressed="${picked.includes(ty.key)}">
                  <span class="type-opt-box" aria-hidden="true"></span>
                  <span class="type-opt-name">${esc(ty.label)}</span>
                  <span class="list-count">${counts[ty.key] || 0}</span>
                </button>`).join('')}
            </div>
          </div>` : ''}
      </div>
      ${chosen.map((ty) => `
        <button class="type-chip" data-type-pick="${esc(`${index}:${ty.key}`)}"
                title="${esc(t('rides.removeType'))}">
          <span>${esc(ty.label)}</span><span class="type-chip-x" aria-hidden="true">&times;</span>
        </button>`).join('')}
    </div>`;
}


/**
 * Close every open moment-type menu but the one named, if any.
 *
 * The open state lives on the message (render() rebuilds the transcript), so
 * closing is a state change the caller then renders. Returns whether anything
 * closed, so a click that changed nothing does not redraw the page.
 */
function closeTypeMenus(except = null) {
  let closed = false;
  state.msgs.forEach((m, i) => {
    if (m.typesOpen && String(i) !== String(except)) {
      m.typesOpen = false;
      closed = true;
    }
  });
  return closed;
}


/**
 * Best first or match order, as one choice with two states.
 *
 * Two of them are on the board and they answer different questions: the one in
 * the head orders the rides — best first puts the round holding the day's
 * strongest moment at the top of the rail — and the one in the pane orders the
 * moments of the round that is open. They used to be a single control, so
 * asking for the best moment of one rider re-sorted every rider, and asking
 * for the day's best could not be asked at all.
 */
function segmentedSort(attr, index, current, label) {
  return `
    <div class="segmented" role="group" aria-label="${esc(label)}">
      ${['score', 'time'].map((key) => `
        <button class="seg-btn" data-${attr}="${index}:${key}"
                aria-pressed="${key === current}">${esc(t(`moments.sort.${key}`))}</button>`).join('')}
    </div>`;
}


const rideTabId = (index, ride) => `ride-tab-${index}-${ride.order ?? 'x'}`;


/**
 * One ride as a tab: who rode and where they stand, on what and when, and how
 * much the analysis found in it.
 *
 *     Loretta Joynson                          Rank 2
 *     Tresais Lancelot · 2:25:15–2:31:58
 *     5 moments found
 *
 * The rider is what the rail is read down, so it is the line in the sans; the
 * horse, the span and the count qualify it and take the mono, as every reading
 * in this app does. The rank is the current placing (rideRank) and says TBD
 * when there is none yet — a live class before its results, a round with no
 * total on screen — rather than leaving a gap that reads as last. The count is
 * what the type filter leaves, so a rider who did none of the chosen movement
 * reads "no moments" rather than disappearing.
 */
function rideTab({ ride, moments }, index, current) {
  const who = `${ride.startNumber ? `#${ride.startNumber} ` : ''}${
    ride.identitySource === 'schedule' ? '~' : ''}${ride.rider || '—'}`;
  const under = [ride.horse, `${clock(ride.startSec)}–${clock(ride.endSec)}`]
    .filter(Boolean).join(' · ');
  const published = [ride.groundedRider, ride.groundedHorse].filter(Boolean).join(' / ');
  const rank = rideRank(ride);
  const found = moments.length === 0 ? t('rides.noMoments')
    : moments.length === 1 ? t('rides.oneMoment')
      : t('rides.momentsFound').replace('{n}', String(moments.length));

  return `
    <button class="ride-tab" role="tab" aria-selected="${ride === current.ride}"
            id="${esc(rideTabId(index, ride))}"
            data-ride-tab="${esc(`${index}:${ride.order ?? ''}`)}"
            ${published ? `title="${esc(published)}"` : ''}>
      <span class="ride-who">
        <span class="ride-name-line">
          <span class="ride-rider">${esc(who)}</span>
          <span class="ride-rank" data-known="${rank != null}"
                ${rank == null ? `title="${esc(t('rides.rankTbdHint'))}"` : ''}>${
            esc(rank == null ? t('rides.rankTbd') : t('rides.rank').replace('{n}', String(rank)))}</span>
        </span>
        <span class="ride-horse">${esc(under)}</span>
        <span class="ride-found">${esc(found)}</span>
      </span>
    </button>`;
}


/**
 * The open ride: the type filter, how it scored, and its moments.
 *
 * The heading carries what the group heading used to — the test, the total and
 * the placing, with the check on a total that failed one — and Watch, which
 * puts the whole ride in the player. The tiles are the same tiles the flat
 * list shows, with the movement on the frame and Details raised to a button
 * across the foot of each: opening a moment's record is what a ride is read
 * for, and a link beside a bordered Add read as the lesser of the two.
 *
 * A ride with nothing of the chosen types says so and says how to get back,
 * because an empty pane beside a rail full of names reads as a broken card.
 */
function ridePane({ ride, moments }, index, msg, types, picked, ofThisRide, boardSort) {
  // Unset, the pane follows the board: one sort is still one answer until
  // somebody asks a different question of this rider.
  const sort = msg.rideSort === 'score' || msg.rideSort === 'time' ? msg.rideSort : boardSort;
  moments = selectMoments(moments, { sort }).list;
  const result = ride.result || {};
  const check = rideCheck({ score_check: result.scoreCheck });
  const reading = [
    ride.testType ? t(`ride.${ride.testType}`) : '',
    result.totalPct == null ? '' : `${Number(result.totalPct).toFixed(3)}%`,
    result.place == null ? '' : `${t('ride.place')} ${result.place}`,
  ].filter(Boolean).join(' · ');
  const view = pageOf(moments, msg.page);

  let body = `<div class="ride-group-empty">${esc(t('ride.none'))}</div>`;
  if (moments.length) {
    body = `<div class="tile-row">${view.slice
      .map((m) => momentTile(m, { inRide: true })).join('')}</div>`;
  } else if (picked.length) {
    body = `<div class="ride-group-empty">${esc(t('rides.noTypeHere'))}</div>`;
  }

  return `
    <div class="ride-pane" role="tabpanel" aria-labelledby="${esc(rideTabId(index, ride))}">
      <div class="ride-pane-controls">
        ${typeFilter(types, picked, index, Boolean(msg.typesOpen), ofThisRide)}
        ${segmentedSort('ride-sort', index, sort, t('rides.sortRide'))}
      </div>
      <div class="ride-pane-head">
        <div class="ride-who">
          <span class="ride-rider">${esc(ride.rider || '—')}</span>
          <span class="ride-horse">${esc([ride.horse,
    `${clock(ride.startSec)}–${clock(ride.endSec)}`].filter(Boolean).join(' · '))}</span>
        </div>
        <div class="ride-group-result">${esc(reading)}${check.text
    ? `<span class="ride-check" data-tone="${check.tone}">${esc(check.text)}</span>` : ''}</div>
        ${playRideButton(ride, 'btn-solid play-ride')}
      </div>
      ${body}
      ${pagerRow(view, index)}
    </div>`;
}


function eventHead(event, { prominent = false } = {}) {
  const meta = [event.discipline, event.competition, event.venue, event.date]
    .filter(Boolean).join(' · ');
  return `
    <div class="event-head">
      <div class="game-row">
        <div class="event-head-text">
          <div class="moment-label">${esc(event.title || '')}</div>
          ${meta ? `<div class="moment-meta">${esc(meta)}</div>` : ''}
          ${event.outcome ? `<div class="game-outcome">${esc(event.outcome)}</div>` : ''}
        </div>
        <div class="moment-actions">
          <button class="${prominent ? 'btn-accent btn-accent-lg' : 'link-btn'}"
                  data-game-details="1">${esc(t('moment.details'))}</button>
        </div>
      </div>
    </div>`;
}


/**
 * One ride and its moments.
 *
 * The heading is the ride-table row from the game popup, reshaped: running
 * order, start number and rider, the horse and when they were in the arena,
 * then the result. Names in the sans and readings in the mono, as the table
 * has them. A ride with no moments says so rather than showing nothing.
 */
function rideGroup({ ride, moments }) {
  const tiles = moments.length
    ? `<div class="tile-row">${moments.map((m) => momentTile(m, { inRide: !!ride })).join('')}</div>`
    : `<div class="ride-group-empty">${esc(t('ride.none'))}</div>`;

  if (!ride) {
    return `
      <section class="ride-group">
        <div class="ride-group-head">
          <span class="ride-num"></span>
          <span class="ride-who"><span class="ride-rider">${esc(t('ride.outside'))}</span></span>
          <span class="ride-group-result"></span>
          <span></span>
          <span class="list-count">${moments.length}</span>
        </div>
        ${tiles}
      </section>`;
  }

  const result = ride.result || {};
  const who = `${ride.startNumber ? `#${ride.startNumber} ` : ''}${
    ride.identitySource === 'schedule' ? '~' : ''}${ride.rider || '—'}`;
  // The published spelling qualifies the on-screen one; it never replaces it.
  const published = [ride.groundedRider, ride.groundedHorse].filter(Boolean).join(' / ');
  const check = rideCheck({ score_check: result.scoreCheck });
  const reading = [
    ride.testType ? t(`ride.${ride.testType}`) : '',
    result.totalPct == null ? '' : `${Number(result.totalPct).toFixed(3)}%`,
    result.place == null ? '' : `${t('ride.place')} ${result.place}`,
  ].filter(Boolean).join(' · ');

  return `
    <section class="ride-group">
      <div class="ride-group-head">
        <span class="ride-num">${esc(String(ride.order ?? ''))}</span>
        <span class="ride-who" ${published ? `title="${esc(published)}"` : ''}>
          <span class="ride-rider">${esc(who)}</span>
          <span class="ride-horse">${esc(ride.horse || '')} · ${clock(ride.startSec)}–${clock(ride.endSec)}</span>
        </span>
        <span class="ride-group-result">${esc(reading)}${check.text
          ? `<span class="ride-check" data-tone="${check.tone}">${esc(check.text)}</span>` : ''}</span>
        ${playRideButton(ride, 'link-btn')}
        <span class="list-count">${moments.length}</span>
      </div>
      ${tiles}
    </section>`;
}


/**
 * A slot, not the player itself.
 *
 * render() rebuilds the transcript's innerHTML, which would destroy a live
 * <video> and restart the clip. Since the events subcollection updates
 * continuously while an analysis runs, that would make a moment unwatchable.
 * The player element is created once and re-parented into this slot after each
 * render, so playback survives.
 */
function playerMarkup(key) {
  return `<div class="player-slot" data-slot="${esc(key)}"></div>`;
}

// Everything the analysis recorded, in the order someone would read it: what
// happened, then when, then who, then how sure. The row is skipped when the
// value is empty rather than printed blank — a table half full of dashes reads
// as broken data rather than as unreadable footage.
const DETAIL_ROWS = [
  ['moment.summary', (m) => m.summary],
  ['moment.description', (m) => m.description],
  // Form rather than outcome. Empty for a sport that is judged on whether it
  // went in; for equestrian it is most of what the record says.
  ['moment.executionDetails', (m) => m.executionDetails],
  ['moment.harmony', (m) => m.harmonyIndex],
  ['moment.class', (m) => m.label || m.momentType],
  ['moment.category', (m) => m.category],
  ['moment.result', (m) => m.actionResult],
  // No start, end or peak rows: the player's range label says where the
  // moment is ("Halt and salute · 11:45–12:18 in source") and the scrubber
  // is the moment itself, so three more timecodes here were noise.
  ['moment.rider', (m) => m.rider],
  ['moment.horse', (m) => m.horse],
  ['moment.startNumber', (m) => m.startNumber],
  // Read off a graphic or inferred from the start list. Shown, because a
  // caption that presents the second as the first is the failure this whole
  // record exists to avoid.
  ['moment.identitySource', (m) => (m.identitySource
    ? t(`identity.${m.identitySource}`) : '')],
  ['moment.participant', (m) => m.participant],
  ['moment.participantRole', (m) => m.participantRole],
  ['moment.actionTeam', (m) => m.actionTeam],
  ['game.homeTeam', (m) => m.team1],
  ['game.awayTeam', (m) => m.team2],
  ['moment.score', (m) => (m.scoreTeam1 == null || m.scoreTeam2 == null
    ? '' : `${m.scoreTeam1}-${m.scoreTeam2}`)],
  ['moment.scoreboard', (m) => m.scoreboard],
  ['moment.confidence', (m) => (m.confidence == null ? '' : `${Math.round(m.confidence * 100)}`)],
  ['moment.excitement', (m) => (m.excitement == null ? '' : m.excitement.toFixed(2))],
  ['moment.highlightScore', (m) => (m.highlightScore == null ? '' : m.highlightScore.toFixed(2))],
  ['moment.evidence', (m) => (m.evidence || []).join('; ')],
  ['moment.isGoal', (m) => (m.isGoal ? t('moment.yes') : '')],
  ['moment.id', (m) => m.momentId],
];


function jobDuration(jobId) {
  return Number(state.jobs.find((j) => j.id === (jobId || state.jobId))?.media?.durationSec || 0);
}


/** A ride of the open event, from its tree. */
function treeRide(order) {
  const tree = state.eventTree;
  if (order == null || !tree || tree.jobId !== state.jobId) return null;
  return (tree.event?.riders || []).find((r) => r.order === order) || null;
}


/** The ride a moment happened in — the tree's say, then the order stored on it. */
function rideOf(m) {
  const tree = state.eventTree;
  if (!m || !tree || tree.jobId !== state.jobId) return null;
  return (tree.event?.riders || []).find((r) => (r.moments || []).some((x) => x.momentId === m.momentId))
    || treeRide(m.rideOrder);
}


function rideLabel(ride) {
  return [ride.rider, ride.testType ? t(`ride.${ride.testType}`) : ''].filter(Boolean).join(' · ');
}


/**
 * Open a moment in the player.
 *
 * The moment plays beside its record rather than instead of it. Opening the
 * details of a play is the point at which someone wants to see it, and the
 * facts are what they are checking it against — reading "double save" and
 * watching the save are the same act here. Three seconds either side of what
 * was asked for — the moment's own times, padded — so the play is
 * seen in its context. Every way into the player comes through here: the
 * row's thumbnail and the details button.
 */
function openDetails(momentId, range = null) {
  const m = momentById(momentId);
  if (!m) return;
  const ride = rideOf(m);
  openPlayer({
    key: momentId,
    moment: m,
    rideOrder: ride ? ride.order : null,
    label: m.label || m.momentType || '',
    title: m.summary || m.label || t('moment.details'),
    ...playRange(range?.start ?? m.startSec, range?.end ?? m.endSec, { duration: jobDuration(m.jobId) }),
  });
}


/**
 * The way into a whole ride, from wherever the ride is shown.
 *
 * Named for what it does and carrying the ride's length, because "Watch" read
 * as nothing in particular and people went into a moment to find the full
 * ride instead. The value is the running order, or the ride's start second
 * when it has no order: an empty value is one the click handler never sees,
 * which made the button silently do nothing.
 */
function playRideButton(ride, className) {
  const key = ride.order != null ? String(ride.order) : `@${ride.startSec}`;
  const length = shortClock(Math.max(0, Number(ride.endSec) - Number(ride.startSec)));
  return `<button class="${className}" data-watch-ride="${esc(key)}">
      <span aria-hidden="true">▶</span> ${esc(t('ride.playFull'))} <span class="play-ride-len">${esc(length)}</span>
    </button>`;
}


/** Open a whole ride in the player, from its first second to its last. */
function openRide(key) {
  const text = String(key ?? '');
  const ride = text.startsWith('@')
    ? (state.eventTree?.event?.riders || []).find((r) => String(r.startSec) === text.slice(1)) || null
    : treeRide(Number(text));
  if (!ride) return;
  const order = ride.order;
  openPlayer({
    key: `ride-${order ?? text}`,
    moment: null,
    rideOrder: order,
    full: true,
    label: rideLabel(ride),
    title: [ride.rider, ride.horse].filter(Boolean).join(' · '),
    start: ride.startSec,
    end: ride.endSec,
  });
}


function openPlayer(p) {
  $('details-title').textContent = p.title;
  // The slot holds the live player; the blocks either side of it are redrawn
  // on their own, so re-aiming the player never rebuilds the video.
  $('details-player').innerHTML = '<div class="player-over"></div>'
    + `${playerMarkup(p.key)}<div class="player-under"></div>`;
  state.details = p.key;
  state.playing = { full: false, loop: false, rate: 1, free: false, ...p };
  renderDetailsBody();
  showDetailsModal(true);
  mountPlayer();
}


/**
 * Everything around the player. Above it: the moments of this ride, as chips
 * that re-aim it — they are what the player is driven by, so they sit where
 * they can be reached without scrolling past the video to find them. Under it:
 * the ride itself, how it scored and where that score came from. Beside it:
 * what the analysis looked for and did not find, the incidents, and the
 * moment's own record. Redrawn when the range changes, so the chip that is
 * playing always describes what is on screen.
 */
function renderDetailsBody() {
  const p = state.playing;
  if (!p) return;
  const ride = treeRide(p.rideOrder);
  const panel = ride ? ridePanel(ride, p) : null;
  const over = $('details-player').querySelector('.player-over');
  if (over) over.innerHTML = panel ? panel.over : '';
  const under = $('details-player').querySelector('.player-under');
  if (under) under.innerHTML = panel ? panel.summary : '';
  const actions = $('details-actions');
  if (actions) actions.innerHTML = detailActions();
  const body = $('details-body');
  body.className = 'detail-body';
  body.innerHTML = state.share?.mode === 'publish'
    ? publishPanel()
    : (panel ? panel.reference : '') + (p.moment ? momentRecord(p.moment, Boolean(ride)) : '');
}


/* ── Taking a moment off the desk ─────────────────────────────────────────────

   Two things can be done with a moment once it has been watched: saved as a
   file, or put on a channel. Both cut the range that is playing, so what comes
   out is what was on screen — the padded moment by default, or whatever the
   trim and Widen have made of it.

   The buttons live in the popup's head rather than on the tile, for the reason
   the tile's buttons were removed: down a page of moments they were most of
   what the page said, and neither is a decision anyone makes without watching
   the thing first. */

function detailActions() {
  const p = state.playing;
  if (!p?.moment) return '';
  const busy = state.share?.status === 'sending';
  const publishing = state.share?.mode === 'publish';
  return `
    <button class="btn-outline detail-action" data-detail-act="download" ${
      state.share?.downloading ? 'disabled' : ''}>${
      esc(state.share?.downloading ? t('share.preparing') : t('share.download'))}</button>
    <button class="btn-accent detail-action" data-detail-act="publish"
            aria-pressed="${publishing}" ${busy ? 'disabled' : ''}>${esc(t('share.publish'))}</button>`;
}


/**
 * The publish panel: what is being published, and what it will say.
 *
 * It replaces the moment's record in the popup's right-hand column rather than
 * opening a dialog of its own, because the preview is the player that is
 * already running beside it — trimming re-aims that video, so what is checked
 * is the cut itself and not a description of one.
 *
 * Only YouTube can be published to. Instagram and TikTok are named because
 * they are what this is for, and marked as not built rather than left off: a
 * missing option reads as an oversight, a marked one reads as a plan.
 */
function publishPanel() {
  const p = state.playing;
  const share = state.share;
  if (!p || !share) return '';
  const length = Math.max(0, p.end - p.start);
  const over = length > MAX_CUT_SEC;
  const status = share.status || 'idle';

  return `
    <div class="publish-panel">
      <div class="panel-label">${esc(t('share.title'))}</div>

      <div class="trim-row">
        <div class="trim-edge">
          <div class="field-label">${esc(t('share.in'))}</div>
          <div class="trim-controls">
            ${trimButton('start', -1)}
            <span class="trim-at">${esc(clock(p.start))}</span>
            ${trimButton('start', 1)}
          </div>
        </div>
        <div class="trim-edge">
          <div class="field-label">${esc(t('share.out'))}</div>
          <div class="trim-controls">
            ${trimButton('end', -1)}
            <span class="trim-at">${esc(clock(p.end))}</span>
            ${trimButton('end', 1)}
          </div>
        </div>
        <div class="trim-edge">
          <div class="field-label">${esc(t('share.length'))}</div>
          <div class="trim-len ${over ? 'is-over' : ''}">${esc(shortClock(length))}</div>
        </div>
        <button class="link-btn" data-trim-reset>${esc(t('share.reset'))}</button>
      </div>

      <label class="field-label" for="share-title">${esc(t('share.videoTitle'))}</label>
      <input class="input" id="share-title" maxlength="100" data-share-field="title"
             value="${esc(share.title)}" />
      <div class="setting-hint">${esc(t('share.titleHint'))}</div>

      <label class="field-label" for="share-description">${esc(t('share.description'))}</label>
      <textarea class="input share-text" id="share-description" rows="5"
                data-share-field="description">${esc(share.description)}</textarea>

      <label class="field-label" for="share-privacy">${esc(t('share.privacy'))}</label>
      <select class="input" id="share-privacy" data-share-field="privacy">
        ${['private', 'unlisted', 'public'].map((v) => `
          <option value="${v}" ${share.privacy === v ? 'selected' : ''}>${
            esc(t(`share.privacy.${v}`))}</option>`).join('')}
      </select>

      <div class="platform-row">
        <button class="btn-accent" data-publish-to="youtube" ${
          status === 'sending' ? 'disabled' : ''}>${
          esc(status === 'sending' ? t('share.sending') : t('share.toYouTube'))}</button>
        <span class="platform-soon">${esc(t('share.instagramSoon'))}</span>
        <span class="platform-soon">${esc(t('share.tiktokSoon'))}</span>
      </div>

      ${status === 'error' ? `<div class="share-error">${esc(share.error || '')}</div>` : ''}
      ${status === 'done' ? `<div class="share-done">${esc(t('share.published'))}
        <a class="link-btn" href="${esc(share.url)}" target="_blank"
           rel="noopener noreferrer">${esc(share.url)}</a></div>` : ''}
      ${over ? `<div class="share-error">${esc(t('share.tooLong'))}</div>` : ''}
    </div>`;
}


function trimButton(edge, by) {
  const p = state.playing;
  const can = canTrim(p, edge, by, { moment: p.moment, duration: jobDuration() });
  return `<button class="pc" data-trim="${edge}:${by}" ${can ? '' : 'disabled'}
    aria-label="${esc(t(by < 0 ? 'share.earlier' : 'share.later'))}">${by < 0 ? '−1s' : '+1s'}</button>`;
}


/** The copy a moment arrives with: what it is, in the match it came from. */
function shareDefaults(m) {
  const game = state.game || {};
  const match = state.job?.title || game.title || '';
  const who = [m.rider, m.horse].filter(Boolean).join(' / ');
  const headline = [who, m.label || m.momentType || ''].filter(Boolean).join(' — ');
  return {
    title: [headline, match].filter(Boolean).join(' · ').slice(0, 100),
    description: [
      m.summary || m.description || '',
      match ? `${match}${game.competition && game.competition !== match ? ` · ${game.competition}` : ''}` : '',
      `${t('share.atTime')} ${clock(m.startSec)}`,
    ].filter(Boolean).join('\n\n'),
  };
}


function openPublish() {
  const p = state.playing;
  if (!p?.moment) return;
  const defaults = shareDefaults(p.moment);
  state.share = {
    mode: 'publish',
    momentId: p.moment.momentId,
    privacy: state.youtube?.privacy || 'private',
    status: 'idle',
    ...defaults,
  };
  // What is configured is a server fact and changes without this tab knowing,
  // so it is asked for when the panel opens rather than trusted from earlier.
  refreshYouTube();
  renderDetailsBody();
}


async function downloadMoment() {
  const p = state.playing;
  if (!p?.moment || state.share?.downloading) return;
  state.share = { ...(state.share || {}), downloading: true, status: 'idle', error: '' };
  renderDetailsBody();
  try {
    const out = await api(
      `/api/jobs/${p.moment.jobId || state.jobId}/moments/${p.moment.momentId}/download`,
      { method: 'POST', body: JSON.stringify({ start_sec: p.start, end_sec: p.end }) },
    );
    // The signed URL carries a content-disposition, so this saves the file
    // rather than navigating away from the desk.
    window.location.href = out.url;
  } catch (err) {
    state.share = { ...(state.share || {}), status: 'error', error: err.message };
  } finally {
    state.share = { ...(state.share || {}), downloading: false };
    renderDetailsBody();
  }
}


async function publishToYouTube() {
  const p = state.playing;
  const share = state.share;
  if (!p?.moment || !share || share.status === 'sending') return;
  state.share = { ...share, status: 'sending', error: '' };
  renderDetailsBody();
  try {
    const out = await api(
      `/api/jobs/${p.moment.jobId || state.jobId}/moments/${p.moment.momentId}/publish/youtube`,
      {
        method: 'POST',
        body: JSON.stringify({
          start_sec: p.start,
          end_sec: p.end,
          title: share.title,
          description: share.description,
          privacy: share.privacy,
        }),
      },
    );
    state.share = { ...state.share, status: 'done', url: out.url };
  } catch (err) {
    state.share = { ...state.share, status: 'error', error: err.message };
  }
  renderDetailsBody();
}


function momentRecord(m, underRide) {
  const rows = DETAIL_ROWS
    .map(([key, read]) => [t(key), read(m)])
    .filter(([, value]) => value !== '' && value != null);
  return `
    ${underRide ? `<div class="panel-label panel-label-record">${esc(t('ride.thisMoment'))}</div>` : ''}
    <div class="detail-grid">${rows.map(([label, value]) => `
      <div class="detail-key">${esc(label)}</div>
      <div class="detail-value">${esc(String(value))}</div>`).join('')}</div>`;
}


/**
 * Where a ride's score came from, in words.
 *
 * Built only from what the record says — read off the screen or taken from
 * the published results, whether the total agrees with its own marks, and
 * what the published figure is when it differs. Nothing here is a judgement
 * the pipeline did not make.
 */
function provenanceText(result) {
  const r = result || {};
  const parts = [];
  if (r.scoreSource === 'observed') parts.push(t('provenance.observed'));
  else if (r.scoreSource) parts.push(t('provenance.published').replace('{source}', r.scoreSource));
  else if (r.totalPct == null) parts.push(t('provenance.none'));

  const check = String(r.scoreCheck || '');
  const confirmed = /^ok, confirmed by (.+)$/.exec(check);
  if (check === 'ok') parts.push(t('provenance.consistent'));
  else if (confirmed) parts.push(t('provenance.confirmed').replace('{source}', confirmed[1]));
  else if (check) parts.push(check);

  if (r.groundedTotalPct != null && r.totalPct != null
      && Number(r.groundedTotalPct).toFixed(3) !== Number(r.totalPct).toFixed(3)) {
    parts.push(t('provenance.differs').replace('{pct}', Number(r.groundedTotalPct).toFixed(3)));
  }
  return parts.join(' ');
}


function panelSection(label, inner) {
  return `<div class="panel-section"><div class="panel-label">${esc(label)}</div>${inner}</div>`;
}


/**
 * One ride, for the player: who, how it scored, what happened in it, and how
 * to take it elsewhere.
 *
 * The moments are chips that re-aim the player, with the whole ride first.
 * What was looked for and not found is the part of the event's record noted
 * in this ride's own analysis windows. Scores come only from what the record
 * holds — a technical and an artistic mark are not split out by the analysis,
 * so they are not shown rather than guessed.
 */
function ridePanel(ride, p) {
  const r = ride.result || {};
  const judges = Array.isArray(state.game?.judges) ? state.game.judges : [];
  // One tile per judge, keyed by where they sat (E, H, C) when the record
  // knows it: the marks are read in that order off the results graphic.
  const marks = (r.judgeMarks || []).map((mark, i) =>
    [`${t('ride.judge')} ${judges[i]?.position || i + 1}`, `${Number(mark).toFixed(2)}%`]);
  const tiles = [
    [t('ride.total'), r.totalPct == null ? '' : `${Number(r.totalPct).toFixed(3)}%`],
    [t('ride.place'), r.place == null ? '' : String(r.place)],
    ...marks,
    [t('ride.published'), r.groundedTotalPct == null ? '' : `${Number(r.groundedTotalPct).toFixed(3)}%`],
  ].filter(([, value]) => value);
  const check = rideCheck({ score_check: r.scoreCheck });
  const who = `${ride.startNumber ? `#${ride.startNumber} ` : ''}${
    ride.identitySource === 'schedule' ? '~' : ''}${ride.rider || '—'}`;
  const sub = [ride.horse, ride.testType ? t(`ride.${ride.testType}`) : '', `${clock(ride.startSec)}–${clock(ride.endSec)}`]
    .filter(Boolean).join(' · ');

  const moments = ride.moments || [];
  const chips = [
    `<button class="range-chip" data-player-range="full" aria-pressed="${Boolean(p.full)}">
       ${esc(t('player.fullRide'))} <span>${clock(ride.startSec)}</span></button>`,
    ...moments.map((mo) => `
     <button class="range-chip" data-player-range="${esc(mo.momentId)}"
             aria-pressed="${!p.full && p.moment?.momentId === mo.momentId}">
       ${esc(mo.label || mo.momentType || '')} <span>${clock(mo.startSec)}</span></button>`),
  ].join('');

  const incidents = moments.filter((mo) => mo.category === 'incident' || mo.requiresHumanReview);
  // Above the player, the one thing that drives it: the chips that pick what
  // is playing. They used to sit under the ride's score tiles, which put the
  // controls for the video below the video and everything about it.
  const over = panelSection(t('ride.momentsInRide'), `<div class="range-chips">${chips}</div>`);

  // Under it, the ride itself — who rode, what it scored and where the score
  // came from. Beside it, the material an editor reads rather than acts on.
  const summary = `
    <section class="ride-panel">
      <div class="ride-panel-head">
        <div class="ride-panel-rider">${esc(who)}</div>
        <div class="ride-panel-sub">${esc(sub)}</div>
        ${ride.identitySource === 'schedule' ? `<div class="ride-panel-note">${esc(t('ride.fromSchedule'))}</div>` : ''}
      </div>
      ${tiles.length ? `<div class="score-tiles">${tiles.map(([k, v]) => `
        <div class="score-tile"><div class="score-key">${esc(k)}</div><div class="score-value">${esc(v)}</div></div>`).join('')}</div>` : ''}
      ${check.text ? `<div class="ride-callout" data-tone="${check.tone}">${esc(check.text)}</div>` : ''}
      ${panelSection(t('ride.provenance'), `<div class="panel-line">${esc(provenanceText(r))}</div>${
        r.scoreboard ? `<pre class="source-ref">${esc(r.scoreboard)}</pre>` : ''}`)}
    </section>`;

  // What the analysis found beyond the moments: whether anything went wrong
  // in the arena. There was a "looked for, not confirmed" section above this
  // one, listing the types the analysis went looking for in this ride's own
  // windows and could not confirm. It was removed at the editor's request: a
  // list of things that did not happen is a paragraph of hedging beside the
  // moments that did, and it was read as doubt about them.
  const reference = `
    <section class="ride-panel">
      ${panelSection(t('ride.incidents'), incidents.length
        ? incidents.map((mo) => `<div class="panel-line"><b>${esc(mo.label || mo.momentType)}</b>
            <span class="mono-soft">${clock(mo.startSec)}</span> — ${esc(mo.summary || '')}</div>`).join('')
        : `<div class="panel-line">${esc(t('ride.noIncidents'))}</div>`)}
    </section>`;

  return { over, summary, reference };
}


/** Re-aim the open player at the whole ride, or at one moment of it. */
function setPlayerRange(which) {
  const p = state.playing;
  if (!p) return;
  const ride = treeRide(p.rideOrder);
  if (which === 'full') {
    if (!ride) return;
    Object.assign(p, { full: true, start: ride.startSec, end: ride.endSec, label: rideLabel(ride) });
  } else {
    const mo = momentById(which) || (ride?.moments || []).find((x) => x.momentId === which);
    if (!mo) return;
    Object.assign(p, {
      full: false,
      moment: mo,
      label: mo.label || mo.momentType || '',
      ...playRange(mo.startSec, mo.endSec, { duration: jobDuration() }),
    });
    $('details-title').textContent = mo.summary || mo.label || t('moment.details');
  }
  p.free = false;
  renderDetailsBody();
  const video = playerEl?.querySelector('video');
  if (video) {
    video.currentTime = p.start;
    video.play().catch(() => {});
  }
  syncPlayer();
}


/** What the scrubber spans for what is playing: its ride, or itself. */
function timelineOf(p) {
  return playerTimeline(p, treeRide(p.rideOrder));
}


/**
 * Move the playhead. Inside the moment's band it still stops at the out
 * point; outside it the ride runs on, because someone who reached past the
 * moment wants to see what came before or after it, not be pulled back.
 */
function moveTo(p, video, at) {
  video.currentTime = at;
  p.free = at < p.start || at > p.end;
  syncPlayer();
}


/** One of the player's controls. */
function playerControl(kind) {
  const p = state.playing;
  const video = playerEl?.querySelector('video');
  if (!p || !video) return;
  if (kind === 'play') {
    if (video.paused) {
      const at = video.currentTime;
      // Play from the in point when the moment has been watched to its end, or
      // when a free run has reached the end of the ride — both re-arm it.
      if (p.free ? at >= timelineOf(p).end - 0.05 : (at >= p.end - 0.05 || at < p.start)) {
        video.currentTime = p.start;
        p.free = false;
      }
      video.play().catch(() => {});
    } else {
      video.pause();
    }
  } else if (kind === 'back' || kind === 'fwd') {
    moveTo(p, video, clampTo(timelineOf(p), video.currentTime + (kind === 'back' ? -5 : 5)));
    return;
  } else if (kind === 'loop') {
    p.loop = !p.loop;
  } else if (kind === 'speed') {
    p.rate = nextSpeed(p.rate);
    video.playbackRate = p.rate;
  } else if (kind === 'widen') {
    Object.assign(p, widen(p, { duration: jobDuration() }), { full: false, free: false });
    renderDetailsBody();
  } else if (kind === 'ride') {
    setPlayerRange('full');
    return;
  }
  syncPlayer();
}


/**
 * Show the popup, with or without its player column.
 *
 * A game record has no moment to play, and an empty column would be a black
 * bar down half the dialog. The class is toggled rather than left to
 * `:empty`, so the grid collapses to the one column that has content.
 */
function showDetailsModal(withPlayer) {
  $('details-split').classList.toggle('solo', !withPlayer);
  $('details').classList.remove('hidden');
}


function closeDetails() {
  $('details').classList.add('hidden');
  $('details-player').innerHTML = '';
  // A publish panel belongs to the moment it was opened over, and its trim is
  // that moment's. Reopening anything starts from the record again.
  state.share = null;
  if (state.details) {
    // The player lived in the popup, so closing the popup stops it. Leaving it
    // running would be audio from a dialog that is no longer on screen.
    state.details = null;
    state.playing = null;
    destroyPlayer();
    render();
  }
}

/**
 * Adding a video, or reserving the pipeline for one that has not been played.
 *
 * Two panels, not two tabs of one. A file and a live event share a sport, a
 * set of context links, and nothing else: one is here
 * now and the other is a booking. Tabbing between them put a datetime picker
 * one click from a drop zone and made the panel read as a single form with
 * half its fields hidden, which is what "split them" was asking to undo.
 *
 * Which one is on the message rather than in `state.upload`, so an ingest
 * panel scrolled back to is the panel that was opened.
 */
function ingestCard(m) {
  const u = state.upload;
  const busy = u.status !== 'idle';
  const live = m?.ingestKind === 'live';
  return `
    <div class="panel ingest">
      <div class="ingest-head">
        <div class="ingest-head-text">
          <div class="ingest-eyebrow">${esc(t(live ? 'ingest.liveHeading' : 'ingest.addHeading'))}</div>
          <div class="ingest-lede">${esc(t(live ? 'ingest.liveLede' : 'ingest.addLede'))}</div>
        </div>
      </div>
      ${live ? liveForm(u, busy) : uploadForm(u, busy)}
    </div>`;
}


/** The sport: the one thing asked of every match. */
function sportRow(u) {
  return `
      <div class="ingest-field">
        <div class="field-label">${esc(t('ingest.sport'))}</div>
        <div class="ingest-sports">
          ${state.sports.map((s) => `
            <button class="chip" data-sport="${esc(s)}" aria-pressed="${u.sport === s}"
                    style="text-transform:capitalize">${esc(s)}</button>`).join('')}
        </div>
      </div>`;
}


function contextField(u) {
  return `
      <div class="ingest-field">
        <label class="field-label" for="context-urls">${esc(t('ingest.contextUrls'))}</label>
        <textarea class="input ctx-textarea" id="context-urls" rows="2"
                  data-context-urls placeholder="${esc(t('reanalyse.placeholder'))}">${esc(u.contextUrls || '')}</textarea>
        <div class="setting-hint">${esc(t('ingest.contextUrlsHint'))}</div>
      </div>`;
}


/**
 * One source, chosen — not three stacked blocks each with its own button.
 *
 * A file, a path into the bucket and a recorded playlist are three answers to
 * one question, and showing all three at once asked it three times: the panel
 * carried three inputs and three buttons where exactly one of them was ever
 * going to be used.
 */
function uploadForm(u, busy) {
  // Offer the most recent stranded upload only. A list of near-identical
  // filenames is a worse prompt than "the one you left behind".
  const pending = state.pendingUploads[0];
  const sources = [['file', t('ingest.srcFile')], ['path', t('ingest.srcPath')],
    ['stream', t('ingest.srcStream')]];
  const ready = u.src === 'file' ? Boolean(u.file)
    : u.src === 'path' ? Boolean(u.gcsUri.trim()) : Boolean(u.hlsUrl.trim());

  let source = `
        <div class="dropzone" id="dropzone">
          <div class="dz-thumb"><span class="thumb-stripes"></span></div>
          <div style="flex:1;min-width:0">
            <div class="dz-name">${esc(u.name || t('ingest.noFile'))}</div>
            <div class="dz-meta">${esc(u.name ? `${u.size} · ${u.sport}` : t('ingest.dropHere'))}</div>
          </div>
          <label class="file-label">${esc(t('ingest.chooseFile'))}
            <input type="file" id="file-input" accept="video/*" style="display:none" />
          </label>
        </div>`;
  if (u.src === 'path') {
    source = `
        <input class="composer-input mono-input" data-gcs-input
               placeholder="gs://bucket/path/to/video.mp4" value="${esc(u.gcsUri || '')}" />
        <div class="setting-hint">${esc(t('ingest.fromStorageHint'))}</div>`;
  } else if (u.src === 'stream') {
    source = `
        <input class="composer-input mono-input" data-hls-input
               placeholder="https://…/master.m3u8" value="${esc(u.hlsUrl || '')}" />
        <div class="setting-hint">${esc(t('ingest.hlsHint'))}</div>`;
  }

  return `
      <div class="ingest-grid">
        <div class="ingest-field ingest-title">
          <div class="field-label">${esc(t('ingest.title'))}</div>
          <input class="composer-input" data-ingest-title
                 placeholder="${esc(t('ingest.titlePlaceholder'))}" value="${esc(u.title || '')}" />
        </div>
        ${sportRow(u)}
        <div class="ingest-field ingest-wide">
          <div class="field-label">${esc(t('ingest.source'))}</div>
          <div class="ingest-sources">
            ${sources.map(([key, label]) => `
              <button class="src-btn" data-ingest-src="${key}"
                      aria-pressed="${u.src === key}">${esc(label)}</button>`).join('')}
          </div>
          ${source}
        </div>
        ${contextField(u)}
      </div>
      ${u.status === 'uploading' || u.status === 'analyzing' ? `
        <div class="ingest-progress">
          <div class="dz-meta">${esc(u.stage || t('ingest.uploading'))}</div>
          <div class="meter-row">
            <div class="meter"><i style="width:${u.pct}%"></i></div>
            <div class="meter-pct">${Math.round(u.pct)}%</div>
          </div>
        </div>` : ''}
      <div class="ingest-foot">
        ${u.src === 'file' ? `
          <button class="btn-accent btn-accent-lg" id="start-analysis"
                  ${u.file && !busy ? '' : 'disabled'}>
            ${esc(u.status === 'uploading' ? t('ingest.uploading')
    : u.status === 'analyzing' ? t('ingest.analysing') : t('ingest.start'))}
          </button>` : u.src === 'path' ? `
          <button class="btn-accent btn-accent-lg" data-register-gcs="1"
                  ${u.gcsUri.trim() && !busy ? '' : 'disabled'}>${esc(t('ingest.useLocation'))}</button>`
    : `
          <button class="btn-accent btn-accent-lg" data-register-hls="1"
                  ${u.hlsUrl.trim() && !busy ? '' : 'disabled'}>${esc(t('ingest.useStream'))}</button>`}
        <span class="ingest-ready">${esc(ready
    ? `${t('ingest.ready')} — ${u.sport}, ${t('ingest.readyMoments')}`
    : t('ingest.pickSource'))}</span>
        ${pending && u.src === 'file' ? `
          <button class="btn-outline" data-resume="${esc(pending.job_id)}" ${busy ? 'disabled' : ''}
                  title="${esc(pending.filename)} — ${bytes(pending.size_bytes)}">
            ${esc(t('ingest.useLastUpload'))}
          </button>` : ''}
      </div>`;
}


function liveForm(u, busy) {
  const l = u.live;
  // Only judge what has been typed: an empty form is not a wrong one.
  const err = (l.hlsUrl || l.start || l.end)
    ? validateLiveEvent({ hlsUrl: l.hlsUrl, start: l.start, end: l.end }) : null;
  const ready = !err && l.hlsUrl && l.start && l.end;
  return `
      <div class="ingest-grid live-grid">
        <div class="ingest-field ingest-wide">
          <div class="field-label">${esc(t('ingest.liveTitle'))}</div>
          <input class="composer-input" data-live-title
                 placeholder="${esc(t('ingest.liveTitlePlaceholder'))}" value="${esc(l.title || '')}" />
        </div>
        <div class="ingest-field">
          <div class="field-label">${esc(t('ingest.liveStart'))}</div>
          <input class="composer-input mono-input" type="datetime-local"
                 data-live-start value="${esc(l.start || '')}" />
        </div>
        <div class="ingest-field">
          <div class="field-label">${esc(t('ingest.liveEnd'))}</div>
          <input class="composer-input mono-input" type="datetime-local"
                 data-live-end value="${esc(l.end || '')}" />
        </div>
        ${sportRow(u)}
        <div class="ingest-field ingest-wide">
          <div class="field-label">${esc(t('ingest.liveHls'))}</div>
          <input class="composer-input mono-input" data-live-hls
                 placeholder="https://…/live.m3u8" value="${esc(l.hlsUrl || '')}" />
          <div class="setting-hint">${esc(t('ingest.liveHint'))}</div>
        </div>
        ${contextField(u)}
      </div>
      <div class="ingest-foot">
        <button class="btn-accent btn-accent-lg" data-schedule-live="1"
                ${ready && !busy ? '' : 'disabled'}>
          ${esc(u.status === 'scheduling' ? t('ingest.scheduling') : t('ingest.schedule'))}
        </button>
        <span class="ingest-ready">${esc(err ? t(err)
    : ready ? `${t('ingest.ready')} — ${u.sport}, ${t('ingest.readyMoments')}`
      : t('ingest.liveNeeds'))}</span>
      </div>`;
}



function toDate(value) {
  if (!value) return null;
  if (typeof value.toDate === 'function') return value.toDate();  // Firestore Timestamp
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function isStalled(job) {
  const updated = toDate(job.updatedAt) || toDate(job.createdAt);
  return !!updated && Date.now() - updated.getTime() > STALLED_AFTER_MS;
}

function sinceLabel(value) {
  const then = toDate(value);
  if (!then) return 'a while';
  const minutes = Math.round((Date.now() - then.getTime()) / 60000);
  if (minutes < 90) return `${minutes} minutes`;
  const hours = Math.round(minutes / 60);
  return hours < 36 ? `${hours} hours` : `${Math.round(hours / 24)} days`;
}


// The run's stages and the share of the bar each one owns, mirroring
// STAGE_SPANS in the agent's pipeline. They are not equal slices: analysis is
// an hour of Gemini calls and everything else is minutes, so equal thirds would
// leave the bar parked mid-way for most of a run.
const STAGES = [
  { key: 'ingest', start: 0, end: 10 },
  { key: 'transcode', start: 10, end: 20 },
  { key: 'analysis', start: 20, end: 80 },
  { key: 'finalize', start: 80, end: 100 },
];

/** How far through its own span a stage is, given overall progress. */
function stageFill(stage, progress) {
  if (progress >= stage.end) return 100;
  if (progress <= stage.start) return 0;
  return ((progress - stage.start) / (stage.end - stage.start)) * 100;
}

function stageStrip(job) {
  const progress = Math.max(0, Math.min(100, job.progress || 0));
  const current = job.stage || '';
  return `
    <div class="stage-strip">
      ${STAGES.map((s) => {
        const fill = stageFill(s, progress);
        const active = s.key === current && fill < 100;
        return `
          <div class="stage" style="flex-grow:${s.end - s.start}"
               data-state="${fill >= 100 ? 'done' : active ? 'active' : 'todo'}">
            <div class="stage-meter"><i style="width:${fill}%"></i></div>
            <div class="stage-label">${esc(t(`stage.${s.key}`))}</div>
          </div>`;
      }).join('')}
    </div>`;
}


// The whole match, in the order someone would read it: what it was, who played,
// how it ended, then how it felt. Grounded values are listed beside the
// observed ones rather than merged into them — a competition read off a caption
// and one found by a web search are different kinds of claim.
const GAME_DETAIL_ROWS = [
  ['game.title', (g) => g.title],
  // The published name of the event and where it was, from Equipe. Beside the
  // observed title rather than in place of it: one was read off a caption and
  // one was found by a search, and they are different kinds of claim.
  ['game.showTitle', (g) => g.showTitle],
  ['game.location', (g) => g.location],
  ['game.sport', (g) => g.sport],
  // With how sure the reading was, because it was read off the footage rather
  // than declared at upload. "Jumping" alone hides that it was a judgement.
  ['game.discipline', (g) => (g.discipline
    ? (g.disciplineConfidence
      ? `${g.discipline} (${Math.round(g.disciplineConfidence * 100)}%)`
      : g.discipline)
    : '')],
  ['game.homeTeam', (g) => g.homeTeam],
  ['game.awayTeam', (g) => g.awayTeam],
  ['game.competition', (g) => g.competition],
  ['game.venue', (g) => g.venue],
  ['game.finalScore', (g) => g.finalScore],
  ['game.outcome', (g) => g.eventOutcome],
  ['game.sentiment', (g) => g.sentiment],
  ['game.mood', (g) => g.mood],
  ['game.summary', (g) => g.summary],
  ['game.moments', (g) => (g.momentCount == null ? '' : String(g.momentCount))],
  ['game.matchDate', (g) => g.matchDate],
  ['game.groundedHomeTeam', (g) => g.groundedHomeTeam],
  ['game.groundedAwayTeam', (g) => g.groundedAwayTeam],
  ['game.groundedCompetition', (g) => g.groundedCompetition],
  ['game.groundedVenue', (g) => g.groundedVenue],
  // The panel, as position and name — E Smith · H Jones — because a judge's
  // mark only means something against which letter they sat at.
  ['game.judges', (g) => (Array.isArray(g.judges) && g.judges.length
    ? g.judges.map((j) => [j.position, j.name].filter(Boolean).join(' ')).join(' · ')
    : '')],
  ['game.startList', (g) => (Array.isArray(g.startList) && g.startList.length
    ? String(g.startList.length) : '')],
  // How the start list was placed against the video. Zero anchors is a real
  // answer — it means no round the schedule names was ever named on screen.
  ['game.scheduleOffset', (g) => (g.scheduleAnchors
    ? `${g.scheduleAnchors} ${t('game.scheduleOffsetHint')}` : '')],
];


/**
 * A title that can be renamed where it is read.
 *
 * One name belongs to a match, and it is shown in three places — the job row,
 * the live row and the game card — so it is editable in all three rather than
 * behind a settings page nobody would look for. Saving writes the job and its
 * game record together, so the two cannot disagree afterwards.
 *
 * The value is held in state while it is being typed: the transcript is
 * rebuilt with innerHTML on every Firestore write, and an analysis writes
 * often, so a name typed into the DOM would vanish mid-word.
 */
function editableTitle(jobId, text, cls) {
  if (state.renaming?.jobId !== jobId) {
    return `<div class="${cls} is-titled">
      <span class="titled-text">${esc(text)}</span>
      <button class="link-btn rename-btn" data-rename="${esc(jobId)}"
              title="${esc(t('rename.hint'))}">${esc(t('rename.action'))}</button>
    </div>`;
  }
  return `<div class="${cls} is-renaming">
    <input class="composer-input rename-input" data-rename-input
           aria-label="${esc(t('rename.label'))}" value="${esc(state.renaming.value)}" />
    <button class="btn-accent" data-rename-save="${esc(jobId)}"
            ${state.renaming.value.trim() ? '' : 'disabled'}>${esc(t('rename.save'))}</button>
    <button class="link-btn" data-rename-cancel="1">${esc(t('rename.cancel'))}</button>
  </div>`;
}


/**
 * Save a new name for a match, in both places it is kept.
 *
 * The field closes first and the request goes afterwards: the listener will
 * bring the new name back within the second, and leaving the box open until
 * the round trip returns makes a rename feel like a form submission rather
 * than an edit. A failure says so in the transcript and the old name is still
 * what the listener is showing, so nothing is lost.
 */
async function saveRename(jobId) {
  const title = (state.renaming?.value || '').trim();
  if (!title) return;
  state.renaming = null;
  render();
  try {
    await api(`/api/jobs/${jobId}/title`, {
      method: 'PATCH', body: JSON.stringify({ title }),
    });
    // A session named after this match is named after the match, not after the
    // string it happened to have when it was opened.
    const session = state.sessions.find((x) => x.id === state.sessionKey);
    if (session && session.jobId === jobId) {
      updateSession(state.sessionKey, { title });
      state.sessions = listSessions();
      render();
    }
  } catch (err) {
    say(`${t('rename.failed')}: ${err.message || err}`);
  }
}


function gameHeadline(g) {
  return g.title
    || [g.homeTeam || g.groundedHomeTeam, g.awayTeam || g.groundedAwayTeam]
      .filter(Boolean).join(' v ')
    || t('game.title');
}


/**
 * A game's record as label-and-value rows.
 *
 * The same rows the popup shows, because they are the same record — a second
 * list for the expanded view is a second thing to keep current, and the one
 * that would quietly stop matching.
 */
function gameRows(g) {
  const rows = GAME_DETAIL_ROWS
    .map(([key, read]) => [t(key), read(g)])
    .filter(([, value]) => value !== '' && value != null);
  if (!rows.length) return '';

  return `<div class="detail-grid detail-inline">${rows.map(([label, value]) => `
    <div class="detail-key">${esc(label)}</div>
    <div class="detail-value">${esc(String(value))}</div>`).join('')}</div>`;
}


/**
 * A game's status, read from its job.
 *
 * The design shows a tick and a duration, or a spinner and "Analyzing". Both
 * of those are the job's, not the game record's — a game document is only
 * written once an analysis has produced one — so this reads the job the record
 * shares an id with, and shows nothing at all when there is no job to read
 * rather than inventing a state for it.
 */
function gameStatus(g) {
  const job = state.jobs.find((j) => j.id === (g.jobId || g.id));
  if (!job) return '';

  const running = ['analyzing', 'transcoding', 'uploaded'].includes(job.status);
  const stalled = running && isStalled(job);
  const tone = job.status === 'failed' || stalled ? 'failed' : running ? 'running' : 'idle';
  const text = stalled ? t('jobs.stalled')
    : running && job.progress ? `${job.status} \u00b7 ${Math.round(job.progress)}%`
      : (job.status || '');

  return text ? `<span class="job-status" data-tone="${tone}">${esc(text)}</span>` : '';
}


/**
 * One match as a tile.
 *
 * The whole tile opens the record rather than a Details button inside it: at
 * this width a button is most of the tile anyway, and the design has no such
 * button because the card itself is the target.
 *
 * The picture slot stays empty on purpose. A game record is written from the
 * whole match rather than from a frame of it, so there is no still to put
 * there — the design's coloured rectangle stands in for one, and filling it
 * with a moment's thumbnail would be a picture of one play captioned as the
 * fixture. It keeps the texture a moment's thumbnail shows before its own
 * picture arrives, which already means "no frame here".
 */
function gameTile(g) {
  const id = g.jobId || g.id;
  const meta = [
    g.sport,
    g.discipline,
    g.competition || g.groundedCompetition,
    g.finalScore,
  ].filter(Boolean).join(' \u00b7 ');

  return `
    <button class="tile" data-open-game="${esc(id)}"
            ${id === state.jobId ? 'aria-current="true"' : ''}>
      <span class="tile-thumb"></span>
      <span class="tile-name">${esc(gameHeadline(g))}</span>
      <span class="tile-meta">${esc(meta || t('game.notIdentified'))}</span>
      ${gameStatus(g)}
    </button>`;
}


/** Which match the moments below belong to, when one is open. */
function focusCaption() {
  const g = state.games.find((x) => (x.jobId || x.id) === state.jobId);
  if (!g) return '';
  return `<div class="focus-caption">${esc(t('games.viewing'))} <b>${esc(gameHeadline(g))}</b></div>`;
}


function gamesCard(msg, index) {
  if (!state.games.length) return emptyCard(t('games.none'));

  // selectGames has existed since the games list learned to filter, and until
  // now nothing called it: "show all handball games" was answered with every
  // game on the desk, which is the same answer as no filter and reads as one
  // that ran and matched everything. The module was tested; the call site was
  // never made, which is exactly the failure a tested pure function cannot
  // catch on its own.
  const found = selectGames(gamesInScope(state.scope, state.games),
    { terms: msg.showAll ? [] : (msg.terms || []) });

  // Asking for the records opens them in place, and a two-column detail table
  // does not fit a 190px tile — so the expanded view keeps the row layout it
  // was built for. Three at a time there, because ten records of a dozen rows
  // each is a page nobody can see the end of.
  if (msg.expandGames) {
    const rows = pageOf(found.list, msg.page, 3);
    return `
      <div class="list">
        ${listHead(found, index, t('games.title'))}
        <div class="panel-light">
          ${rows.slice.map((g) => `
            <div class="row">
              <div class="game-row">
                <div style="min-width:0">
                  <div class="moment-label">${esc(gameHeadline(g))}</div>
                  <div class="moment-meta">${esc([g.sport, g.discipline,
                    g.competition || g.groundedCompetition, g.finalScore, g.mood]
                    .filter(Boolean).join(' \u00b7 ') || t('game.notIdentified'))}</div>
                </div>
                <div class="moment-actions">
                  <button class="link-btn" data-open-game="${esc(g.jobId || g.id)}">
                    ${esc(t('moment.details'))}
                  </button>
                </div>
              </div>
              ${gameRows(g)}
            </div>`).join('')}
          ${pagerRow(rows, index)}
        </div>
      </div>`;
  }

  const view = pageOf(found.list, msg.page);
  return `
    <div class="list">
      ${listHead(found, index, t('games.title'))}
      <div class="tile-row">${view.slice.map(gameTile).join('')}</div>
      ${focusCaption()}
      ${pagerRow(view, index)}
    </div>`;
}


function gameCard() {
  const g = state.game;
  if (!g) return emptyCard(t('game.none'));

  // The same shape as a moment row: a headline worth reading, the facts that
  // qualify it underneath, and everything else a click away.
  const meta = [
    g.sport,
    g.competition || g.groundedCompetition,
    g.venue || g.groundedVenue,
    g.finalScore,
    g.mood,
  ].filter(Boolean).join(' · ');

  return `
    <div class="panel-light">
      <div class="row">
        <div class="game-row">
          <div style="min-width:0">
            ${editableTitle(g.jobId || g.id || state.jobId, gameHeadline(g), 'moment-label')}
            <div class="moment-meta">${esc(meta || t('game.notIdentified'))}</div>
            ${g.eventOutcome ? `<div class="game-outcome">${esc(g.eventOutcome)}</div>` : ''}
          </div>
          <div class="moment-actions">
            <button class="link-btn" data-game-details="1">${esc(t('moment.details'))}</button>
          </div>
        </div>
        ${g.summary ? `<div class="game-summary">${esc(g.summary)}</div>` : ''}
      </div>
    </div>`;
}


/**
 * What a ride's score check means, for display.
 *
 * The check string is written by the pipeline for people, and the three states
 * it can be in want three different treatments: a mismatch is a warning, a
 * confirmation is quiet reassurance, and plain "ok" needs nothing said.
 */
function rideCheck(ride) {
  const text = String(ride.score_check || '');
  if (/^mismatch|disagrees/.test(text)) return { text, tone: 'failed' };
  if (/^ok, confirmed/.test(text)) return { text, tone: 'confirmed' };
  return { text: '', tone: '' };
}


/**
 * The day's rounds, in running order, inside the game popup.
 *
 * A competition day is a list of rides before it is anything else, so this is
 * the table an editor scans — who rode, when, what they scored, whether the
 * number can be trusted. A published total sits in its own column beside the
 * displayed one rather than replacing it; a total that failed its own check is
 * flagged rather than hidden, because a wrong number that is invisible is
 * worse than one that is marked.
 */
function ridesTable(g) {
  const rides = Array.isArray(g.rides) ? g.rides : [];
  if (!rides.length) return '';
  return `
    <div class="detail-key rides-section">${esc(t('game.rides'))} · ${rides.length}</div>
    <div class="rides-section rides">
      <div class="ride ride-head">
        <span>#</span><span>${esc(t('ride.rider'))} / ${esc(t('ride.horse'))}</span>
        <span>${esc(t('ride.test'))}</span><span>${esc(t('ride.total'))}</span>
        <span>${esc(t('ride.place'))}</span><span>${esc(t('ride.check'))}</span>
      </div>
      ${rides.map((r) => {
        const check = rideCheck(r);
        const test = r.test_type ? t(`ride.${r.test_type}`) : '';
        const total = r.total_pct == null ? '' : Number(r.total_pct).toFixed(3);
        const published = r.grounded_total_pct == null ? '' : Number(r.grounded_total_pct).toFixed(3);
        // The published spelling qualifies the on-screen one; it never replaces it.
        const who = [r.grounded_rider, r.grounded_horse].filter(Boolean).join(' / ');
        return `
        <div class="ride">
          <span class="ride-num">${esc(String(r.order ?? ''))}</span>
          <span class="ride-who" ${who ? `title="${esc(who)}"` : ''}>
            <span class="ride-rider">${esc(r.rider || '—')}</span>
            <span class="ride-horse">${esc(r.horse || '')} · ${clock(r.start_sec || 0)}</span>
          </span>
          <span class="ride-test">${esc(test)}</span>
          <span class="ride-total">${esc(total)}${r.score_source && r.score_source !== 'observed'
            ? `<span class="ride-tag">${esc(r.score_source)}</span>` : ''}${
            published && published !== total ? `<span class="ride-published">${esc(published)}</span>` : ''}</span>
          <span class="ride-place">${esc(r.final_place == null ? '' : String(r.final_place))}</span>
          <span class="ride-check" data-tone="${check.tone}">${esc(check.text)}</span>
        </div>`;
      }).join('')}
    </div>`;
}



function openGameDetails(game) {
  const g = game || state.game;
  if (!g) return;

  const rows = GAME_DETAIL_ROWS
    .map(([key, read]) => [t(key), read(g)])
    .filter(([, value]) => value !== '' && value != null);

  // The sources belong in the popup rather than the card: they qualify the
  // grounded rows, and are meaningless next to a row nobody is looking at.
  const equipe = g.equipeUrl
    ? `<div class="detail-key">${esc(t('game.equipe'))}</div>
       <div class="detail-value"><a href="${esc(g.equipeUrl)}" target="_blank"
          rel="noopener noreferrer" class="link-btn">${esc(g.equipeUrl)}</a></div>`
    : '';
  const sources = (g.grounded && g.groundingSources?.length)
    ? `<div class="detail-key">${esc(t('game.groundedBy'))}</div>
       <div class="detail-value">${g.groundingSources.slice(0, 5).map((src) => `
         <a href="${esc(src.uri)}" target="_blank" rel="noopener noreferrer"
            class="link-btn">${esc(src.title || src.uri)}</a>`).join('<br />')}</div>`
    : '';

  $('details-title').textContent = gameHeadline(g);
  // The player's column is laid out as panels; a game record is the table.
  $('details-body').className = 'detail-grid';
  $('details-body').innerHTML = rows.map(([label, value]) => `
    <div class="detail-key">${esc(label)}</div>
    <div class="detail-value">${esc(String(value))}</div>`).join('')
    + ridesTable(g) + equipe + sources;

  // The two share one dialog, so a game opened after a moment would otherwise
  // inherit that moment's video — playing, beside a record it has nothing to
  // do with.
  $('details-player').innerHTML = '';
  if (state.details) {
    state.details = null;
    state.playing = null;
    destroyPlayer();
  }
  showDetailsModal(false);
}


/** Open a session: its match if it has one, otherwise a clean conversation. */
function openSession(sessionId) {
  const session = listSessions().find((s) => s.id === sessionId);
  if (!session) return;

  state.sessionKey = sessionId;
  // The conversation continues where it left off: the transcript and the
  // agent's own session are both held on the session, so switching back is
  // switching back, not starting again.
  state.sessionId = session.agentSessionId || null;
  state.scope = session.scope || null;
  state.msgs = Array.isArray(session.msgs) && session.msgs.length
    ? session.msgs.map((m) => ({ ...m, searching: false, deskLoading: false }))
    : [];
  state.msgs.forEach((m) => animatedMsgs.add(m));
  if (!state.scope && !state.msgs.some((m) => m.showScope)) {
    state.msgs.push({ who: 'agent', text: t('scope.prompt'), showScope: true, scopeStep: 'choose' });
  }
  state.playing = null;
  destroyPlayer();

  if (session.jobId) {
    selectJob(session.jobId);
  } else {
    state.unsubscribe.forEach((off) => off());
    state.unsubscribe = [];
    state.jobId = null;
    state.job = null;
    state.moments = [];
    state.events = [];
    state.game = null;
    // A conversation about no particular match still shows the desk's most
    // recent one, so "show me the best moments" has something to answer with.
    ensureJobContext();
    render();
  }
}


function startSession() {
  const session = createSession();
  state.sessions = listSessions();
  openSession(session.id);
}


/**
 * Remove a conversation.
 *
 * Only the conversation. A match is hours of analysis over a multi-gigabyte
 * upload; a session is a few lines in localStorage. Deleting the cheap thing
 * must never take the expensive one with it, and a sidebar tidy-up is exactly
 * the moment someone would do that by accident. Matches are deleted from the
 * job card, where the confirmation says what actually goes.
 */
function deleteSession(sessionId) {
  const session = listSessions().find((s) => s.id === sessionId);
  if (!session) return;

  const name = session.title || t('sessions.untitled');
  if (!window.confirm(
    `${t('sessions.deleteConfirm')} "${name}"?\n\n${t('sessions.deleteNote')}`)) return;

  removeSession(sessionId);
  state.sessions = listSessions();

  if (state.sessionKey === sessionId) {
    const next = state.sessions[0];
    if (next) openSession(next.id); else startSession();
  } else {
    render();
  }
}

/**
 * Analyse again, with the evidence in view.
 *
 * A recording once grounded to the right show and the wrong class, and every
 * field it filled looked plausible — three riders who compete in several
 * classes still matched. The only way to see it was to see what the search
 * had been told and what it had looked at, and nothing on screen showed
 * either. So this shows both, lets the editor fix the links, and only then
 * runs the analysis again: the links are saved first, because the agent reads
 * them off the job when it grounds.
 */
function reanalysePanel(j) {
  const r = state.reanalyse;
  const game = state.games.find((g) => (g.jobId || g.id) === j.id) || {};
  const sources = Array.isArray(game.groundingSources) ? game.groundingSources : [];
  const queries = Array.isArray(game.groundingQueries) ? game.groundingQueries : [];
  const none = `<div class="ctx-muted">${esc(t('reanalyse.none'))}</div>`;
  return `
    <div class="reanalyse">
      <div class="panel-head-title">${esc(t('reanalyse.title'))}</div>
      <div class="setting-hint">${esc(t('reanalyse.hint'))}</div>

      <div class="field-label">${esc(t('reanalyse.links'))}</div>
      <div class="ctx-list">
        ${r.urls.length ? r.urls.map((u, i) => `
          <div class="ctx-row">
            <a class="link-btn ctx-link" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>
            <button class="link-btn" data-ctx-remove="${i}">${esc(t('reanalyse.remove'))}</button>
          </div>`).join('') : none}
        <div class="ctx-row">
          <input class="input ctx-input" data-ctx-input placeholder="${esc(t('reanalyse.placeholder'))}" />
          <button class="btn-outline" data-ctx-add="1">${esc(t('reanalyse.add'))}</button>
        </div>
      </div>

      <div class="field-label">${esc(t('reanalyse.sources'))}</div>
      <div class="ctx-list">
        ${game.equipeUrl ? `<a class="link-btn ctx-link" href="${esc(game.equipeUrl)}" target="_blank" rel="noopener noreferrer">${esc(game.equipeUrl)}</a>` : ''}
        ${sources.length ? sources.map((src) => `
          <a class="link-btn ctx-link" href="${esc(src.uri)}" target="_blank" rel="noopener noreferrer">${esc(src.title || src.uri)}</a>`).join('')
          : (game.equipeUrl ? '' : none)}
      </div>

      <div class="field-label">${esc(t('reanalyse.queries'))}</div>
      <div class="ctx-list ctx-muted">${queries.length ? queries.map((q) => `<div>${esc(q)}</div>`).join('') : none}</div>

      <div class="ctx-actions">
        <button class="btn-solid" data-reanalyse-go="${esc(j.id)}">${esc(t('reanalyse.go'))}</button>
        <button class="link-btn" data-reanalyse-cancel="1">${esc(t('reanalyse.cancel'))}</button>
      </div>
    </div>`;
}


/** Distinct sports on the desk. The sport chooser is shown only when there are two. */
function sportsOnDesk() {
  return [...new Set(state.games.map((g) => (g.sport || '').toLowerCase()).filter(Boolean))];
}


/**
 * Where to look, and how narrowly.
 *
 * Two scopes: the open match, or every match on the desk. The desk scope can
 * be narrowed to a sport — offered only when the desk actually has more than
 * one, because a chooser with one option is a question with one answer — and
 * to particular matches. Ranking is the same on both paths; only the set of
 * candidates changes.
 */
function searchPanel(msg, index) {
  const sports = sportsOnDesk();
  const all = msg.searchMode === 'all';
  const chip = (attr, value, label, pressed) => `
    <button class="chip" data-${attr}="${index}:${esc(value)}" aria-pressed="${pressed}">${esc(label)}</button>`;
  return `
    <div class="search-panel">
      <div class="panel-head-title">${esc(t('search.title'))}</div>
      <div class="search-query">${esc(msg.query || '')}</div>
      <div class="search-opts">
        ${chip('search-mode', 'job', t('search.scopeJob'), !all)}
        ${chip('search-mode', 'all', t('search.scopeAll'), all)}
      </div>
      ${all && sports.length > 1 ? `
        <div class="field-label">${esc(t('search.sport'))}</div>
        <div class="search-opts">
          ${chip('search-sport', '', t('search.anySport'), !msg.searchSport)}
          ${sports.map((sp) => chip('search-sport', sp, sp, msg.searchSport === sp)).join('')}
        </div>` : ''}
      ${all && state.games.length > 1 ? `
        <div class="field-label">${esc(t('search.games'))}</div>
        <div class="search-opts">
          ${state.games
            .filter((g) => !msg.searchSport || (g.sport || '').toLowerCase() === msg.searchSport)
            .map((g) => chip('search-game', g.jobId || g.id, gameHeadline(g),
                             (msg.searchJobs || []).includes(g.jobId || g.id))).join('')}
        </div>` : ''}
      <div class="ctx-actions">
        <button class="btn-solid" data-search-run="${index}" ${msg.searching ? 'disabled' : ''}>
          ${esc(msg.searching ? t('search.running') : t('search.run'))}
        </button>
      </div>
    </div>`;
}


/**
 * The results: each one names its game, because across the desk a moment
 * without its match is a sentence without a subject. Rows are drawn from the
 * response rather than from the open match's listeners, which only hold the
 * moments of the match that is open.
 */
function searchCard(msg, index, head = '') {
  const rows = msg.searchResults;
  if (rows === null || rows === undefined) return '';
  if (!rows.length) return emptyCard(t('search.none'));
  return `
    <div class="list">
      <div class="list-head">
        <div class="panel-head-title">${esc(head || t('search.results'))}</div>
        <div class="panel-head-meta"><span class="list-count">${rows.length}</span></div>
      </div>
      <div class="panel-light">
        ${rows.map((r, k) => {
          const game = r.game || {};
          const who = [r.rider, r.horse].filter(Boolean).join(' / ');
          return `
          <div class="row">
            <button class="search-result" data-search-open="${index}:${k}">
              <div class="search-game">${esc(game.title || game.job_id || r.job_id || '')}${
                game.discipline || game.sport ? ` · ${esc(game.discipline || game.sport)}` : ''}</div>
              <div class="moment-label">${esc(r.summary || r.label || r.moment_type || '')}</div>
              <div class="tile-meta">${esc([r.label || r.moment_type, who, clock(r.start_sec || 0),
                r.rerank_score != null ? `rank ${Number(r.rerank_score).toFixed(2)}` : ''].filter(Boolean).join(' · '))}</div>
              ${r.rerank_reason ? `<div class="search-why">${esc(r.rerank_reason)}</div>` : ''}
            </button>
          </div>`;
        }).join('')}
      </div>
    </div>`;
}


/**
 * The desk's key moments, best first.
 *
 * Drawn from the response rather than the open match's listeners, which hold
 * only the open match. Games still analysing are named as such: their moments
 * do not exist yet, which is not the same as their having none.
 */
function deskMomentsCard(msg, index) {
  if (msg.deskLoading) {
    return `<div class="list"><div class="ctx-muted">${esc(t('desk.loading'))}</div></div>`;
  }
  const rows = Array.isArray(msg.searchResults) ? msg.searchResults : [];
  const running = Array.isArray(msg.running) ? msg.running : [];
  const note = running.length
    ? `<div class="desk-running">${running.map((id) => {
        const g = state.games.find((x) => (x.jobId || x.id) === id)
          || state.jobs.find((x) => x.id === id);
        return esc(g ? (g.title || gameHeadline(g)) : id);
      }).join(', ')} — ${esc(t('desk.running'))}</div>`
    : '';
  if (!rows.length) return emptyCard(t('search.none')) + note;

  // The same tiles as the open match's key moments, with the game on each.
  // Rows arrive snake_case from the store; the tile reads the camelCase the
  // listeners produce, so they are mapped once here rather than in the tile.
  const camel = (row) => Object.fromEntries(Object.entries(row).map(([k, v]) =>
    [k.replace(/_([a-z])/g, (_, c) => c.toUpperCase()), v]));
  return `
    <div class="list">
      <div class="list-head">
        <div class="panel-head-title">${esc(t('desk.title'))}</div>
        <div class="panel-head-meta"><span class="list-count">${rows.length}</span></div>
      </div>
      <div class="tile-row">${rows.map((row, k) =>
        momentTile(camel(row), { game: row.game || {}, open: `${index}:${k}` })).join('')}</div>
      ${note}
    </div>`;
}


async function loadDeskMoments(index) {
  const msg = state.msgs[index];
  if (!msg) return;
  try {
    const filters = scopeFilters(state.scope, state.games);
    const res = await api('/api/jobs/top-moments', {
      method: 'POST',
      body: JSON.stringify({ limit: 20, sport: filters.sport, job_ids: filters.jobIds }),
    });
    msg.searchResults = res.moments || [];
    msg.running = res.running || [];
  } catch (err) {
    msg.searchResults = [];
    say(`${t('desk.title')}: ${err.message || err}`);
  } finally {
    msg.deskLoading = false;
    render();
  }
}


async function runSearch(index) {
  const msg = state.msgs[index];
  if (!msg || !msg.query) return;
  msg.searching = true;
  render();
  try {
    const all = msg.searchMode === 'all';
    if (!all && !state.jobId) throw new Error(t('game.none'));
    const res = all
      ? await api('/api/jobs/search', {
          method: 'POST',
          body: JSON.stringify({ query: msg.query, limit: 10, rerank: true,
                                 sport: msg.searchSport || '', job_ids: msg.searchJobs || [] }),
        })
      : await api(`/api/jobs/${encodeURIComponent(state.jobId)}/search`, {
          method: 'POST', body: JSON.stringify({ query: msg.query, limit: 10, rerank: true }),
        });
    msg.searchResults = res.moments || res.results || [];
  } catch (err) {
    msg.searchResults = [];
    say(`${t('search.title')}: ${err.message || err}`);
  } finally {
    msg.searching = false;
    render();
  }
}


/**
 * Open a result. The row already holds the record, so the popup is drawn from
 * it directly — selecting the match first is for the player, whose listeners
 * only exist for the open job.
 */
function openSearchResult(index, k) {
  const row = state.msgs[index]?.searchResults?.[k];
  if (!row) return;
  if (row.job_id && row.job_id !== state.jobId) selectJob(row.job_id);
  const m = Object.fromEntries(Object.entries(row).map(([key, v]) =>
    [key.replace(/_([a-z])/g, (_, c) => c.toUpperCase()), v]));
  // The same entry openDetails uses, from the row rather than the listener:
  // the match may only just have been selected, and its moments not arrived.
  // A ride panel needs that match's tree, which is not here yet either, so a
  // result opens as the moment alone.
  openPlayer({
    key: m.momentId,
    moment: m,
    rideOrder: null,
    label: m.label || m.momentType || '',
    title: m.summary || m.label || t('moment.details'),
    ...playRange(Number(m.startSec) || 0, Number(m.endSec) || 0, { duration: jobDuration(m.jobId) }),
  });
}


function jobsCard(msg, index) {
  if (!state.jobs.length) return emptyCard(t('jobs.none'));
  const view = pageOf(state.jobs, msg.page);
  return `<div class="panel-light">${view.slice.map((j) => {
    if (j.kind === 'live') return liveJobRow(j);
    const running = ['analyzing', 'transcoding', 'uploaded'].includes(j.status);
    const failed = j.status === 'failed';
    const stalled = running && isStalled(j);
    const tone = failed || stalled ? 'failed' : running ? 'running' : 'idle';
    return `
      <div class="job">
        ${state.reanalyse?.jobId === j.id ? reanalysePanel(j) : ''}
        <div class="job-top">
          ${editableTitle(j.id, j.title || j.source?.originalName || j.id, 'job-name')}
          <div class="job-status" data-tone="${tone}">${
            stalled ? esc(t('jobs.stalled')) : esc(j.status || 'unknown')}</div>
        </div>
        <div class="job-stage">${esc(j.stage || '')}${
          j.media?.segmentCount ? ` · ${j.media.segmentCount} segments` : ''}${
          j.recovery?.attempts ? ` · ${esc(t('jobs.recovered'))} ×${j.recovery.attempts}` : ''}</div>
        ${running && !stalled ? `
          ${stageStrip(j)}
          <div class="meter-row">
            <div class="meter meter-neutral"><i style="width:${j.progress || 0}%"></i></div>
            <div class="meter-pct">${Math.round(j.progress || 0)}%</div>
          </div>` : ''}
        ${stalled ? `
          <div class="job-error">
            <p>${esc(t('jobs.noProgress'))} ${esc(sinceLabel(j.updatedAt))}.
               ${esc(t('jobs.deadRun'))}</p>
            <button class="btn-outline" data-retry="${esc(j.id)}">${esc(t('jobs.retry'))}</button>
          </div>` : ''}
        ${failed && j.error ? `
          <div class="job-error">
            <p>${esc(j.error)}</p>
            <button class="btn-outline" data-retry="${esc(j.id)}">${esc(t('jobs.retry'))}</button>
          </div>` : ''}
        <div class="job-actions">
          ${running && !stalled
            ? `<button class="link-btn" data-cancel-job="${esc(j.id)}">${esc(t('jobs.cancel'))}</button>`
            : `<button class="link-btn" data-reanalyse="${esc(j.id)}">${esc(t('jobs.analyseAgain'))}</button>`}
          <button class="link-btn" data-delete-job="${esc(j.id)}"
                  data-title="${esc(j.title || j.id)}">${esc(t('jobs.delete'))}</button>
        </div>
      </div>`;
  }).join('')}${pagerRow(view, index)}</div>`;
}

/**
 * A live event's row: its own state rather than the job status, a strip of
 * the three things that happen to it, and — once it is over — the game
 * record where the moments list would otherwise sit.
 *
 * Progress is counted in chunks against how many the window will produce,
 * because that is the only unit a live event has: there is no duration to
 * be a fraction of until the event has ended.
 */
function liveJobRow(j) {
  const live = liveSummary(j);
  const active = live.state === 'scheduled' || live.state === 'live';
  const tone = live.state === 'failed' ? 'failed' : live.state === 'live' ? 'running' : 'idle';
  const game = state.games.find((g) => (g.jobId || g.id) === j.id);
  const when = (v) => (v ? new Date(v).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '');
  let line = '';
  if (live.state === 'scheduled') {
    line = `${t('live.startsAt')} ${when(live.start)} · ${t('live.captureLead')}`;
  } else if (live.state === 'live') {
    line = `${live.analysed} ${t('live.of')} ${live.expected} ${t('live.chunks')}`
      + (live.waiting ? ` · ${live.waiting} ${t('live.captured')}` : ` · ${t('live.waiting')}`)
      + (live.moments ? ` · ${live.moments} ${t('live.moments')}` : '');
  } else {
    line = `${live.captured} ${t('live.chunksDone')} · ${live.moments} ${t('live.moments')}`;
  }
  if (live.restarts) line += ` · ${t('live.restarted')} ×${live.restarts}`;
  return `
      <div class="job">
        <div class="job-top">
          ${editableTitle(j.id, j.title || j.id, 'job-name')}
          <div class="job-status" data-tone="${tone}">${esc(t(`live.${live.state}`) || live.state)}</div>
        </div>
        <div class="job-stage">${esc(line)}</div>
        ${liveStrip(live)}
        ${j.status === 'failed' && j.error ? `
          <div class="job-error"><p>${esc(j.error)}</p></div>` : ''}
        ${live.state === 'complete' ? (game ? `
          <div class="live-game">
            <div class="moment-label">${esc(gameHeadline(game))}</div>
            <div class="moment-meta">${esc([
              game.competition || game.groundedCompetition,
              game.venue || game.groundedVenue, game.mood,
            ].filter(Boolean).join(' · ') || t('game.notIdentified'))}</div>
            ${game.summary ? `<div class="game-summary">${esc(game.summary)}</div>` : ''}
            <div class="moment-actions">
              <button class="link-btn" data-open-game="${esc(j.id)}">${esc(t('moment.details'))}</button>
            </div>
          </div>` : `
          <div class="job-stage">${esc(t('live.gamePending'))}</div>`) : ''}
        <div class="job-actions">
          ${active
            ? `<button class="link-btn" data-cancel-job="${esc(j.id)}">${esc(t('jobs.cancel'))}</button>`
            : ''}
          <button class="link-btn" data-delete-job="${esc(j.id)}"
                  data-title="${esc(j.title || j.id)}">${esc(t('jobs.delete'))}</button>
        </div>
      </div>`;
}

function liveStrip(live) {
  const fills = liveStageFills(live);
  const stages = [
    ['scheduled', fills.scheduled, live.state === 'scheduled'],
    ['capture', fills.capture, live.state === 'live' && fills.capture < 100],
    ['analysis', fills.analysis, live.state === 'live' && fills.analysis < 100],
  ];
  return `
    <div class="stage-strip">
      ${stages.map(([key, fill, active]) => `
        <div class="stage" style="flex-grow:${key === 'scheduled' ? 1 : 3}"
             data-state="${fill >= 100 ? 'done' : active ? 'active' : 'todo'}">
          <div class="stage-meter"><i style="width:${fill}%"></i></div>
          <div class="stage-label">${esc(t(`live.stage.${key}`))}</div>
        </div>`).join('')}
    </div>
    <div class="meter-row">
      <div class="meter meter-neutral"><i style="width:${fills.analysis}%"></i></div>
      <div class="meter-pct">${Math.round(fills.analysis)}%</div>
    </div>`;
}

/* ─────────────────────────────────────────────────────────── scope ── */

/**
 * What this conversation is about, asked at the start of every session.
 *
 * Three doors: everything, one sport and any of its disciplines, or games
 * picked by name. The choice is held on the session (see scope.js), names it
 * in the sidebar, narrows the games list, the desk shortlist and the search
 * panel, and is sent to the agent with every message — so "the best moments"
 * means the best moments *of these games* on both sides of the conversation.
 *
 * The steps live on the message rather than in global state, so the card
 * re-renders from the transcript like every other card and survives a
 * session switch.
 */
function scopeCard(m, i) {
  const step = m.scopeStep || 'choose';
  const chip = (attr, value, label, pressed) => `
    <button class="chip" data-${attr}="${i}:${esc(value)}" aria-pressed="${pressed}">${esc(label)}</button>`;
  const back = `<button class="link-btn" data-scope-back="${i}">${esc(t('scope.back'))}</button>`;

  if (step === 'done' && m.scope) {
    return `
      <div class="scope-summary">
        <span class="field-label">${esc(t('scope.current'))}</span>
        <span class="scope-name">${esc(scopeTitle(m.scope, state.games, t, gameHeadline))}</span>
        <button class="link-btn" data-scope-change="${i}">${esc(t('scope.change'))}</button>
      </div>`;
  }

  if (step === 'category') {
    const sports = sportsAvailable(state.games, state.sports);
    return `
      <div class="scope-panel">
        <div class="field-label">${esc(t('scope.pickSport'))}</div>
        <div class="search-opts">
          ${sports.map((sp) => chip('scope-sport', sp, sp, false)).join('')}
        </div>
        <div class="ctx-actions">${back}</div>
      </div>`;
  }

  if (step === 'discipline') {
    const discs = disciplinesFor(state.games, m.scopeSport);
    const chosen = m.scopeDisciplines || [];
    return `
      <div class="scope-panel">
        <div class="field-label">${esc(t('scope.pickDisciplines'))} · ${esc(m.scopeSport)}</div>
        <div class="search-opts">
          ${chip('scope-disc', '*', t('scope.allDisciplines'), !chosen.length)}
          ${discs.map((d) => chip('scope-disc', d, d, chosen.includes(d))).join('')}
        </div>
        <div class="ctx-actions">
          <button class="btn-solid" data-scope-done="${i}">${esc(t('scope.done'))}</button>
          ${back}
        </div>
      </div>`;
  }

  if (step === 'games') {
    const found = findGames(state.games, m.scopeQuery || '', gameHeadline);
    const chosen = m.scopeJobs || [];
    return `
      <div class="scope-panel">
        <div class="field-label">${esc(t('scope.searchGames'))}</div>
        <input class="composer-input" data-scope-query="${i}" style="margin-top:6px"
               placeholder="${esc(t('scope.searchPlaceholder'))}" value="${esc(m.scopeQuery || '')}" />
        <div class="search-opts">
          ${found.length ? found.slice(0, 30).map((g) => chip('scope-game', g.jobId || g.id,
              gameHeadline(g), chosen.includes(g.jobId || g.id))).join('')
            : `<span class="setting-hint">${esc(t('scope.noMatches'))}</span>`}
        </div>
        <div class="ctx-actions">
          <button class="btn-solid" data-scope-done="${i}" ${chosen.length ? '' : 'disabled'}>
            ${esc(t('scope.done'))}${chosen.length ? ` (${chosen.length})` : ''}
          </button>
          ${back}
        </div>
      </div>`;
  }

  return openerCard(i);
}


/* ──────────────────────────────────────────────────────── the opener ── */

/**
 * The icons the opener draws, as the design draws them: a recording arriving
 * and a search. Inline rather than fetched — two shapes are cheaper as markup
 * than as a request, and a missing asset here would leave the option nameless.
 */
const OPENER_ICONS = {
  upload: '<path d="M12 16V5"/><path d="m7.5 9.5 4.5-4.5 4.5 4.5"/>'
    + '<path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
};


/**
 * What a session can be about, and the ways in.
 *
 * Two things happen on this desk — a match arrives, and a match is searched —
 * and the second carries four questions about *which* matches. So the scope is
 * not a separate interrogation: it is the second half of the answer to what
 * the session is for. "Find moments · Across a Sport" both names the session
 * and narrows every card in it, which is what the scope card did on its own
 * and in a step nobody could connect to what they had come to do.
 *
 * There was a third option — generate clips — over the same four scopes. Clip
 * generation is being rebuilt and offering a way into something that no longer
 * runs is worse than offering nothing.
 *
 * The first option is the exception, and deliberately: a video that is not on
 * the desk yet has no scope to pick, so its ways in open a panel instead.
 */
const OPENER_SCOPES = ['all', 'recent', 'games', 'category'];

function openerOptions() {
  return [
    { key: 'add', icon: 'upload', links: ['upload', 'live', 'status'] },
    { key: 'find', icon: 'search', links: OPENER_SCOPES },
  ];
}


/**
 * The way back from whatever the opener swapped in.
 *
 * Only when the opener put it there — a stage strip or an ingest panel the
 * editor asked for in words has no opener behind it to return to, and a Back
 * that restored a card nobody had seen would be a trapdoor rather than a way
 * out. `showScope: false` with the step still on 'choose' is exactly the state
 * the swap leaves behind, and nothing else produces it.
 */
function openerBackRow(m, i) {
  if (m.showScope !== false || m.scopeStep !== 'choose') return '';
  if (!m.showIngest && !m.showJobs) return '';
  return `<div class="opener-back">
    <button class="link-btn" data-opener-back="${i}">${esc(t('opener.back'))}</button>
  </div>`;
}


function openerCard(i) {
  return `
    <div class="opener">
      ${openerOptions().map((o) => `
        <section class="opener-option">
          <div class="opener-icon">
            <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
                 aria-hidden="true">${OPENER_ICONS[o.icon]}</svg>
          </div>
          <div class="opener-text">
            <div class="opener-title">${esc(t(`opener.${o.key}`))}</div>
            <div class="opener-desc">${esc(t(`opener.${o.key}Desc`))}</div>
            <div class="opener-links">
              ${o.links.map((key) => `
                <button class="opener-link" data-opener="${i}:${o.key}:${key}"
                        >${esc(t(`opener.link.${key}`))}</button>`).join('')}
            </div>
          </div>
        </section>`).join('')}
    </div>`;
}


/** Back out of whatever the opener swapped in, to the opener itself. */
function onOpenerBack(hit) {
  const msg = state.msgs[Number(hit.dataset.openerBack)];
  if (!msg) return;
  msg.showIngest = false;
  msg.ingestKind = null;
  msg.showJobs = false;
  msg.showScope = true;
  msg.scopeStep = 'choose';
  msg.text = t('scope.prompt');
  persistTranscript();
  render();
}


/**
 * A way in, chosen.
 *
 * The first three swap a card into the message that offered them — the two
 * ingest panels, and the stage strip for "check status". They are local: the
 * strip renders from the jobs listener, so asking the agent what is running
 * would be a round trip for something already on the client, and on a desk
 * mid-analysis the answer would arrive after the bar had moved.
 *
 * The rest are a scope question with an intent attached: the scope steps are
 * the ones the scope card already had, and when one settles `applyScope` asks
 * the agent for what the session was opened to get. The intent rides on the
 * message so it survives the two or three renders a discipline picker takes.
 */
function onOpenerClick(hit) {
  const [rawIndex, group, key] = hit.dataset.opener.split(':');
  const i = Number(rawIndex);
  const msg = state.msgs[i];
  if (!msg) return;

  if (group === 'add') {
    // The card replaces the opener in the message that offered it, rather
    // than arriving under it: they are two states of the same question, and
    // stacked they read as a card that did not clear.
    msg.showScope = false;
    msg.text = '';
    if (key === 'status') {
      msg.showJobs = true;
      msg.page = 0;
    } else {
      msg.showIngest = true;
      msg.ingestKind = key === 'live' ? 'live' : 'upload';
    }
    persistTranscript();
    render();
    return;
  }

  msg.intent = group;                      // 'find'
  if (key === 'all') { applyScope(i, { kind: 'all' }); return; }
  if (key === 'recent') {
    // The desk's newest analysed match. With none, there is nothing to scope
    // to and the whole catalogue is the honest answer.
    const recent = state.games[0];
    applyScope(i, recent
      ? { kind: 'games', jobIds: [recent.jobId || recent.id] } : { kind: 'all' });
    return;
  }
  msg.scopeStep = key === 'games' ? 'games' : 'category';
  msg.scopeSport = '';
  msg.scopeDisciplines = [];
  msg.scopeJobs = [];
  msg.scopeQuery = '';
  render();
}

function applyScope(i, scope) {
  const msg = state.msgs[i];
  if (msg) {
    msg.scope = scope;
    msg.scopeStep = 'done';
  }
  state.scope = scope;
  const inScope = gamesInScope(scope, state.games);
  const first = inScope[0] ? (inScope[0].jobId || inScope[0].id) : null;
  if (state.sessionKey) {
    updateSession(state.sessionKey, {
      scope,
      title: scopeTitle(scope, state.games, t, gameHeadline),
      ...(first ? { jobId: first } : {}),
    });
    state.sessions = listSessions();
  }
  if (first && first !== state.jobId) selectJob(first);
  persistTranscript();
  render();

  // The session was opened to do something, not to be scoped. Asking is the
  // difference between the opener naming the session and the opener answering
  // the question it asked.
  const intent = msg?.intent;
  if (intent) {
    msg.intent = null;
    // No cards passed, so the reply is routed through attachCards the way a
    // typed question is — "the best moments" gets the moments card.
    ask(t('opener.askMoments'));
  }
}

function onScopeClick(hit) {
  const parse = (v) => { const k = v.indexOf(':'); return [Number(v.slice(0, k)), v.slice(k + 1)]; };
  if (hit.dataset.scopePick) {
    const [i, key] = parse(hit.dataset.scopePick);
    const msg = state.msgs[i];
    if (!msg) return;
    if (key === 'all') { applyScope(i, { kind: 'all' }); return; }
    msg.scopeStep = key;                      // 'category' or 'games'
    msg.scopeSport = '';
    msg.scopeDisciplines = [];
    msg.scopeJobs = [];
    msg.scopeQuery = '';
    render();
    return;
  }
  if (hit.dataset.scopeSport) {
    const [i, sport] = parse(hit.dataset.scopeSport);
    const msg = state.msgs[i];
    if (!msg) return;
    msg.scopeSport = sport;
    // A sport with no disciplines on the desk has nothing to ask.
    if (!disciplinesFor(state.games, sport).length) {
      applyScope(i, { kind: 'category', sport, disciplines: [] });
      return;
    }
    msg.scopeStep = 'discipline';
    msg.scopeDisciplines = [];
    render();
    return;
  }
  if (hit.dataset.scopeDisc) {
    const [i, disc] = parse(hit.dataset.scopeDisc);
    const msg = state.msgs[i];
    if (!msg) return;
    const set = new Set(msg.scopeDisciplines || []);
    if (disc === '*') set.clear();
    else if (set.has(disc)) set.delete(disc); else set.add(disc);
    msg.scopeDisciplines = [...set];
    render();
    return;
  }
  if (hit.dataset.scopeGame) {
    const [i, id] = parse(hit.dataset.scopeGame);
    const msg = state.msgs[i];
    if (!msg) return;
    const set = new Set(msg.scopeJobs || []);
    if (set.has(id)) set.delete(id); else set.add(id);
    msg.scopeJobs = [...set];
    render();
    return;
  }
  if (hit.dataset.scopeDone) {
    const i = Number(hit.dataset.scopeDone);
    const msg = state.msgs[i];
    if (!msg) return;
    if (msg.scopeStep === 'discipline') {
      applyScope(i, { kind: 'category', sport: msg.scopeSport, disciplines: msg.scopeDisciplines || [] });
    } else if (msg.scopeStep === 'games' && (msg.scopeJobs || []).length) {
      applyScope(i, { kind: 'games', jobIds: msg.scopeJobs });
    }
    return;
  }
  if (hit.dataset.scopeBack) {
    const msg = state.msgs[Number(hit.dataset.scopeBack)];
    if (!msg) return;
    msg.scopeStep = msg.scopeStep === 'discipline' ? 'category' : 'choose';
    render();
    return;
  }
  if (hit.dataset.scopeChange) {
    const msg = state.msgs[Number(hit.dataset.scopeChange)];
    if (!msg) return;
    msg.scopeStep = 'choose';
    render();
  }
}


function activityCard(msg, index) {
  if (!state.events.length) return emptyCard(t('activity.none'));
  // Every event, newest first, a page at a time. The feed is how a long run is
  // followed, so stopping at a dozen hides exactly the part someone scrolled
  // back for — but eighty of them in one message buries the conversation.
  const view = pageOf(state.events, msg.page);
  return `<div class="panel-light">${view.slice.map((e) => `
    <div class="job">
      <div class="job-top">
        <div class="job-name" style="font-weight:400;font-size:11.5px">${esc(e.message)}</div>
        <div class="job-status" data-tone="${e.level === 'error' ? 'failed' : 'idle'}">${esc(e.stage || '')}</div>
      </div>
    </div>`).join('')}${pagerRow(view, index)}</div>`;
}

function actionsRow(msg) {
  if (!msg.showActions || !msg.actions?.length) return '';
  return `<div class="chip-row">${msg.actions.map((a) => `
    <button class="chip" data-ask="${esc(a)}">${esc(a)}</button>`).join('')}</div>`;
}

/* ────────────────────────────────────────────────────────── render ── */

/**
 * Whether a message's card already answers it, so the prose is a second copy.
 *
 * "Show me the best moments" comes back as a card of every moment and a
 * paragraph re-typing the first ten of them, timecodes and all — the same
 * answer twice, the worse one first, and hundreds of tokens spent writing out
 * what the screen is already showing.
 *
 * Only when the card has something in it. An empty card says "no moments yet",
 * which is not the same as "I could not read them": the missing-index failure
 * arrived as prose beside an empty card, and hiding it unconditionally would
 * have made that unreadable.
 */
function cardAnswersIt(m) {
  if (m.showMoments) return momentsFor(m).list.length > 0;
  // Only a card with rides in it answers; "no ride matched" leaves the
  // agent's own reply on screen, which may know why.
  if (m.showRides) return Boolean(ridesFor(m).asked);
  if (m.showGames) return state.games.length > 0;
  if (m.showGame) return Boolean(state.game);
  return false;
}


/**
 * Agent turns that have already played their entrance.
 *
 * The transcript is rewritten with innerHTML on every render and an agent
 * reply re-renders on every streamed token, so a fade-in class left in the
 * markup would replay the animation on each of the dozens of writes a reply
 * takes to arrive — a card that strobes rather than one that appears. The
 * message objects are stable across renders even though their elements are
 * not, so they are what gets remembered; a WeakSet does it without keeping a
 * cleared transcript alive.
 */
const animatedMsgs = new WeakSet();


function render() {
  renderSessions();
  $('transcript').innerHTML = currentTurn(state.msgs).map(([m, i]) => {
    const agent = m.who === 'agent';
    const fresh = agent && !animatedMsgs.has(m);
    if (fresh) animatedMsgs.add(m);
    return `
    <div class="msg ${agent ? `msg-agent card${fresh ? ' fade-in' : ''}` : 'msg-user'}">
      <div class="msg-label">${agent ? 'Agent' : 'You'}</div>
      ${!m.text || cardAnswersIt(m) ? '' : `<div class="msg-text">${esc(m.text)}</div>`}
      ${openerBackRow(m, i)}
      ${m.showScope ? scopeCard(m, i) : ''}
      ${m.showSearch ? searchPanel(m, i) + searchCard(m, i) : ''}
      ${m.showDeskMoments ? deskMomentsCard(m, i) : ''}
      ${m.showMoments ? momentsCard(m, i) : ''}
      ${m.showRides ? ridesCard(m, i) : ''}
      ${m.showIngest ? ingestCard(m) : ''}
      ${m.showJobs ? jobsCard(m, i) : ''}
      ${m.showGame ? gameCard() : ''}
      ${m.showGames ? gamesCard(m, i) : ''}
      ${m.showActivity ? activityCard(m, i) : ''}
      ${actionsRow(m)}
    </div>`;
  }).join('')
    // No fade on the waiting card: it is not a message, so there is no object
    // to remember it by, and it would re-enter on every render while it waits.
    + (state.thinking ? `
      <div class="msg msg-agent card">
        <div class="msg-label">${esc(t('agent.label'))}</div>
        <div class="thinking-text">${esc(t('composer.thinking'))}</div>
      </div>` : '');

  if (state.playing) mountPlayer();
  // The field is destroyed and rebuilt by every render — an analysis writes
  // often enough that a rename typed across two of them would lose its caret
  // mid-word. The same restoration the scope search box does.
  if (state.renaming) {
    const field = document.querySelector('[data-rename-input]');
    if (field && document.activeElement !== field) {
      const at = Math.min(state.renaming.at ?? field.value.length, field.value.length);
      field.focus();
      field.setSelectionRange(at, at);
    }
  }
  loadThumbs();
}


/**
 * Sign the stills for the moments currently on screen.
 *
 * The media bucket is private and an <img> carries no Authorization header, so
 * the picture needs a URL that is its own credential. Signing is a round trip
 * to IAM per URL, which is why this asks for the page being looked at rather
 * than for the two hundred moments a match has.
 *
 * `asked` is marked before the request goes out. render() is what calls this,
 * and this calls render() when the URLs land — without that mark the two would
 * chase each other, and a failure would retry for ever.
 */
async function loadThumbs() {
  if (!state.user) return;

  // Stills are signed by path — jobs/<job>/moments/<id>.png — so every id has
  // to go to the route of the job it belongs to. A tile says which with
  // data-thumb-job; one that does not is the open match's. Grouping is what
  // lets a card of moments from several games get its pictures at all.
  const groups = new Map();
  for (const el of document.querySelectorAll('[data-thumb]')) {
    const id = el.dataset.thumb;
    const jobId = el.dataset.thumbJob || state.jobId;
    if (!id || !jobId || state.thumbs.asked.has(id)) continue;
    if (!groups.has(jobId)) groups.set(jobId, []);
    if (groups.get(jobId).length < 50) groups.get(jobId).push(id);
  }
  if (!groups.size) return;

  for (const ids of groups.values()) ids.forEach((id) => state.thumbs.asked.add(id));
  let got = false;
  await Promise.all([...groups].map(async ([jobId, ids]) => {
    try {
      const res = await api(`/api/jobs/${encodeURIComponent(jobId)}/thumbnails`, {
        method: 'POST',
        body: JSON.stringify({ moment_ids: ids }),
      });
      // Moment ids are unique across the desk, so a response for any job can
      // be merged whatever match is open now. selectJob resets this cache, so
      // a response that lands after a switch simply fills a fresh map.
      Object.assign(state.thumbs.urls, res.thumbnails || {});
      got = true;
    } catch (err) {
      // A still that will not sign is a placeholder, which is what the row
      // showed before any of this existed. Not worth an error in the transcript.
      console.warn('could not sign moment thumbnails', err);
    }
  }));
  if (got) render();
}

/* ───────────────────────────────────────────────── inline playback ── */

let hls = null;
let playbackUrl = null;
let playerEl = null;      // survives innerHTML rebuilds
let playerFor = null;     // which moment playerEl is bound to

/**
 * Fetch the playback URL and let the API set the Cloud CDN cookie.
 *
 * The cookie, not a signed URL, is what authorises playback: an HLS playlist
 * references segments relatively, so a query-string signature would cover the
 * playlist and none of its thousands of segments. The browser attaches the
 * cookie to every one of them without the player knowing.
 */
/**
 * Fetch the playback URL; the API sets the Cloud CDN cookie alongside it.
 *
 * The CDN is served from this same hostname through the load balancer, so the
 * cookie is same-origin and the browser attaches it to the playlist and to
 * every segment automatically. A signed URL could not do this: HLS playlists
 * reference segments relatively, so the query string is dropped on resolution
 * and only the playlist itself would be authorised.
 */
async function ensurePlaybackUrl() {
  if (playbackUrl) return playbackUrl;
  const p = await api(`/api/jobs/${state.jobId}/playback`);
  playbackUrl = p.hls_url;
  return playbackUrl;
}

/**
 * The player: the frame, what range it is on, a scrubber across that range,
 * and the controls an editor judges a movement with.
 *
 * Built once per open popup and kept across renders; the range it plays lives
 * in state.playing and is read on every tick, so a chip, Widen or Full ride
 * re-aims this same video rather than building another.
 *
 * The scrubber is the whole ride (playerTimeline), with the moment marked on
 * it as a band between an in and an out marker — a movement is judged in its
 * round, not cut out of it. The clock reads the same bar: "5:12 / 6:30" into
 * the ride, not two hours into the recording. Playing stops at the moment's
 * out point (or loops) as it always did; a seek outside the band lets the
 * ride run on (`free`), and a chip, Widen or Play from the end re-arms it.
 */
function buildPlayerEl() {
  const el = document.createElement('div');
  el.className = 'inline-player';
  el.innerHTML = `
    <div class="player-frame">
      <video playsinline preload="none"></video>
      <span class="player-tag" data-player-tag></span>
    </div>
    <div class="scrub" data-scrub role="slider" tabindex="0" aria-label="${esc(t('player.position'))}">
      <div class="scrub-track" data-scrub-track>
        <div class="scrub-fill" data-scrub-fill></div>
        <div class="scrub-range" data-scrub-range hidden>
          <span class="scrub-mark scrub-mark-in" data-scrub-in></span>
          <span class="scrub-mark scrub-mark-out" data-scrub-out></span>
        </div>
        <div class="scrub-head" data-scrub-head></div>
      </div>
    </div>
    <div class="player-controls">
      <button class="btn-solid player-play" data-pc="play">${esc(t('player.play'))}</button>
      <button class="pc" data-pc="back" aria-label="${esc(t('player.back5'))}">−5s</button>
      <button class="pc" data-pc="fwd" aria-label="${esc(t('player.fwd5'))}">+5s</button>
      <button class="pc" data-pc="loop" aria-pressed="false">${esc(t('player.loop'))}</button>
      <button class="pc" data-pc="speed" aria-label="${esc(t('player.speed'))}">1×</button>
      <button class="pc" data-pc="widen" title="${esc(t('player.widenHint'))}">${esc(t('player.widen'))}</button>
      <button class="pc" data-pc="ride" aria-pressed="false">${esc(t('player.fullRide'))}</button>
      <span class="player-time" data-player-time>0:00 / 0:00</span>
    </div>`;

  const video = el.querySelector('video');
  const scrub = el.querySelector('[data-scrub]');
  // Clicking the picture is play/pause, as on every player people know.
  video.addEventListener('click', () => playerControl('play'));

  // Drag or click along the bar to move inside the range; arrow keys step a
  // second, which is what finding the frame a foot lands on takes.
  const seekTo = (clientX) => {
    const p = state.playing;
    if (!p) return;
    const box = el.querySelector('[data-scrub-track]').getBoundingClientRect();
    const share = Math.min(Math.max((clientX - box.left) / Math.max(box.width, 1), 0), 1);
    const tl = timelineOf(p);
    moveTo(p, video, tl.start + share * (tl.end - tl.start));
  };
  scrub.addEventListener('pointerdown', (e) => {
    scrub.setPointerCapture(e.pointerId);
    seekTo(e.clientX);
    const move = (ev) => seekTo(ev.clientX);
    scrub.addEventListener('pointermove', move);
    scrub.addEventListener('pointerup', () => scrub.removeEventListener('pointermove', move), { once: true });
  });
  scrub.addEventListener('keydown', (e) => {
    const p = state.playing;
    if (!p || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    e.preventDefault();
    moveTo(p, video, clampTo(timelineOf(p), video.currentTime + (e.key === 'ArrowLeft' ? -1 : 1)));
  });

  // The out point is read from state on every tick, because the range moves.
  video.addEventListener('timeupdate', () => {
    const p = state.playing;
    if (!p) return;
    if (p.free) {
      if (video.currentTime >= timelineOf(p).end) video.pause();
    } else if (video.currentTime >= p.end) {
      if (p.loop) video.currentTime = p.start;
      else video.pause();
    }
    syncPlayer();
  });
  video.addEventListener('play', syncPlayer);
  video.addEventListener('pause', syncPlayer);
  return el;
}


/** Bring the controls in line with what is playing and where it is. */
function syncPlayer() {
  const p = state.playing;
  if (!playerEl || !p) return;
  const video = playerEl.querySelector('video');
  const tl = timelineOf(p);
  const length = Math.max(0, tl.end - tl.start);
  const at = Math.min(Math.max((video.currentTime || p.start) - tl.start, 0), length);
  const share = `${length ? (at / length) * 100 : 0}%`;
  const q = (sel) => playerEl.querySelector(sel);

  q('[data-player-tag]').textContent = `${p.label} · ${clock(p.start)}–${clock(p.end)} ${t('player.inSource')}`;
  q('[data-scrub-fill]').style.width = share;
  q('[data-scrub-head]').style.left = share;
  // The moment on the ride: a band between its in and out markers, or nothing
  // when what is playing is the whole bar.
  const band = rangeBand(p, tl);
  const range = q('[data-scrub-range]');
  range.hidden = !band;
  q('[data-scrub-track]').dataset.banded = String(Boolean(band));
  if (band) {
    range.style.left = `${band.left}%`;
    range.style.width = `${band.width}%`;
    q('[data-scrub-in]').title = `${t('player.markIn')} ${clock(p.start)}`;
    q('[data-scrub-out]').title = `${t('player.markOut')} ${clock(p.end)}`;
  }
  const scrub = q('[data-scrub]');
  scrub.setAttribute('aria-valuemin', '0');
  scrub.setAttribute('aria-valuemax', String(Math.round(length)));
  scrub.setAttribute('aria-valuenow', String(Math.round(at)));
  scrub.setAttribute('aria-valuetext', `${shortClock(at)} / ${shortClock(length)}`);
  q('[data-player-time]').textContent = `${shortClock(at)} / ${shortClock(length)}`;
  q('[data-pc="play"]').textContent = video.paused ? t('player.play') : t('player.pause');
  q('[data-pc="loop"]').setAttribute('aria-pressed', String(Boolean(p.loop)));
  q('[data-pc="speed"]').textContent = `${p.rate}×`;
  const ride = q('[data-pc="ride"]');
  ride.hidden = p.rideOrder == null;
  ride.setAttribute('aria-pressed', String(Boolean(p.full)));
}


/** Re-parent the live player into this render's slot, building it once. */
async function mountPlayer() {
  const { key, start } = state.playing;
  // A moment plays in its details popup and nowhere else. The transcript used
  // to hold a slot of its own under the row, which meant two elements with the
  // same data-slot and the wrong one — the one behind the dialog — winning on
  // document order.
  const slot = state.details === key
    ? $('details-player').querySelector(`[data-slot="${CSS.escape(key)}"]`)
    : null;
  if (!slot) return;

  if (playerFor !== key) {
    destroyPlayer();
    playerEl = buildPlayerEl();
    playerFor = key;
  }

  if (playerEl.parentElement !== slot) slot.appendChild(playerEl);
  syncPlayer();

  const video = playerEl.querySelector('video');
  if (video.dataset.mounted) return;
  video.dataset.mounted = '1';

  let url;
  try {
    url = await ensurePlaybackUrl();
  } catch (err) {
    // Packaging is independent of the analysis, so a job can have moments and
    // still have nothing to play — and re-running the whole analysis to fix
    // that would be an hour spent on the wrong thing. Offer the packaging.
    const notReady = /still being prepared/i.test(err.message);
    playerEl.insertAdjacentHTML('beforeend', `
      <div class="error-note">
        <p>${notReady
          ? esc(t('player.notPackaged'))
          : `${esc(t('player.notReady'))}: ${esc(err.message)}`}</p>
        ${notReady && state.jobId ? `
          <button class="btn-outline" data-prepare-playback="${esc(state.jobId)}">
            ${esc(t('player.preparePlayback'))}
          </button>` : ''}
      </div>`);
    return;
  }

  if (hls) { hls.destroy(); hls = null; }
  const begin = () => {
    video.currentTime = state.playing?.start ?? start;
    video.playbackRate = state.playing?.rate || 1;
    video.play().catch(() => {});
  };
  if (window.Hls?.isSupported()) {
    hls = new window.Hls({
      startPosition: start,
      maxBufferLength: 30,
      // Same-origin, so the CDN cookie rides along on every playlist and
      // segment request without any per-request setup.
      xhrSetup: (xhr) => { xhr.withCredentials = true; },
    });
    // A 403 here is the CDN refusing the request, and it means one specific
    // thing: the signed cookie the API just set did not come back with it. The
    // player's own message for that is "manifestLoadError", which sends the
    // reader to the encode rather than to the cookie.
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      const status = data?.response?.code;
      if (status !== 403 || !data.fatal) return;
      playerEl.insertAdjacentHTML('beforeend',
        `<div class="error-note">${esc(t('player.forbidden'))}</div>`);
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.MANIFEST_PARSED, begin);
  } else {
    video.src = url;                       // Safari plays HLS natively
    video.addEventListener('loadedmetadata', begin, { once: true });
  }
}


// Space plays and pauses, and the arrow keys step five seconds, while the
// player is open — unless someone is typing, or the scrubber (which steps a
// second) has the focus.
document.addEventListener('keydown', (e) => {
  if (!state.playing || !playerEl || $('details').classList.contains('hidden')) return;
  if (e.target.closest('input, textarea, select, [data-scrub], button')) return;
  if (e.key === ' ') { e.preventDefault(); playerControl('play'); }
  if (e.key === 'ArrowLeft') { e.preventDefault(); playerControl('back'); }
  if (e.key === 'ArrowRight') { e.preventDefault(); playerControl('fwd'); }
});


function destroyPlayer() {
  if (hls) { hls.destroy(); hls = null; }
  if (playerEl?.parentElement) playerEl.remove();
  playerEl = null;
  playerFor = null;
}


/* ──────────────────────────────────────────────────────────── agent ── */

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const r = await api('/api/agent/sessions', { method: 'POST' });
  state.sessionId = r.session_id;
  return state.sessionId;
}

/**
 * Send a message and stream the reply.
 *
 * `cards` attaches a card to the agent's message the moment it exists, rather
 * than when the turn ends. That matters for anything long: card selection
 * normally runs on the finished reply, so an analysis would show its progress
 * widget an hour after the progress was worth watching.
 */
async function ask(text, cards = null) {
  push({ who: 'you', text });
  state.thinking = true;
  render();

  const target = { text: '' };
  let msgIndex = -1;

  try {
    const sessionId = await ensureSession();
    const res = await fetch(`${API}/api/agent/messages`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(state.user ? { Authorization: `Bearer ${await getIdToken(state.user)}` } : {}),
      },
      body: JSON.stringify({
        message: text, session_id: sessionId, job_id: state.jobId,
        context: scopeContextLine(state.scope, state.games, gameHeadline) || null,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`Agent returned ${res.status}`);

    state.thinking = false;
    state.msgs.push({ who: 'agent', text: '', ...(cards || {}) });
    msgIndex = state.msgs.length - 1;
    render();

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      pending += decoder.decode(value, { stream: true });
      const frames = pending.split('\n\n');
      pending = frames.pop() || '';

      for (const frame of frames) {
        const ev = /event: (.+)/.exec(frame)?.[1];
        const raw = /data: (.+)/.exec(frame)?.[1];
        if (!ev || !raw) continue;
        const data = JSON.parse(raw);
        if (ev === 'text') {
          target.text += data.text;
          state.msgs[msgIndex].text = target.text;
        } else if (ev === 'tool') {
          state.msgs[msgIndex].text = `${target.text}${target.text ? '\n' : ''}· ${data.name}…`;
        } else if (ev === 'error') {
          state.msgs[msgIndex].text = `${target.text}\n\n${data.error}`;
        }
        render();
        scrollDown();
      }
    }
    if (target.text) state.msgs[msgIndex].text = target.text;
    // Cards chosen by the caller are deliberate; inferring more from the
    // wording on top of them would only fight what was asked for.
    if (!cards) attachCards(msgIndex, text);
  } catch (err) {
    state.thinking = false;
    if (msgIndex >= 0) state.msgs[msgIndex].text = `Could not reach the agent: ${err.message}`;
    else say(`Could not reach the agent: ${err.message}`);
  }
  persistTranscript();
  render();
  scrollDown();
}

/**
 * Decide which inline card belongs under the agent's reply.
 *
 * Driven by the user's intent plus what actually exists in Firestore, so a card
 * never renders empty — the prototype could assume its fixtures were present.
 */
function attachCards(index, question) {
  const msg = state.msgs[index];
  if (!msg) return;

  // Which card answers this lives in cards.js, where it is tested against the
  // phrasings people actually type. What is left here is what each card needs
  // once it has been chosen.
  const card = chooseCard(question);

  if (card === 'desk-moments' && !gameNamedIn(question)) {
    // The ranked shortlist with no match named is a question about the desk.
    msg.showDeskMoments = true;
    msg.searchResults = null;
    msg.deskLoading = true;
    loadDeskMoments(index);
  } else if (card === 'desk-moments') {
    // A match was named, so it is that match's shortlist after all.
    msg.showMoments = true;
    msg.showActions = true;
    msg.sort = 'score';
  } else if (card === 'search') {
    // The route only fires on scope words, so the panel opens set to the
    // whole desk; the editor can pull it back to the open match.
    msg.showSearch = true;
    msg.query = question;
    msg.searchMode = 'all';
    // Narrowed to the session's scope; the editor can widen it in the panel.
    const filters = scopeFilters(state.scope, state.games);
    msg.searchSport = filters.sport;
    msg.searchJobs = filters.jobIds;
    msg.searchResults = null;
  } else if (card === 'activity') {
    msg.showActivity = true;
  } else if (card === 'rides') {
    // The rides of one event, narrowed by the rider, horse or score bar the
    // question names. Which event: the one that ran the named rider — the
    // open one if it did — else the open one if it has rides at all, else the
    // first in scope that has. Selecting it re-points the listeners, and the
    // card waits for its tree rather than answering from the last match's.
    msg.showRides = true;
    msg.rideQuery = question;
    msg.sort = 'score';
    msg.jobId = rideJobFor(question);
    if (msg.jobId && msg.jobId !== state.jobId) selectJob(msg.jobId);
  } else if (card === 'games') {
    msg.showGames = true;
    // "show all game details" asked for the records. Leaving each behind its
    // own Details button answers with the index rather than the answer.
    msg.expandGames = wantsDetail(question);
  } else if (card === 'game') {
    msg.showGame = true;
    msg.showActions = true;
    msg.actions = [t('action.bestMoments'), t('action.processing')];
  } else if (card === 'ingest') {
    msg.showIngest = true;
  } else if (card === 'jobs') {
    msg.showJobs = true;
    msg.showActions = true;
    msg.actions = [t('action.bestMoments'), t('action.ingest')];
  } else {
    // Every moment, not a top handful. A list that silently stops at six looks
    // like the analysis found six. The order is chosen here rather than baked
    // in, so the card's own toggle can change it afterwards.
    msg.showMoments = true;
    // No action row: it offered "Cut all of these" and "Cut a 30-second
    // short", and both asked for a thing this desk no longer does. The card's
    // own sort and its tiles are what a moments answer is followed up with.

    // A question that names a match is about that match, whichever one happens
    // to be open. Selecting it re-points every listener, so the ids are not
    // snapshotted — there are none yet — and the card reads the live list until
    // they arrive.
    const named = gameNamedIn(question);
    // "show every penalty" asked for penalties. Without this the card answered
    // every question with every moment, which is the same answer as no filter
    // at all and looks like the filter ran and found everything.
    Object.assign(msg, filterAsked(question, named ? gameHeadline(named) : ''));
    if (named && (named.jobId || named.id) !== state.jobId) {
      msg.jobId = named.jobId || named.id;
      msg.momentIds = null;
      selectJob(msg.jobId);
    } else {
      msg.jobId = state.jobId;
      msg.momentIds = state.moments.map((m) => m.momentId);
    }
  }
}


/* ─────────────────────────────────────────────────────────── upload ── */

/**
 * Turn an object that is already in the bucket into a job the agent can run.
 *
 * Shared by a fresh upload and by resuming one that never got registered — the
 * bytes are in the same place either way, so only this second half differs.
 */
async function registerAndAnalyse({ job_id, filename, size_bytes, content_type, uploaded_by }) {
  const u = state.upload;
  u.status = 'uploading';
  u.stage = 'Registering the job';
  render();

  await api('/api/jobs', {
    method: 'POST',
    body: JSON.stringify({
      job_id,
      title: state.upload.title.trim() || filename.replace(/\.[^.]+$/, ''),
      sport: u.sport,
      filename,
      size_bytes,
      content_type,
      // Copied onto the job rather than read at analysis time: what a match's
      // descriptions are written in is a property of that match, not of
      // whoever opens it later.
      metadata_language: getSettings().metadataLanguage,
      title_source: state.upload.title.trim() ? 'editor' : 'derived',
      context_urls: contextUrlList(),
      // Only set when picking up an orphan somebody else left: the bytes are
      // under their prefix, not this caller's.
      ...(uploaded_by ? { uploaded_by } : {}),
    }),
  });

  // It has a job document now, so it is no longer stranded.
  state.pendingUploads = state.pendingUploads.filter((p) => p.job_id !== job_id);

  // Note what this conversation is now about, so reopening it comes back here.
  // A bookmark, not a claim: the job is not owned by this session, and outlives
  // it.
  if (state.sessionKey) {
    updateSession(state.sessionKey, {
      scope: { kind: 'games', jobIds: [job_id] },
      jobId: job_id,
      title: state.upload.title.trim() || filename.replace(/\.[^.]+$/, ''),
    });
    state.scope = { kind: 'games', jobIds: [job_id] };
    state.sessions = listSessions();
  }

  // Idle the moment the job exists, not when the agent's turn ends. The
  // panel's job is done once the match is registered — the run's progress is
  // the stage strip's business — and the turn stays open for the whole
  // analysis. Holding the form busy for that long disabled every ingest
  // button, including Schedule Live on the other tab, and an engine that
  // never answered left them disabled for good.
  u.status = 'idle';
  u.stage = 'Handed to the agent';
  u.file = null;
  u.name = '';
  u.title = '';
  selectJob(job_id);
  playbackUrl = null;
  render();

  await ask('Analyse this match and find its key moments.', {
    showJobs: true,
    showActions: true,
    actions: [t('action.processing'), t('action.bestMoments')],
  });
  render();
}


/**
 * Register a job against a video already in Cloud Storage.
 *
 * The browser upload is one non-resumable PUT, and these recordings are eight
 * hours and twelve gigabytes: a dropped connection starts the whole thing
 * again. `gcloud storage cp` is resumable and parallel, so for a file that size
 * the right answer is to let it do the copying and hand the location over.
 */
/**
 * The context links as a list, from the form's textarea.
 *
 * One per line, blanks dropped, duplicates dropped. Read from the DOM rather
 * than held in state so a link typed after the file was chosen still goes.
 */
function contextUrlList() {
  const raw = document.querySelector('[data-context-urls]')?.value || '';
  return [...new Set(raw.split(/\s+/).map((x) => x.trim()).filter(Boolean))].slice(0, 10);
}


function addContextUrl() {
  const input = document.querySelector('[data-ctx-input]');
  const url = (input?.value || '').trim();
  if (!url || !state.reanalyse) return;
  if (!/^https?:\/\/\S+$/.test(url)) { input.classList.add('input-bad'); return; }
  if (!state.reanalyse.urls.includes(url) && state.reanalyse.urls.length < 10) {
    state.reanalyse.urls.push(url);
  }
  render();
}


/**
 * Save the links, then ask for the analysis. In that order, and awaited: the
 * agent reads the links off the job document when it grounds, so a request
 * that raced ahead of the save would analyse against the old ones.
 */
async function reanalyseWithContext(jobId) {
  const urls = state.reanalyse?.urls || [];
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/context`, {
      method: 'PATCH', body: JSON.stringify({ context_urls: urls }),
    });
  } catch (err) {
    say(`${t('reanalyse.title')}: ${err.message || err}`);
    return;
  }
  state.reanalyse = null;
  ask('Clear this job\'s previous results and analyse the match again.', { showJobs: true });
}


async function registerFromStorage() {
  const u = state.upload;
  const uri = (u.gcsUri || '').trim();
  if (!uri) return;

  u.status = 'uploading';
  u.stage = 'Checking the location';
  render();

  try {
    const job = await api('/api/jobs/from-source', {
      method: 'POST',
      body: JSON.stringify({
        gcs_uri: uri,
        title: u.title.trim() || uri.split('/').pop().replace(/\.[^.]+$/, '') || uri,
        sport: u.sport,
        metadata_language: getSettings().metadataLanguage,
          title_source: state.upload.title.trim() ? 'editor' : 'derived',
      context_urls: contextUrlList(),
      }),
    });

    u.gcsUri = '';
    u.title = '';
    // Idle before the turn, for the same reason as the upload path.
    u.status = 'idle';
    u.stage = 'Handed to the agent';
    selectJob(job.job_id);
    playbackUrl = null;
    if (state.sessionKey) {
      updateSession(state.sessionKey, { scope: { kind: 'games', jobIds: [job.job_id] }, jobId: job.job_id, title: job.title || uri });
      state.sessions = listSessions();
      state.scope = { kind: 'games', jobIds: [job.job_id] };
    }
    render();

    await ask('Analyse this match and find its key moments.', {
      showJobs: true,
      showActions: true,
      actions: [t('action.processing'), t('action.bestMoments')],
    });
  } catch (err) {
    say(`That location could not be used: ${err.message}`);
  }
  u.status = 'idle';
  render();
}

async function registerFromHls() {
  const u = state.upload;
  const url = (u.hlsUrl || '').trim();
  if (!url) return;

  u.status = 'uploading';
  u.stage = 'Registering the stream';
  render();

  try {
    const job = await api('/api/jobs/from-hls', {
      method: 'POST',
      body: JSON.stringify({
        hls_url: url,
        title: u.title.trim() || url.split('/').pop().split('?')[0].replace(/\.[^.]+$/, '') || url,
        sport: u.sport,
        metadata_language: getSettings().metadataLanguage,
          title_source: state.upload.title.trim() ? 'editor' : 'derived',
        context_urls: contextUrlList(),
      }),
    });

    u.hlsUrl = '';
    u.title = '';
    // Idle before the turn, for the same reason as the upload path.
    u.status = 'idle';
    u.stage = 'Handed to the agent';
    selectJob(job.job_id);
    playbackUrl = null;
    if (state.sessionKey) {
      updateSession(state.sessionKey, { scope: { kind: 'games', jobIds: [job.job_id] }, jobId: job.job_id, title: job.title || url });
      state.sessions = listSessions();
      state.scope = { kind: 'games', jobIds: [job.job_id] };
    }
    render();

    await ask('Analyse this match and find its key moments.', {
      showJobs: true,
      showActions: true,
      actions: [t('action.processing'), t('action.bestMoments')],
    });
  } catch (err) {
    u.status = 'idle';
    say(`That stream could not be used: ${err.message}`);
  }
  render();
}

/**
 * Schedule a live event. Nothing is asked of the agent here: the job is a
 * document with a window, and the scheduler's tick finds it when its start is
 * five minutes away. The jobs card is attached so the "scheduled" row is on
 * screen from the moment it exists.
 */
async function scheduleLiveEvent() {
  const u = state.upload;
  const l = u.live;
  const err = validateLiveEvent({ hlsUrl: l.hlsUrl, start: l.start, end: l.end });
  if (err) { say(t(err)); return; }

  u.status = 'scheduling';
  render();
  try {
    const startIso = new Date(l.start).toISOString();
    const job = await api('/api/jobs/live', {
      method: 'POST',
      body: JSON.stringify({
        hls_url: l.hlsUrl.trim(),
        title: l.title.trim() || l.hlsUrl.trim().split('/').pop().split('?')[0] || 'Live event',
        sport: u.sport,
        event_start: startIso,
        event_end: new Date(l.end).toISOString(),
        metadata_language: getSettings().metadataLanguage,
          title_source: l.title.trim() ? 'editor' : 'derived',
        stall_minutes: getSettings().liveStallMinutes,
        context_urls: contextUrlList(),
      }),
    });
    u.live = { title: '', hlsUrl: '', start: '', end: '' };
    u.status = 'idle';
    selectJob(job.job_id);
    if (state.sessionKey) {
      updateSession(state.sessionKey, { scope: { kind: 'games', jobIds: [job.job_id] }, jobId: job.job_id, title: job.title || 'Live event' });
      state.scope = { kind: 'games', jobIds: [job.job_id] };
      state.sessions = listSessions();
    }
    const when = new Date(startIso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    say(t('live.scheduledMsg').replace('{start}', when), { showJobs: true });
  } catch (err) {
    u.status = 'idle';
    say(`The live event could not be scheduled: ${err.message}`);
  }
  render();
}


async function resumeUpload(jobId) {
  const pending = state.pendingUploads.find((p) => p.job_id === jobId);
  if (!pending) return;
  try {
    await registerAndAnalyse(pending);
  } catch (err) {
    state.upload.status = 'idle';
    say(`That upload could not be picked up: ${err.message}`);
    render();
  }
}


async function startUpload() {
  const u = state.upload;
  if (!u.file) return;

  u.status = 'uploading';
  u.pct = 0;
  u.stage = 'Uploading to Cloud Storage';
  render();

  try {
    const ticket = await api('/api/jobs/upload-url', {
      method: 'POST',
      body: JSON.stringify({
        filename: u.file.name,
        content_type: u.file.type || 'video/mp4',
        size_bytes: u.file.size,
      }),
    });

    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', ticket.upload_url, true);
      xhr.setRequestHeader('Content-Type', u.file.type || 'video/mp4');
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        u.pct = (e.loaded / e.total) * 100;
        u.stage = `Uploading — ${bytes(e.loaded)} of ${bytes(e.total)}`;
        render();
      };
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
        ? resolve() : reject(new Error(`Upload failed: ${xhr.status}`)));
      xhr.onerror = () => reject(new Error('Upload failed.'));
      xhr.send(u.file);
    });

    await registerAndAnalyse({
      job_id: ticket.job_id,
      filename: u.file.name,
      size_bytes: u.file.size,
      content_type: u.file.type || '',
    });
  } catch (err) {
    u.status = 'idle';
    u.stage = '';
    render();
    say(`The upload did not complete: ${err.message}`);
  }
}

/* ──────────────────────────────────────────────────── interactions ── */

document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target.matches('[data-ctx-input]')) {
    event.preventDefault();
    addContextUrl();
  }
});

document.addEventListener('keydown', (event) => {
  if (!state.renaming || !event.target.matches?.('[data-rename-input]')) return;
  if (event.key === 'Enter') {
    event.preventDefault();
    saveRename(state.renaming.jobId);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    state.renaming = null;
    render();
  }
});


document.addEventListener('click', (event) => {
  const hit = event.target.closest('[data-ask],[data-play],[data-retry],'
    + '[data-sport],[data-close-player],[data-prepare-playback],'
    + '[data-reanalyse],[data-cancel-job],[data-delete-job],[data-session],'
    + '[data-delete-session],[data-details],[data-remove-clip],[data-game-details],'
    + '[data-open-game],[data-page],[data-sort],[data-show-all],[data-register-gcs],'
    + '[data-ctx-remove],[data-ctx-add],[data-reanalyse-go],[data-reanalyse-cancel],'
    + '[data-search-mode],[data-search-sport],[data-search-game],[data-search-run],[data-search-open],'
    + '[data-register-hls],[data-schedule-live],'
    + '[data-opener],[data-opener-back],[data-ingest-src],'
    + '[data-rename],[data-rename-save],[data-rename-cancel],[data-ride-sort],'
    + '[data-scope-pick],[data-scope-sport],[data-scope-disc],[data-scope-game],'
    + '[data-scope-done],[data-scope-back],[data-scope-change],'
    + '[data-pc],[data-player-range],[data-watch-ride],'
    + '[data-detail-act],[data-trim],[data-trim-reset],[data-publish-to],'
    + '[data-youtube-act],'
    + '[data-ride-tab],[data-type-menu],[data-type-pick]');

  // An open moment-type menu closes on any click outside its own filter. Its
  // toggle used to be the only way out, and a menu only its own button can
  // close is one people get stuck in. Read before any handler re-renders: a
  // rebuilt transcript detaches the clicked element, and a detached element is
  // outside everything.
  const menuHere = event.target.closest('.ride-filter')
    ?.querySelector('[data-type-menu]')?.dataset.typeMenu;
  const closedMenu = closeTypeMenus(menuHere);
  if (!hit) {
    if (closedMenu) render();
    return;
  }
  // Most handlers render; the few that do not still must not leave the menu
  // drawn open after its state has closed.
  if (closedMenu) queueMicrotask(render);

  if (hit.dataset.pc) { playerControl(hit.dataset.pc); return; }
  if (hit.dataset.playerRange) { setPlayerRange(hit.dataset.playerRange); return; }
  if (hit.dataset.watchRide) { openRide(hit.dataset.watchRide); return; }

  if (hit.dataset.detailAct) {
    if (hit.dataset.detailAct === 'download') downloadMoment();
    // A second press closes the panel again: the button reads as a toggle
    // because it is pressed, and a toggle that only opens is a trap.
    else if (state.share?.mode === 'publish') { state.share = null; renderDetailsBody(); }
    else openPublish();
    return;
  }
  if (hit.dataset.trim) {
    const [edge, by] = hit.dataset.trim.split(':');
    const p = state.playing;
    // Trimming re-aims the video that is already playing: the preview is the
    // cut itself rather than a description of one.
    Object.assign(p, trim(p, edge, Number(by), { moment: p.moment, duration: jobDuration() }),
                  { full: false, free: false });
    renderDetailsBody();
    syncPlayer();
    return;
  }
  if (hit.dataset.trimReset !== undefined) {
    const p = state.playing;
    Object.assign(p, playRange(p.moment.startSec, p.moment.endSec, { duration: jobDuration() }),
                  { full: false, free: false });
    renderDetailsBody();
    syncPlayer();
    return;
  }
  if (hit.dataset.publishTo) { publishToYouTube(); return; }
  if (hit.dataset.youtubeAct) { onYouTubeAction(hit.dataset.youtubeAct); return; }

  if (hit.dataset.ask) {
    const q = hit.dataset.ask;
    if (/ingest|upload/i.test(q)) {
      // The upload panel is an affordance, not an answer. Attaching it up front
      // means it appears with the agent's first token rather than after the
      // turn ends — and a turn that starts a pipeline does not end for an hour.
      ask(q, { showIngest: true });
    } else {
      ask(q);
    }
    return;
  }
  if (hit.dataset.play) { openDetails(hit.dataset.play); return; }
  if (hit.dataset.closePlayer) { state.playing = null; destroyPlayer(); render(); return; }
  if (hit.dataset.details) { openDetails(hit.dataset.details); return; }
  if (hit.dataset.gameDetails) { openGameDetails(state.game); return; }
  if (hit.dataset.openGame) {
    openGameDetails(state.games.find((g) => (g.jobId || g.id) === hit.dataset.openGame));
    return;
  }
  if (hit.dataset.registerGcs) { registerFromStorage(); return; }
  if ('scopePick' in hit.dataset || 'scopeSport' in hit.dataset || 'scopeDisc' in hit.dataset
      || 'scopeGame' in hit.dataset || 'scopeDone' in hit.dataset || 'scopeBack' in hit.dataset
      || 'scopeChange' in hit.dataset) { onScopeClick(hit); return; }
  if (hit.dataset.registerHls) { registerFromHls(); return; }
  if (hit.dataset.scheduleLive) { scheduleLiveEvent(); return; }
  if (hit.dataset.ingestSrc) { state.upload.src = hit.dataset.ingestSrc; render(); return; }
  if (hit.dataset.opener) { onOpenerClick(hit); return; }
  if (hit.dataset.openerBack) { onOpenerBack(hit); return; }
  if (hit.dataset.rename) {
    const job = state.jobs.find((j) => j.id === hit.dataset.rename);
    const game = state.games.find((g) => (g.jobId || g.id) === hit.dataset.rename);
    const text = job?.title || game?.title || '';
    state.renaming = { jobId: hit.dataset.rename, value: text, at: text.length };
    render();
    return;
  }
  if (hit.dataset.renameSave) { saveRename(hit.dataset.renameSave); return; }
  if (hit.dataset.renameCancel) { state.renaming = null; render(); return; }
  if (hit.dataset.showAll) {
    const msg = state.msgs[Number(hit.dataset.showAll)];
    if (msg) { msg.showAll = true; msg.page = 0; }
    render();
    return;
  }
  if (hit.dataset.rideTab) {
    const [index, order] = hit.dataset.rideTab.split(':');
    const msg = state.msgs[Number(index)];
    // Back to the first page: page two of this ride's moments is not page two
    // of the next one's, and a ride with fewer moments would open on nothing.
    if (msg) { msg.ride = order; msg.page = 0; }
    render();
    return;
  }
  if (hit.dataset.typeMenu) {
    const msg = state.msgs[Number(hit.dataset.typeMenu)];
    if (msg) msg.typesOpen = !msg.typesOpen;
    render();
    return;
  }
  if (hit.dataset.typePick) {
    const [index, key] = hit.dataset.typePick.split(':');
    const msg = state.msgs[Number(index)];
    if (msg) {
      const picked = msg.types || [];
      // The blank key is "the whole ride" — the resting state, so it clears
      // rather than being a type of its own. The menu stays open either way:
      // choosing several is one question, and closing after each would make it
      // three round trips.
      msg.types = !key ? []
        : picked.includes(key) ? picked.filter((k) => k !== key) : [...picked, key];
      // Clear is the way back to the whole ride, so it is also done with the
      // menu. With nothing chosen it used to change nothing at all and look
      // broken.
      if (!key) msg.typesOpen = false;
      // The rail is narrowed by the choice, so the open ride may no longer be
      // on it; ridesCard falls back to the first, and the page has to follow.
      msg.page = 0;
    }
    render();
    return;
  }
  if (hit.dataset.rideSort) {
    const [index, key] = hit.dataset.rideSort.split(':');
    const msg = state.msgs[Number(index)];
    if (msg) { msg.rideSort = key; msg.page = 0; }
    render();
    return;
  }
  if (hit.dataset.sort) {
    const [index, key] = hit.dataset.sort.split(':');
    const msg = state.msgs[Number(index)];
    // Back to the first page: page four of a score-ranked list is not page four
    // of the same moments in match order.
    // The pane follows the board again: the board's sort is the answer to "how
    // should this be ordered", and a pane still holding the previous one would
    // make the new choice look like it had done nothing.
    if (msg) { msg.sort = key; msg.rideSort = null; msg.page = 0; }
    render();
    return;
  }
  if (hit.dataset.page) {
    const [index, page] = hit.dataset.page.split(':').map(Number);
    // The page lives on the message, so scrolling back to an earlier answer
    // finds it where it was left rather than reset to the first page.
    if (state.msgs[index]) state.msgs[index].page = page;
    render();
    return;
  }
  if (hit.dataset.deleteSession) { deleteSession(hit.dataset.deleteSession); return; }
  if (hit.dataset.session) {
    if (hit.dataset.session !== state.sessionKey) openSession(hit.dataset.session);
    return;
  }
  if ('searchMode' in hit.dataset || 'searchSport' in hit.dataset || 'searchGame' in hit.dataset) {
    const raw = hit.dataset.searchMode ?? hit.dataset.searchSport ?? hit.dataset.searchGame;
    const sep = raw.indexOf(':');
    const msg = state.msgs[Number(raw.slice(0, sep))];
    const value = raw.slice(sep + 1);
    if (msg) {
      if ('searchMode' in hit.dataset) msg.searchMode = value;
      else if ('searchSport' in hit.dataset) { msg.searchSport = value; msg.searchJobs = []; }
      else {
        const list = msg.searchJobs || [];
        msg.searchJobs = list.includes(value) ? list.filter((x) => x !== value) : [...list, value];
      }
      msg.searchResults = null;
    }
    render();
    return;
  }
  if (hit.dataset.searchRun) { runSearch(Number(hit.dataset.searchRun)); return; }
  if (hit.dataset.searchOpen) {
    const [i, k] = hit.dataset.searchOpen.split(':').map(Number);
    openSearchResult(i, k);
    return;
  }
  if (hit.dataset.reanalyse) {
    // Not straight away: show what the last grounding was told and read, and
    // let the links be fixed first. The analysis runs from the panel.
    const jobId = hit.dataset.reanalyse;
    const job = state.jobs.find((x) => x.id === jobId) || {};
    selectJob(jobId);
    state.reanalyse = { jobId, urls: [...(job.contextUrls || [])] };
    render();
    return;
  }
  if (hit.dataset.ctxRemove !== undefined && state.reanalyse) {
    state.reanalyse.urls.splice(Number(hit.dataset.ctxRemove), 1);
    render();
    return;
  }
  if (hit.dataset.ctxAdd && state.reanalyse) {
    addContextUrl();
    return;
  }
  if (hit.dataset.reanalyseCancel) {
    state.reanalyse = null;
    render();
    return;
  }
  if (hit.dataset.reanalyseGo) {
    reanalyseWithContext(hit.dataset.reanalyseGo);
    return;
  }
  if (hit.dataset.cancelJob) {
    selectJob(hit.dataset.cancelJob);
    ask('Cancel the analysis running on this job.', { showJobs: true });
    return;
  }
  if (hit.dataset.deleteJob) {
    // Deleting takes the uploaded match with it, so the confirmation names what
    // goes rather than asking a generic "are you sure?".
    const name = hit.dataset.title || 'this job';
    if (!window.confirm(`${t('jobs.delete')} "${name}"?\n\n${t('jobs.deleteConfirm')}`)) return;
    selectJob(hit.dataset.deleteJob);
    ask('Delete this job, its video and everything found in it.');
    return;
  }
  if (hit.dataset.preparePlayback) {
    // The button names the match. "This match" left the agent to work out
    // which one from the conversation, and when it could not it answered
    // without calling anything — the player kept saying the match was not
    // packaged and nothing had been asked to package it.
    const jobId = hit.dataset.preparePlayback;
    selectJob(jobId);
    ask(`Prepare playback for job ${jobId}. The analysis is done; it just needs packaging.`,
      { showJobs: true });
    return;
  }
  if (hit.dataset.retry) {
    selectJob(hit.dataset.retry);
    ask('The run on this job stopped without finishing. Start the analysis again.',
      { showJobs: true });
    return;
  }
  if (hit.dataset.sport) { state.upload.sport = hit.dataset.sport; render(); }
});

// The transcript re-renders on every Firestore write, which rebuilds the field
// underneath whoever is typing in it. Holding the value in state and writing it
// back is what keeps a pasted path from vanishing mid-analysis.
document.addEventListener('input', (event) => {
  const el = event.target;
  if (!el.matches) return;
  const u = state.upload;
  // The publish panel's fields are held rather than re-rendered: the panel is
  // redrawn whenever the trim moves, and redrawing it under a caret would take
  // the caret with it.
  if (el.matches('[data-share-field]')) {
    if (state.share) state.share[el.dataset.shareField] = el.value;
    return;
  }
  if (el.matches('[data-youtube-field]')) {
    state.youtubeForm = { ...(state.youtubeForm || {}), [el.dataset.youtubeField]: el.value };
    return;
  }
  // Only re-render when a button's enabled state or a validation message
  // actually changes; doing it on every keystroke would move the caret to
  // the end of the field.
  if (el.matches('[data-gcs-input]')) {
    const wasEmpty = !u.gcsUri;
    u.gcsUri = el.value;
    if (wasEmpty !== !u.gcsUri) render();
  } else if (el.matches('[data-hls-input]')) {
    const wasEmpty = !u.hlsUrl.trim();
    u.hlsUrl = el.value;
    if (wasEmpty !== !u.hlsUrl.trim()) render();
  } else if (el.matches('[data-scope-query]')) {
    const msg = state.msgs[Number(el.dataset.scopeQuery)];
    if (!msg) return;
    msg.scopeQuery = el.value;
    // The list under the box follows the typing, which means re-rendering;
    // the caret is put back where it was so typing is not interrupted.
    const at = el.selectionStart;
    render();
    const again = document.querySelector('[data-scope-query]');
    if (again) { again.focus(); again.setSelectionRange(at, at); }
  } else if (el.matches('[data-rename-input]')) {
    if (!state.renaming) return;
    const wasEmpty = !state.renaming.value.trim();
    state.renaming.value = el.value;
    state.renaming.at = el.selectionStart;
    // Only when Save changes between enabled and not: re-rendering on every
    // keystroke would move the caret to the end of the field.
    if (wasEmpty !== !state.renaming.value.trim()) render();
  } else if (el.matches('[data-ingest-title]')) {
    u.title = el.value;
  } else if (el.matches('[data-live-title]')) {
    u.live.title = el.value;
  } else if (el.matches('[data-live-hls],[data-live-start],[data-live-end]')) {
    const before = validateLiveEvent(u.live);
    if (el.matches('[data-live-hls]')) u.live.hlsUrl = el.value;
    if (el.matches('[data-live-start]')) u.live.start = el.value;
    if (el.matches('[data-live-end]')) u.live.end = el.value;
    if (validateLiveEvent(u.live) !== before) render();
  }
});


document.addEventListener('change', (event) => {
  if (event.target.matches?.('[data-share-field]')) {
    if (state.share) state.share[event.target.dataset.shareField] = event.target.value;
    return;
  }
  if (event.target.id !== 'file-input') return;
  const f = event.target.files?.[0];
  if (!f) return;
  state.upload.file = f;
  state.upload.name = f.name;
  state.upload.size = bytes(f.size);
  state.upload.status = 'idle';
  render();
});

document.addEventListener('click', (event) => {
  if (event.target.id === 'start-analysis') { startUpload(); return; }
  const resume = event.target.closest('[data-resume]');
  if (resume) resumeUpload(resume.dataset.resume);
});

$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('draft');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  ask(text);
});

// The header chrome gets its own listeners rather than going through the
// delegated handler. They are fixed elements that exist for the life of the
// page, so delegation buys nothing, and it put them behind a selector and a
// chain of early returns that had already broken them once.
$('account-btn').addEventListener('click', (event) => {
  event.stopPropagation();
  toggleAccountMenu();
});

// Anywhere else, and Escape. A menu that only closes by clicking its own
// button is one people leave open.
document.addEventListener('click', (event) => {
  if (!event.target.closest('.sessions-footer')) toggleAccountMenu(false);
});

$('open-settings')?.addEventListener('click', openSettings);
$('close-settings')?.addEventListener('click', closeSettings);
$('close-details')?.addEventListener('click', closeDetails);
// Clicking the backdrop closes; clicking the card must not.
$('details')?.addEventListener('click', (event) => {
  if (event.target.id === 'details') closeDetails();
});
$('sign-out')?.addEventListener('click', signOutNow);
$('account-menu').addEventListener('click', () => toggleAccountMenu(false));

$('new-session').addEventListener('click', startSession);

state.msgs = [];
render();
