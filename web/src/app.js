/**
 * SPRTZ AI — chat-first sports video agent.
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
  signInWithEmailAndPassword,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import {
  getFirestore, collection, doc, query, where, orderBy, limit, onSnapshot,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

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
  unsubscribe: [],
};

const PLATFORM_SPEC = {
  tiktok: { name: 'TikTok', spec: '9:16 · captions burned in' },
  instagram: { name: 'Instagram Reels', spec: '9:16 · cover frame at 00:03' },
  youtube: { name: 'YouTube Shorts', spec: '9:16 · title from caption' },
};

const GREETING = {
  who: 'agent',
  text: 'Upload a match and I will watch all of it, mark every moment that clears '
      + 'confidence, and cut what you ask for.\n\nTell me what you want and I will cut it.',
  showActions: true,
  actions: ['Ingest a new game', "What's still processing?", 'Show me the best moments'],
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

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.user) headers.Authorization = `Bearer ${await getIdToken(state.user)}`;
  const res = await fetch(`${API}${path}`, { ...options, headers, credentials: 'include' });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
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
    'auth/invalid-credential': 'That email and password do not match an account.',
    'auth/wrong-password': 'That email and password do not match an account.',
    'auth/user-not-found': 'No account with that email. Ask an administrator for access.',
    'auth/unauthorized-domain':
      'This hostname is not in the Identity Platform authorised domains list.',
    'auth/operation-not-allowed':
      'That sign-in method is not enabled on this project.',
  }[code] || err?.message || 'Sign-in failed.';
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

onAuthStateChanged(auth, async (user) => {
  state.user = user;
  $('signin').classList.toggle('hidden', !!user);
  $('app').classList.toggle('hidden', !user);
  if (!user) return;

  state.msgs = [GREETING];
  render();
  watchJobs(user.uid);
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

function watchJobs(uid) {
  onSnapshot(
    query(collection(db, 'jobs'), where('ownerUid', '==', uid),
          orderBy('createdAt', 'desc'), limit(50)),
    (snap) => {
      state.jobs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      if (!state.jobId && state.jobs.length) selectJob(state.jobs[0].id);
      else render();
    },
    (err) => console.error('jobs listener', err),
  );
}

function selectJob(jobId) {
  state.unsubscribe.forEach((fn) => fn());
  state.unsubscribe = [];
  state.jobId = jobId;
  state.moments = [];
  state.clips = [];
  state.events = [];
  state.playing = null;
  playbackUrl = null;
  destroyPlayer();

  state.unsubscribe.push(onSnapshot(doc(db, 'jobs', jobId), (snap) => {
    if (snap.exists()) { state.job = { id: snap.id, ...snap.data() }; render(); }
  }));

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

function momentsCard(msg) {
  const list = (msg.momentIds || [])
    .map(momentById)
    .filter(Boolean);
  if (!list.length) return '';

  return `<div class="panel-light">${list.map((m) => {
    const inReel = state.clips.some((c) => c.momentId === m.momentId);
    const meta = [
      m.category,
      `${Math.round(m.endSec - m.startSec)}s`,
      (m.highlightScore ?? 0).toFixed(2),
    ].filter(Boolean).join(' · ');
    const open = state.playing?.momentId === m.momentId;
    return `
      <div class="row">
        <div class="moment-row">
          <button class="thumb" data-play="${esc(m.momentId)}" title="Play this moment">
            <span class="thumb-stripes"></span>
            <span class="thumb-clock">${clock(m.startSec)}</span>
          </button>
          <div style="min-width:0">
            <div class="moment-label">${esc(m.label || m.momentType)}</div>
            <div class="moment-meta">${esc(meta)}</div>
            ${m.rerankReason ? `<div class="rerank-why">${esc(m.rerankReason)}</div>` : ''}
          </div>
          <button class="btn-outline" data-add="${esc(m.momentId)}" ${inReel ? 'disabled' : ''}>
            ${inReel ? '✓ Added' : 'Add'}
          </button>
        </div>
        ${open ? playerMarkup(m) : ''}
      </div>`;
  }).join('')}</div>`;
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
            <div class="dz-name">${esc(u.name || 'No file chosen')}</div>
            <div class="dz-meta">${esc(u.name
              ? `${u.size} · ${u.sport} · via Upload`
              : 'Drop a file here, or choose one')}</div>
          </div>
          <label class="file-label">Choose file
            <input type="file" id="file-input" accept="video/*" style="display:none" />
          </label>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;align-items:center">
          <div class="field-label" style="margin-right:4px">Sport</div>
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
            ${u.status === 'uploading' ? 'Uploading…' : u.status === 'analyzing' ? 'Analysing…' : 'Start analysis'}
          </button>
          ${pending ? `
            <button class="btn-outline" data-resume="${esc(pending.job_id)}"
                    ${u.status === 'idle' ? '' : 'disabled'}
                    title="${esc(pending.filename)} — ${bytes(pending.size_bytes)}">
              Use last night's upload
            </button>` : ''}
        </div>
        ${pending ? `
          <div class="dz-meta" style="margin-top:8px">
            ${esc(pending.filename)} · ${bytes(pending.size_bytes)} reached storage but was
            never analysed. Pick it up instead of uploading again.
          </div>` : ''}
      </div>
    </div>`;
}

function reelCard() {
  if (!state.clips.length) return '';
  const total = state.clips.reduce((a, c) => a + (c.durationSec || 0), 0);
  const aspect = state.clips[0]?.aspect || '9:16';
  return `
    <div class="panel">
      <div class="panel-head">
        <div class="panel-head-title">Working reel</div>
        <div class="panel-head-meta">${dur(total)} · ${esc(aspect)}</div>
      </div>
      <div class="reel-bars">
        ${state.clips.map((c, i) => `
          <div class="reel-bar" style="flex-grow:${c.durationSec || 1};
               background:${i % 2 ? 'var(--color-neutral-400)' : 'var(--color-neutral-700)'}"></div>`).join('')}
      </div>
      ${state.clips.map((c, i) => `
        <div class="clip-row">
          <div class="clip-n">${String(i + 1).padStart(2, '0')}</div>
          <div class="clip-label">${esc(c.title || c.hookText || 'Clip')}</div>
          <div class="stepper">
            <button class="step-btn" data-clip-shorter="${esc(c.clipId)}">&minus;</button>
            <div class="step-val">${(c.durationSec || 0).toFixed(1)}s</div>
            <button class="step-btn" data-clip-longer="${esc(c.clipId)}">+</button>
          </div>
          <button class="link-btn" data-clip-play="${esc(c.clipId)}">Play</button>
        </div>`).join('')}
      <div class="panel-actions">
        <button class="btn-solid" data-ask="Generate the video">Generate video</button>
        <button class="btn-outline" data-ask="Reframe it vertical">Reframe 9:16</button>
        <button class="btn-outline" data-ask="Prepare it for publishing">Prepare publish</button>
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


function jobsCard() {
  if (!state.jobs.length) {
    return `<div class="panel-light"><div class="job">
      <div class="job-stage">No jobs yet.</div></div></div>`;
  }
  return `<div class="panel-light">${state.jobs.map((j) => {
    const running = ['analyzing', 'transcoding', 'uploaded'].includes(j.status);
    const failed = j.status === 'failed';
    const stalled = running && isStalled(j);
    const tone = failed || stalled ? 'failed' : running ? 'running' : 'idle';
    return `
      <div class="job">
        <div class="job-top">
          <div class="job-name">${esc(j.title || j.source?.originalName || j.id)}</div>
          <div class="job-status" data-tone="${tone}">${
            stalled ? 'stalled' : esc(j.status || 'unknown')}</div>
        </div>
        <div class="job-stage">${esc(j.stage || '')}${
          j.media?.segmentCount ? ` · ${j.media.segmentCount} segments` : ''}</div>
        ${running && !stalled ? `
          <div class="meter-row">
            <div class="meter meter-neutral"><i style="width:${j.progress || 0}%"></i></div>
            <div class="meter-pct">${Math.round(j.progress || 0)}%</div>
          </div>` : ''}
        ${stalled ? `
          <div class="job-error">
            <p>No progress for ${esc(sinceLabel(j.updatedAt))}. The run that owned this
               job is gone — a deploy or a restart ends one mid-flight, and nothing
               picks it up again. Retrying starts it over.</p>
            <button class="btn-outline" data-retry="${esc(j.id)}">Retry</button>
          </div>` : ''}
        ${failed && j.error ? `
          <div class="job-error">
            <p>${esc(j.error)}</p>
            <button class="btn-outline" data-retry="${esc(j.id)}">Retry</button>
          </div>` : ''}
      </div>`;
  }).join('')}</div>`;
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
          <div class="field-label">Sprtz packages clips for download — it does not post on your behalf.</div>
          <div style="flex:1"></div>
          <button class="btn-solid" data-ask="Finalise the job for publishing">
            ${posted ? '✓ Packaged' : 'Prepare package'}
          </button>
        </div>
      </div>
    </div>`;
}

function activityCard() {
  if (!state.events.length) return '';
  return `<div class="panel-light">${state.events.slice(0, 12).map((e) => `
    <div class="job">
      <div class="job-top">
        <div class="job-name" style="font-weight:400;font-size:11.5px">${esc(e.message)}</div>
        <div class="job-status" data-tone="${e.level === 'error' ? 'failed' : 'idle'}">${esc(e.stage || '')}</div>
      </div>
    </div>`).join('')}</div>`;
}

function actionsRow(msg) {
  if (!msg.showActions || !msg.actions?.length) return '';
  return `<div class="chip-row">${msg.actions.map((a) => `
    <button class="chip" data-ask="${esc(a)}">${esc(a)}</button>`).join('')}</div>`;
}

/* ────────────────────────────────────────────────────────── render ── */

