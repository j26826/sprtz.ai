/**
 * What an editor is told when something fails.
 *
 * Nothing from a library, a stack or a status line reaches the screen. Firebase
 * says "Firebase: Error (auth/too-many-requests).", a dead Cloud Run instance
 * says "ConnectError: ", an MCP server says "TypeError: 'NoneType' object is
 * not subscriptable" — each of them true, and none of them a sentence anyone
 * can act on. They read as a broken product, and they say things about the
 * inside of the system that a desk has no business showing.
 *
 * Three sources of a message, in order of how much they know:
 *
 * 1. A code this app recognises — a Firebase auth code, an HTTP status. These
 *    map to sentences written here.
 * 2. A detail the API wrote. Those are written for editors ("This event has
 *    already started; it can no longer be rescheduled") and are better than
 *    anything a generic map could say — but only when they read as prose
 *    rather than as a traceback, which is what `looksHuman` decides.
 * 3. The fallback for the thing being attempted.
 *
 * Pure and importing nothing, so `node --test` reaches it: `t` is passed in.
 */

/** Firebase auth codes, to the keys that answer them. */
export const AUTH_KEYS = {
  'auth/invalid-credential': 'auth.badCredentials',
  'auth/invalid-login-credentials': 'auth.badCredentials',
  'auth/wrong-password': 'auth.badCredentials',
  'auth/invalid-email': 'auth.badEmail',
  'auth/user-not-found': 'auth.noAccount',
  'auth/user-disabled': 'auth.disabled',
  'auth/too-many-requests': 'auth.tooMany',
  'auth/network-request-failed': 'error.offline',
  'auth/unauthorized-domain': 'auth.unauthorizedDomain',
  'auth/operation-not-allowed': 'auth.notEnabled',
  'auth/popup-closed-by-user': 'auth.popupClosed',
  'auth/popup-blocked': 'auth.popupBlocked',
  'auth/requires-recent-login': 'auth.signInAgain',
  'auth/id-token-expired': 'auth.signInAgain',
  'auth/user-token-expired': 'auth.signInAgain',
};

/** HTTP statuses, to the keys that answer them. */
export const STATUS_KEYS = {
  401: 'auth.signInAgain',
  403: 'error.notAllowed',
  404: 'error.gone',
  408: 'error.slow',
  413: 'error.tooLarge',
  429: 'error.busy',
  500: 'error.desk',
  502: 'error.desk',
  503: 'error.desk',
  504: 'error.slow',
};

/**
 * Whether a message was written for a person.
 *
 * The test is deliberately strict: anything that looks like it came out of a
 * runtime is refused, and a sentence that merely reads oddly is kept. Getting
 * this wrong in one direction shows someone a traceback; in the other it
 * replaces a specific message with a general one.
 */
export function looksHuman(text) {
  const value = String(text ?? '').trim();
  if (value.length < 12 || value.length > 400) return false;
  // A runtime's fingerprints: an exception class, a module path, a stack, a
  // status line, a payload, a code nobody types.
  const machine = [
    /\b[A-Z][A-Za-z]*(Error|Exception)\b/,       // TypeError, ConnectError, ValueError
    /\bTraceback\b/i,
    /\bat [\w.$]+ \(/,                            // a JS stack frame
    /^\s*[{[]/,                                   // a JSON body
    /\b(?:auth|firestore|storage|functions)\/[a-z-]+\b/,  // auth/too-many-requests
    /\bFirebase\b|\bFirestore\b|\bgRPC\b|\bgoogleapis\b/i,
    /\b(?:HTTP )?\d{3} (?:Bad|Internal|Service|Gateway|Not|Unauthorized|Forbidden)\b/,
    /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/,              // FAILED_PRECONDITION, PERMISSION_DENIED
    /^\s*\d+\s+[A-Z_]{3,}\b/,                     // a gRPC status: "7 PERMISSION_DENIED: …"
    /\bhttps?:\/\/\S{20,}/,                       // a URL long enough to be an endpoint
    /\berror code\b|\bcode \d+\b/i,               // "Error code 9", "code 13"
    /\bline \d+\b|\.py\b|\.js\b|\.mjs\b/,
    /\bundefined\b|\bNoneType\b|\bnull\b/,
  ];
  if (machine.some((pattern) => pattern.test(value))) return false;
  // Prose has spaces; a bare token or a path does not. And it starts like a
  // sentence — which is what separates "This event has already started" from
  // "upstream connect error or disconnect/reset before headers", the one piece
  // of infrastructure noise that otherwise reads as English.
  if (!/\s/.test(value)) return false;
  if (!/^[A-Z"“']/.test(value)) return false;
  return /[a-z]/.test(value);
}


/**
 * The sentence to show for a failure.
 *
 * `err` is whatever was caught: a fetch error carrying `status` and `detail`,
 * a Firebase error carrying `code`, or anything else. `fallback` is the key for
 * what was being attempted, so the message says which thing failed even when
 * nothing else is known.
 */
export function humanMessage(err, { t, fallback = 'error.generic' } = {}) {
  const say = typeof t === 'function' ? t : (key) => key;
  const code = String(err?.code || '');
  if (AUTH_KEYS[code]) return say(AUTH_KEYS[code]);

  const status = Number(err?.status || 0);
  const detail = err?.detail ?? err?.message ?? '';

  // Nothing reached anywhere: a fetch that failed on the network, or an SDK
  // that could not get out. Decided before any text is read, because "Failed
  // to fetch" is a sentence and still tells an editor nothing.
  if (!status && /network|failed to fetch|offline|load failed/i.test(String(err?.message || ''))) {
    return say('error.offline');
  }

  // A detail the API wrote is worth quoting whatever the status carried it.
  // Publishing answers with 502 and YouTube's own refusal — "the stored
  // refresh token has been revoked; reconnect the channel in Settings" — which
  // is the most useful sentence available and the one thing to do about it.
  // `looksHuman` is what separates those from the infrastructure's own noise,
  // and it is stricter than any rule about status numbers could be.
  if (looksHuman(detail)) return String(detail).trim();
  if (STATUS_KEYS[status]) return say(STATUS_KEYS[status]);
  if (status >= 500) return say('error.desk');

  return say(fallback);
}


/**
 * The text to put beside a failed job.
 *
 * The reason is written by a pipeline stage and is usually a sentence — "the
 * analysis produced no moments" — but a stage that died of an exception
 * records that instead, and "ConnectError: " is not a thing to show anyone.
 */
export function jobFailure(reason, { t } = {}) {
  const say = typeof t === 'function' ? t : (key) => key;
  return looksHuman(reason) ? String(reason).trim() : say('error.runFailed');
}
