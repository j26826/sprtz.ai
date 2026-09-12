/**
 * A competition day's moments, grouped under the ride they happened in.
 *
 * The grouping itself is the catalog's (GET /api/jobs/{id}/event, built by
 * mcp/catalog_server/event_tree.py): which ride each moment belongs to is
 * decided there, once, for every reader. What is left here is applying that to
 * the list the editor is actually looking at — the live moments, already
 * filtered and sorted — so the tiles keep their live state (their thumbnail)
 * and the order the editor chose.
 *
 * A group is one ride: a rider on one horse.
 *
 * Its own module, with no imports, for the same reason as cards.js: app.js
 * cannot be loaded outside a browser, and this is worth testing.
 */

/**
 * @param {object|null} event   The tree's event: { riders: [{ order, moments: [{ momentId }] }] }.
 * @param {object[]} moments    The moments to place, in the order to show them.
 * @param {{ filtered?: boolean }} opts
 *   filtered: the list is narrowed by a search, so a ride with nothing matching
 *   is left out. Unfiltered, every ride is shown — a ride with no key moments
 *   is still a ride somebody may be looking for.
 * @returns {{ ride: object|null, moments: object[] }[]}
 *   One group per ride in running order, then a group with ride null for
 *   moments outside every ride, only when there are any.
 */
export function groupByRide(event, moments, { filtered = false } = {}) {
  const riders = Array.isArray(event?.riders) ? event.riders : [];
  const where = new Map();
  const byOrder = new Map();
  riders.forEach((ride, i) => {
    (ride.moments || []).forEach((m) => where.set(m.momentId, i));
    if (ride.order != null) byOrder.set(ride.order, i);
  });

  const groups = riders.map((ride) => ({ ride, moments: [] }));
  const outside = { ride: null, moments: [] };
  for (const m of moments || []) {
    // A moment newer than the tree — the listener answered before the tree
    // was fetched again — is placed by the ride order stored on it, which is
    // the same field the tree would have used.
    let i = where.get(m.momentId);
    if (i === undefined && m.rideOrder != null) i = byOrder.get(m.rideOrder);
    (i === undefined ? outside : groups[i]).moments.push(m);
  }

  const shown = filtered ? groups.filter((g) => g.moments.length) : groups;
  return outside.moments.length ? [...shown, outside] : shown;
}


/**
 * Letters and digits only, accents folded, so "Lumière" typed as "lumiere"
 * still finds the horse and an apostrophe cannot split a name from itself.
 */
function nameKey(text) {
  return ` ${String(text ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()} `;
}


/**
 * Whether a question names this ride — its rider or its horse.
 *
 * Matched against the names the desk already holds rather than parsed out of
 * the sentence: "show me the ride for Gareth Hughes" names somebody, and the
 * rides say who. The whole name of either half matches, and so does the
 * rider's surname on its own ("Hughes' ride"), because that is how people are
 * talked about on a competition day. A horse needs its whole name — horses are
 * called Star and Dancer, and those are words.
 *
 * Works on a ride from the tree or from the game record; both say rider and
 * horse.
 */
export function rideNamedIn(question, ride) {
  const q = nameKey(question);
  const rider = nameKey(ride?.rider).trim();
  const horse = nameKey(ride?.horse).trim();
  if (rider && q.includes(` ${rider} `)) return true;
  if (horse && q.includes(` ${horse} `)) return true;
  const words = rider.split(' ');
  const surname = words.length > 1 ? words[words.length - 1] : '';
  return surname.length >= 4 && q.includes(` ${surname} `);
}


const NUMBER = String.raw`(\d{1,3}(?:[.,]\d+)?)\s*(?:%|percent|per cent)?`;
const AT_LEAST = new RegExp(String.raw`(?:at least|no less than|>=|≥)\s*${NUMBER}`);
const MORE_THAN = new RegExp(String.raw`(?:more than|greater than|higher than|better than|over|above|>)\s*${NUMBER}`);
const OR_MORE = /(\d{1,3}(?:[.,]\d+)?)\s*(?:%|percent|per cent)\s*(?:or (?:more|above|higher|better)|and (?:above|up|over)|\+|plus)/;


/**
 * The score bar a question sets for rides, if it sets one.
 *
 * "rides scoring more than 70%" is strictly over; "at least 70%" and "70% or
 * more" include it. The per cent sign is optional — a ride's total is a
 * percentage, so "rides over 72" can only mean one thing.
 *
 * @returns {{ min: number, inclusive: boolean } | null}
 */
export function rideScoreAsked(question) {
  const q = String(question ?? '').toLowerCase();
  const read = (m) => Number(m[1].replace(',', '.'));
  let m = q.match(AT_LEAST);
  if (m) return { min: read(m), inclusive: true };
  m = q.match(OR_MORE);
  if (m) return { min: read(m), inclusive: true };
  m = q.match(MORE_THAN);
  if (m) return { min: read(m), inclusive: false };
  return null;
}


// A total the pipeline could not reconcile with its own judges' marks, or one
// the published results disagree with. Not acted on: the same rule list_rides
// applies for the agent, so the card and the agent's answer agree.
const UNTRUSTED = /^mismatch|disagrees/;


/**
 * The ride groups a question asks for.
 *
 * A name narrows to that rider's or that horse's rides; a score bar narrows to
 * the rides over it and ranks them by total, best first, because that is the
 * order a question about scores is asking to see. A ride whose total failed
 * its check is left out of a score question and counted, so the card can say
 * so — a wrong number that is invisible is worse than one that is marked.
 *
 * @param {{ ride: object|null, moments: object[] }[]} groups  From groupByRide.
 * @param {string} question
 * @returns {{ groups: object[], names: string[], score: object|null,
 *             unchecked: number, narrowed: boolean }}
 */
