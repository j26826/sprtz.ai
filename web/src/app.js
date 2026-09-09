/**
 * Arenos — chat-first sports video agent.
 *
 * Originally implemented `SPRTZ AI Chat.dc.html` on the Modernist design
 * system; re-skinned onto the Arenos design system (brand standards v1.0) with
 * the same structure. This is the interface driven by the real system:
 *
 *   - the transcript and its inline cards render from Firestore, which the
 *     agents write through the catalog MCP server, so the moment list and the
 *     reel update live while an analysis runs;
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
import {
  filterAsked, gameNamedIn as namedGame, selectGames, selectMoments,
} from './search.js';
import {
  METADATA_LANGUAGES, applyTheme, getSettings, loadSettings, saveSettings, themeOptions,
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
  games: [],           // every match with a game record, for the games list
  sessions: [],
  sessionKey: null,    // the open session, which may not have a job yet
  jobId: null,
  job: null,
  moments: [],
  clips: [],
  events: [],
  thinking: false,
  sessionId: null,
  sports: ['handball'],
  platforms: { tiktok: true, instagram: true, youtube: false },
  playing: null,          // { momentId, start, end }
  upload: { file: null, sport: 'handball', status: 'idle', pct: 0, name: '', size: '', gcsUri: '' },
  pendingUploads: [],     // uploaded to GCS but never registered as a job
  thumbs: { urls: {}, asked: new Set() },  // momentId -> signed URL for its still
  details: null,          // the moment whose popup is open, and playing inside it
  unsubscribe: [],
};

const PLATFORM_SPEC = {
  tiktok: { name: 'TikTok', spec: '9:16 · captions burned in' },
  instagram: { name: 'Instagram Reels', spec: '9:16 · cover frame at 00:03' },
  youtube: { name: 'YouTube Shorts', spec: '9:16 · title from caption' },
};

// Built per render rather than once, so switching language re-reads it.
const greeting = () => ({
  who: 'agent',
  text: t('greeting'),
  showActions: true,
  actions: [t('action.ingest'), t('action.processing'), t('action.bestMoments')],
});

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
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
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
 * because they are built from templates that call t() as they run. The greeting
 * is rebuilt only when it is the only thing on screen — replacing it mid-
 * conversation would rewrite something the editor has already read.
 */
function applyLocale() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  if (state.msgs.length === 1 && state.msgs[0].who === 'agent') {
    state.msgs = [greeting()];
  }
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
}

function openSettings() {
  renderSettings();
  $('settings').classList.remove('hidden');
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

  $('set-theme')?.addEventListener('change', (e) => {
    saveSettings({ theme: e.target.value });
    applyTheme(e.target.value);
  });

  // Clicking the backdrop closes; clicking the card must not.
  $('settings')?.addEventListener('click', (e) => {
    if (e.target.id === 'settings') closeSettings();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSettings(); closeDetails(); toggleAccountMenu(false); }
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
      // A failure here is not fatal on its own — api() still force-refreshes on
      // a 401 — so it is logged rather than shown.
      console.warn('token refresh failed', err);
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
    state.msgs = [];
    state.jobs = [];
    state.jobId = null;
    return;
  }

  state.msgs = [greeting()];
  render();
  watchJobs();
  watchGames();
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
      // every card that reads moments, clips or the game record is empty
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
 * The moments, clips, events and game record are all read through listeners
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
  selectJob(state.jobs[0].id);
}


function selectJob(jobId) {
  state.unsubscribe.forEach((fn) => fn());
  state.unsubscribe = [];
  state.jobId = jobId;
  state.moments = [];
  state.clips = [];
  state.game = null;
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
    render();
  }, () => { state.game = null; }));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'moments'), orderBy('startSec', 'asc'), limit(500)),
    (snap) => { state.moments = snap.docs.map((d) => ({ id: d.id, ...d.data() })); render(); },
  ));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'clips'), orderBy('score', 'desc'), limit(200)),
    (snap) => { state.clips = snap.docs.map((d) => ({ id: d.id, ...d.data() })); render(); },
  ));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'events'), orderBy('ts', 'desc'), limit(80)),
    (snap) => { state.events = snap.docs.map((d) => ({ id: d.id, ...d.data() })); render(); },
  ));
}

/* ────────────────────────────────────────────────────── transcript ── */

