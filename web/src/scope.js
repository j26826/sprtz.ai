/**
 * A session's scope: what the conversation is about.
 *
 * Every new session starts by asking — all the games, one sport (and any of
 * its disciplines), or particular games — and the answer is held on the
 * session, names it, narrows every card, and is sent to the agent with every
 * message. Without it "show the best moments" answered from whichever match
 * happened to be open, and the editor could not tell which.
 *
 * Pure functions over plain values, so `node --test` reaches them; app.js
 * passes in what it alone knows (the headline for a game, the translator).
 *
 * Shapes:
 *   { kind: 'all' }
 *   { kind: 'category', sport, disciplines: [] }   // [] means every discipline
 *   { kind: 'games', jobIds: [...] }
 */

/* A game is an event; a job is a recording, and one recording can hold several
   events. A day's live capture crosses class after class, and each class is a
   record of its own with the same `jobId` — so identity is the document, and
   the recording is a second question with a second answer. Scoping to either
   works: an id that names a recording takes every competition in it. */
const idOf = (g) => g.id || g.jobId;
const jobIdOf = (g) => g.jobId || g.id;
const low = (s) => (s || '').toLowerCase();

/** Sports the editor can choose from: what is on the desk, plus what is configured. */
export function sportsAvailable(games, configured = []) {
  const seen = new Set();
  for (const s of [...games.map((g) => g.sport), ...configured]) {
    const key = low(s);
    if (key) seen.add(key);
  }
  return [...seen];
}

/** Disciplines the desk has actually seen for a sport — never a list kept here. */
export function disciplinesFor(games, sport) {
  const seen = new Set();
  for (const g of games) {
    if (low(g.sport) === low(sport) && g.discipline) seen.add(g.discipline);
  }
  return [...seen].sort();
}

/** The games a scope covers. No scope, or "all", is every game. */
export function gamesInScope(scope, games) {
  if (!scope || scope.kind === 'all') return games;
  if (scope.kind === 'category') {
    const wanted = (scope.disciplines || []).map(low);
    return games.filter((g) => low(g.sport) === low(scope.sport)
      && (!wanted.length || wanted.includes(low(g.discipline))));
  }
  if (scope.kind === 'games') {
    const ids = new Set(scope.jobIds || []);
    return games.filter((g) => ids.has(idOf(g)) || ids.has(jobIdOf(g)));
  }
  return games;
}

/** Games whose title contains every word typed, for the search dialog. */
export function findGames(games, query, headline) {
  const words = low(query).split(/\s+/).filter(Boolean);
  if (!words.length) return games;
  return games.filter((g) => {
    const text = low(`${headline(g)} ${g.sport || ''} ${g.discipline || ''} ${g.competition || ''}`);
    return words.every((w) => text.includes(w));
  });
}

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** The session's name, from its scope. */
export function scopeTitle(scope, games, t, headline) {
  if (!scope) return '';
  if (scope.kind === 'all') return t('scope.titleAll');
  if (scope.kind === 'category') {
    const discs = scope.disciplines || [];
    return `${cap(scope.sport)} · ${discs.length ? discs.join(', ') : t('scope.titleAllDisciplines')}`;
  }
  if (scope.kind === 'games') {
    const chosen = gamesInScope(scope, games);
    if (chosen.length === 1) return headline(chosen[0]);
    return t('scope.titleGames').replace('{n}', String((scope.jobIds || []).length));
  }
  return '';
}

/**
 * What the agent is told, on every message, about the scope.
 *
 * Job ids as well as names: the agent narrows `search_moments` and
 * `list_top_moments` by `job_ids` and `sport`, and a name is what a vector
 * search is worst at.
 */
export function scopeContextLine(scope, games, headline) {
  if (!scope) return '';
  if (scope.kind === 'all') return 'all games on the desk';
  const chosen = gamesInScope(scope, games);
  const ids = chosen.map(idOf);
  if (scope.kind === 'category') {
    const discs = scope.disciplines || [];
    return `sport=${scope.sport}; disciplines=${discs.length ? discs.join(', ') : 'all'}; `
      + `job_ids=${ids.length ? ids.join(',') : 'none analysed yet'}`;
  }
  if (scope.kind === 'games') {
    return `job_ids=${(scope.jobIds || []).join(',')}; titles=${chosen.map(headline).join(' | ')}`;
  }
  return '';
}

/** What the search panel and the desk shortlist should be narrowed to. */
export function scopeFilters(scope, games) {
  if (!scope || scope.kind === 'all') return { sport: '', jobIds: [] };
  if (scope.kind === 'category') {
    const discs = scope.disciplines || [];
    // A whole sport is the sport filter; some of its disciplines are the
    // matching games, because the index knows sports and not disciplines.
    return discs.length
      ? { sport: '', jobIds: [...new Set(gamesInScope(scope, games).map(jobIdOf))] }
      : { sport: scope.sport, jobIds: [] };
  }
  // What goes to the agent is recordings, not events: its `job_ids` filter is
  // over jobs, and a class id would match nothing. A scope naming one class
  // still narrows every card here, which is where the class lives.
  const named = gamesInScope(scope, games);
  return {
    sport: '',
    jobIds: named.length
      ? [...new Set(named.map(jobIdOf))]
      : [...(scope.jobIds || [])],
  };
}
