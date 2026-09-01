/**
 * SPRTZ AI Editor.
 *
 * Two data paths, deliberately different:
 *  - Firestore onSnapshot for anything that changes while an analysis runs
 *    (job status, the activity feed, moments and clips as they appear). The
 *    agents write through the catalog server, so the UI updates with no polling.
 *  - The REST API for anything that needs a server-side decision: signed upload
 *    URLs, signed CDN playback URLs, semantic search, and the agent conversation.
 *
 * Playback is a single HLS stream per match. Reviewing a moment seeks within it
 * and stops at the moment's out point, so nothing has to be rendered to watch a
 * suggestion.
 */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js';
import {
  getAuth, signInWithPopup, GoogleAuthProvider, onAuthStateChanged, getIdToken,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import {
  getFirestore, collection, doc, query, where, orderBy, limit, onSnapshot,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

const CONFIG = window.SPRTZ_CONFIG || {};
const API = (CONFIG.apiBaseUrl || '').replace(/\/$/, '');

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ state */

const state = {
  user: null,
  jobs: [],
  jobId: null,
  job: null,
  moments: [],
  clips: [],
  events: [],
  searchResults: null,
  selectedId: null,
  /** When set, the player stops at `end` — how a moment is reviewed. */
  range: null,
  tab: 'clips',
  sessionId: null,
  unsubscribe: [],
};

const STAGES = ['ingest', 'transcode', 'analysis', 'clips', 'captions', 'complete'];

const CATEGORY_OF = {};

/* ------------------------------------------------------------------ utils */

function fmtTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtClock(ts) {
  if (!ts) return '--:--';
  const d = ts.toDate ? ts.toDate() : new Date(ts);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtBytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Escape before interpolating anything model- or user-authored into HTML. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.user) {
    // IAP authenticates at the edge; this is belt-and-braces for local runs
    // where IAP is not in front of the API.
    headers.Authorization = `Bearer ${await getIdToken(state.user)}`;
  }
  const response = await fetch(`${API}${path}`, { ...options, headers, credentials: 'include' });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

/* ------------------------------------------------------------------- auth */

const firebaseApp = initializeApp({
  apiKey: CONFIG.firebaseApiKey,
  authDomain: CONFIG.firebaseAuthDomain,
  projectId: CONFIG.projectId,
});
const auth = getAuth(firebaseApp);
const db = getFirestore(firebaseApp);

$('signin-btn').addEventListener('click', async () => {
  try {
    await signInWithPopup(auth, new GoogleAuthProvider());
  } catch (error) {
    const box = $('signin-error');
    box.textContent = error.message || 'Sign-in failed.';
    box.classList.remove('hidden');
  }
});

onAuthStateChanged(auth, (user) => {
  state.user = user;
  $('signin').classList.toggle('hidden', !!user);
  $('app').classList.toggle('hidden', !user);
  if (user) {
    $('user-email').textContent = user.email || user.uid;
    $('user-avatar').textContent = (user.email || '?')[0].toUpperCase();
    watchJobs(user.uid);
  }
});

/* ------------------------------------------------- realtime subscriptions */

function clearSubscriptions() {
  state.unsubscribe.forEach((fn) => fn());
  state.unsubscribe = [];
}

function watchJobs(uid) {
  const q = query(
    collection(db, 'jobs'),
    where('ownerUid', '==', uid),
    orderBy('createdAt', 'desc'),
    limit(50),
  );
  onSnapshot(q, (snap) => {
    state.jobs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    renderJobs();
    if (!state.jobId && state.jobs.length) selectJob(state.jobs[0].id);
  }, (error) => console.error('jobs listener', error));
}

function watchJob(jobId) {
  clearSubscriptions();

  state.unsubscribe.push(onSnapshot(doc(db, 'jobs', jobId), (snap) => {
    if (!snap.exists()) return;
    const previous = state.job;
    state.job = { id: snap.id, ...snap.data() };
    renderHeader();
    renderStages();
    // Load playback the moment the transcode stage publishes a URL.
    if (state.job.playback?.hlsUrl && !previous?.playback?.hlsUrl) loadPlayback();
  }));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'moments'), orderBy('startSec', 'asc'), limit(500)),
    (snap) => {
      state.moments = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      state.moments.forEach((m) => { CATEGORY_OF[m.momentId] = m.category; });
      $('count-moments').textContent = state.moments.length;
      renderTimeline();
      renderPanel();
      updateTransport();
    },
  ));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'clips'), orderBy('score', 'desc'), limit(200)),
    (snap) => {
      state.clips = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      $('count-clips').textContent = state.clips.length;
      renderPanel();
    },
  ));

  state.unsubscribe.push(onSnapshot(
    query(collection(db, 'jobs', jobId, 'events'), orderBy('ts', 'desc'), limit(200)),
    (snap) => {
      state.events = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      if (state.tab === 'activity') renderPanel();
    },
  ));
}

