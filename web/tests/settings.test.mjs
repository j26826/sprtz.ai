import { test } from 'node:test';
import assert from 'node:assert/strict';

import { clampStallMinutes } from '../src/settings.js';

// The API refuses a stall limit outside 1..240 minutes. A value it would reject
// is better corrected where it was typed than found as a failed booking an hour
// before the event.

test('a sensible value is kept', () => {
  assert.equal(clampStallMinutes(5), 5);
  assert.equal(clampStallMinutes('12'), 12);
});

test('under a minute becomes a minute: one slow segment is not the end of an event', () => {
  assert.equal(clampStallMinutes(0), 1);
  assert.equal(clampStallMinutes(-3), 1);
});

test('past four hours becomes four hours', () => {
  assert.equal(clampStallMinutes(900), 240);
});

test('a fraction is rounded, because the field steps in whole minutes', () => {
  assert.equal(clampStallMinutes(4.6), 5);
});

test('nonsense falls back to the default rather than to nothing', () => {
  assert.equal(clampStallMinutes('five'), 5);
  assert.equal(clampStallMinutes(undefined), 5);
});
