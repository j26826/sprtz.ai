import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  disciplinesFor, findGames, gamesInScope, scopeContextLine, scopeFilters, scopeTitle,
  sportsAvailable,
} from '../src/scope.js';

const games = [
  { jobId: 'a', title: 'SWE v DEN — EHF Euro', sport: 'handball' },
  { jobId: 'b', title: 'Grand Prix Freestyle', sport: 'equestrian', discipline: 'Dressage' },
  { jobId: 'c', title: 'CSI Aachen', sport: 'Equestrian', discipline: 'Show Jumping' },
  { jobId: 'd', title: 'Somerford Kür', sport: 'equestrian', discipline: 'Dressage' },
];
const headline = (g) => g.title;
const t = (k) => ({
  'scope.titleAll': 'All games',
  'scope.titleAllDisciplines': 'all disciplines',
  'scope.titleGames': '{n} games',
}[k] || k);

test('sports come from the desk and the configuration, once each, lower-cased', () => {
  assert.deepEqual(sportsAvailable(games, ['Handball', 'football']), ['handball', 'equestrian', 'football']);
});

test('disciplines are what the desk has seen for that sport', () => {
  assert.deepEqual(disciplinesFor(games, 'Equestrian'), ['Dressage', 'Show Jumping']);
  assert.deepEqual(disciplinesFor(games, 'handball'), []);
});

test('a scope narrows the games', () => {
  const ids = (s) => gamesInScope(s, games).map((g) => g.jobId);
  assert.deepEqual(ids(null), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ids({ kind: 'all' }), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ids({ kind: 'category', sport: 'equestrian', disciplines: [] }), ['b', 'c', 'd']);
  assert.deepEqual(ids({ kind: 'category', sport: 'equestrian', disciplines: ['dressage'] }), ['b', 'd']);
  assert.deepEqual(ids({ kind: 'games', jobIds: ['c', 'a'] }), ['a', 'c']);
});

test('the search dialog matches every typed word against the title and its facts', () => {
  assert.deepEqual(findGames(games, 'grand prix', headline).map((g) => g.jobId), ['b']);
  assert.deepEqual(findGames(games, 'dressage', headline).map((g) => g.jobId), ['b', 'd']);
  assert.deepEqual(findGames(games, '', headline).length, 4);
  assert.deepEqual(findGames(games, 'nothing here', headline), []);
});

test('the session is named from its scope', () => {
  assert.equal(scopeTitle({ kind: 'all' }, games, t, headline), 'All games');
  assert.equal(scopeTitle({ kind: 'category', sport: 'equestrian', disciplines: [] }, games, t, headline),
    'Equestrian · all disciplines');
  assert.equal(scopeTitle({ kind: 'category', sport: 'equestrian', disciplines: ['Dressage', 'Show Jumping'] }, games, t, headline),
    'Equestrian · Dressage, Show Jumping');
  assert.equal(scopeTitle({ kind: 'games', jobIds: ['b'] }, games, t, headline), 'Grand Prix Freestyle');
  assert.equal(scopeTitle({ kind: 'games', jobIds: ['b', 'c'] }, games, t, headline), '2 games');
  assert.equal(scopeTitle(null, games, t, headline), '');
});

test('the agent is told ids as well as names', () => {
  assert.equal(scopeContextLine({ kind: 'all' }, games, headline), 'all games on the desk');
  assert.equal(scopeContextLine({ kind: 'category', sport: 'equestrian', disciplines: ['Dressage'] }, games, headline),
    'sport=equestrian; disciplines=Dressage; job_ids=b,d');
  assert.equal(scopeContextLine({ kind: 'category', sport: 'football', disciplines: [] }, games, headline),
    'sport=football; disciplines=all; job_ids=none analysed yet');
  assert.equal(scopeContextLine({ kind: 'games', jobIds: ['a', 'c'] }, games, headline),
    'job_ids=a,c; titles=SWE v DEN — EHF Euro | CSI Aachen');
  assert.equal(scopeContextLine(null, games, headline), '');
});

test('search and desk filters follow the scope', () => {
  assert.deepEqual(scopeFilters({ kind: 'all' }, games), { sport: '', jobIds: [] });
  assert.deepEqual(scopeFilters({ kind: 'category', sport: 'equestrian', disciplines: [] }, games),
    { sport: 'equestrian', jobIds: [] });
  // The index filters by sport, not discipline, so a discipline becomes its games.
  assert.deepEqual(scopeFilters({ kind: 'category', sport: 'equestrian', disciplines: ['Dressage'] }, games),
    { sport: '', jobIds: ['b', 'd'] });
  assert.deepEqual(scopeFilters({ kind: 'games', jobIds: ['c'] }, games), { sport: '', jobIds: ['c'] });
});
