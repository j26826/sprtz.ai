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


const SAFE = /^[\w.\/-]+$/;
const quote = (text) => (SAFE.test(text) ? text : `'${text.replace(/'/g, "'\\''")}'`);

/**
 * Where a range sits in the source file, and the command that cuts it out.
 *
 * For an editor who takes the footage elsewhere: the file, the offset and the
 * length, then the ffmpeg line that copies exactly that span without
 * re-encoding. Names with spaces are quoted so the line pastes as it is.
 *
 * @param {string} file     The source's own file name.
 * @param {{start:number,end:number}} range
 * @param {string} suffix   What the cut is, for its file name: "ride-4", "piaffe-482".
 */
export function sourceReference(file, range, suffix) {
  const name = file || 'source.mp4';
  const start = Number(range.start).toFixed(1);
  const length = Math.max(0, Number(range.end) - Number(range.start)).toFixed(1);
  const stem = name.replace(/\.[^./]+$/, '');
  const slug = String(suffix || 'cut').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const out = `${stem}-${slug}.mp4`;
  return {
    at: `${name} @ ${start}s + ${length}s`,
    command: `ffmpeg -ss ${start} -t ${length} -i ${quote(name)} -c copy ${quote(out)}`,
  };
}
