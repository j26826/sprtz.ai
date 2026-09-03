/**
 * Sportscut — chat-first sports video agent.
 *
 * Implements `SPRTZ AI Chat.dc.html` on the Modernist design system. The design
 * is a prototype driven by fixture data; this is the same interface driven by
 * the real system:
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
import { filterAsked, gameNamedIn as namedGame, selectMoments } from './moments.js';
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
  upload: { file: null, sport: 'handball', status: 'idle', pct: 0, name: '', size: '' },
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
    if (e.key === 'Escape') { closeSettings(); closeDetails(); }
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
function renderSessions() {
  const list = $('sessions-list');
  if (!list) return;

  if (!state.sessions.length) {
    list.innerHTML = `<div class="sessions-empty">${esc(t('sessions.empty'))}</div>`;
    return;
  }

  const jobsById = new Map(state.jobs.map((j) => [j.id, j]));

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

    return `
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
function momentsHead(view, index) {
  const sort = view.sort === 'time' ? 'time' : 'score';
  const asked = [
    ...view.terms,
    ...(view.half ? [t(`moments.half.${view.half}`)] : []),
  ].join(', ');

  let title = `<div class="panel-head-title">${esc(t('moments.sortBy'))}</div>`;
  if (view.narrowed) {
    title = `
      <div class="panel-head-title">
        ${esc(t('moments.filtered'))} ${esc(asked)}
        · ${view.list.length} ${esc(t('pager.of'))} ${view.total}
        <button class="link-btn" data-show-all="${index}">${esc(t('moments.showAll'))}</button>
      </div>`;
  } else if (view.missed) {
    title = `<div class="panel-head-title">${esc(t('moments.noMatch'))} ${esc(asked)}</div>`;
  }

  return `
    <div class="panel-head">
      ${title}
      <div class="panel-head-meta">
        ${['score', 'time'].map((key) => `
          <button class="link-btn" data-sort="${index}:${key}"
                  aria-pressed="${key === sort}">${esc(t(`moments.sort.${key}`))}</button>`).join('')}
      </div>
    </div>`;
}


function momentsCard(msg, index) {
  const found = momentsFor(msg);
  if (!found.list.length) return emptyCard(t('moments.none'));

  const view = pageOf(found.list, msg.page);
  return `<div class="panel-light">${momentsHead({ ...found, sort: msg.sort }, index)}${view.slice.map((m) => {
    const clip = state.clips.find((c) => c.momentId === m.momentId);
    const meta = [
      m.label || m.momentType,
      m.category,
      `${Math.round(m.endSec - m.startSec)}s`,
      (m.highlightScore ?? 0).toFixed(2),
    ].filter(Boolean).join(' · ');
    // The summary is the line an editor scans by — who did what — so it takes
    // the emphasis. The type and category drop into the meta line beneath,
    // where they are still there to filter on but are not the headline.
    const headline = m.summary || m.label || m.momentType;

    return `
      <div class="row">
        <div class="moment-row">
          <button class="thumb" data-play="${esc(m.momentId)}" title="${esc(t('moment.play'))}"
                  ${m.thumbUri && !state.thumbs.urls[m.momentId] ? `data-thumb="${esc(m.momentId)}"` : ''}>
            ${state.thumbs.urls[m.momentId]
              ? `<img src="${esc(state.thumbs.urls[m.momentId])}" alt="" loading="lazy">`
              : '<span class="thumb-stripes"></span>'}
            <span class="thumb-clock">${clock(m.startSec)}</span>
          </button>
          <div style="min-width:0">
            <div class="moment-label">${esc(headline)}</div>
            <div class="moment-meta">${esc(meta)}</div>
            ${m.rerankReason ? `<div class="rerank-why">${esc(m.rerankReason)}</div>` : ''}
          </div>
          <div class="moment-actions">
            <button class="link-btn" data-details="${esc(m.momentId)}">${esc(t('moment.details'))}</button>
            ${clip
              ? `<button class="btn-outline" data-remove-clip="${esc(m.momentId)}">${esc(t('moment.remove'))}</button>`
              : `<button class="btn-outline" data-add="${esc(m.momentId)}">${esc(t('moment.add'))}</button>`}
          </div>
        </div>
      </div>`;
  }).join('')}${pagerRow(view, index)}</div>`;
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
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;align-items:center">
          <div class="field-label" style="margin-right:4px">${esc(t('ingest.sport'))}</div>
          ${state.sports.map((s) => `
            <button class="chip" data-sport="${esc(s)}" aria-pressed="${u.sport === s}"
                    style="text-transform:capitalize">${esc(s)}</button>`).join('')}
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


function gamesCard(msg, index) {
  if (!state.games.length) return emptyCard(t('games.none'));

  // Three at a time when the records are open: ten of them is a dozen rows
  // each, and a page nobody can see the end of is not a page.
  const view = pageOf(state.games, msg.page, msg.expandGames ? 3 : PAGE_SIZE);
  return `
    <div class="panel-light">
      ${view.slice.map((g) => {
        const meta = [
          g.sport,
          g.discipline,
          g.competition || g.groundedCompetition,
          g.finalScore,
          g.mood,
        ].filter(Boolean).join(' · ');
        return `
          <div class="row">
            <div class="game-row">
              <div style="min-width:0">
                <div class="moment-label">${esc(gameHeadline(g))}</div>
                <div class="moment-meta">${esc(meta || t('game.notIdentified'))}</div>
              </div>
              <div class="moment-actions">
                <button class="link-btn" data-open-game="${esc(g.jobId || g.id)}">
                  ${esc(t('moment.details'))}
                </button>
              </div>
            </div>
            ${msg.expandGames ? gameRows(g) : ''}
          </div>`;
      }).join('')}
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


function openGameDetails(game) {
  const g = game || state.game;
  if (!g) return;

  const rows = GAME_DETAIL_ROWS
    .map(([key, read]) => [t(key), read(g)])
    .filter(([, value]) => value !== '' && value != null);

  // The sources belong in the popup rather than the card: they qualify the
  // grounded rows, and are meaningless next to a row nobody is looking at.
  const sources = (g.grounded && g.groundingSources?.length)
    ? `<div class="detail-key">${esc(t('game.groundedBy'))}</div>
       <div class="detail-value">${g.groundingSources.slice(0, 5).map((src) => `
         <a href="${esc(src.uri)}" target="_blank" rel="noopener noreferrer"
            class="link-btn">${esc(src.title || src.uri)}</a>`).join('<br />')}</div>`
    : '';

  $('details-title').textContent = gameHeadline(g);
  $('details-body').innerHTML = rows.map(([label, value]) => `
    <div class="detail-key">${esc(label)}</div>
    <div class="detail-value">${esc(String(value))}</div>`).join('') + sources;

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


function render() {
  renderSessions();
  $('transcript').innerHTML = state.msgs.map((m, i) => `
    <div class="msg ${m.who === 'agent' ? 'msg-agent' : ''}">
      <div class="msg-label">${m.who === 'agent' ? 'Agent' : 'You'}</div>
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
    </div>`).join('')
    + (state.thinking ? `
      <div class="msg msg-agent">
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

document.addEventListener('click', (event) => {
  const hit = event.target.closest('[data-ask],[data-play],[data-add],[data-platform],'
    + '[data-clip-shorter],[data-clip-longer],[data-clip-play],[data-retry],'
    + '[data-sport],[data-close-player],[data-prepare-playback],'
    + '[data-reanalyse],[data-cancel-job],[data-delete-job],[data-session],'
    + '[data-delete-session],[data-details],[data-remove-clip],[data-game-details],'
    + '[data-open-game],[data-page],[data-sort],[data-show-all]');
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
    selectJob(hit.dataset.reanalyse);
    ask('Clear this job\'s previous results and analyse the match again.',
      { showJobs: true });
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
$('open-settings')?.addEventListener('click', openSettings);
$('close-settings')?.addEventListener('click', closeSettings);
$('close-details')?.addEventListener('click', closeDetails);
// Clicking the backdrop closes; clicking the card must not.
$('details')?.addEventListener('click', (event) => {
  if (event.target.id === 'details') closeDetails();
});
$('sign-out')?.addEventListener('click', signOutNow);

$('new-session').addEventListener('click', startSession);

state.msgs = [greeting()];
render();