/* ------------------------------------------------------------- rendering */

function renderJobs() {
  const host = $('job-list');
  if (!state.jobs.length) {
    host.innerHTML = '<div class="empty" style="padding:24px 12px;">No matches yet.</div>';
    return;
  }
  host.innerHTML = state.jobs.map((job) => `
    <button class="job" data-job="${esc(job.id)}" aria-current="${job.id === state.jobId}">
      <div class="job-title">${esc(job.title || 'Untitled match')}</div>
      <div class="job-meta">
        <span>${esc(job.status || 'uploaded')}</span>
        ${job.media?.durationSec ? `<span>· ${fmtTime(job.media.durationSec)}</span>` : ''}
        ${job.counts?.clips ? `<span>· ${job.counts.clips} clips</span>` : ''}
      </div>
    </button>`).join('');

  host.querySelectorAll('.job').forEach((el) => {
    el.addEventListener('click', () => selectJob(el.dataset.job));
  });
}

function renderHeader() {
  const job = state.job;
  if (!job) return;

  $('stage-title').textContent = job.title || 'Untitled match';

  const bits = [];
  if (job.sport) bits.push(job.sport);
  if (job.media?.durationSec) bits.push(fmtTime(job.media.durationSec));
  if (job.media?.width) bits.push(`${job.media.width}x${job.media.height}`);
  if (job.media?.fps) bits.push(`${job.media.fps}fps`);
  if (job.media?.segmentCount) bits.push(`${job.media.segmentCount} segments`);
  if (job.source?.bytes) bits.push(fmtBytes(job.source.bytes));
  $('stage-sub').textContent = bits.join(' · ') || '—';

  const pill = $('status-pill');
  const running = ['analyzing', 'transcoding', 'uploaded'].includes(job.status);
  pill.classList.remove('hidden', 'pill-live', 'pill-error');
  if (job.status === 'failed') pill.classList.add('pill-error');
  else if (running) pill.classList.add('pill-live');
  pill.querySelector('.dot').classList.toggle('dot-pulse', running);
  $('status-text').textContent = job.status || 'unknown';

  $('analyze-btn').disabled = ['analyzing', 'transcoding'].includes(job.status);
  $('analyze-btn').textContent = job.counts?.moments ? 'Re-analyse' : 'Analyse';
}

function renderStages() {
  const job = state.job;
  const current = STAGES.indexOf(job?.stage || 'ingest');
  $('stage-steps').innerHTML = STAGES.map((_, i) => {
    let stateName = 'idle';
    if (job?.status === 'failed' && i === current) stateName = 'failed';
    else if (i < current) stateName = 'done';
    else if (i === current) stateName = job?.status === 'ready' ? 'done' : 'active';
    return `<div class="stage-step" data-state="${stateName}"></div>`;
  }).join('');
}

function momentAt(id) {
  return state.moments.find((m) => m.momentId === id || m.id === id);
}

function renderTimeline() {
  const duration = state.job?.media?.durationSec || 0;
  const host = $('timeline');
  host.querySelectorAll('.marker').forEach((el) => el.remove());
  if (!duration) return;

  state.moments.forEach((m) => {
    const el = document.createElement('div');
    el.className = 'marker';
    el.dataset.category = m.category || '';
    el.dataset.moment = m.momentId;
    el.dataset.selected = m.momentId === state.selectedId;
    const left = (m.startSec / duration) * 100;
    const width = Math.max(0.25, ((m.endSec - m.startSec) / duration) * 100);
    el.style.left = `${left}%`;
    el.style.width = `${width}%`;
    el.title = `${m.label} — ${fmtTime(m.startSec)}`;
    el.addEventListener('click', (event) => {
      event.stopPropagation();
      playMoment(m);
    });
    host.appendChild(el);
  });
}

function categoryColor(category) {
  return `var(--${category || 'text-faint'})`;
}

