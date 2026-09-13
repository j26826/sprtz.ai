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
import { LOCALES, setLocale, t } from '../src/i18n.js';

// `setLocale` records the language on the document and in localStorage.
// Neither exists here, and neither is what is being tested.
globalThis.document = globalThis.document || { documentElement: {} };
globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null, setItem: () => {},
};

const routes = (cases) => {
  for (const [question, card] of Object.entries(cases)) {
    it(`${question} → ${card}`, () => assert.equal(chooseCard(question), card));
  }
};


describe('every game on the desk', () => {
  routes({
    // Naming a sport is still a request for the games list; the list narrows
    // itself afterwards.
    'show all handball games': 'games',
    'show all handball game details': 'games',
    'show all equestrian games': 'games',
    'show all equestrian game details': 'games',
    'show all dressage games': 'games',
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
    // Moved to the desk: no match is named, so it is the desk's shortlist.
    'show me the best moments': 'desk-moments',
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
    // Clip generation is being rebuilt: a question about cutting has no card
    // of its own any more, and the moments are the honest answer to it.
    'cut all of these into clips': 'moments',
    'prepare publish': 'moments',
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


describe('a search for plays across the desk', () => {
  // Scope words are what separate "find the double save" — the open match —
  // from "find the double save across all games", which is every match on
  // the desk with each result naming its game.
  routes({
    'find every double save across all games': 'search',
    'is there a buck anywhere in the library': 'search',
    'search the equestrian videos for a pat': 'search',
    'which match has the best fast break': 'search',
    'show me pirouettes in any match': 'search',
    'look for tack failures across all the recordings': 'search',
    // The same questions without scope words stay on the open match.
    'find the double save': 'moments',
    'show me pirouettes': 'moments',
    // And a list of games is still a list of games, not a search.
    'show all games': 'games',
    'list all the matches': 'games',
    // Getting a recording in is still ingest even with "all" in it.
    'upload all the games from yesterday': 'ingest',
  });
});


describe("the desk's key moments", () => {
  // The ranked shortlist with no match named is a question about everything
  // on the desk. Answering it from whichever match was open is how "no key
  // moments were found" got said about a desk full of them.
  routes({
    'show all key moments': 'desk-moments',
    'show me the best moments': 'desk-moments',
    'what are the highlights': 'desk-moments',
    'the top plays': 'desk-moments',
    // A type is a filter on the open match, and stays there.
    'show all goals': 'moments',
    'show me every save': 'moments',
    'find the double save': 'moments',
    // A named match is that match's shortlist; the override is in app.js,
    // which holds the game list, but the route itself is moments.
    'show all moments of the FAG v TVB match': 'moments',
    // A scope wins over the shortlist words: this is a search.
    'find the best saves across all games': 'search',
  });
});


// The four examples the composer offers. Whatever else changes here, what the
// placeholder suggests has to land on the card that answers it.
describe('the composer\'s own examples', () => {
  routes({
    'show me all events': 'games',
    'show me the ride for Gareth Hughes': 'rides',
    'best pirouettes': 'moments',
    'rides scoring more than 70%': 'rides',
  });
});

describe('events are games', () => {
  routes({
    'list every event': 'games',
    'show all equestrian events': 'games',
    'which competitions do we have': 'games',
    // Still the log, not the list.
    'show the event log': 'activity',
    // Still getting one in.
    'upload a new event': 'ingest',
  });
});

describe('rides', () => {
  routes({
    'show all rides': 'rides',
    "show me Hughes' ride": 'rides',
    'which rider scored highest': 'rides',
    'riders over 72 percent': 'rides',
    'show the moments from the ride for Anna Berger': 'rides',
    'rides in every event': 'rides',
    // Cutting or publishing one is a question about that ride now that
    // neither has a card of its own.
    'cut a 30 second short of the ride for Anna Berger': 'rides',
    'publish the ride for Anna Berger': 'rides',
  });
});


describe('every locale, against the desk\'s own words', () => {
  // The strings below are not invented phrasings: they are what the app itself
  // puts in the composer when someone clicks a chip or answers the opener. If
  // one routes somewhere its English twin does not, that button is broken in
  // that language — which is how "Zeig mir die besten Szenen" came to answer
  // with the wrong card for every German editor, silently, because nothing
  // here read anything but English.
  //
  // Read from i18n.js rather than copied, so a retranslation cannot drift away
  // from the routing without this failing.
  const GENERATED = [
    ['opener.askMoments', 'desk-moments'],
    ['action.bestMoments', 'desk-moments'],
    ['action.processing', 'jobs'],
    ['action.ingest', 'ingest'],
  ];

  for (const locale of LOCALES) {
    it(`routes the desk's own questions in ${locale}`, () => {
      setLocale(locale);
      try {
        for (const [stringKey, card] of GENERATED) {
          const asked = t(stringKey);
          assert.equal(chooseCard(asked), card,
            `${locale} ${stringKey}: ${JSON.stringify(asked)}`);
        }
      } finally {
        setLocale('en-GB');
      }
    });
  }

  it('reads a question in whatever language it was typed in', () => {
    // The UI language is a display preference, not a promise about the
    // keyboard. A German desk asked in English, and an English desk asked in
    // German, are both ordinary and both have to work — which is why every
    // language compiles into one alternation rather than the current locale
    // selecting a set.
    assert.equal(chooseCard('zeig mir alle Spiele'), 'games');
    assert.equal(chooseCard('mostrami tutte le partite'), 'games');
    assert.equal(chooseCard('montre-moi tous les matchs'), 'games');
    assert.equal(chooseCard('muéstrame todos los partidos'), 'games');
    assert.equal(chooseCard('zeig mir die Ritte'), 'rides');
    assert.equal(chooseCard('mostrami le riprese'), 'rides');
    assert.equal(chooseCard('montre-moi les reprises'), 'rides');
    assert.equal(chooseCard('muéstrame los recorridos'), 'rides');
  });

  it('folds accents rather than destroying them', () => {
    // `[^a-z0-9]` alone turned "événements" into " v nements" and "Aktivität"
    // into "aktivit t", so every accented language was matched on its
    // fragments and none of these reached its card.
    assert.equal(chooseCard('montre-moi tous les événements'), 'games');
    assert.equal(chooseCard('quelle est l’activité ?'), 'activity');
    assert.equal(chooseCard('¿cuál es la actividad?'), 'activity');
    assert.equal(chooseCard('mostrami l’attività'), 'activity');
    assert.equal(chooseCard('zeig mir die Aktivität'), 'activity');
    // A German keyboard without umlauts types the ae/oe/ue spelling, which
    // folding cannot produce from the umlaut or the other way round, so both
    // are listed.
    assert.equal(chooseCard('zeig mir die Aktivitaet'), 'activity');
  });
});

describe('finding a reel that was already made', () => {
  it('a question naming a reel is about the reel', () => {
    // Before this route existed it answered with a moments list, and the
    // agent's own reply was hidden — a card that claims the answer suppresses
    // the prose beside it.
    assert.equal(chooseCard('show me my reels'), 'reels');
    assert.equal(chooseCard('open the highlights reel'), 'reels');
    assert.equal(chooseCard('what reels have I made'), 'reels');
  });

  it('beats rides, which would otherwise catch the rider in the name', () => {
    assert.equal(chooseCard('the reel for Gareth Hughes'), 'reels');
  });

  it('does not steal a question about cutting a new one', () => {
    // Building a reel is done by picking moments on screen, so this is still
    // a moments question.
    assert.equal(chooseCard('cut all of these into clips'), 'moments');
  });

  it('does not steal ingesting', () => {
    assert.equal(chooseCard('upload a new reel of the match'), 'ingest');
  });

  it('leaves ordinary ride and moment questions alone', () => {
    assert.equal(chooseCard('show me all rides'), 'rides');
    assert.equal(chooseCard('show all moments'), 'moments');
    assert.equal(chooseCard('show me all events'), 'games');
  });
});
