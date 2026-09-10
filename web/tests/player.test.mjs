import { test } from 'node:test';
import assert from 'node:assert/strict';

import { PREVIEW_PAD_SEC, playRange } from '../src/player.js';

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
