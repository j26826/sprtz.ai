import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  PREVIEW_PAD_SEC, SPEEDS, clampTo, nextSpeed, playRange, shortClock, sourceReference, widen,
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

test('the reference names the file, the offset and the length, and cuts without re-encoding', () => {
  const ref = sourceReference('psg-720p.mp4', { start: 7606, end: 7912 }, 'ride-12');
  assert.equal(ref.at, 'psg-720p.mp4 @ 7606.0s + 306.0s');
  assert.equal(ref.command, 'ffmpeg -ss 7606.0 -t 306.0 -i psg-720p.mp4 -c copy psg-720p-ride-12.mp4');
});

test('a file name with spaces is quoted so the line pastes as it is', () => {
  const ref = sourceReference("Day 2 Arena's feed.mov", { start: 1, end: 2 }, 'Piaffe 482');
  assert.equal(ref.command,
    "ffmpeg -ss 1.0 -t 1.0 -i 'Day 2 Arena'\\''s feed.mov' -c copy 'Day 2 Arena'\\''s feed-piaffe-482.mp4'");
});