function momentCard(m, extra = '') {
  const selected = m.momentId === state.selectedId;
  return `
    <div class="card" data-moment="${esc(m.momentId)}" data-selected="${selected}">
      <div class="card-top">
        <div class="card-label">
          <i style="background:${categoryColor(m.category)}"></i>
          <span>${esc(m.label || m.momentType)}</span>
        </div>
        <div class="card-time">${fmtTime(m.startSec)}–${fmtTime(m.endSec)}</div>
      </div>
      <div class="card-desc">${esc(m.description)}</div>
      <div class="card-foot">
        <span class="score">${(m.highlightScore ?? 0).toFixed(2)}</span>
        <span class="bar"><i style="width:${Math.round((m.highlightScore ?? 0) * 100)}%"></i></span>
        ${m.isGoal ? '<span class="tag">goal</span>' : ''}
        ${m.scoreboard ? `<span class="tag">${esc(m.scoreboard)}</span>` : ''}
      </div>
      ${extra}
    </div>`;
}

function clipCard(c) {
  const selected = c.clipId === state.selectedId;
  const tags = (c.hashtags || []).slice(0, 4).map((h) => `<span class="tag">#${esc(h)}</span>`).join('');
  return `
    <div class="card" data-clip="${esc(c.clipId)}" data-moment="${esc(c.momentId)}" data-selected="${selected}">
      <div class="card-top">
        <div class="card-label">
          <i style="background:${categoryColor(CATEGORY_OF[c.momentId])}"></i>
          <span>${esc(c.title || 'Untitled clip')}</span>
        </div>
        <div class="card-time">${fmtTime(c.startSec)} · ${Math.round(c.durationSec)}s</div>
      </div>
      ${c.hookText ? `<div class="card-desc"><strong>${esc(c.hookText)}</strong></div>` : ''}
      ${c.captions?.tiktok ? `<div class="card-desc">${esc(c.captions.tiktok)}</div>` : ''}
      <div class="card-foot">
        <span class="score">${(c.score ?? 0).toFixed(2)}</span>
        <span class="bar"><i style="width:${Math.round((c.score ?? 0) * 100)}%"></i></span>
        ${tags}
      </div>
    </div>`;
}

function renderPanel() {
  const host = $('panel');
  $('search-row').classList.toggle('hidden', state.tab === 'activity');

  if (state.tab === 'activity') {
    host.innerHTML = state.events.length
      ? state.events.map((e) => `
          <div class="event" data-level="${esc(e.level || 'info')}">
            <div class="event-time">${fmtClock(e.ts)}</div>
            <div class="event-body">
              <div class="event-stage">${esc(e.stage || '')}</div>
              <div class="event-msg">${esc(e.message)}</div>
            </div>
          </div>`).join('')
      : '<div class="empty"><strong>Nothing yet</strong>Progress appears here as the agents work.</div>';
    return;
  }

  if (state.searchResults) {
    const results = state.searchResults;
    host.innerHTML = `
      <div class="card-foot" style="padding:2px 4px 10px;">
        <span>${results.length} result${results.length === 1 ? '' : 's'}</span>
        <button class="btn btn-sm" id="clear-search">Clear</button>
      </div>
      ${results.map((m) => momentCard(
        m,
        m.rerank_reason ? `<div class="rerank-why">${esc(m.rerank_reason)}</div>` : '',
      )).join('')}`;
    $('clear-search').addEventListener('click', () => {
      state.searchResults = null;
      $('search-input').value = '';
      renderPanel();
    });
    bindCards();
    return;
  }

  if (state.tab === 'clips') {
    host.innerHTML = state.clips.length
      ? state.clips.map(clipCard).join('')
      : '<div class="empty"><strong>No clips yet</strong>Run an analysis and suggestions will appear here.</div>';
  } else {
    host.innerHTML = state.moments.length
      ? state.moments.map((m) => momentCard(m)).join('')
      : '<div class="empty"><strong>No moments yet</strong>Run an analysis to find the key moments in this match.</div>';
  }
  bindCards();
}

function bindCards() {
  $('panel').querySelectorAll('.card').forEach((el) => {
    el.addEventListener('click', () => {
      const clipId = el.dataset.clip;
      if (clipId) {
        const clip = state.clips.find((c) => c.clipId === clipId);
        if (clip) playRange(clip.startSec, clip.endSec, clip.title || 'Clip', clipId);
        return;
      }
      const m = momentAt(el.dataset.moment);
      if (m) playMoment(m);
    });
  });
}

/* -------------------------------------------------------------- playback */

const video = $('video');
let hls = null;

async function loadPlayback() {
  try {
    const playback = await api(`/api/jobs/${state.jobId}/playback`);
    attachHls(playback.hls_url);
    $('player-empty').classList.add('hidden');
    $('tc-total').textContent = fmtTime(playback.duration_sec || 0);
  } catch (error) {
    console.warn('playback unavailable:', error.message);
  }
}

