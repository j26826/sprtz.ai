import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  PREVIEW_PAD_SEC, SPEEDS, clampTo, nextSpeed, playRange, playerTimeline, rangeBand, shortClock, widen,
} from '../src/player.js';

test('the player shows three seconds either side of the moment', () => {
  assert.equal(PREVIEW_PAD_SEC, 3);
  assert.deepEqual(playRange(100, 112), { start: 97, end: 115 });
});

test('the pad never starts before the match does', () => {
  assert.deepEqual(playRange(1.5, 10), { start: 0, end: 13 });
});

test('the pad never runs past the end of the match when it is known', () => {
  assert.deepEqual(playRange(13490, 13497, { duration: 13497.6 }), { start: 13487, end: 13497.6 });
  assert.deepEqual(playRange(13490, 13497), { start: 13487, end: 13500 });
});

test("a clip's own trim is padded the same way", () => {
  assert.deepEqual(playRange(40.5, 55.25, { pad: 3 }), { start: 37.5, end: 58.25 });
});


test('speed steps slower first, then back round to real time', () => {
  assert.deepEqual(SPEEDS.map(nextSpeed), [0.5, 0.25, 2, 1]);
  assert.equal(nextSpeed(3), SPEEDS[0], 'an unknown rate starts the cycle');
});

test('widen opens five seconds either side, inside the recording', () => {
  assert.deepEqual(widen({ start: 100, end: 110 }), { start: 95, end: 115 });
  assert.deepEqual(widen({ start: 2, end: 110 }, { duration: 112 }), { start: 0, end: 112 });
});

test('a seek stays inside the range being played', () => {
  const r = { start: 10, end: 20 };
  assert.equal(clampTo(r, 5), 10);
  assert.equal(clampTo(r, 25), 20);
  assert.equal(clampTo(r, 14.5), 14.5);
});

test('positions read as m:ss, and h:mm:ss past the hour', () => {
  assert.equal(shortClock(1), '0:01');
  assert.equal(shortClock(306), '5:06');
  assert.equal(shortClock(7606), '2:06:46');
  assert.equal(shortClock(-3), '0:00');
});



test('the bar is the ride, and always covers what is playing', () => {
  const ride = { startSec: 700, endSec: 1100 };
  assert.deepEqual(playerTimeline({ start: 719, end: 735 }, ride), { start: 700, end: 1100 });
  assert.deepEqual(playerTimeline({ start: 690, end: 735 }, ride), { start: 690, end: 1100 });
  assert.deepEqual(playerTimeline({ start: 719, end: 735 }, null), { start: 719, end: 735 });
});

test('the moment is marked where it sits on the ride', () => {
  const band = rangeBand({ start: 800, end: 900 }, { start: 700, end: 1100 });
  assert.equal(band.left, 25);
  assert.equal(band.width, 25);
});

test('nothing is marked when the range is the whole bar', () => {
  assert.equal(rangeBand({ start: 700, end: 1100 }, { start: 700, end: 1100 }), null);
  assert.equal(rangeBand({ start: 5, end: 5 }, { start: 5, end: 5 }), null);
});
