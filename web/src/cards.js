/**
 * Which card answers a question.
 *
 * The editor's screen is the answer to most questions, so getting this wrong is
 * not a cosmetic miss — "show all games" answered with a list of moments is the
 * wrong data entirely, and the prose that would have explained it is hidden
 * because a card claimed the answer.
 *
 * Its own module, with no imports, for the same reason as moments.js: app.js
 * cannot be loaded outside a browser, and this is worth testing. The phrasings
 * in cards.test.mjs are the ones people actually type, several of them
 * collected from getting it wrong.
 */

/** Letters and digits only, so punctuation and spacing cannot break a match. */
function key(text) {
  return ` ${String(text ?? '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()} `;
}

// Asking for the whole set rather than for one of them. "show all game
// details" is this, even though it says game in the singular — an exact-phrase
// list matched "all games" and missed it, along with "show all the games",
// where one extra word was enough.
const EVERY = /\b(all|every|each|list|browse|both|which)\b/;

const GAMES = /\b(games|matches|fixtures)\b/;
const GAME = /\b(game|match|fixture)\b/;

// A question naming the plays inside a match is about the plays, whatever else
// it mentions: "show all moments of the FAG v TVB match" says match, and means
// moments.
const PLAYS = /\b(moment|moments|play|plays|action|actions|clip|clips|goal|goals|penalty|penalties|save|saves|shot|shots)\b/;

// Each of these keeps the phrasing it had, because each was arrived at from
// questions that were asked. Only the game rules are new.
const RULES = [
  ['activity', /\b(activity|event log|what happened|progress log|history)\b/],
  ['ingest', /\b(ingest|upload|uploaded|import)\b|\bnew (game|match|recording)\b|\banaly[sz]e a\b/],
  ['jobs', /\b(process|processing|job|jobs|status|fail|failed|error|still running|analysing|analyzing|analysis)\b/],
  ['publish', /\b(publish|post|schedule|package)\b/],
  ['reel', /\b(cut|reel|montage|render|generate|reframe|vertical|shorter|tighten)\b/],
];

// The one-match record: its teams, competition, venue, score and how it felt.
const GAME_DETAIL = /\bgame detail|\babout (the|this) (game|match)\b|\bwho played\b|\bfinal score\b|\bfind (the|a) (game|match)\b|\bwhat was the (game|match)\b|\bgame info\b|\bthe game\s*$/;


/**
 * The card a question asks for.
 *
 * Order decides ties, and it is not arbitrary: a question about the plays
 * inside a match and a question about the match are asked in almost the same
 * words, so what separates them is checked before anything more general.
 */
/**
 * Whether a question asked for the detail behind each row, not just the list.
 *
 * "show all game details" is a request for the records, so putting them behind
 * a Details button each is answering with the index instead of the answer.
 */
export function wantsDetail(question) {
  return /\bdetail|\bfull\b|\beverything\b|\bsummar/.test(key(question));
}


export function chooseCard(question) {
  const q = key(question);

  if (RULES[0][1].test(q)) return 'activity';

  // Every game on the desk. Plural says it outright; "all"/"list"/"which" says
  // it with the noun in the singular, which is how "show all game details"
  // reads. A question naming plays is about the plays, so it is excluded here
  // rather than being caught by the word match inside it.
  if (!PLAYS.test(q) && (GAMES.test(q) || (GAME.test(q) && EVERY.test(q)))) {
    // "ingest a new game" and "upload every match" are about getting one in,
    // not about listing what is here.
    if (!RULES[1][1].test(q)) return 'games';
  }

  if (!PLAYS.test(q) && GAME_DETAIL.test(q)) return 'game';

  for (const [card, pattern] of RULES.slice(1)) {
    if (pattern.test(q)) return card;
  }

  return 'moments';
}
