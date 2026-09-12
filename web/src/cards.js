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

// An equestrian day is an event, not a game, and the desk calls both games:
// "show me all events" is the same list as "show me all games".
const GAMES = /\b(games|matches|fixtures|events|competitions)\b/;
const GAME = /\b(game|match|fixture|event|competition)\b/;

// A competition day's rounds: who rode, how they scored. "show me the ride for
// Gareth Hughes", "rides scoring more than 70%". Answered by the rides card —
// the event's rides with their moments under them — never by a moments list
// filtered by the words "ride" and "scoring".
const RIDES = /\b(ride|rides|rider|riders)\b/;

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
];

// There was a 'reel' route here, and a 'publish' one, for cutting a montage
// and packaging it. Clip generation is being rebuilt and neither card exists,
// so those questions fall through to the moments list — which is the honest
// answer to "cut me the best bits" while there is nothing that cuts.

// Looking across the desk rather than inside the open match. The signal is a
// scope led by a preposition or a question about which match — "in any match",
// "across all games", "anywhere in the library", "which match has" — because
// that is unambiguous whatever the play is called. It has to be, since the
// plays of one sport are not the plays of another: a pirouette is a moment as
// much as a save is, and a list of nouns would always be missing one. The bare
// "all games" is deliberately not here — that is asking for the list.
const SCOPE = new RegExp([
  String.raw`\b(across|anywhere|throughout)\b`,
  // Led by a locative preposition. "of all games" is possessive — "details of
  // all games" is the list — so `of` is deliberately not here; and the bare
  // "every game" or "any match" is the list too, which is why the preposition
  // is required rather than the quantifier alone.
  String.raw`\b(in|from) (any|every|other|all( the)?) (game|match|video|recording|games|matches|videos|recordings|content|assets)\b`,
  String.raw`\bwhich (game|match|video|recording)\b`,
  String.raw`\b(whole|entire) (desk|library|archive)\b`,
  String.raw`\b(library|archive)\b`,
  // A search verb aimed at a collection — "search the equestrian videos",
  // "look through the recordings". "games" is not in this noun set on purpose:
  // "show all equestrian games" is the list filtered by sport, not a search.
  String.raw`\b(search|look)( (in|through|across))?( the)?( (handball|equestrian|dressage|jumping|eventing))? (videos|recordings|footage|library|archive)\b`,
].join('|'));

// The desk's key moments rather than one match's. "Show all key moments",
// "the best moments", "highlights" — the ranked shortlist, with no match named
// — is a question about everything on the desk. Answering it from whichever
// match happened to be open is how "no key moments were found" got said about
// a desk full of them. When a match *is* named, app.js sees that and keeps it
// to the match; this module cannot, because it does not hold the game list.
const DESK_MOMENTS = /\b(key|best|top|greatest) (moments|plays|highlights)\b|\bhighlights\b/;

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

  // Rides before the games list and the desk search: "rides in every event"
  // is about rides. Getting one in is still ingesting, whatever it is a ride of.
  const acting = RULES.filter(([card]) => card === 'ingest')
    .some(([, pattern]) => pattern.test(q));
  if (RIDES.test(q) && !acting) return 'rides';

  // A search across the desk, before the games route sees "games" or "which
  // match" in it and answers with a list of matches instead.
  if (SCOPE.test(q) && !RULES[1][1].test(q)) return 'search';

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

  if (DESK_MOMENTS.test(q)) return 'desk-moments';

  return 'moments';
}
