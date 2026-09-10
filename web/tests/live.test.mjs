import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  LIVE_LEAD_SEC, expectedChunks, isHlsUrl, liveStageFills, liveSummary, validateLiveEvent,
} from '../src/live.js';

const NOW = Date.parse('2026-09-10T12:00:00Z');
const later = (h) => new Date(NOW + h * 3600e3).toISOString();

test('only an https URL with a host is a stream', () => {
  assert.equal(isHlsUrl('https://cdn.example.com/live/master.m3u8?token=abc'), true);
  assert.equal(isHlsUrl('http://cdn.example.com/live.m3u8'), false, 'plain http can be steered at the metadata server');
  assert.equal(isHlsUrl('cdn.example.com/live.m3u8'), false);
  assert.equal(isHlsUrl(''), false);
});

test('a valid window passes and each fault names its own key', () => {
  const ok = { hlsUrl: 'https://x.test/l.m3u8', start: later(1), end: later(3), now: NOW };
  assert.equal(validateLiveEvent(ok), null);
  assert.equal(validateLiveEvent({ ...ok, hlsUrl: 'ftp://x' }), 'live.error.url');
  assert.equal(validateLiveEvent({ ...ok, end: '' }), 'live.error.times');
  assert.equal(validateLiveEvent({ ...ok, end: later(0.5) }), 'live.error.order');
  assert.equal(validateLiveEvent({ ...ok, start: later(-3), end: later(-1) }), 'live.error.past');
  assert.equal(validateLiveEvent({ ...ok, end: later(14) }), 'live.error.long');
});

test('an event already under way can still be scheduled', () => {
  // The tick picks it up on the next minute; refusing it would refuse the
  // common case of someone scheduling after the stream has started.
  assert.equal(validateLiveEvent({
    hlsUrl: 'https://x.test/l.m3u8', start: later(-0.5), end: later(1), now: NOW,
  }), null);
});

test('expected chunks count the lead-in', () => {
  const live = { eventStart: later(0), eventEnd: later(1), chunkSec: 300 };
  assert.equal(expectedChunks(live), (3600 + LIVE_LEAD_SEC) / 300);
  assert.equal(expectedChunks({}), 1, 'no window is still one chunk, never zero');
});

test('the summary reads the event state, not the job status', () => {
  const job = {
    status: 'analyzing',
    counts: { moments: 41 },
    live: { state: 'live', chunksCaptured: 5, chunksAnalysed: 3, eventStart: later(0), eventEnd: later(1) },
  };
  const s = liveSummary(job);
  assert.equal(s.state, 'live');
  assert.deepEqual([s.captured, s.analysed, s.waiting, s.moments], [5, 3, 2, 41]);
});

test('the strip fills follow the chunks and finish together', () => {
  // 55 minutes plus the five-minute lead-in is exactly twelve chunks.
  const s = liveSummary({ live: { state: 'live', chunksCaptured: 6, chunksAnalysed: 3, eventStart: later(0), eventEnd: later(55 / 60), chunkSec: 300 } });
  const fills = liveStageFills(s);
  assert.equal(fills.scheduled, 100);
  assert.equal(fills.capture, 50);
  assert.equal(fills.analysis, 25);
  assert.deepEqual(liveStageFills({ ...s, state: 'complete' }), { scheduled: 100, capture: 100, analysis: 100 });
  assert.equal(liveStageFills(liveSummary({ live: { state: 'scheduled' } })).scheduled, 0);
});
