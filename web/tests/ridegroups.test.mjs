import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  countTypesIn, filterByTypes, groupByRide, hasRides, momentTypesIn,
  rideNamedIn, rideRank, rideScoreAsked, ridesAsked, sortRideGroups,
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


/* ── the second axis: moment type ─────────────────────────────────────────── */

const board = [
  {
    ride: { order: 1, rider: 'Loretta Joynson' },
    moments: [
      { momentId: 'a', momentType: 'half_pass', label: 'Half-pass' },
      { momentId: 'b', momentType: 'piaffe', label: 'Piaffe' },
    ],
  },
  {
    ride: { order: 2, rider: 'Jonas Keller' },
    moments: [{ momentId: 'c', momentType: 'half_pass', label: 'Half-pass' }],
  },
  { ride: { order: 3, rider: 'Marie Duval' }, moments: [] },
];

test('the types offered are the ones the moments carry, commonest first', () => {
  assert.deepEqual(momentTypesIn(board), [
    { key: 'half_pass', label: 'Half-pass', count: 2 },
    { key: 'piaffe', label: 'Piaffe', count: 1 },
  ]);
});

test('two types seen equally often are in a stable alphabetical order', () => {
  const groups = [{ moments: [{ momentType: 'z', label: 'Zig' }, { momentType: 'a', label: 'Alpha' }] }];
  assert.deepEqual(momentTypesIn(groups).map((ty) => ty.label), ['Alpha', 'Zig']);
});

test('a moment with no code is keyed by its label rather than dropped', () => {
  assert.deepEqual(momentTypesIn([{ moments: [{ label: 'Halt and salute' }] }]),
    [{ key: 'Halt and salute', label: 'Halt and salute', count: 1 }]);
});

test('a moment with neither a code nor a label is not a type', () => {
  assert.deepEqual(momentTypesIn([{ moments: [{ momentId: 'x' }] }]), []);
});

test('no types offered for no rides', () => {
  assert.deepEqual(momentTypesIn(null), []);
});

test('choosing nothing is the whole ride, not an empty one', () => {
  assert.deepEqual(filterByTypes(board, []), board);
  assert.deepEqual(filterByTypes(board, null), board);
});

test('choosing a type narrows each ride to it', () => {
  const shown = filterByTypes(board, ['half_pass']);
  assert.deepEqual(shown.map((g) => g.moments.map((m) => m.momentId)), [['a'], ['c'], []]);
});

test('every ride stays in the rail, so the list cannot move under the pointer', () => {
  const shown = filterByTypes(board, ['piaffe']);
  assert.deepEqual(shown.map((g) => g.ride.rider),
    ['Loretta Joynson', 'Jonas Keller', 'Marie Duval']);
  // And the ones left with nothing say nothing rather than vanishing.
  assert.deepEqual(shown.slice(1).map((g) => g.moments.length), [0, 0]);
});

test('several types are one question, and the ride keeps both', () => {
  const shown = filterByTypes(board, ['half_pass', 'piaffe']);
  assert.deepEqual(shown.map((g) => g.moments.map((m) => m.momentId)), [['a', 'b'], ['c'], []]);
});

test('the count beside a type is the open ride\'s, not the event\'s', () => {
  assert.deepEqual(countTypesIn(board[0].moments), { half_pass: 1, piaffe: 1 });
  assert.deepEqual(countTypesIn(board[1].moments), { half_pass: 1 });
  assert.deepEqual(countTypesIn(board[2].moments), {});
});

test('a moment with neither a code nor a label is counted as no type', () => {
  assert.deepEqual(countTypesIn([{ momentId: 'x' }, { label: 'Piaffe' }]), { Piaffe: 1 });
});

test('filtering never writes back to the groups it was given', () => {
  filterByTypes(board, ['piaffe']);
  assert.equal(board[0].moments.length, 2);
});


/* ── ordering the rail ────────────────────────────────────────────────────── */

const day = [
  { ride: { order: 1, rider: 'Susanna Wade' },
    moments: [{ momentId: 'a', highlightScore: 0.61 }, { momentId: 'b', highlightScore: 0.72 }] },
  { ride: { order: 2, rider: 'Elan Williams' }, moments: [] },
  { ride: { order: 3, rider: 'Loretta Joynson' },
    moments: [{ momentId: 'c', highlightScore: 0.94 }] },
  { ride: { order: 4, rider: 'Marcus Ainsley' }, moments: [] },
];
const riders = (groups) => groups.map((g) => g.ride.rider);

