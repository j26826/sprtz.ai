/**
 * Sessions: the conversations listed down the left.
 *
 * A session is a conversation, not a match. It remembers which job it is
 * currently about so that reopening it returns to the same place, but that link
 * is a bookmark rather than ownership: several sessions may be about the same
 * match, a session may be about none, and deleting one never touches a job.
 *
 * That separation is the point. A match is expensive — hours of analysis over a
 * multi-gigabyte upload — and a conversation is cheap. Tying the two together
 * would mean tidying the sidebar destroyed work, which is not a trade anyone
 * would make deliberately and is far too easy to make by accident.
 *
 * They live in localStorage. What a session records is a conversation on this
 * device, and none of it is data the analysis depends on: jobs are the durable
 * record and are reachable through the agent whether a session mentions them or
 * not. Losing the list costs the sidebar, not a match.
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

export function sessionsAboutJob(jobId) {
  // Plural on purpose: nothing stops two conversations being about one match.
  return sessions.filter((s) => s.jobId === jobId);
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
