/**
 * The arithmetic of a reel: what is picked, how long it runs, and which cut
 * the playhead is in.
 *
 * Its own module with no imports, for the same reason as cards.js and
 * ridegroups.js: app.js imports the Firebase SDK from a CDN and cannot be
 * loaded outside a browser, so anything worth testing has to live somewhere
 * `node --test` can reach.
 *
 * All of it is worth testing. A key that collides puts two different moments
 * in one slot; a total that disagrees with the cuts misreports what will be
 * published; and picking the wrong cut for the playhead is what makes a
 * preview jump to the wrong place with nothing on screen to explain it.
 */

/**
 * How a picked moment is addressed.
 *
 * By its match as well as by itself: ids are unique within a job and a reel
 * may hold cuts from four of them. The separator is a pipe rather than a
 * colon because the delegated click handler already splits on colons for its
 * `index:value` attributes, and a moment id is free to contain one.
 */
export function pickKey(jobId, momentId) {
  return `${jobId || ''}|${momentId || ''}`;
}

export function parsePickKey(key) {
  const at = String(key ?? '').indexOf('|');
  if (at < 0) return { jobId: '', momentId: String(key ?? '') };
  return { jobId: key.slice(0, at), momentId: key.slice(at + 1) };
}

export function isPickedIn(pick, jobId, momentId) {
  const key = pickKey(jobId, momentId);
  return (pick || []).some((p) => pickKey(p.jobId, p.momentId) === key);
}

/**
 * Add or remove one moment. Returns a new list rather than mutating one.
 *
 * Order is the order things were picked, and it becomes the running order of
 * the reel — so re-picking something that was removed puts it at the end
 * rather than back where it used to be, which is what the gesture means.
 */
export function togglePicked(pick, entry) {
  const key = pickKey(entry.jobId, entry.momentId);
  const without = (pick || []).filter((p) => pickKey(p.jobId, p.momentId) !== key);
  return without.length === (pick || []).length ? [...without, entry] : without;
}

/** The reel's running time: the sum of its cuts, not the span they cover. */
export function reelLength(cuts) {
  return (cuts || []).reduce((n, c) => n + Math.max(0, (c.endMs || 0) - (c.startMs || 0)), 0);
}

/** How many distinct matches a reel draws on. */
export function matchCount(cuts) {
  return new Set((cuts || []).map((c) => c.jobId).filter(Boolean)).size;
}

/**
 * A timecode with its milliseconds, because that is what a cut is stored in.
 *
 * The player's `shortClock` stops at seconds, which is right for judging a
 * movement and wrong here: an editor nudging an in point by 100ms needs to see
 * the 100ms move, or the control looks broken.
 */
export function msClock(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0));
  const m = Math.floor(total / 60000);
  const s = Math.floor((total % 60000) / 1000);
  return `${m}:${String(s).padStart(2, '0')}.${String(total % 1000).padStart(3, '0')}`;
}

/** The smallest a cut may be nudged to. Below this a cut is not a cut. */
export const MIN_CUT_MS = 100;

/**
 * Move one edge of a cut.
 *
 * Clamps rather than refuses, like every other trim on this desk: a control
 * held at its limit should stop, not start failing. The far edge is what gives
 * way, so nudging an in point past its out point shortens the cut to the
 * minimum instead of inverting it. What the *record* allows is decided by the
 * server, which re-plans every cut it is sent; this only keeps the browser
 * from asking for something incoherent.
 */
export function nudge(cut, edge, byMs) {
  const startMs = Number(cut.startMs) || 0;
  const endMs = Number(cut.endMs) || 0;
  if (edge === 'start') {
    return { ...cut, startMs: Math.max(0, Math.min(startMs + byMs, endMs - MIN_CUT_MS)) };
  }
  return { ...cut, endMs: Math.max(startMs + MIN_CUT_MS, endMs + byMs) };
}

/** Swap a cut with its neighbour. Out of range is a no-op, not an error. */
export function moveCut(cuts, from, by) {
  const to = from + by;
  if (!Array.isArray(cuts) || to < 0 || to >= cuts.length || from < 0 || from >= cuts.length) {
    return cuts || [];
  }
  const out = [...cuts];
  [out[from], out[to]] = [out[to], out[from]];
  return out;
}

/**
 * Which cut comes next, or null at the end of the reel.
 *
 * Separate from the player so the end-of-reel case is testable: the difference
 * between "advance" and "stop" is one comparison, and getting it wrong either
 * loops the last cut for ever or drops the final one.
 */
export function nextCut(cuts, at) {
  return (cuts || [])[at + 1] ? at + 1 : null;
}

/**
 * Whether the playhead has run past the cut it is in.
 *
 * Takes seconds because that is what a video element reports, and compares in
 * milliseconds because that is what a cut is stored in.
 */
export function pastEnd(cut, atSec) {
  if (!cut) return false;
  return (Number(atSec) || 0) * 1000 >= (Number(cut.endMs) || 0);
}

/* ── The trim strip ───────────────────────────────────────────────────────
   A cut is dragged inside a window wider than the moment itself, because the
   point of trimming is to take in the run-up or let the reaction breathe —
   a strip that stopped at the detected edges could only ever shorten. The
   window is deliberately not the full slack the record allows: 120s either
   side across a few hundred pixels is a pixel per half-second, and no amount
   of care with a mouse lands a millisecond there. Dragging is the coarse
   control and the nudge buttons are the fine one. */

/** The smallest half-window, so a very short moment still has room to grow. */
export const MIN_PAD_MS = 2000;

/**
 * The span the strip covers: the detected range plus half its length either
 * side, floored at MIN_PAD_MS.
 */
export function trimWindow(detectedStartMs, detectedEndMs) {
  const start = Math.max(0, Number(detectedStartMs) || 0);
  const end = Math.max(start, Number(detectedEndMs) || 0);
  const pad = Math.max(MIN_PAD_MS, Math.round((end - start) / 2));
  return { fromMs: Math.max(0, start - pad), toMs: end + pad };
}

/** Where a timestamp sits in the window, as a percentage of its width. */
export function pctOf(ms, win) {
  const span = (win.toMs - win.fromMs) || 1;
  return Math.max(0, Math.min(100, ((ms - win.fromMs) / span) * 100));
}

/** The timestamp a fraction across the window, for a drag. */
export function msAt(fraction, win) {
  const span = win.toMs - win.fromMs;
  const clamped = Math.max(0, Math.min(1, Number(fraction) || 0));
  return Math.round(win.fromMs + clamped * span);
}

/**
 * Evenly spaced marks across the window, including both ends.
 *
 * The ruler is what makes the strip readable as time rather than as a
 * proportion — without it a handle two-thirds along says nothing about when.
 */
export function rulerTicks(win, count = 7) {
  const n = Math.max(2, count);
  const span = win.toMs - win.fromMs;
  return Array.from({ length: n }, (_, i) => Math.round(win.fromMs + (span * i) / (n - 1)));
}

/**
 * Whether a cut still matches what the analysis found.
 *
 * The editor shows the detected range beside the trimmed one, so an editor can
 * see at a glance what they changed and put it back.
 */
export function isTrimmed(cut) {
  if (!cut || cut.detectedStartMs == null) return false;
  return cut.startMs !== cut.detectedStartMs || cut.endMs !== cut.detectedEndMs;
}
