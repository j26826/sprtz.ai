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
