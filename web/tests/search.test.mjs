/**
 * Which moments a question asks for.
 *
 * This is the layer that was missing: the card answered every question with
 * every moment, so "show all goals" and "show me the best moments" produced
 * the same 346 rows — which looks like a filter that ran and matched
 * everything rather than one that never existed.
 *
 * Run with: node --test web/tests
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  FILTER_NOISE, filterAsked, gameNamedIn, halfSplit, selectGames,
  selectMoments, stem,
} from '../src/search.js';

const moment = (id, over = {}) => {
  const m = {
    momentId: id, momentType: 'jump_shot', label: 'Jump Shot', category: 'offense',
    actionResult: 'Goal', isGoal: true, startSec: 100,
    summary: 'A goal', highlightScore: 0.8, ...over,
  };
  return { ...m, endSec: over.endSec ?? m.startSec + 6 };
};

// A handball match as the analysis writes it: the labels are the taxonomy's,
// and the first action is half an hour in because the upload has pre-roll.
const MATCH = [
  moment('jump-1', { startSec: 1787, summary: 'Schiller scores the opening goal' }),
  moment('wing-1', { momentType: 'wing_shot', label: 'Wing Shot', startSec: 1819,
    summary: 'Häfner scores from the wing' }),
  moment('save-1', { momentType: 'double_save', label: 'Double Save', category: 'defense',
    actionResult: 'Save', isGoal: false, startSec: 2179, summary: 'Keeper double save' }),
  moment('pen-1', { momentType: 'seven_metre_penalty', label: '7-Metre Penalty',
    category: 'officiating', actionResult: 'Penalty', isGoal: false, startSec: 2586,
    summary: 'Foul leads to a 7-metre' }),
  moment('susp-1', { momentType: 'two_minute_suspension', label: '2-Minute Suspension',
    category: 'officiating', actionResult: 'Suspension', isGoal: false, startSec: 4755,
    summary: 'Rubin suspended for two minutes' }),
  moment('wing-2', { momentType: 'wing_shot', label: 'Wing Shot', actionResult: 'Saved',
    isGoal: false, startSec: 5200, summary: 'Wing shot saved late on' }),
  moment('jump-2', { startSec: 5400, summary: 'Late goal to seal it' }),
];

/** Ask a question of the match, the way attachCards does. */
const ask = (question, namedTitle = '') =>
  selectMoments(MATCH, { ...filterAsked(question, namedTitle), sort: 'time' });

const ids = (result) => result.list.map((m) => m.momentId);


describe('the prompts this was asked for', () => {
  it('show every penalties', () => {
    const r = ask('show every penalties');
    assert.deepEqual(ids(r), ['pen-1']);
    assert.equal(r.narrowed, true);
  });

  it('every wing shot in the 1st half', () => {
    // Both halves have a wing shot; only one is before the turn.
    const r = ask('every wing shot in the 1st half');
    assert.deepEqual(ids(r), ['wing-1']);
    assert.equal(r.half, 1);
  });

  it('show all goals', () => {
    assert.deepEqual(ids(ask('show all goals')), ['jump-1', 'wing-1', 'jump-2']);
  });
});


describe('a kind, not a word in it', () => {
  it('wing shot does not answer with every shot', () => {
    // Matching any word made "wing shot" mean "shot", and jump shots came back
    // as wing shots. Every word has to be there.
    assert.deepEqual(ids(ask('wing shots')), ['wing-1', 'wing-2']);
  });

  it('two kinds fall back to either', () => {
    // No moment is both a penalty and a suspension, and "and" is not a word
    // the filter keeps — so requiring every term would match nothing.
    assert.deepEqual(ids(ask('penalties and suspensions')), ['pen-1', 'susp-1']);
  });

  it('reads the taxonomy vocabulary, not a list of its own', () => {
    // "Double Save" is the label the sport profile defines. Nothing here
    // knows the word; it matches because the analysis wrote it.
    assert.deepEqual(ids(ask('show me the double save')), ['save-1']);
  });

  it('matches a plural against a singular label', () => {
    assert.deepEqual(ids(ask('suspensions')), ['susp-1']);
  });
});


