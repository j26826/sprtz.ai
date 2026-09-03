/**
 * Which card answers a question.
 *
 * Getting this wrong is not cosmetic: "show all games" answered with a list of
 * moments is the wrong data entirely, and the prose that would have explained
 * it is hidden because a card claimed the answer. The phrasings below are the
 * ones people type — several of them collected from getting it wrong.
 *
 * Run with: node --test web/tests
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { chooseCard, wantsDetail } from '../src/cards.js';

const routes = (cases) => {
  for (const [question, card] of Object.entries(cases)) {
    it(`${question} → ${card}`, () => assert.equal(chooseCard(question), card));
  }
};


describe('every game on the desk', () => {
  routes({
    // An exact-phrase list matched "all games" and missed the rest: one extra
    // word, or the noun in the singular, was enough to fall through to moments.
    'show all games': 'games',
    'show all the games': 'games',
    'list all the games': 'games',
    'show all game details': 'games',
    'show me all games': 'games',
    'show details of all games': 'games',
    'every game': 'games',
    'which games do we have': 'games',
    'browse the matches': 'games',
    'what games are there': 'games',
  });
});


describe('one match', () => {
  routes({
    'game details': 'game',
    'what was the game': 'game',
    'who played': 'game',
    'tell me about this match': 'game',
    'what was the final score': 'game',
  });
});


describe('the plays inside a match', () => {
  routes({
    'show me the best moments': 'moments',
    'show all goals': 'moments',
    'show every penalties': 'moments',
    'every wing shot in the 1st half': 'moments',
    // Says match, means moments. The game rules must not take it on the word.
    'show all moments of the FAG v TVB match': 'moments',
    'show all moments of FAG v TVB — DAIKIN HBL': 'moments',
    'show me every save': 'moments',
  });
});


describe('the rest of the cards', () => {
  routes({
    // "new game" is a game word and "every match" is a scope word, and neither
    // of these is a request to list what is already here.
    'ingest a new game': 'ingest',
    'upload a new match': 'ingest',
    'use last nights upload': 'ingest',
    "what's still processing?": 'jobs',
    'did any job fail': 'jobs',
    'prepare publish': 'publish',
    'cut all of these into clips': 'reel',
    'what happened during the analysis': 'activity',
  });
});


describe('asking for the records, not the index', () => {
  it('detail means open them in place', () => {
    // Leaving each behind its own Details button answers a request for the
    // records with a list of names.
    assert.equal(wantsDetail('show all game details'), true);
    assert.equal(wantsDetail('full details of every match'), true);
    assert.equal(wantsDetail('summarise all the games'), true);
  });

  it('a plain list is a plain list', () => {
    assert.equal(wantsDetail('show all games'), false);
    assert.equal(wantsDetail('which games do we have'), false);
  });
});