export function ridesAsked(groups, question) {
  const rides = (groups || []).filter((g) => g.ride);
  const named = rides.filter((g) => rideNamedIn(question, g.ride));
  const score = rideScoreAsked(question);

  let shown = named.length ? named : rides;
  let unchecked = 0;
  if (score) {
    const over = (pct) => (score.inclusive ? pct >= score.min : pct > score.min);
    shown = shown.filter((g) => {
      const result = g.ride.result || {};
      const pct = result.totalPct;
      if (pct == null || !over(Number(pct))) return false;
      if (UNTRUSTED.test(String(result.scoreCheck || ''))) { unchecked += 1; return false; }
      return true;
    }).sort((a, b) => Number(b.ride.result.totalPct) - Number(a.ride.result.totalPct));
  }

  const names = [...new Set(named.map((g) => g.ride.rider || g.ride.horse).filter(Boolean))];
  return { groups: shown, names, score, unchecked, narrowed: Boolean(named.length || score) };
}


/**
 * A moment's type as a key: the taxonomy's code, or its label when a record
 * predates the code being stored. Blank for a moment that carries neither,
 * which is a moment no type filter can be about.
 */
function typeKey(moment) {
  return String(moment?.momentType || moment?.label || '').trim();
}


/**
 * The moment types present across a set of ride groups, with how many there
 * are of each.
 *
 * Read from the moments rather than from the sport's catalogue on purpose: a
 * dressage day's catalogue is thirty movements and a class may contain six of
 * them, and a filter offering twenty-four choices that match nothing is a
 * filter nobody trusts. The label is the record's own, so the list is in the
 * taxonomy's words without this module holding a copy of them.
 *
 * Commonest first — a filter is scanned for the thing there is a lot of — then
 * alphabetically, so the order is stable between two types seen equally often.
 *
 * @param {{ moments: object[] }[]} groups
 * @returns {{ key: string, label: string, count: number }[]}
 */
export function momentTypesIn(groups) {
  const seen = new Map();
  for (const group of groups || []) {
    for (const moment of group?.moments || []) {
      const key = typeKey(moment);
      if (!key) continue;
      const entry = seen.get(key) || { key, label: moment.label || key, count: 0 };
      entry.count += 1;
      seen.set(key, entry);
    }
  }
  return [...seen.values()]
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}


/**
 * The ride groups narrowed to the chosen moment types.
 *
 * Choosing nothing is the whole ride, not an empty one — the filter's resting
 * state has to be the answer the card was asked for.
 *
 * Every ride stays, including the ones left with nothing. The rail is
 * navigation, and navigation that shortens as you filter moves the rider you
 * were about to click; the count against each name falling to zero says the
 * same thing without the list jumping, and it says it for the riders who did
 * *not* do the movement, which is half of what the question was asking.
 *
 * @param {{ ride: object|null, moments: object[] }[]} groups
 * @param {string[]} types  Type keys, as `momentTypesIn` reports them.
 */
export function filterByTypes(groups, types) {
  const want = new Set((types || []).filter(Boolean));
  if (!want.size) return [...(groups || [])];
  return (groups || []).map((group) => ({
    ...group,
    moments: (group?.moments || []).filter((m) => want.has(typeKey(m))),
  }));
}


/**
 * How many of each type one ride holds, keyed as `momentTypesIn` keys them.
 *
 * The filter's list is the whole event's — stable, so it does not reshuffle
 * under the pointer when another rider is opened — but the number beside each
 * entry is this ride's, because the question the count answers is "how much of
 * this is in what I am looking at".
 *
 * @param {object[]} moments
 * @returns {Record<string, number>}
 */
export function countTypesIn(moments) {
  const counts = {};
  for (const moment of moments || []) {
    const key = typeKey(moment);
    if (key) counts[key] = (counts[key] || 0) + 1;
  }
  return counts;
}


/**
 * The rides in the order the board's own sort asks for.
 *
 * Two questions are being asked of one screen, and they were sharing one
 * control. The board's sort is about the event — best first means the round
 * with the strongest moment in it comes first, so the day's highlight is the
 * first tab rather than somewhere down a running order of forty. Match order
 * is the running order, which is how the day was ridden and how the log reads.
 *
 * Ordering the rail by each ride's *best moment* rather than by its score is
 * deliberate: this control sits above the moments and orders them, and a round
 * that scored 68 can still hold the thing worth cutting.
 *
 * A ride with nothing found scores zero and sinks, but keeps its place among
 * the others that found nothing — the sort is stable, so the rail never
 * reshuffles rides the question cannot tell apart.
 *
 * @param {{ ride: object, moments: object[] }[]} groups
 * @param {string} sort  'score' or 'time'.
 */
export function sortRideGroups(groups, sort) {
  const all = [...(groups || [])];
  if (sort !== 'score') return all;
  const best = (group) => (group?.moments || [])
    .reduce((top, m) => Math.max(top, Number(m.highlightScore) || 0), 0);
  return all
    .map((group, at) => ({ group, at, best: best(group) }))
    .sort((a, b) => b.best - a.best || a.at - b.at)
    .map(({ group }) => group);
}


/**
 * A ride's current rank in its class, or null when there is none yet.
 *
 * The published placing when grounding found one, else the rank the results
 * graphic showed at the time (the tree's `result.place` is already that
 * choice). Only a whole number from 1 up is a rank: a live class whose results
 * have not been shown, or a total nobody put on screen, has none — and saying
 * so beats printing a 0 or leaving a blank that reads as last.
 */
export function rideRank(ride) {
  const place = Number(ride?.result?.place);
  return Number.isInteger(place) && place >= 1 ? place : null;
}