describe('halves are the match, not the clock', () => {
  it('splits on what was found rather than on 30:00', () => {
    // The first action is at 29:47 because of pre-roll. A fixed half-hour
    // would put the entire first half into the second.
    assert.ok(halfSplit(MATCH) > 1787 && halfSplit(MATCH) < 5400);
    assert.deepEqual(ids(ask('goals in the 2nd half')), ['jump-2']);
  });

  it('first wave is not first half', () => {
    // "first" only means a half when the word half follows it, or a taxonomy
    // with a First Wave in it could never be searched.
    assert.equal(filterAsked('show me the first wave').half, null);
    assert.ok(filterAsked('show me the first wave').terms.includes('first'));
  });
});


describe('asking for no narrowing at all', () => {
  it('the best moments is an order, not a kind', () => {
    // The app's own suggestion chip. If "best" were a term this card would be
    // empty, which reads as an analysis that found nothing.
    const r = ask('show me the best moments');
    assert.equal(r.list.length, MATCH.length);
    assert.equal(r.narrowed, false);
    assert.equal(r.missed, false);
  });

  it('a bare request for the list', () => {
    assert.equal(ask('show all moments').list.length, MATCH.length);
  });

  it('a named fixture says which match, not which moment', () => {
    // Without dropping the title, its words match every moment of that team —
    // or none, which would empty the card.
    const r = ask('show all moments of FAG v TVB — DAIKIN HBL', 'FAG v TVB — DAIKIN HBL');
    assert.deepEqual(r.terms, []);
    assert.equal(r.list.length, MATCH.length);
  });
});


describe('a word the taxonomy does not use', () => {
  it('shows everything and says it matched nothing', () => {
    // An empty card would claim the analysis found nothing. The moments are
    // right there; it is the word that is wrong.
    const r = ask('show me every dunk');
    assert.equal(r.list.length, MATCH.length);
    assert.equal(r.missed, true);
    assert.equal(r.narrowed, false);
  });
});


describe('ordering', () => {
  it('by score', () => {
    const scored = [moment('low', { highlightScore: 0.1, startSec: 10 }),
      moment('high', { highlightScore: 0.9, startSec: 20 })];
    assert.deepEqual(
      selectMoments(scored, { sort: 'score' }).list.map((m) => m.momentId),
      ['high', 'low'],
    );
  });

  it('by time', () => {
    assert.deepEqual(ids(selectMoments(MATCH, { sort: 'time' })),
      ['jump-1', 'wing-1', 'save-1', 'pen-1', 'susp-1', 'wing-2', 'jump-2']);
  });

  it('does not reorder the caller\'s array', () => {
    const given = [...MATCH];
    selectMoments(given, { sort: 'score' });
    assert.deepEqual(given.map((m) => m.momentId), MATCH.map((m) => m.momentId));
  });
});


describe('naming a match', () => {
  const games = [
    { jobId: 'j1', title: 'FAG v TVB — DAIKIN HBL' },
    { jobId: 'j2', title: 'TBV v LEI — DAIKIN HBL' },
    { jobId: 'j3', title: 'FAG v TVB' },
  ];
  const headline = (g) => g.title;

  it('finds the fixture in the sentence', () => {
    assert.equal(gameNamedIn('Show all moments of TBV v LEI — DAIKIN HBL', games, headline).jobId, 'j2');
  });

  it('survives an em dash typed as a hyphen', () => {
    assert.equal(gameNamedIn('moments of fag v tvb - daikin hbl', games, headline).jobId, 'j1');
  });

  it('prefers the longest title', () => {
    // j3's title is a prefix of j1's and must not answer for it.
    assert.equal(gameNamedIn('moments of FAG v TVB — DAIKIN HBL', games, headline).jobId, 'j1');
    assert.equal(gameNamedIn('moments of FAG v TVB', games, headline).jobId, 'j3');
  });

  it('names nothing when nothing is named', () => {
    assert.equal(gameNamedIn('show me the best moments', games, headline), null);
    assert.equal(gameNamedIn('', games, headline), null);
  });
});


