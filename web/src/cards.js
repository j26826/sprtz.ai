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
 *
 * **It reads all six locales at once, not the one the UI is set to.** The
 * language setting is a display preference — which words the buttons wear —
 * and says nothing about which language a question will be typed in: a German
 * editor asking in English is ordinary, and so is the reverse. Every language's
 * words therefore compile into one alternation per concept and are matched
 * together, listed per language so that adding a locale is adding a line and a
 * word that turns out to be wrong can be found in the language it was wrong in.
 *
 * This module used to read English only, and the failure was not subtle: the
 * desk's own opener sends `opener.askMoments` — "Zeig mir die besten Szenen",
 * "Mostrami i momenti migliori" — so the Find-moments flow answered with the
 * wrong card in four of the six languages the app ships. The test asserts every
 * locale's own generated strings against this module for exactly that reason:
 * those are the questions the app puts in people's mouths, and they cannot be
 * allowed to route anywhere but where their English twin routes.
 */

/**
 * Letters and digits only, so punctuation and spacing cannot break a match.
 *
 * Accents are folded rather than stripped. `[^a-z0-9]` on its own turned
 * "événements" into " v nements" and "Aktivität" into "aktivit t", which is
 * not a word in any of the vocabularies below — every accented language was
 * being matched on its fragments. ß is spelled out, because it does not
 * decompose.
 */
function key(text) {
  return ` ${String(text ?? '').toLowerCase().replace(/ß/g, 'ss')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, ' ').trim()} `;
}

/**
 * One concept, in every language the desk speaks: one group of alternatives
 * per language, compiled to a single word-bounded alternation.
 *
 * German words are given in their folded spelling ("lauft", "hohepunkte") and
 * again in the ae/oe/ue transcription people type on a keyboard that has no
 * umlauts, since folding cannot turn one into the other.
 */
const anyOf = (...groups) => new RegExp(String.raw`\b(${groups.join('|')})\b`);

// Asking for the whole set rather than for one of them. "show all game
// details" is this, even though it says game in the singular — an exact-phrase
// list matched "all games" and missed it, along with "show all the games",
// where one extra word was enough.
const EVERY = anyOf(
  'all|every|each|list|browse|both|which',
  'alle|alles|jede[nrs]?|samtliche|saemtliche|liste|auflisten|beide|welche[nrs]?',
  'tutti|tutte|tutto|ogni|elenco|elenca|elencare|entrambi|entrambe|quali?',
  'tous|toutes|tout|chaque|liste|lister|quels?|quelles?',
  'todos|todas|todo|cada|lista|listar|ambos|ambas|cuales?',
);

// An equestrian day is an event, not a game, and the desk calls both games:
// "show me all events" is the same list as "show me all games".
const GAMES = anyOf(
  'games|matches|fixtures|events|competitions',
  'spiele|spielen|partien|begegnungen|veranstaltungen|turniere|wettbewerbe|events|prufungen|pruefungen',
  'partite|gare|incontri|eventi|competizioni|manifestazioni',
  'matchs|rencontres|evenements|epreuves|competitions',
  'partidos|encuentros|eventos|competiciones|pruebas|concursos',
);
const GAME = anyOf(
  'game|match|fixture|event|competition',
  'spiel|partie|begegnung|veranstaltung|turnier|wettbewerb|event|prufung|pruefung',
  'partita|gara|incontro|evento|competizione|manifestazione',
  'match|rencontre|evenement|epreuve|competition|concours',
  'partido|encuentro|evento|competicion|prueba|concurso',
);

// A competition day's rounds: who rode, how they scored. "show me the ride for
// Gareth Hughes", "rides scoring more than 70%". Answered by the rides card —
// the event's rides with their moments under them — never by a moments list
// filtered by the words "ride" and "scoring".
//
// The nouns are the ones the desk's own UI uses for this list (Ritte, Riprese,
// Reprises, Recorridos), plus what a rider is called. Nothing generic — "tour",
// "giro", "vuelta" and "Runde" all mean a round and all mean five other things,
// and this route is checked before the games list and the desk search, so a
// false match here costs a whole screen.
const RIDES = anyOf(
  'ride|rides|rider|riders',
  'ritt|ritte|reiter|reiterin|reiterinnen|reitern',
  'ripresa|riprese|percorso|percorsi|cavaliere|cavalieri|amazzone',
  'reprise|reprises|parcours|cavalier|cavaliers|cavaliere|cavalieres',
  'recorrido|recorridos|jinete|jinetes|amazona',
);

