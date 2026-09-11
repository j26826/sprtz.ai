import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  groupByRide, notesForRide, rideNamedIn, rideScoreAsked, ridesAsked,
} from '../src/ridegroups.js';

const event = {
  riders: [
    { order: 1, rider: 'Anna Berger', horse: 'Lumière', moments: [{ momentId: 'a' }, { momentId: 'b' }] },
    { order: 2, rider: 'Jonas Keller', horse: 'Falkenstein', moments: [{ momentId: 'c' }] },
    { order: 3, rider: 'Marie Duval', horse: 'Cassiopeia', moments: [] },
  ],
};
const ids = (group) => group.moments.map((m) => m.momentId);

test('each ride gets its moments, in running order', () => {
  const groups = groupByRide(event, [{ momentId: 'a' }, { momentId: 'c' }, { momentId: 'b' }]);
  assert.deepEqual(groups.map((g) => g.ride.rider), ['Anna Berger', 'Jonas Keller', 'Marie Duval']);
  assert.deepEqual(groups.map(ids), [['a', 'b'], ['c'], []]);
});

test('the order within a ride is the list order, so the chosen sort survives', () => {
  const groups = groupByRide(event, [{ momentId: 'b' }, { momentId: 'a' }]);
  assert.deepEqual(ids(groups[0]), ['b', 'a']);
});

test('a ride with nothing in it is still shown when nothing is filtered', () => {
  const groups = groupByRide(event, [{ momentId: 'a' }]);
  assert.equal(groups.length, 3);
  assert.deepEqual(ids(groups[2]), []);
});

test('a filtered list drops the rides with nothing matching', () => {
  const groups = groupByRide(event, [{ momentId: 'c' }], { filtered: true });
  assert.deepEqual(groups.map((g) => g.ride.rider), ['Jonas Keller']);
});

test('a moment outside every ride goes last, under no ride, and is not dropped', () => {
  const groups = groupByRide(event, [{ momentId: 'a' }, { momentId: 'prize-giving' }]);
  const last = groups[groups.length - 1];
  assert.equal(last.ride, null);
  assert.deepEqual(ids(last), ['prize-giving']);
});

test('no outside group when every moment has a ride', () => {
  const groups = groupByRide(event, [{ momentId: 'a' }, { momentId: 'c' }]);
  assert.ok(groups.every((g) => g.ride));
});

test('a moment newer than the tree is placed by the ride order it carries', () => {
  const groups = groupByRide(event, [{ momentId: 'new', rideOrder: 2 }]);
  assert.deepEqual(ids(groups[1]), ['new']);
});

test('no event, or one without rides, puts everything outside', () => {
  for (const e of [null, {}, { riders: [] }]) {
    const groups = groupByRide(e, [{ momentId: 'a' }]);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].ride, null);
  }
});


const ride = (order, rider, horse, totalPct, scoreCheck = 'ok') => ({
  order, rider, horse, result: { totalPct, scoreCheck },
});

test('a ride is named by its whole rider, its whole horse, or the rider\'s surname', () => {
  const r = ride(1, 'Gareth Hughes', 'Classic Briolinca', 74.1);
  assert.ok(rideNamedIn('show me the ride for Gareth Hughes', r));
  assert.ok(rideNamedIn("show me Hughes' ride", r));
  assert.ok(rideNamedIn('how did classic briolinca go', r));
  assert.ok(!rideNamedIn('show me the ride for Anna Berger', r));
  // Half a horse's name is a word, not the horse.
  assert.ok(!rideNamedIn('a classic ride', r));
});

test('accents do not stand between a name and the way it was typed', () => {
  assert.ok(rideNamedIn('the ride on lumiere', ride(1, 'Anna Berger', 'Lumière', 70)));
});

test('a one-word rider is matched by that whole word only, never as a surname', () => {
  assert.ok(rideNamedIn('show me Mozart', ride(1, 'Mozart', '', 70)));
  assert.ok(!rideNamedIn('show me the most', ride(1, 'Mo', 'X', 70)));
});

test('the score bar: over is strict, at least and or more are not', () => {
  assert.deepEqual(rideScoreAsked('rides scoring more than 70%'), { min: 70, inclusive: false });
  assert.deepEqual(rideScoreAsked('rides over 72.5 percent'), { min: 72.5, inclusive: false });
  assert.deepEqual(rideScoreAsked('riders with at least 70%'), { min: 70, inclusive: true });
  assert.deepEqual(rideScoreAsked('tests of 75% or more'), { min: 75, inclusive: true });
  assert.deepEqual(rideScoreAsked('rides above 68,5'), { min: 68.5, inclusive: false });
  assert.equal(rideScoreAsked('show me all rides'), null);
});

const groups = [
  { ride: ride(1, 'Anna Berger', 'Lumière', 74.37), moments: [{ momentId: 'a' }] },
  { ride: ride(2, 'Jonas Keller', 'Falkenstein', 76.02), moments: [] },
  { ride: ride(3, 'Marie Duval', 'Cassiopeia', 71.85, 'mismatch: 3 marks average 71.700'), moments: [] },
  { ride: ride(4, 'Gareth Hughes', 'Classic Briolinca', 69.9), moments: [] },
  { ride: null, moments: [{ momentId: 'prize' }] },
];

test('a name narrows to that rider\'s ride and says who', () => {
  const out = ridesAsked(groups, 'show me the ride for Gareth Hughes');
  assert.deepEqual(out.groups.map((g) => g.ride.order), [4]);
  assert.deepEqual(out.names, ['Gareth Hughes']);
  assert.ok(out.narrowed);
});

test('a score bar keeps the rides over it, best first, and counts the untrusted one', () => {
  const out = ridesAsked(groups, 'rides scoring more than 70%');
  assert.deepEqual(out.groups.map((g) => g.ride.rider), ['Jonas Keller', 'Anna Berger']);
  assert.equal(out.unchecked, 1, 'Duval\'s 71.85 does not match its own marks');
});

test('exactly on the bar is in for at least, out for more than', () => {
  const at = [{ ride: ride(1, 'A B', 'H', 70), moments: [] }];
  assert.equal(ridesAsked(at, 'rides at least 70%').groups.length, 1);
  assert.equal(ridesAsked(at, 'rides more than 70%').groups.length, 0);
});

test('a name and a bar together narrow by both', () => {
  const out = ridesAsked(groups, 'did Gareth Hughes score over 70%');
  assert.equal(out.groups.length, 0);
});

test('no name and no bar is every ride, in running order, without the outside group', () => {
  const out = ridesAsked(groups, 'show all rides');
  assert.deepEqual(out.groups.map((g) => g.ride.order), [1, 2, 3, 4]);
  assert.ok(!out.narrowed);
});


test('only the not-confirmed notes from a ride\'s own windows are about that ride', () => {
  const record = [
    { momentType: 'pirouette', notes: ['[segment 0] two candidates near 07:40', '[segment 3] a corner at 02:10'] },
    { momentType: 'passage', notes: [] },
    { momentType: 'piaffe', notes: ['[segment 3] no piaffe in this window'] },
  ];
  assert.deepEqual(notesForRide(record, [0, 1]),
    [{ momentType: 'pirouette', notes: ['two candidates near 07:40'] }]);
  assert.deepEqual(notesForRide(record, []), []);
  assert.deepEqual(notesForRide(null, [0]), []);
});