function attachHls(url) {
  if (hls) { hls.destroy(); hls = null; }

  if (window.Hls?.isSupported()) {
    hls = new window.Hls({ maxBufferLength: 30, startPosition: 0 });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.ERROR, (_, data) => {
      if (data.fatal) console.error('hls fatal', data.type, data.details);
    });
  } else {
    // Safari plays HLS natively and does not need the polyfill.
    video.src = url;
  }
  $('play-btn').disabled = false;
}

/** Seek to a moment and stop at its out point. */
function playRange(startSec, endSec, label, selectedId) {
  state.range = { start: startSec, end: endSec };
  state.selectedId = selectedId;

  $('range-lock').classList.remove('hidden');
  $('range-lock-text').textContent = `${label} · ${fmtTime(startSec)}–${fmtTime(endSec)}`;

  if (video.readyState === 0 && !video.src && !hls) return;
  video.currentTime = startSec;
  video.play().catch(() => { /* autoplay blocked; the user can press play */ });

  renderTimeline();
  renderPanel();
}

function playMoment(m) {
  playRange(m.startSec, m.endSec, m.label || m.momentType, m.momentId);
}

video.addEventListener('timeupdate', () => {
  $('tc-current').textContent = fmtTime(video.currentTime);
  const duration = state.job?.media?.durationSec || video.duration || 0;
  if (duration) {
    $('timeline-progress').style.width = `${(video.currentTime / duration) * 100}%`;
  }
  // Stop at the out point so reviewing a suggestion shows exactly the cut.
  if (state.range && video.currentTime >= state.range.end) {
    video.pause();
  }
});

video.addEventListener('play', () => { $('play-btn').textContent = '⏸'; });
video.addEventListener('pause', () => { $('play-btn').textContent = '▶'; });

$('play-btn').addEventListener('click', () => {
  if (video.paused) video.play().catch(() => {}); else video.pause();
});

$('range-lock-clear').addEventListener('click', () => {
  state.range = null;
  state.selectedId = null;
  $('range-lock').classList.add('hidden');
  renderTimeline();
  renderPanel();
});

$('timeline').addEventListener('click', (event) => {
  const duration = state.job?.media?.durationSec || 0;
  if (!duration) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const ratio = (event.clientX - rect.left) / rect.width;
  state.range = null;
  $('range-lock').classList.add('hidden');
  video.currentTime = Math.max(0, Math.min(duration, ratio * duration));
});

function updateTransport() {
  const has = state.moments.length > 0;
  $('prev-btn').disabled = !has;
  $('next-btn').disabled = !has;
  $('moment-counter').textContent = has ? `${state.moments.length} moments` : '';
}

function step(direction) {
  if (!state.moments.length) return;
  const index = state.moments.findIndex((m) => m.momentId === state.selectedId);
  const next = index === -1
    ? (direction > 0 ? 0 : state.moments.length - 1)
    : Math.max(0, Math.min(state.moments.length - 1, index + direction));
  playMoment(state.moments[next]);
}

$('prev-btn').addEventListener('click', () => step(-1));
$('next-btn').addEventListener('click', () => step(1));

document.addEventListener('keydown', (event) => {
  if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA') return;
  if (event.code === 'Space') { event.preventDefault(); $('play-btn').click(); }
  if (event.key === 'ArrowLeft' && event.shiftKey) step(-1);
  if (event.key === 'ArrowRight' && event.shiftKey) step(1);
});

/* ----------------------------------------------------------------- tabs */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', 'false'));
    tab.setAttribute('aria-selected', 'true');
    state.tab = tab.dataset.tab;
    renderPanel();
  });
});

/* --------------------------------------------------------------- search */

async function runSearch() {
  const q = $('search-input').value.trim();
  if (!q || !state.jobId) return;
  $('search-btn').disabled = true;
  try {
    const result = await api(`/api/jobs/${state.jobId}/search`, {
      method: 'POST',
      body: JSON.stringify({ query: q, limit: 12, rerank: true }),
    });
    state.searchResults = (result.moments || []).map((m) => ({
      momentId: m.moment_id,
      label: m.label,
      momentType: m.moment_type,
      category: m.category,
      startSec: m.start_sec,
      endSec: m.end_sec,
      description: m.description,
      highlightScore: m.rerank_score ?? m.highlight_score,
      isGoal: m.is_goal,
      scoreboard: m.scoreboard,
      rerank_reason: m.rerank_reason,
    }));
    renderPanel();
  } catch (error) {
    $('panel').innerHTML = `<div class="error-note">${esc(error.message)}</div>`;
  } finally {
    $('search-btn').disabled = false;
  }
}