// A question naming the plays inside a match is about the plays, whatever else
// it mentions: "show all moments of the FAG v TVB match" says match, and means
// moments.
const PLAYS = anyOf(
  'moment|moments|play|plays|action|actions|clip|clips|goal|goals'
    + '|penalty|penalties|save|saves|shot|shots',
  'moment|momente|szene|szenen|hohepunkt|hohepunkte|hoehepunkt|hoehepunkte'
    + '|schlusselmomente|schluesselmomente|tor|tore|wurf|wurfe|wuerfe'
    + '|strafwurf|parade|paraden|aktion|aktionen|clip|clips',
  'momento|momenti|azione|azioni|giocata|giocate|clip|gol|rigore|rigori'
    + '|parata|parate|tiro|tiri',
  'moment|moments|action|actions|clip|clips|but|buts|penalty|penalties'
    + '|arret|arrets|tir|tirs',
  'momento|momentos|accion|acciones|jugada|jugadas|clip|clips|gol|goles'
    + '|penalti|penaltis|parada|paradas|tiro|tiros',
);

// Each of these keeps the phrasing it had, because each was arrived at from
// questions that were asked. Only the game rules are new.
const RULES = [
  ['activity', anyOf(
    'activity|event log|what happened|progress log|history',
    'aktivitat|aktivitaet|verlauf|protokoll|historie|was ist passiert',
    'attivita|cronologia|registro|storico|cosa e successo',
    'activite|journal|historique|que s est il passe',
    'actividad|registro|historial|que ha pasado',
  )],
  ['ingest', new RegExp([
    anyOf(
      'ingest|upload|uploaded|import',
      'einlesen|hochladen|hochgeladen|importieren|import',
      'carica|caricare|caricato|importa|importare',
      'importer|televerser|televerser|charger',
      'cargar|carga|subir|importar',
    ).source,
    String.raw`\bnew (game|match|recording)\b`,
    String.raw`\bneue[sr]? (spiel|match|aufnahme|videodatei)\b`,
    String.raw`\bnuov[ao] (partita|gara|registrazione|video)\b`,
    String.raw`\bnouve(au|lle|l) (match|rencontre|enregistrement|video)\b`,
    String.raw`\bnuev[ao] (partido|grabacion|video)\b`,
    String.raw`\banaly[sz]e a\b`,
  ].join('|'))],
  ['jobs', anyOf(
    'process|processing|job|jobs|status|fail|failed|error|still running'
      + '|analysing|analyzing|analysis',
    'verarbeitung|verarbeitet|lauft|laeuft|laufen|auftrag|auftrage|auftraege'
      + '|status|fehler|fehlgeschlagen|analyse|analysiert',
    'lavorazione|elaborazione|lavoro|lavori|stato|errore|fallito|analisi|in corso',
    'traitement|traitements|en cours|statut|etat|erreur|echoue|analyse',
    'proceso|procesando|procesamiento|trabajo|trabajos|estado|error|fallado'
      + '|fallido|analisis|en curso',
  )],
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
  String.raw`\b(uberall|ueberall|quer durch)\b`,
  String.raw`\b(ovunque|dappertutto)\b`,
  String.raw`\b(partout)\b`,
  String.raw`\b(en todas partes|en cualquier parte)\b`,
  // Led by a locative preposition. "of all games" is possessive — "details of
  // all games" is the list — so `of` is deliberately not here; and the bare
  // "every game" or "any match" is the list too, which is why the preposition
  // is required rather than the quantifier alone.
  String.raw`\b(in|from) (any|every|other|all( the)?) (game|match|video|recording|games|matches|videos|recordings|content|assets)\b`,
  String.raw`\b(in|aus) (allen|jedem|anderen) (spielen|spiel|matches|videos|aufnahmen)\b`,
  String.raw`\b(in|da) (tutte le|ogni|altre) (partite|gare|registrazioni|video)\b`,
  String.raw`\b(dans|de) (tous les|toutes les|chaque|autres) (matchs|rencontres|videos|enregistrements)\b`,
  String.raw`\b(en|de) (todos los|todas las|cada|otros) (partidos|videos|grabaciones)\b`,
  String.raw`\bwhich (game|match|video|recording)\b`,
  String.raw`\bwelche[sr]? (spiel|match|video|aufnahme)\b`,
  String.raw`\bqual[ei] (partita|gara|video|registrazione)\b`,
  String.raw`\bquel(le)? (match|rencontre|video|enregistrement)\b`,
  String.raw`\bque (partido|video|grabacion)\b`,
  String.raw`\b(whole|entire) (desk|library|archive)\b`,
  String.raw`\b(library|archive|bibliothek|archiv|libreria|archivio|bibliotheque|archives|biblioteca|archivo)\b`,
  // A search verb aimed at a collection — "search the equestrian videos",
  // "look through the recordings". "games" is not in this noun set on purpose:
  // "show all equestrian games" is the list filtered by sport, not a search.
  String.raw`\b(search|look)( (in|through|across))?( the)?( (handball|equestrian|dressage|jumping|eventing))? (videos|recordings|footage|library|archive)\b`,
  String.raw`\b(suche|suchen|durchsuche|durchsuchen)( in)?( den| die| allen)? (videos|aufnahmen|bibliothek|archiv)\b`,
  String.raw`\b(cerca|cercare|cerchi)( (in|tra))?( le| i| tutte le)? (registrazioni|video|libreria|archivio)\b`,
  String.raw`\b(cherche|chercher|recherche|rechercher)( (dans|parmi))?( les| la)? (videos|enregistrements|bibliotheque|archives)\b`,
  String.raw`\b(busca|buscar)( (en|entre))?( los| las| la)? (videos|grabaciones|biblioteca|archivo)\b`,
].join('|'));

