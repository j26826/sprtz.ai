/**
 * Live events, the part that needs no browser.
 *
 * What the ingest panel checks before it lets someone schedule an event, and
 * what the jobs card derives from a live job's document to draw its progress.
 * Pure functions over plain values, so `node --test` reaches them — the same
 * reason search.js and cards.js are separate from app.js.
 *
 * Two figures here mirror the agent's settings and must move with them:
 * LIVE_LEAD_SEC is SPRTZ_LIVE_LEAD_SECONDS and DEFAULT_CHUNK_SEC is the
 * live_chunk_seconds Terraform variable. The job document carries its own
 * chunkSec once the capture has started, so the default only matters before.
 */

export const LIVE_LEAD_SEC = 300;
export const DEFAULT_CHUNK_SEC = 300;
export const MAX_LIVE_HOURS = 12;

/** An https URL with a host. Whether it is a playlist is for the recorder to find out. */
export function isHlsUrl(value) {
  return /^https:\/\/[^\s/?#]+\S*$/.test((value || '').trim());
}

/**
 * Why a live event cannot be scheduled yet, as an i18n key — or null.
 *
 * `start` and `end` are whatever the datetime-local inputs hold, parsed by
 * Date.parse, so they are read in the browser's own zone; the API gets UTC.
 */
export function validateLiveEvent({ hlsUrl, start, end, now = Date.now() }) {
  if (!isHlsUrl(hlsUrl)) return 'live.error.url';
  if (!start || !end) return 'live.error.times';
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (Number.isNaN(s) || Number.isNaN(e)) return 'live.error.times';
  if (e <= s) return 'live.error.order';
  if (e <= now) return 'live.error.past';
  if (e - s > MAX_LIVE_HOURS * 3600e3) return 'live.error.long';
  return null;
}

/** How many chunks the whole recording will be, lead-in included. */
export function expectedChunks(live) {
  const s = Date.parse(live?.eventStart);
  const e = Date.parse(live?.eventEnd);
  if (Number.isNaN(s) || Number.isNaN(e) || e <= s) return 1;
  const chunk = Number(live.chunkSec) || DEFAULT_CHUNK_SEC;
  return Math.max(1, Math.ceil(((e - s) / 1000 + LIVE_LEAD_SEC) / chunk));
}

/**
 * The figures the jobs card draws a live job from.
 *
 * `state` is the event's own state, not the job status: a live job's status
 * says "analyzing" for the whole event, which is true and useless.
 */
export function liveSummary(job) {
  const live = job?.live || {};
  const captured = Number(live.chunksCaptured) || 0;
  const analysed = Math.min(Number(live.chunksAnalysed) || 0, captured || Infinity);
  return {
    state: live.state || 'scheduled',
    start: live.eventStart || '',
    end: live.eventEnd || '',
    expected: expectedChunks(live),
    captured,
    analysed: Number.isFinite(analysed) ? analysed : 0,
    waiting: Math.max(0, captured - (Number(live.chunksAnalysed) || 0)),
    moments: Number(job?.counts?.moments) || 0,
    restarts: Number(live.captureRestarts) || 0,
  };
}

/** Fill (0-100) of each of the three live stages, for the strip. */
export function liveStageFills(summary) {
  const done = summary.state === 'complete';
  const share = (n) => Math.max(0, Math.min(100, (n / Math.max(1, summary.expected)) * 100));
  return {
    scheduled: summary.state === 'scheduled' ? 0 : 100,
    capture: done ? 100 : share(summary.captured),
    analysis: done ? 100 : share(summary.analysed),
  };
}