function push(msg) {
  state.msgs.push(msg);
  render();
  scrollDown();
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



/* A star, for a moment that is already in the reel. */
const STAR = '<svg width="8" height="8" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
  + '<path d="M12 2l2.9 6.6L22 9.6l-5 4.9 1.2 7L12 18.1 5.8 21.5 7 14.5l-5-4.9 7.1-1z"/></svg>';


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


function momentsHead(view, index, inReel) {
  const sort = view.sort === 'time' ? 'time' : 'score';
  const stars = inReel
    ? `<span class="list-count" title="${esc(t('moment.inReel'))}">${STAR} ${inReel}</span>`
    : '';
  return listHead(view, index, t('moments.title'), stars + ['score', 'time'].map((key) => `
    <button class="link-btn" data-sort="${index}:${key}"
            aria-pressed="${key === sort}">${esc(t(`moments.sort.${key}`))}</button>`).join(''));
}


/**
 * One moment as a tile.
 *
 * The summary is still the headline, as it is in the row this replaces — the
 * line an editor scans by is who did what, not what the taxonomy calls it — so
 * the class drops into the meta line underneath. It is clamped rather than
 * truncated at a character count, because where a sentence can be cut without
 * losing its subject depends on the sentence.
 *
 * Confidence goes in that meta line rather than on the picture. The design
 * puts a badge in the corner of the frame, but a still from a match is not a
 * flat colour: text laid straight onto it is legible against a dark crowd and
 * gone against a bright floor. The star is the exception, and only because a
 * filled amber disc carries its own contrast wherever it lands.
 */
function momentTile(m) {
  const clip = state.clips.find((c) => c.momentId === m.momentId);
  const meta = [
    // H.No and rider first: on a competition day that is what a tile is
    // scanned for. A schedule-inferred name is marked with a tilde.
    m.rider ? `${m.startNumber ? `#${m.startNumber} ` : ''}${m.identitySource === 'schedule' ? '~' : ''}${m.rider}` : '',
    m.label || m.momentType,
    `${Math.round(m.endSec - m.startSec)}s`,
    m.confidence == null ? '' : `${Math.round(m.confidence * 100)}%`,
  ].filter(Boolean).join(' \u00b7 ');

  return `
    <div class="tile">
      <button class="thumb" data-play="${esc(m.momentId)}" title="${esc(t('moment.play'))}"
              ${m.thumbUri && !state.thumbs.urls[m.momentId] ? `data-thumb="${esc(m.momentId)}"` : ''}>
        ${state.thumbs.urls[m.momentId]
          ? `<img src="${esc(state.thumbs.urls[m.momentId])}" alt="" loading="lazy">`
          : '<span class="thumb-stripes"></span>'}
        ${clip ? `<span class="tile-star" title="${esc(t('moment.inReel'))}">${STAR}</span>` : ''}
        <span class="thumb-clock">${clock(m.startSec)}</span>
      </button>
      <div class="tile-name">${esc(m.summary || m.label || m.momentType)}</div>
      <div class="tile-meta">${esc(meta)}</div>
      ${m.rerankReason ? `<div class="tile-why">${esc(m.rerankReason)}</div>` : ''}
      <div class="tile-actions">
        <button class="link-btn" data-details="${esc(m.momentId)}">${esc(t('moment.details'))}</button>
        ${clip
          ? `<button class="btn-outline" data-remove-clip="${esc(m.momentId)}">${esc(t('moment.remove'))}</button>`
          : `<button class="btn-outline" data-add="${esc(m.momentId)}">${esc(t('moment.add'))}</button>`}
      </div>
    </div>`;
}


function momentsCard(msg, index) {
  const found = momentsFor(msg);
  if (!found.list.length) return emptyCard(t('moments.none'));

  const view = pageOf(found.list, msg.page);
  const inReel = found.list
    .filter((m) => state.clips.some((c) => c.momentId === m.momentId)).length;

  // The row scrolls sideways within a page rather than instead of one. A match
  // yields a couple of hundred moments, and one scroller holding all of them is
  // the truncation problem in the other axis: everything present, nothing
  // findable, and no way to tell how much is left.
  return `
    <div class="list">
      ${momentsHead({ ...found, sort: msg.sort }, index, inReel)}
      <div class="tile-row">${view.slice.map(momentTile).join('')}</div>
      ${pagerRow(view, index)}
    </div>`;
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
function playerMarkup(m) {
  return `<div class="player-slot" data-slot="${esc(m.momentId)}"></div>`;
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
  ['moment.start', (m) => clock(m.startSec)],
  ['moment.end', (m) => clock(m.endSec)],
  ['moment.peak', (m) => clock(m.peakSec)],
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


function openDetails(momentId, range = null) {
  const m = momentById(momentId);
  if (!m) return;

  const rows = DETAIL_ROWS
    .map(([key, read]) => [t(key), read(m)])
    .filter(([, value]) => value !== '' && value != null);

  $('details-title').textContent = m.summary || m.label || t('moment.details');
  $('details-body').innerHTML = rows.map(([label, value]) => `
    <div class="detail-key">${esc(label)}</div>
    <div class="detail-value">${esc(String(value))}</div>`).join('');

  // The moment plays beside its record rather than instead of it. Opening the
  // details of a play is the point at which someone wants to see it, and the
  // facts are what they are checking it against — reading "double save" and
  // watching the save are the same act here.
  $('details-player').innerHTML = playerMarkup(m);
  state.details = momentId;
  state.playing = {
    momentId,
    start: range?.start ?? m.startSec,
    end: range?.end ?? m.endSec,
  };
  showDetailsModal(true);
  mountPlayer();
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
  if (state.details) {
    // The player lived in the popup, so closing the popup stops it. Leaving it
    // running would be audio from a dialog that is no longer on screen.
    state.details = null;
    state.playing = null;
    destroyPlayer();
    render();
  }
}

function ingestCard() {
  const u = state.upload;
  // Offer the most recent one only. A list of near-identical filenames is a
  // worse prompt than "the one you left behind", and the rest stay reachable.
  const pending = state.pendingUploads[0];
  const sources = ['Upload', 'Dropbox', 'Drive', 'Camera roll'];
  return `
    <div class="panel">
      <div class="source-tabs">
        ${sources.map((s, i) => `
          <button class="source-tab" aria-selected="${i === 0}" ${i === 0 ? '' : 'disabled'}
                  title="${i === 0 ? '' : 'Not connected yet'}">${s}</button>`).join('')}
      </div>
      <div style="padding:16px;border-bottom:1px solid var(--color-neutral-300)">
        <div class="dropzone" id="dropzone">
          <div class="dz-thumb"><span class="thumb-stripes"></span></div>
          <div style="flex:1;min-width:0">
            <div class="dz-name">${esc(u.name || t('ingest.noFile'))}</div>
            <div class="dz-meta">${esc(u.name
              ? `${u.size} · ${u.sport} · via Upload`
              : t('ingest.dropHere'))}</div>
          </div>
          <label class="file-label">${esc(t('ingest.chooseFile'))}
            <input type="file" id="file-input" accept="video/*" style="display:none" />
          </label>
        </div>
        <div class="gcs-row">
          <div class="field-label">${esc(t('ingest.fromStorage'))}</div>
          <div class="gcs-input-row">
            <input class="composer-input" data-gcs-input
                   placeholder="gs://bucket/path/to/video.mp4"
                   value="${esc(u.gcsUri || '')}" />
            <button class="btn-outline" data-register-gcs="1"
                    ${u.gcsUri ? '' : 'disabled'}>${esc(t('ingest.useLocation'))}</button>
          </div>
          <div class="setting-hint">${esc(t('ingest.fromStorageHint'))}</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;align-items:center">
          <div class="field-label" style="margin-right:4px">${esc(t('ingest.sport'))}</div>
          ${state.sports.map((s) => `
            <button class="chip" data-sport="${esc(s)}" aria-pressed="${u.sport === s}"
                    style="text-transform:capitalize">${esc(s)}</button>`).join('')}
        </div>
        <div class="ingest-context">
          <label class="field-label" for="context-urls">${esc(t('ingest.contextUrls'))}</label>
          <div class="setting-hint">${esc(t('ingest.contextUrlsHint'))}</div>
          <textarea class="input ctx-textarea" id="context-urls" rows="3"
                    data-context-urls placeholder="${esc(t('reanalyse.placeholder'))}">${esc(u.contextUrls || '')}</textarea>
        </div>
      </div>
      <div style="padding:14px 16px">
        ${u.status === 'uploading' || u.status === 'analyzing' ? `
          <div>
            <div style="font-size:11.5px;color:var(--color-neutral-800);line-height:1.4">
              ${esc(u.stage || 'Uploading')}
            </div>
            <div class="meter-row">
              <div class="meter"><i style="width:${u.pct}%"></i></div>
              <div class="meter-pct">${Math.round(u.pct)}%</div>
            </div>
          </div>` : ''}
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="btn-solid" id="start-analysis" ${u.file && u.status === 'idle' ? '' : 'disabled'}>
            ${u.status === 'uploading' ? t('ingest.uploading')
               : u.status === 'analyzing' ? t('ingest.analysing') : t('ingest.start')}
          </button>
          ${pending ? `
            <button class="btn-outline" data-resume="${esc(pending.job_id)}"
                    ${u.status === 'idle' ? '' : 'disabled'}
                    title="${esc(pending.filename)} — ${bytes(pending.size_bytes)}">
              ${esc(t('ingest.useLastUpload'))}
            </button>` : ''}
        </div>
        ${pending ? `
          <div class="dz-meta" style="margin-top:8px">
            ${esc(pending.filename)} · ${bytes(pending.size_bytes)}
            ${esc(t('ingest.strandedNote'))}
          </div>` : ''}
      </div>
    </div>`;
}

function reelCard(msg, index) {
  if (!state.clips.length) return emptyCard(t('reel.none'));
  const total = state.clips.reduce((a, c) => a + (c.durationSec || 0), 0);
  const aspect = state.clips[0]?.aspect || '9:16';
  // The bar strip stays whole — it is the shape of the reel, and a page of it
  // would be a different reel. Only the editable rows page.
  const view = pageOf(state.clips, msg.page);
  return `
    <div class="panel">
      <div class="panel-head">
        <div class="panel-head-title">${esc(t('reel.title'))}</div>
        <div class="panel-head-meta">${dur(total)} · ${esc(aspect)}</div>
      </div>
      <div class="reel-bars">
        ${state.clips.map((c, i) => `
          <div class="reel-bar" style="flex-grow:${c.durationSec || 1};
               background:${i % 2 ? 'var(--color-neutral-400)' : 'var(--color-neutral-700)'}"></div>`).join('')}
      </div>
      ${view.slice.map((c, i) => `
        <div class="clip-row">
          <div class="clip-n">${String(view.from + i).padStart(2, '0')}</div>
          <div class="clip-label">${esc(c.title || c.hookText || 'Clip')}</div>
          <div class="stepper">
            <button class="step-btn" data-clip-shorter="${esc(c.clipId)}">&minus;</button>
            <div class="step-val">${(c.durationSec || 0).toFixed(1)}s</div>
            <button class="step-btn" data-clip-longer="${esc(c.clipId)}">+</button>
          </div>
          <button class="link-btn" data-clip-play="${esc(c.clipId)}">${esc(t('reel.play'))}</button>
        </div>`).join('')}
      ${pagerRow(view, index)}
      <div class="panel-actions">
        <button class="btn-solid" data-ask="Generate the video">${esc(t('reel.generate'))}</button>
        <button class="btn-outline" data-ask="Reframe it vertical">${esc(t('reel.reframe'))}</button>
        <button class="btn-outline" data-ask="Prepare it for publishing">${esc(t('reel.publish'))}</button>
      </div>
    </div>`;
}

// A run that dies takes its progress reporting with it, so the job keeps the
// status it had and looks alive for ever. Nothing retries on its own, so the
// only honest reading of a long silence is that it needs starting again.
const STALLED_AFTER_MS = 15 * 60 * 1000;

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
  { key: 'ingest', start: 0, end: 5 },
  { key: 'transcode', start: 5, end: 20 },
  { key: 'analysis', start: 20, end: 80 },
  { key: 'clips', start: 80, end: 95 },
  { key: 'captions', start: 95, end: 100 },
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
  const found = selectGames(state.games, { terms: msg.showAll ? [] : (msg.terms || []) });

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
            <div class="moment-label">${esc(gameHeadline(g))}</div>
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


/** What the analysis looked for and did not find. An absence is a finding. */
function notConfirmedBlock(g) {
  const items = Array.isArray(g.notConfirmed) ? g.notConfirmed : [];
  if (!items.length) return '';
  return `
    <div class="detail-key">${esc(t('game.notConfirmed'))}</div>
    <div class="detail-value">${items.map((n) => `
      <div class="not-confirmed"><b>${esc(n.momentType || '')}</b>${
        (n.notes || []).length ? ` — ${esc((n.notes || []).join(' '))}` : ''}</div>`).join('')}</div>`;
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
  $('details-body').innerHTML = rows.map(([label, value]) => `
    <div class="detail-key">${esc(label)}</div>
    <div class="detail-value">${esc(String(value))}</div>`).join('')
    + ridesTable(g) + notConfirmedBlock(g) + equipe + sources;

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
  state.sessionId = null;          // a new agent session per conversation
  state.msgs = [greeting()];
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
    state.clips = [];
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


function jobsCard(msg, index) {
  if (!state.jobs.length) return emptyCard(t('jobs.none'));
  const view = pageOf(state.jobs, msg.page);
  return `<div class="panel-light">${view.slice.map((j) => {
    const running = ['analyzing', 'transcoding', 'uploaded'].includes(j.status);
    const failed = j.status === 'failed';
    const stalled = running && isStalled(j);
    const tone = failed || stalled ? 'failed' : running ? 'running' : 'idle';
    return `
      <div class="job">
        ${state.reanalyse?.jobId === j.id ? reanalysePanel(j) : ''}
        <div class="job-top">
          <div class="job-name">${esc(j.title || j.source?.originalName || j.id)}</div>
          <div class="job-status" data-tone="${tone}">${
            stalled ? esc(t('jobs.stalled')) : esc(j.status || 'unknown')}</div>
        </div>
        <div class="job-stage">${esc(j.stage || '')}${
          j.media?.segmentCount ? ` · ${j.media.segmentCount} segments` : ''}</div>
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

function publishCard() {
  const posted = state.job?.status === 'ready';
  return `
    <div class="panel">
      ${Object.entries(PLATFORM_SPEC).map(([key, p]) => {
        const on = state.platforms[key];
        return `
          <button class="platform" data-platform="${key}" aria-pressed="${on}">
            <span class="checkmark"></span>
            <span>
              <span class="platform-name" style="display:block">${esc(p.name)}</span>
              <span class="platform-spec" style="display:block">${esc(p.spec)}</span>
            </span>
            <span class="platform-state">${on ? (posted ? 'Packaged' : 'Selected') : 'Off'}</span>
          </button>`;
      }).join('')}
      <div style="padding:12px">
        <div class="field-label" style="margin-bottom:6px">Caption drafted by the agent</div>
        <textarea class="caption-box" id="caption" rows="3">${
          esc(state.clips[0]?.captions?.tiktok || '')}</textarea>
        <div style="display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap">
          <div class="field-label">${esc(t('publish.note'))}</div>
          <div style="flex:1"></div>
          <button class="btn-solid" data-ask="Finalise the job for publishing">
            ${posted ? '✓ Packaged' : 'Prepare package'}
          </button>
        </div>
      </div>
    </div>`;
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
  $('transcript').innerHTML = state.msgs.map((m, i) => {
    const agent = m.who === 'agent';
    const fresh = agent && !animatedMsgs.has(m);
    if (fresh) animatedMsgs.add(m);
    return `
    <div class="msg ${agent ? `msg-agent card${fresh ? ' fade-in' : ''}` : 'msg-user'}">
      <div class="msg-label">${agent ? 'Agent' : 'You'}</div>
      ${cardAnswersIt(m) ? '' : `<div class="msg-text">${esc(m.text)}</div>`}
      ${m.showMoments ? momentsCard(m, i) : ''}
      ${m.showIngest ? ingestCard() : ''}
      ${m.showReel ? reelCard(m, i) : ''}
      ${m.showJobs ? jobsCard(m, i) : ''}
      ${m.showGame ? gameCard() : ''}
      ${m.showGames ? gamesCard(m, i) : ''}
      ${m.showPublish ? publishCard() : ''}
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

  $('suggestions').innerHTML = [
    'Ingest a new game',
    'Show me the best moments',
    "What's still processing?",
  ].map((s) => `<button class="chip" data-ask="${esc(s)}">${esc(s)}</button>`).join('');

  if (state.playing) mountPlayer();
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
  const jobId = state.jobId;
  if (!jobId || !state.user) return;

  const wanted = [...document.querySelectorAll('[data-thumb]')]
    .map((el) => el.dataset.thumb)
    .filter((id) => id && !state.thumbs.asked.has(id))
    .slice(0, 50);
  if (!wanted.length) return;

  wanted.forEach((id) => state.thumbs.asked.add(id));
  try {
    const res = await api(`/api/jobs/${jobId}/thumbnails`, {
      method: 'POST',
      body: JSON.stringify({ moment_ids: wanted }),
    });
    if (state.jobId !== jobId) return;   // the editor opened another match meanwhile
    Object.assign(state.thumbs.urls, res.thumbnails || {});
    render();
  } catch (err) {
    // A still that will not sign is a placeholder, which is what the row showed
    // before any of this existed. It is not worth an error in the transcript.
    console.warn('could not sign moment thumbnails', err);
  }
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

function buildPlayerEl(m) {
  const el = document.createElement('div');
  el.className = 'inline-player';
  el.innerHTML = `
    <video playsinline controls preload="none"></video>
    <div class="player-bar">
      <span>${clock(m.startSec)} → ${clock(m.endSec)}</span>
      <span style="flex:1"></span>
      <button class="link-btn" data-close-player="1">${esc(t('player.close'))}</button>
    </div>`;
  return el;
}

/** Re-parent the live player into this render's slot, building it once. */
async function mountPlayer() {
  const { momentId, start, end } = state.playing;
  // A moment plays in its details popup and nowhere else. The transcript used
  // to hold a slot of its own under the row, which meant two elements with the
  // same data-slot and the wrong one — the one behind the dialog — winning on
  // document order.
  const slot = state.details === momentId
    ? $('details-player').querySelector(`[data-slot="${CSS.escape(momentId)}"]`)
    : null;
  if (!slot) return;

  if (playerFor !== momentId) {
    destroyPlayer();
    // The range being played, not the moment's own: a clip carries its own
    // trim, and the bar under the video is what says where it stops.
    playerEl = buildPlayerEl({ startSec: start, endSec: end });
    playerFor = momentId;
  }

  if (playerEl.parentElement !== slot) slot.appendChild(playerEl);

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
        ${notReady ? `
          <button class="btn-outline" data-prepare-playback="1">
            ${esc(t('player.preparePlayback'))}
          </button>` : ''}
      </div>`);
    return;
  }

  if (hls) { hls.destroy(); hls = null; }
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
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => { video.currentTime = start; video.play().catch(() => {}); });
  } else {
    video.src = url;                       // Safari plays HLS natively
    video.addEventListener('loadedmetadata', () => {
      video.currentTime = start; video.play().catch(() => {});
    }, { once: true });
  }

  // Stop at the out point — this is what replaces a timeline.
  video.addEventListener('timeupdate', () => {
    if (video.currentTime >= end) video.pause();
  });
}

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
      body: JSON.stringify({ message: text, session_id: sessionId, job_id: state.jobId }),
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

  if (card === 'activity') {
    msg.showActivity = true;
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
  } else if (card === 'publish') {
    msg.showPublish = true;
  } else if (card === 'reel') {
    msg.showReel = true;
    msg.showActions = true;
    msg.actions = [t('reel.generate'), t('reel.reframe'), t('reel.publish')];
  } else {
    // Every moment, not a top handful. A list that silently stops at six looks
    // like the analysis found six. The order is chosen here rather than baked
    // in, so the card's own toggle can change it afterwards.
    msg.showMoments = true;
    msg.showActions = true;
    msg.actions = ['Cut all of these', 'Cut a 30-second short'];

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
      title: filename.replace(/\.[^.]+$/, ''),
      sport: u.sport,
      filename,
      size_bytes,
      content_type,
      // Copied onto the job rather than read at analysis time: what a match's
      // descriptions are written in is a property of that match, not of
      // whoever opens it later.
      metadata_language: getSettings().metadataLanguage,
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
      jobId: job_id,
      title: filename.replace(/\.[^.]+$/, ''),
    });
    state.sessions = listSessions();
  }

  u.status = 'analyzing';
  u.stage = 'Handed to the agent';
  selectJob(job_id);
  playbackUrl = null;
  render();

  await ask('Analyse this match and suggest clips.', {
    showJobs: true,
    showActions: true,
    actions: [t('action.processing'), t('action.bestMoments')],
  });
  u.status = 'idle';
  u.file = null;
  u.name = '';
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
        title: uri.split('/').pop().replace(/\.[^.]+$/, '') || uri,
        sport: u.sport,
        metadata_language: getSettings().metadataLanguage,
      context_urls: contextUrlList(),
      }),
    });

    u.gcsUri = '';
    u.status = 'analyzing';
    u.stage = 'Handed to the agent';
    selectJob(job.job_id);
    playbackUrl = null;
    if (state.sessionKey) {
      updateSession(state.sessionKey, { jobId: job.job_id, title: job.title || uri });
      state.sessions = listSessions();
    }
    render();

    await ask('Analyse this match and suggest clips.', {
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

document.addEventListener('click', (event) => {
  const hit = event.target.closest('[data-ask],[data-play],[data-add],[data-platform],'
    + '[data-clip-shorter],[data-clip-longer],[data-clip-play],[data-retry],'
    + '[data-sport],[data-close-player],[data-prepare-playback],'
    + '[data-reanalyse],[data-cancel-job],[data-delete-job],[data-session],'
    + '[data-delete-session],[data-details],[data-remove-clip],[data-game-details],'
    + '[data-open-game],[data-page],[data-sort],[data-show-all],[data-register-gcs],'
    + '[data-ctx-remove],[data-ctx-add],[data-reanalyse-go],[data-reanalyse-cancel]');
  if (!hit) return;

  if (hit.dataset.ask) {
    const q = hit.dataset.ask;
    if (q === 'Cut all of these') {
      ask('Cut all of these into clips.');
    } else if (/ingest|upload/i.test(q)) {
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
  if (hit.dataset.showAll) {
    const msg = state.msgs[Number(hit.dataset.showAll)];
    if (msg) { msg.showAll = true; msg.page = 0; }
    render();
    return;
  }
  if (hit.dataset.sort) {
    const [index, key] = hit.dataset.sort.split(':');
    const msg = state.msgs[Number(index)];
    // Back to the first page: page four of a score-ranked list is not page four
    // of the same moments in match order.
    if (msg) { msg.sort = key; msg.page = 0; }
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
  if (hit.dataset.removeClip) {
    const m = momentById(hit.dataset.removeClip);
    const clip = state.clips.find((c) => c.momentId === hit.dataset.removeClip);
    // Named so the agent removes the clip and leaves the moment: "remove the
    // jump shot" on its own reads as either.
    ask(`Remove the clip "${clip?.title || m?.label || 'this one'}" from the reel. `
      + 'Keep the moment itself.');
    return;
  }
  if (hit.dataset.add) {
    const m = momentById(hit.dataset.add);
    ask(`Add the ${m?.label || 'moment'} at ${clock(m?.startSec || 0)} to the reel.`);
    return;
  }
  if (hit.dataset.clipPlay) {
    // Into the popup as well, with the clip's own trim rather than the
    // moment's. Setting state.playing alone only worked while the moment's row
    // happened to be rendered somewhere to hold the player.
    const c = state.clips.find((x) => x.clipId === hit.dataset.clipPlay);
    if (c) openDetails(c.momentId, { start: c.startSec, end: c.endSec });
    return;
  }
  if (hit.dataset.clipShorter || hit.dataset.clipLonger) {
    const id = hit.dataset.clipShorter || hit.dataset.clipLonger;
    const c = state.clips.find((x) => x.clipId === id);
    const delta = hit.dataset.clipShorter ? -1 : 1;
    ask(`Make the clip "${c?.title || id}" ${Math.abs(delta)} second ${delta < 0 ? 'shorter' : 'longer'}.`);
    return;
  }
  if (hit.dataset.platform) {
    state.platforms[hit.dataset.platform] = !state.platforms[hit.dataset.platform];
    render();
    return;
  }
  if (hit.dataset.deleteSession) { deleteSession(hit.dataset.deleteSession); return; }
  if (hit.dataset.session) {
    if (hit.dataset.session !== state.sessionKey) openSession(hit.dataset.session);
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
    ask('Prepare playback for this match. The analysis is done; it just needs packaging.');
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
  if (!event.target.matches?.('[data-gcs-input]')) return;
  const wasEmpty = !state.upload.gcsUri;
  state.upload.gcsUri = event.target.value;
  // Only re-render when the button's enabled state actually changes; doing it
  // on every keystroke would move the caret to the end of the field.
  if (wasEmpty !== !state.upload.gcsUri) render();
});


document.addEventListener('change', (event) => {
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

state.msgs = [greeting()];
render();
