/**
 * Choosing and ordering the things a question asked for — moments, and games.
 *
 * Its own module, and one with no imports, for two reasons. The logic is worth
 * testing — a stopword added carelessly empties the card that the app's own
 * suggestion chip opens — and `app.js` cannot be loaded outside a browser,
 * because it imports the Firebase SDK from a CDN. Everything here is a pure
 * function over plain objects, so `node --test` can reach it.
 *
 * These are text filters over what is already loaded, not searches. That is the
 * right tool here: they are instant, they are exact about what they did, and
 * the words they match are the record's own — the moment's class, category and
 * result, the game's sport, discipline and competition. So they answer in the
 * taxonomy's vocabulary rather than in one hard-coded here. "Wing Shot",
 * "7-Metre Penalty" and "Para-Dressage" are what the analysis wrote; "wing
 * shots", "penalties" and "para dressage" are what an editor types.
 */

/** Letters and digits only. Everything else is punctuation drift. */
export function titleKey(text) {
  return String(text ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}


/**
 * Words that name nothing about a moment.
 *
 * This list is what separates "show every penalty" from "show every moment":
 * both are mostly scaffolding, and the one content word is the whole request.
 * Ranking words are in here too — "the best moments" asks for an order, not a
 * kind, and treating "best" as a filter would empty the card the suggestion
 * chip opens.
 */
export const FILTER_NOISE = new Set([
  'show', 'give', 'find', 'get', 'list', 'see', 'watch', 'want', 'please',
  'the', 'all', 'any', 'every', 'some', 'each', 'many', 'much', 'more',
  'and', 'or', 'for', 'from', 'with', 'that', 'this', 'these', 'those',
  'what', 'which', 'where', 'who', 'when', 'how', 'was', 'were', 'are', 'is',
  'moment', 'moments', 'clip', 'clips', 'play', 'plays', 'video', 'videos',
  'game', 'games', 'match', 'matches', 'reel', 'highlight', 'highlights',
  'best', 'good', 'great', 'top', 'key', 'main', 'important', 'interesting',
  'exciting', 'strongest', 'biggest',
  'sort', 'order', 'time', 'score', 'rank', 'ranked', 'chronological',
  'his', 'her', 'their', 'its', 'you', 'your', 'out', 'about', 'into',
  // How to show a list, not what to put in it. "show all equestrian game
  // details" asks for equestrian; "details" is an instruction to the card.
  'detail', 'details', 'full', 'everything', 'summary', 'summarise',
  'summarize', 'info', 'information', 'record', 'records',
  // The axis rather than the value. A game's sport field holds "handball", not
  // the word "sport", so "games by sport handball" must search for handball.
  'sport', 'sports', 'discipline', 'disciplines', 'type', 'types',
  'profile', 'profiles', 'category', 'categories', 'kind', 'kinds',
]);


/** Crude, and it does not need to be better: matching is by substring. */
export function stem(word) {
  if (word.endsWith('ies') && word.length > 4) return `${word.slice(0, -3)}y`;
  if (word.endsWith('s') && !word.endsWith('ss') && word.length > 3) return word.slice(0, -1);
  return word;
}


// "the 1st half", not "the first wave" — the phrase names a half only when the
// word half is in it, which is also what keeps "first" usable as a term.
const HALVES = [[/\b(?:1st|first) half\b/, 1], [/\b(?:2nd|second) half\b/, 2]];


// Match order rather than best first. "in order" and "by time" are how it gets
// asked for; the card's own toggle is how it gets changed afterwards.
const ASKS_FOR_TIME = /\bby time\b|\bchronolog|\bin order\b|\bmatch order\b|\btimeline\b|\bearliest\b|\bin sequence\b/;


/**
 * What a question narrows the list to: a kind, a half of the match, or neither.
 *
 * `namedTitle` is the fixture the question named, if it named one. It is
 * removed first: a match's name says which match, not which kind of moment
 * inside it, and its words would otherwise match every moment of that team.
 */
export function filterAsked(question, namedTitle = '') {
  let text = titleKey(question);
  if (namedTitle) text = text.replace(titleKey(namedTitle), ' ');

  let half = null;
  for (const [pattern, which] of HALVES) {
    if (pattern.test(text)) {
      half = which;
      text = text.replace(pattern, ' ');
    }
  }

  // Parsed here rather than beside the card, so one reading of the question
  // produces everything the list needs. The words this looks for are all in
  // FILTER_NOISE, so asking for an order never also asks for a kind.
  const sort = ASKS_FOR_TIME.test(text) ? 'time' : 'score';

  const keep = (word) => word.length > 2 && !FILTER_NOISE.has(word);
  const terms = [...new Set(text.split(' '))].filter(keep).map(stem).filter(keep);
  return { terms, half, sort };
}


/** Everything about a moment that a word in a question could be naming. */
/**
 * Narrow a list to the records every term names, then to any of them.
 *
 * Every-then-any is the whole trick. "wing shot" is one kind of moment, and
 * matching either word makes it every shot in the match — which is how asking
 * for wing shots returned jump shots. "penalties and suspensions" is the other
 * case: two kinds, no record is both, and there any is the answer.
 */
export function matchTerms(items, terms, textOf) {
  if (!terms.length) return items;
  const text = new Map(items.map((item) => [item, textOf(item)]));
  const every = items.filter((i) => terms.every((term) => text.get(i).includes(term)));
  return every.length
    ? every
    : items.filter((i) => terms.some((term) => text.get(i).includes(term)));
}


export function momentText(m) {
  return titleKey([
    m.momentType, m.label, m.category, m.actionResult, m.participantRole,
    m.participant, m.actionTeam, m.summary, m.isGoal ? 'goal' : '',
    // A sport judged on form puts most of its searchable words here: "clean
    // take-off", "horse fighting the contact" are in neither the label nor the
    // summary.
    m.executionDetails, m.harmonyIndex,
  ].join(' '));
}


/**
 * Where the match turns over, in video time.
 *
 * Not 30:00. The upload carries whatever was recorded before throw-off — the
 * first goal of one of these matches is at 29:47 — so a fixed clock would put
 * the whole first half in the second. The halves are the earlier and later
 * parts of the span the analysis actually found.
 */
export function halfSplit(list) {
  if (list.length < 2) return null;
  const from = Math.min(...list.map((m) => m.startSec ?? 0));
  // The later of each moment's own two timestamps. endSec is stored with a
  // default of 0, so a record missing one would otherwise drag the end of the
  // match to before its start and collapse the split to nothing.
  const to = Math.max(...list.map((m) => Math.max(m.endSec ?? 0, m.startSec ?? 0)));
  return to > from ? (from + to) / 2 : null;
}


export const MOMENT_SORTS = {
  // Highest scoring first: the answer to "show me the best moments".
  score: (a, b) => (b.highlightScore ?? 0) - (a.highlightScore ?? 0),
  // Match order: the answer to "in order", and how the log reads.
  time: (a, b) => (a.startSec ?? 0) - (b.startSec ?? 0),
};


/**
 * Apply a question's filter and order to a match's moments.
 *
 * A filter that matches nothing shows everything and says so. The vocabulary
 * is the taxonomy's, so a word it does not use matches nothing at all — and
 * "no moments" is the wrong answer to that, because the moments are right
 * there. `missed` is how the card tells the difference.
 */
export function selectMoments(all, { terms = [], half = null, sort = 'score' } = {}) {
  const withTerms = matchTerms(all, terms, momentText);

  const split = half ? halfSplit(all) : null;
  const inHalf = (m) => {
    if (!split) return true;
    const at = m.startSec ?? 0;
    return half === 1 ? at < split : at >= split;
  };

  const hits = (terms.length || split) ? withTerms.filter(inHalf) : [];
  const list = [...(hits.length ? hits : all)].sort(MOMENT_SORTS[sort] || MOMENT_SORTS.score);

  return {
    list,
    total: all.length,
    terms,
    half,
    // Asked for a narrowing and got one, as against asking and getting
    // nothing — which shows everything with a note rather than an empty card.
    narrowed: hits.length > 0 && hits.length < all.length,
    missed: Boolean(terms.length || half) && hits.length === 0 && all.length > 0,
  };
}


/**
 * The match a question names, if it names one this desk has.
 *
 * Matched against the titles already in the browser rather than by asking the
 * agent: a name is exactly what a vector search is worst at, and the answer is
 * here. The longest match wins, so a fixture whose title is a prefix of
 * another's cannot answer for it, and a one-word name is not a name.
 */
export function gameNamedIn(question, games, headline) {
  const asked = titleKey(question);
  if (!asked) return null;

  return games
    .map((game) => ({ game, key: titleKey(headline(game)) }))
    .filter(({ key }) => key.split(' ').length > 1 && asked.includes(key))
    .sort((a, b) => b.key.length - a.key.length)[0]?.game || null;
}


/** Everything about a game that a word in a question could be naming. */
export function gameText(g) {
  return titleKey([
    g.sport, g.discipline, g.title,
    g.homeTeam, g.awayTeam, g.groundedHomeTeam, g.groundedAwayTeam,
    g.competition, g.groundedCompetition, g.venue, g.groundedVenue,
    g.eventOutcome, g.mood, g.sentiment, g.summary,
    // The final score is left out for the same reason a moment's scoreline is:
    // "24-23" as text matches nothing anyone would type, and a bare number
    // dilutes the words that do.
  ].join(' '));
}


/**
 * The games a question asked for.
 *
 * "Show all handball games" and "show all equestrian game details" both name a
 * sport, and the list used to answer either with every game on the desk — which
 * reads as a filter that ran and matched everything. The words matched are the
 * record's own, so a discipline works the same way a sport does: "show all
 * dressage games" finds them because that is what the analysis wrote.
 *
 * Same contract as selectMoments, including the important one: a filter that
 * matches nothing shows everything and says so, because an empty card would
 * claim there are no games when there are.
 */
export function selectGames(all, { terms = [] } = {}) {
  const hits = terms.length ? matchTerms(all, terms, gameText) : [];
  return {
    list: hits.length ? hits : all,
    total: all.length,
    terms,
    narrowed: hits.length > 0 && hits.length < all.length,
    missed: Boolean(terms.length) && hits.length === 0 && all.length > 0,
  };
}
