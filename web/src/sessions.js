/**
 * Sessions: the conversations listed down the left.
 *
 * A session is not the same thing as a job, and conflating them is what made
 * "New session" impossible to implement. A job exists only once a match has
 * been uploaded; a session exists the moment someone starts talking, which is
 * before there is anything to upload it to. So a session holds a `jobId` that
 * is null until a match is registered inside it, and the panel shows the job's
 * live status through that link.
 *
 * They live in localStorage rather than Firestore. What a session records is a
 * conversation on this device — which match it is about, and what it is called
 * — and none of it is data the analysis depends on. Losing the list costs the
 * ordering of a sidebar, not a match: every job is still reachable, because
 * `reconcile` adopts any job that has no session into a fresh one.
 */

const STORAGE_KEY = 'sportscut.sessions';

let sessions = [];

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
  } catch { /* the list still works for this tab */ }
}

function newId() {
  return `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function loadSessions() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    sessions = Array.isArray(parsed) ? parsed.filter((s) => s && s.id) : [];
  } catch {
    sessions = [];
  }
  return listSessions();
}

export function listSessions() {
  return [...sessions].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
}

export function getSession(id) {
  return sessions.find((s) => s.id === id) || null;
}

export function sessionForJob(jobId) {
  return sessions.find((s) => s.jobId === jobId) || null;
}

export function createSession(title = '') {
  const session = { id: newId(), title, jobId: null, createdAt: Date.now() };
  sessions.push(session);
  persist();
  return session;
}

export function updateSession(id, patch) {
  const session = getSession(id);
  if (!session) return null;
  Object.assign(session, patch);
  persist();
  return session;
}

export function removeSession(id) {
  sessions = sessions.filter((s) => s.id !== id);
  persist();
}

/**
 * Make sure every job is reachable from the list.
 *
 * Jobs are the durable record and the sessions list is not, so the jobs win: a
 * match uploaded on another device, or one whose session was deleted from this
 * browser's storage, would otherwise be invisible here despite existing. Any
 * job without a session gets one.
 */
export function reconcile(jobs) {
  const known = new Set(sessions.map((s) => s.jobId).filter(Boolean));
  let added = false;
  for (const job of jobs) {
    if (known.has(job.id)) continue;
    sessions.push({
      id: newId(),
      title: job.title || job.source?.originalName || '',
      jobId: job.id,
      // Ordered by the match's own age, not by when this browser noticed it.
      createdAt: toMillis(job.createdAt) || Date.now(),
    });
    added = true;
  }
  if (added) persist();
  return listSessions();
}

function toMillis(value) {
  if (!value) return 0;
  if (typeof value.toDate === 'function') return value.toDate().getTime();
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}
