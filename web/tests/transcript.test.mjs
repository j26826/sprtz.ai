import { test } from 'node:test';
import assert from 'node:assert/strict';

import { currentTurn } from '../src/transcript.js';

const you = (text) => ({ who: 'you', text });
const agent = (text) => ({ who: 'agent', text });

test('the opener stands alone until something is asked', () => {
  const msgs = [agent('What would you like to work on?')];
  assert.deepEqual(currentTurn(msgs), [[msgs[0], 0]]);
});

test('only the last question and its answer are on screen', () => {
  const msgs = [agent('opener'), you('first'), agent('one'), you('second'), agent('two')];
  assert.deepEqual(currentTurn(msgs).map(([m]) => m.text), ['second', 'two']);
});

test('every message keeps its own index, because that is what the buttons use', () => {
  const msgs = [agent('opener'), you('first'), agent('one'), you('second'), agent('two')];
  assert.deepEqual(currentTurn(msgs).map(([, i]) => i), [3, 4]);
});

test('a question still waiting for its answer is shown on its own', () => {
  const msgs = [you('first'), agent('one'), you('second')];
  assert.deepEqual(currentTurn(msgs).map(([, i]) => i), [2]);
});

test('an answer in several parts stays whole', () => {
  const msgs = [you('cut these'), agent('clips'), agent('and the reel')];
  assert.deepEqual(currentTurn(msgs).map(([, i]) => i), [0, 1, 2]);
});

test('nothing at all is nothing, not a crash', () => {
  assert.deepEqual(currentTurn([]), []);
  assert.deepEqual(currentTurn(null), []);
});