// The desk's key moments rather than one match's. "Show all key moments",
// "the best moments", "highlights" — the ranked shortlist, with no match named
// — is a question about everything on the desk. Answering it from whichever
// match happened to be open is how "no key moments were found" got said about
// a desk full of them. When a match *is* named, app.js sees that and keeps it
// to the match; this module cannot, because it does not hold the game list.
//
// Both orders are matched, because the adjective does not sit in the same
// place in all six languages: English and German put it first ("best moments",
// "besten Szenen") and Italian puts it last ("momenti migliori", "azioni
// migliori"), which is what the desk's own Italian opener says. Matching one
// order only is how this was wrong in the first place.
const BEST = 'key|best|top|greatest'
  + '|beste[nrs]?|wichtigste[nrs]?|starksten|staerksten'
  + '|migliori|miglior|chiave|principali|salienti'
  + '|meilleurs?|meilleures?|cles?|principa(ux|les)|forts'
  + '|mejores|mejor|clave|principales|destacadas|destacados';
const HIGHLIGHTS = 'moments|plays|highlights'
  + '|momente|szenen|hohepunkte|hoehepunkte|aktionen'
  + '|momenti|azioni|giocate'
  + '|actions|temps'
  + '|momentos|jugadas|acciones';
const DESK_MOMENTS = new RegExp([
  String.raw`\b(${BEST}) (${HIGHLIGHTS})\b`,
  String.raw`\b(${HIGHLIGHTS}) (${BEST})\b`,
  // The one-word name for the same thing in each language.
  String.raw`\b(highlights|hohepunkte|hoehepunkte|schlusselmomente|schluesselmomente|temps forts|destacados)\b`,
].join('|'));

// The one-match record: its teams, competition, venue, score and how it felt.
const GAME_DETAIL = new RegExp([
  String.raw`\bgame detail|\babout (the|this) (game|match)\b|\bwho played\b|\bfinal score\b`,
  String.raw`\bfind (the|a) (game|match)\b|\bwhat was the (game|match)\b|\bgame info\b|\bthe game\s*$`,
  String.raw`\bspieldaten\b|\bspielinfo\b|\buber (das|dieses) spiel\b|\bueber (das|dieses) spiel\b`,
  String.raw`\bwer hat gespielt\b|\bendstand\b|\bendergebnis\b`,
  String.raw`\bdati della partita\b|\binfo partita\b|\bchi ha giocato\b|\b(risultato|punteggio) finale\b`,
  String.raw`\bdetails du match\b|\binfos? du match\b|\bqui a joue\b|\b(score|resultat) final\b`,
  String.raw`\bdatos del partido\b|\binfo del partido\b|\bquien jugo\b|\b(resultado|marcador) final\b`,
].join('|'));


/**
 * Whether a question asked for the detail behind each row, not just the list.
 *
 * "show all game details" is a request for the records, so putting them behind
 * a Details button each is answering with the index instead of the answer.
 */
export function wantsDetail(question) {
  return new RegExp([
    String.raw`\bdetail|\bfull\b|\beverything\b|\bsummar`,
    String.raw`\bdaten\b|\bvollstandig|\bvollstaendig|\balles\b|\bzusammenfassung`,
    String.raw`\bdettagl|\bcompleto\b|\bcompleta\b|\btutto\b|\briepilogo`,
    String.raw`\bdetails?\b|\bcomplet\b|\bcomplete\b|\bresume`,
    String.raw`\bdetalle|\bcompleto\b|\bcompleta\b|\bresumen`,
  ].join('|')).test(key(question));
}


/**
 * The card a question asks for.
 *
 * Order decides ties, and it is not arbitrary: a question about the plays
 * inside a match and a question about the match are asked in almost the same
 * words, so what separates them is checked before anything more general.
 */
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
