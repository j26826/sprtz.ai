/**
 * What the preview player plays for a moment.
 *
 * A moment's in point is deliberately a second or two of run-up and its out
 * point is the end of the play; the preview shows three seconds either side
 * of both, because the editor is judging the play in its context — the
 * approach before it and what happened after. The record still says the
 * moment's own times; only the playback is wider. Pure, so `node --test`
 * reaches it.
 */

export const PREVIEW_PAD_SEC = 3;

/**
 * The range to play for a moment (or a clip's own trim), padded either side.
 * Clamped at zero and, when the match's length is known, at its end.
 */
export function playRange(start, end, { pad = PREVIEW_PAD_SEC, duration = 0 } = {}) {
  const from = Math.max(0, Number(start) - pad);
  let to = Number(end) + pad;
  if (duration > 0) to = Math.min(to, duration);
  return { start: from, end: Math.max(to, from) };
}


/**
 * The speeds the speed control steps through. Slower first: judging a
 * movement — a piaffe's rhythm, a foot on the line — means watching it slowly,
 * and faster than real time is the rarer ask.
 */
export const SPEEDS = [1, 0.5, 0.25, 2];

export function nextSpeed(rate) {
  const i = SPEEDS.indexOf(Number(rate));
  return SPEEDS[(i + 1) % SPEEDS.length];
}


/** How far one press of Widen opens the range, each side. */
export const WIDEN_SEC = 5;

/**
 * A range opened out either side: the approach before a moment and what came
 * after it, a little more with each press. Clamped like playRange.
 */
export function widen(range, { by = WIDEN_SEC, duration = 0 } = {}) {
  const start = Math.max(0, Number(range.start) - by);
  let end = Number(range.end) + by;
  if (duration > 0) end = Math.min(end, duration);
  return { start, end: Math.max(end, start) };
}


/** A time kept inside the range being played. */
export function clampTo(range, at) {
  return Math.min(Math.max(Number(at), Number(range.start)), Number(range.end));
}


/** Seconds as m:ss (h:mm:ss past the hour), for a position inside a range. */
export function shortClock(sec) {
  const t = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = String(t % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}



/**
 * What the scrubber spans: the whole ride the range is in, or the range alone.
 *
 * A moment is judged in its round — what led into the piaffe, what came after
 * the halt — so the bar is the ride and the moment is marked inside it. The
 * span always covers the range, so a range widened past the ride's own edges
 * is never drawn off the end of its bar.
 *
 * @param {{start:number,end:number}} range  What is playing.
 * @param {{startSec:number,endSec:number}|null} ride
 */
export function playerTimeline(range, ride) {
  const start = Number(range.start);
  const end = Number(range.end);
  if (!ride) return { start, end };
  return {
    start: Math.min(start, Number(ride.startSec)),
    end: Math.max(end, Number(ride.endSec)),
  };
}


/**
 * Where the range sits on the bar, as percentages, or null when the range is
 * the whole bar (a full ride, or a moment with no ride) and there is nothing
 * to mark.
 */
export function rangeBand(range, timeline) {
  const span = Number(timeline.end) - Number(timeline.start);
  if (span <= 0) return null;
  const left = (Number(range.start) - Number(timeline.start)) / span;
  const width = (Number(range.end) - Number(range.start)) / span;
  if (left <= 0.0005 && width >= 0.999) return null;
  return {
    left: Math.max(0, Math.min(100, left * 100)),
    width: Math.max(0, Math.min(100 - left * 100, width * 100)),
  };
}