$('search-btn').addEventListener('click', runSearch);
$('search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });

/* --------------------------------------------------------------- upload */

$('upload-btn').addEventListener('click', () => $('file-input').click());

$('file-input').addEventListener('change', async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  event.target.value = '';

  const box = $('upload-progress');
  box.classList.remove('hidden');
  $('upload-name').textContent = file.name;
  $('upload-bar-fill').style.width = '0%';
  $('upload-pct').textContent = 'Preparing…';

  try {
    const ticket = await api('/api/jobs/upload-url', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || 'video/mp4',
        size_bytes: file.size,
      }),
    });

    // Straight to GCS with the signed URL — the API never sees the bytes.
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', ticket.upload_url, true);
      xhr.setRequestHeader('Content-Type', file.type || 'video/mp4');
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        $('upload-bar-fill').style.width = `${pct}%`;
        $('upload-pct').textContent = `${pct}% · ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)}`;
      };
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`Upload failed: ${xhr.status}`)));
      xhr.onerror = () => reject(new Error('Upload failed.'));
      xhr.send(file);
    });

    $('upload-pct').textContent = 'Registering…';
    await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        job_id: ticket.job_id,
        title: file.name.replace(/\.[^.]+$/, ''),
        sport: 'handball',
        filename: file.name,
        size_bytes: file.size,
      }),
    });

    selectJob(ticket.job_id);
    box.classList.add('hidden');
  } catch (error) {
    $('upload-pct').textContent = error.message;
  }
});

/* ------------------------------------------------------------ job select */

function selectJob(jobId) {
  state.jobId = jobId;
  state.selectedId = null;
  state.range = null;
  state.searchResults = null;
  state.moments = [];
  state.clips = [];
  state.events = [];
  $('range-lock').classList.add('hidden');
  $('player-empty').classList.remove('hidden');
  if (hls) { hls.destroy(); hls = null; }
  video.removeAttribute('src');
  video.load();

  renderJobs();
  renderPanel();
  watchJob(jobId);
}

/* ----------------------------------------------------------------- agent */

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const result = await api('/api/agent/sessions', { method: 'POST' });
  state.sessionId = result.session_id;
  return state.sessionId;
}

function appendMessage(role, text) {
  const log = $('chat-log');
  const el = document.createElement('div');
  el.className = `msg msg-${role}`;
  el.innerHTML = `<div class="msg-role">${role === 'user' ? 'You' : 'Sprtz'}</div>
                  <div class="msg-body">${esc(text)}</div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el.querySelector('.msg-body');
}

async function sendToAgent(text) {
  appendMessage('user', text);
  const target = appendMessage('agent', '');
  let buffer = '';

  try {
    const sessionId = await ensureSession();
    const response = await fetch(`${API}/api/agent/messages`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(state.user ? { Authorization: `Bearer ${await getIdToken(state.user)}` } : {}),
      },
      body: JSON.stringify({ message: text, session_id: sessionId, job_id: state.jobId }),
    });
    if (!response.ok || !response.body) throw new Error(`Agent returned ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      pending += decoder.decode(value, { stream: true });

      const frames = pending.split('\n\n');
      pending = frames.pop() || '';

      for (const frame of frames) {
        const event = /event: (.+)/.exec(frame)?.[1];
        const raw = /data: (.+)/.exec(frame)?.[1];
        if (!event || !raw) continue;
        const data = JSON.parse(raw);
        if (event === 'text') {
          buffer += data.text;
          target.textContent = buffer;
        } else if (event === 'tool') {
          target.textContent = `${buffer}${buffer ? '\n' : ''}· ${data.name}…`;
        } else if (event === 'error') {
          target.textContent = `${buffer}\n\n${data.error}`;
        }
        $('chat-log').scrollTop = $('chat-log').scrollHeight;
      }
    }
    if (buffer) target.textContent = buffer;
  } catch (error) {
    target.textContent = `Could not reach the agent: ${error.message}`;
  }
}

$('chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  sendToAgent(text);
});

$('analyze-btn').addEventListener('click', () => {
  if (!state.jobId) return;
  state.tab = 'activity';
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-selected', String(t.dataset.tab === 'activity'));
  });
  renderPanel();
  sendToAgent('Analyse this match and suggest clips.');
});

renderStages();
renderPanel();