function render() {
  const job = state.job;
  $('game-title').textContent = job ? (job.title || 'Untitled match') : 'No match loaded';

  const parts = [];
  if (job?.media?.durationSec) parts.push(dur(job.media.durationSec));
  if (state.moments.length) parts.push(`${state.moments.length} moments`);
  parts.push(state.clips.length ? `${state.clips.length} in reel` : 'no reel yet');
  $('context-line').textContent = job ? parts.join(' · ') : 'Upload a recording to begin';

  $('transcript').innerHTML = state.msgs.map((m) => `
    <div class="msg ${m.who === 'agent' ? 'msg-agent' : ''}">
      <div class="msg-label">${m.who === 'agent' ? 'Agent' : 'You'}</div>
      <div class="msg-text">${esc(m.text)}</div>
      ${m.showMoments ? momentsCard(m) : ''}
      ${m.showIngest ? ingestCard() : ''}
      ${m.showReel ? reelCard() : ''}
      ${m.showJobs ? jobsCard() : ''}
      ${m.showPublish ? publishCard() : ''}
      ${m.showActivity ? activityCard() : ''}
      ${actionsRow(m)}
    </div>`).join('')
    + (state.thinking ? `
      <div class="msg msg-agent">
        <div class="msg-label">Agent</div>
        <div class="thinking-text">Scanning the game…</div>
      </div>` : '');

  $('suggestions').innerHTML = [
    'Ingest a new game',
    'Show me the best moments',
    "What's still processing?",
  ].map((s) => `<button class="chip" data-ask="${esc(s)}">${esc(s)}</button>`).join('');

  if (state.playing) mountPlayer();
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
      <button class="link-btn" data-close-player="1">Close</button>
    </div>`;
  return el;
}

/** Re-parent the live player into this render's slot, building it once. */
async function mountPlayer() {
  const { momentId, start, end } = state.playing;
  const slot = document.querySelector(`[data-slot="${CSS.escape(momentId)}"]`);
  if (!slot) return;

  if (playerFor !== momentId) {
    destroyPlayer();
    playerEl = buildPlayerEl(momentById(momentId) || { startSec: start, endSec: end });
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
    playerEl.insertAdjacentHTML('beforeend',
      `<div class="error-note">Playback is not ready yet: ${esc(err.message)}</div>`);
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

function playMoment(momentId) {
  const m = momentById(momentId);
  if (!m) return;
  state.playing = { momentId, start: m.startSec, end: m.endSec };
  render();
}

/* ──────────────────────────────────────────────────────────── agent ── */

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const r = await api('/api/agent/sessions', { method: 'POST' });
  state.sessionId = r.session_id;
  return state.sessionId;
}

async function ask(text) {
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
    state.msgs.push({ who: 'agent', text: '' });
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
    attachCards(msgIndex, text);
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
  const q = question.toLowerCase();
  const msg = state.msgs[index];
  if (!msg) return;

  if (/ingest|upload|new game|new match|import|analy[sz]e a/.test(q)) {
    msg.showIngest = true;
  } else if (/process|job|status|fail|error|still running/.test(q)) {
    msg.showJobs = true;
    msg.showActions = true;
    msg.actions = ['Show me the best moments', 'Ingest a new game'];
  } else if (/publish|post|schedule|package/.test(q)) {
    msg.showPublish = true;
  } else if (/cut|reel|montage|render|generate|reframe|vertical|shorter|tighten/.test(q)) {
    if (state.clips.length) {
      msg.showReel = true;
      msg.showActions = true;
      msg.actions = ['Generate video', 'Reframe 9:16', 'Prepare publish'];
    }
  } else if (state.moments.length) {
    // A question about the match: show what it matched on.
    msg.showMoments = true;
    msg.momentIds = [...state.moments]
      .sort((a, b) => (b.highlightScore ?? 0) - (a.highlightScore ?? 0))
      .slice(0, 6)
      .map((m) => m.momentId);
    msg.showActions = true;
    msg.actions = ['Cut all of these', 'Cut a 30-second short'];
  }
}

/* ─────────────────────────────────────────────────────────── upload ── */

/**
 * Turn an object that is already in the bucket into a job the agent can run.
 *
 * Shared by a fresh upload and by resuming one that never got registered — the
 * bytes are in the same place either way, so only this second half differs.
 */
async function registerAndAnalyse({ job_id, filename, size_bytes, content_type }) {
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
    }),
  });

  // It has a job document now, so it is no longer stranded.
  state.pendingUploads = state.pendingUploads.filter((p) => p.job_id !== job_id);

  u.status = 'analyzing';
  u.stage = 'Handed to the agent';
  selectJob(job_id);
  playbackUrl = null;
  render();

  await ask('Analyse this match and suggest clips.');
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
  const t = event.target.closest('[data-ask],[data-play],[data-add],[data-platform],'
    + '[data-clip-shorter],[data-clip-longer],[data-clip-play],[data-retry],'
    + '[data-sport],[data-close-player]');
  if (!t) return;

  if (t.dataset.ask) {
    const q = t.dataset.ask;
    if (q === 'Cut all of these') {
      ask('Cut all of these into clips.');
    } else {
      ask(q);
    }
    return;
  }
  if (t.dataset.play) { playMoment(t.dataset.play); return; }
  if (t.dataset.closePlayer) { state.playing = null; destroyPlayer(); render(); return; }
  if (t.dataset.add) {
    const m = momentById(t.dataset.add);
    ask(`Add the ${m?.label || 'moment'} at ${clock(m?.startSec || 0)} to the reel.`);
    return;
  }
  if (t.dataset.clipPlay) {
    const c = state.clips.find((x) => x.clipId === t.dataset.clipPlay);
    if (c) { state.playing = { momentId: c.momentId, start: c.startSec, end: c.endSec }; render(); }
    return;
  }
  if (t.dataset.clipShorter || t.dataset.clipLonger) {
    const id = t.dataset.clipShorter || t.dataset.clipLonger;
    const c = state.clips.find((x) => x.clipId === id);
    const delta = t.dataset.clipShorter ? -1 : 1;
    ask(`Make the clip "${c?.title || id}" ${Math.abs(delta)} second ${delta < 0 ? 'shorter' : 'longer'}.`);
    return;
  }
  if (t.dataset.platform) {
    state.platforms[t.dataset.platform] = !state.platforms[t.dataset.platform];
    render();
    return;
  }
  if (t.dataset.retry) {
    selectJob(t.dataset.retry);
    ask('The run on this job stopped without finishing. Start the analysis again.');
    return;
  }
  if (t.dataset.sport) { state.upload.sport = t.dataset.sport; render(); }
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

$('new-session').addEventListener('click', () => {
  state.msgs = [GREETING];
  state.sessionId = null;
  state.playing = null;
  render();
});

state.msgs = [GREETING];
render();