test('match order is the running order, untouched', () => {
  assert.deepEqual(riders(sortRideGroups(day, 'time')), riders(day));
});

test('best first puts the ride holding the strongest moment at the top', () => {
  assert.deepEqual(riders(sortRideGroups(day, 'score')),
    ['Loretta Joynson', 'Susanna Wade', 'Elan Williams', 'Marcus Ainsley']);
});

test('a ride is ranked by its best moment, not by how many it has', () => {
  const groups = [
    { ride: { order: 1, rider: 'Many' },
      moments: [{ highlightScore: 0.4 }, { highlightScore: 0.4 }, { highlightScore: 0.4 }] },
    { ride: { order: 2, rider: 'One good one' }, moments: [{ highlightScore: 0.9 }] },
  ];
  assert.deepEqual(riders(sortRideGroups(groups, 'score')), ['One good one', 'Many']);
});

test('rides the sort cannot tell apart keep their running order', () => {
  // Stable: two rounds that found nothing must not swap places between renders.
  assert.deepEqual(riders(sortRideGroups(day, 'score')).slice(2),
    ['Elan Williams', 'Marcus Ainsley']);
});

test('ordering never writes back to the groups it was given', () => {
  sortRideGroups(day, 'score');
  assert.deepEqual(riders(day),
    ['Susanna Wade', 'Elan Williams', 'Loretta Joynson', 'Marcus Ainsley']);
});

test('an unscored moment does not lift its ride', () => {
  const groups = [
    { ride: { order: 1, rider: 'Unscored' }, moments: [{ momentId: 'x' }] },
    { ride: { order: 2, rider: 'Scored' }, moments: [{ highlightScore: 0.1 }] },
  ];
  assert.deepEqual(riders(sortRideGroups(groups, 'score')), ['Scored', 'Unscored']);
});

test('nothing to order is nothing, not a crash', () => {
  assert.deepEqual(sortRideGroups(null, 'score'), []);
});


test('a rank is a whole placing from 1 up, and anything else is not a rank yet', () => {
  const r = (place) => ({ result: { place } });
  assert.equal(rideRank(r(1)), 1);
  assert.equal(rideRank(r(12)), 12);
  for (const none of [null, undefined, 0, -1, 2.5, '', 'first', NaN]) {
    assert.equal(rideRank(r(none)), null, `place ${String(none)}`);
  }
  assert.equal(rideRank({}), null);
  assert.equal(rideRank(null), null);
});


// `hasRides` is how the desk decides an event is a competition day, and it
// decides two things: whether the moments card becomes the board, and whether
// a best-moments question about a single event is answered by the board
// rather than by the desk shortlist. Getting it wrong either way is a whole
// screen: a handball match on a rail of riders, or a dressage day as a flat
// grid of two hundred tiles.
test('a game with rounds ridden in it is a competition day', () => {
  assert.equal(hasRides({ rides: [{ order: 1, rider: 'Anna Berger' }] }), true);
  assert.equal(hasRides({ rides: [{}, {}, {}] }), true);
});

test('anything without rounds is not, however it says so', () => {
  // A handball match, an equestrian day whose analysis has not produced a ride
  // yet, and every shape a missing field arrives in. An empty rail is not a
  // board, so none of these may route to one.
  assert.equal(hasRides({ sport: 'handball', rides: [] }), false);
  assert.equal(hasRides({ sport: 'equestrian' }), false);
  assert.equal(hasRides({ rides: null }), false);
  assert.equal(hasRides({ rides: 0 }), false);
  // A record whose `rides` is an object rather than a list — which is what a
  // half-written document looks like — is not a list of rounds, and `.length`
  // on it would be undefined rather than an error, so the Array check is what
  // stops it reading as truthy further down.
  assert.equal(hasRides({ rides: { 0: { order: 1 } } }), false);
  assert.equal(hasRides({}), false);
  assert.equal(hasRides(null), false);
  assert.equal(hasRides(undefined), false);
});