describe('the pieces', () => {
  it('stems plurals crudely, because matching is by substring', () => {
    assert.equal(stem('goals'), 'goal');
    assert.equal(stem('penalties'), 'penalty');
    assert.equal(stem('pass'), 'pass');
  });

  it('keeps the words that name a moment out of the noise list', () => {
    for (const word of ['goal', 'penalty', 'save', 'block', 'shot', 'wing',
      'suspension', 'break', 'steal', 'card', 'timeout']) {
      assert.ok(!FILTER_NOISE.has(word), `${word} must stay searchable`);
    }
  });
});


describe('filtering the games list by sport', () => {
  // What the analysis wrote: a sport, and for equestrian a discipline it read
  // off the footage. The filter matches those rather than a list kept in the UI,
  // so a sport added tomorrow is searchable the day it is added.
  const DESK = [
    { jobId: 'h1', sport: 'handball', title: 'FAG v TVB — DAIKIN HBL',
      competition: 'DAIKIN HBL', homeTeam: 'FAG', awayTeam: 'TVB' },
    { jobId: 'h2', sport: 'handball', title: 'TBV v LEI — DAIKIN HBL',
      competition: 'DAIKIN HBL', homeTeam: 'TBV', awayTeam: 'LEI' },
    { jobId: 'e1', sport: 'equestrian', discipline: 'Jumping',
      title: 'Jumping — CSI Aachen', competition: 'CSI Aachen' },
    { jobId: 'e2', sport: 'equestrian', discipline: 'Dressage',
      title: 'Dressage — CDI Aachen', competition: 'CDI Aachen' },
    { jobId: 'e3', sport: 'equestrian', discipline: 'Para-Dressage',
      title: 'Para-Dressage — CPEDI Aachen', competition: 'CPEDI Aachen' },
  ];

  const ask = (question) => selectGames(DESK, filterAsked(question));
  const ids = (r) => r.list.map((g) => g.jobId);

  it('show all handball games', () => {
    assert.deepEqual(ids(ask('show all handball games')), ['h1', 'h2']);
  });

  it('show all handball game details', () => {
    // "details" says how to show the list, not what to put in it. Treating it
    // as a term would match nothing and show everything.
    assert.deepEqual(ids(ask('show all handball game details')), ['h1', 'h2']);
  });

  it('show all equestrian games', () => {
    assert.deepEqual(ids(ask('show all equestrian games')), ['e1', 'e2', 'e3']);
  });

  it('show all equestrian game details', () => {
    assert.deepEqual(ids(ask('show all equestrian game details')), ['e1', 'e2', 'e3']);
  });

  it('narrows to a discipline the same way', () => {
    // Nothing here knows what a discipline is; it matches because the analysis
    // wrote "Dressage" onto the record.
    assert.deepEqual(ids(ask('show all dressage games')), ['e2', 'e3']);
    assert.deepEqual(ids(ask('show all para dressage games')), ['e3']);
  });

  it('finds a competition, a team and a venue too', () => {
    assert.deepEqual(ids(ask('games at CSI Aachen')), ['e1']);
    assert.deepEqual(ids(ask('every game TBV played')), ['h2']);
  });

  it('a plain request for the list is not a filter', () => {
    const r = ask('show all games');
    assert.equal(r.list.length, DESK.length);
    assert.equal(r.narrowed, false);
    assert.equal(r.missed, false);
  });

  it('a sport nobody has shows everything and says it matched nothing', () => {
    // An empty card would claim there are no games when there are five.
    const r = ask('show all polo games');
    assert.equal(r.list.length, DESK.length);
    assert.equal(r.missed, true);
  });

  it('does not reorder the caller\'s array', () => {
    const given = [...DESK];
    selectGames(given, { terms: ['handball'] });
    assert.deepEqual(given.map((g) => g.jobId), DESK.map((g) => g.jobId));
  });
});
