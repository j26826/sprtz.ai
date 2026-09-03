/**
 * Translations for the editor.
 *
 * `en-GB` is the base rather than `en-US` because the product's own voice is
 * British — the agent says "analyse", the moment catalogue says "colour" — so
 * en-US is the variant that differs, not the other way round. Keeping that
 * direction means the American spellings live in one small override instead of
 * every other locale being diffed against a copy nobody edits.
 *
 * A missing key falls back to `en-GB` rather than rendering the key itself. A
 * half-translated locale should read as slightly English, not as
 * `header.signOut` in the middle of a sentence.
 */

const BASE = 'en-GB';

const STRINGS = {
  'en-GB': {
    'lang.name': 'English (UK)',
    'player.forbidden': 'The CDN refused this stream. Reload the page to renew the playback cookie; if it keeps happening, the package may need preparing again.',
    'pager.previous': 'Previous',
    'pager.next': 'Next',
    'pager.of': 'of',
    'games.none': 'No games yet. They are written when an analysis finishes.',
    'moments.none': 'No moments yet. They appear as the analysis finds them.',
    'moments.sortBy': 'Order',
    'moments.filtered': 'Showing',
    'moments.noMatch': 'Nothing matched',
    'moments.showAll': 'Show all',
    'moments.half.1': 'first half',
    'moments.half.2': 'second half',
    'moments.sort.score': 'Best first',
    'moments.sort.time': 'Match order',
    'reel.none': 'Nothing in the reel yet. Add a moment to start one.',
    'activity.none': 'No activity yet for this match.',
    'game.summary': 'Summary',
    'game.matchDate': 'Date',
    'game.groundedHomeTeam': 'Home team (from search)',
    'game.groundedAwayTeam': 'Away team (from search)',
    'game.groundedCompetition': 'Competition (from search)',
    'game.groundedVenue': 'Venue (from search)',
    'game.notIdentified': 'Nothing on screen identified this match',
    'moment.category': 'Category',
    'moment.play': 'Play this moment',
    'moment.details': 'Details',
    'moment.add': 'Add',
    'moment.remove': 'Remove',
    'moment.summary': 'Summary',
    'moment.description': 'Description',
    'moment.executionDetails': 'Execution',
    'moment.harmony': 'Harmony',
    'moment.class': 'Action',
    'moment.result': 'Result',
    'moment.start': 'Start',
    'moment.end': 'End',
    'moment.peak': 'Peak',
    'moment.participant': 'Participant',
    'moment.participantRole': 'Role',
    'moment.actionTeam': 'Team in action',
    'moment.score': 'Score',
    'moment.scoreboard': 'Score bug',
    'moment.confidence': 'Confidence',
    'moment.excitement': 'Excitement',
    'moment.highlightScore': 'Highlight score',
    'moment.evidence': 'Evidence',
    'moment.isGoal': 'Goal',
    'moment.id': 'Moment ID',
    'moment.yes': 'Yes',
    'sessions.deleteNote': 'This removes the conversation only. The match, its moments and its clips are not affected.',
    'sessions.noMatch': 'No match yet',
    'sessions.untitled': 'New session',
    'sessions.deleteConfirm': 'Delete the session',

    'sessions.empty': 'No sessions yet. Upload a match to start one.',

    'header.settings': 'Settings',
    'settings.title': 'Settings',
    'settings.close': 'Close',
    'settings.appLanguage': 'App language',
    'settings.appLanguageHint': "The language of buttons, labels and the agent's own replies.",
    'settings.followBrowser': 'Follow my browser',
    'settings.metadataLanguage': 'Metadata language',
    'settings.metadataLanguageHint':
      'The language moment descriptions and game summaries are written in. Applies '
      + 'to matches analysed from now on; those already analysed keep the language '
      + 'they were written in.',
    'settings.theme': 'Theme',
    'settings.themeHint': 'How the editor looks.',

    'header.newSession': 'New session',
    'header.signOut': 'Sign out',

    'signin.title': 'Sportscut',
    'signin.blurb': 'Sign in to analyse a match and cut it into short-form clips.',
    'signin.email': 'Email',
    'signin.password': 'Password',
    'signin.submit': 'Sign in',
    'signin.or': 'or',
    'signin.google': 'Continue with Google',

    'auth.badCredentials': 'That email and password do not match an account.',
    'auth.noAccount': 'No account with that email. Ask an administrator for access.',
    'auth.unauthorizedDomain':
      'This hostname is not in the Identity Platform authorised domains list.',
    'auth.notEnabled': 'That sign-in method is not enabled on this project.',
    'auth.failed': 'Sign-in failed.',

    'greeting':
      'Upload a match and I will watch all of it, mark every moment that clears '
      + 'confidence, and cut what you ask for.\n\nTell me what you want and I will cut it.',
    'action.ingest': 'Ingest a new game',
    'action.processing': "What's still processing?",
    'action.bestMoments': 'Show me the best moments',

    'ingest.chooseFile': 'Choose file',
    'ingest.noFile': 'No file chosen',
    'ingest.dropHere': 'Drop a file here, or choose one',
    'ingest.sport': 'Sport',
    'ingest.start': 'Start analysis',
    'ingest.uploading': 'Uploading…',
    'ingest.analysing': 'Analysing…',
    'ingest.useLastUpload': "Use last night's upload",
    'ingest.strandedNote':
      'reached storage but was never analysed. Pick it up instead of uploading again.',

    'jobs.none': 'No jobs yet.',
    'jobs.stalled': 'stalled',
    'jobs.noProgress': 'No progress for',
    'jobs.deadRun':
      'The run that owned this job is gone — a deploy or a restart ends one '
      + 'mid-flight, and nothing picks it up again. Retrying starts it over.',
    'jobs.retry': 'Retry',
    'jobs.analyseAgain': 'Analyse again',
    'jobs.cancel': 'Cancel',
    'jobs.delete': 'Delete',
    'jobs.deleteConfirm':
      'This removes the uploaded video, every detected moment, the clips and the '
      + 'game details. It cannot be undone.',

    'stage.ingest': 'Ingest',
    'stage.transcode': 'Playback',
    'stage.analysis': 'Analysis',
    'stage.clips': 'Clips',
    'stage.captions': 'Captions',

    'game.title': 'Game details',
    'game.none': 'No game details yet — they are written when the analysis finishes.',
    'game.moments': 'moments',
    'game.sport': 'Sport',
    'game.discipline': 'Discipline',
    'game.homeTeam': 'Home team',
    'game.awayTeam': 'Away team',
    'game.competition': 'Competition',
    'game.venue': 'Venue',
    'game.finalScore': 'Final score',
    'game.outcome': 'Outcome',
    'game.sentiment': 'Sentiment',
    'game.mood': 'Mood',
    'game.groundedBy': 'Fixture identified by web search',

    'reel.title': 'Working reel',
    'reel.generate': 'Generate video',
    'reel.reframe': 'Reframe 9:16',
    'reel.publish': 'Prepare publish',
    'reel.play': 'Play',

    'player.close': 'Close',
    'player.notPackaged': 'This match has not been packaged for playback yet.',
    'player.preparePlayback': 'Prepare playback',
    'player.notReady': 'Playback is not ready yet',

    'composer.send': 'Send',
    'composer.thinking': 'Scanning the game…',
    'agent.label': 'Agent',
    'publish.note': 'Sportscut packages clips for download — it does not post on your behalf.',
  },

  'en-US': {
    'lang.name': 'English (US)',

    'settings.metadataLanguageHint':
      'The language moment descriptions and game summaries are written in. Applies '
      + 'to matches analyzed from now on; those already analyzed keep the language '
      + 'they were written in.',
    // Only what actually differs. Everything else falls through to en-GB.
    'signin.blurb': 'Sign in to analyze a match and cut it into short-form clips.',
    'greeting':
      'Upload a match and I will watch all of it, mark every moment that clears '
      + 'confidence, and cut what you ask for.\n\nTell me what you want and I will cut it.',
    'ingest.start': 'Start analysis',
    'ingest.analysing': 'Analyzing…',
    'ingest.strandedNote':
      'reached storage but was never analyzed. Pick it up instead of uploading again.',
    'jobs.analyseAgain': 'Analyze again',
    'composer.thinking': 'Scanning the game…',
  },

  de: {
    'lang.name': 'Deutsch',
    'player.forbidden': 'Das CDN hat den Stream abgelehnt. Laden Sie die Seite neu, um das Wiedergabe-Cookie zu erneuern; bleibt es dabei, muss das Paket neu erstellt werden.',
    'pager.previous': 'Zurück',
    'pager.next': 'Weiter',
    'pager.of': 'von',
    'games.none': 'Noch keine Spiele. Sie entstehen, wenn eine Analyse fertig ist.',
    'moments.none': 'Noch keine Szenen. Sie erscheinen, sobald die Analyse sie findet.',
    'moments.sortBy': 'Reihenfolge',
    'moments.filtered': 'Angezeigt',
    'moments.noMatch': 'Keine Treffer für',
    'moments.showAll': 'Alle anzeigen',
    'moments.half.1': 'erste Halbzeit',
    'moments.half.2': 'zweite Halbzeit',
    'moments.sort.score': 'Beste zuerst',
    'moments.sort.time': 'Spielverlauf',
    'reel.none': 'Noch nichts im Zusammenschnitt. Fügen Sie eine Szene hinzu.',
    'activity.none': 'Noch keine Aktivität für dieses Spiel.',
    'game.summary': 'Zusammenfassung',
    'game.matchDate': 'Datum',
    'game.groundedHomeTeam': 'Heimmannschaft (aus Websuche)',
    'game.groundedAwayTeam': 'Gastmannschaft (aus Websuche)',
    'game.groundedCompetition': 'Wettbewerb (aus Websuche)',
    'game.groundedVenue': 'Spielstätte (aus Websuche)',
    'game.notIdentified': 'Nichts auf dem Bild hat dieses Spiel identifiziert',
    'moment.category': 'Kategorie',
    'moment.play': 'Diese Szene abspielen',
    'moment.details': 'Details',
    'moment.add': 'Hinzufügen',
    'moment.remove': 'Entfernen',
    'moment.summary': 'Zusammenfassung',
    'moment.description': 'Beschreibung',
    'moment.executionDetails': 'Ausführung',
    'moment.harmony': 'Harmonie',
    'moment.class': 'Aktion',
    'moment.result': 'Ergebnis',
    'moment.start': 'Beginn',
    'moment.end': 'Ende',
    'moment.peak': 'Höhepunkt',
    'moment.participant': 'Beteiligte Person',
    'moment.participantRole': 'Rolle',
    'moment.actionTeam': 'Mannschaft am Ball',
    'moment.score': 'Spielstand',
    'moment.scoreboard': 'Anzeigetafel',
    'moment.confidence': 'Konfidenz',
    'moment.excitement': 'Spannung',
    'moment.highlightScore': 'Highlight-Wert',
    'moment.evidence': 'Belege',
    'moment.isGoal': 'Tor',
    'moment.id': 'Szenen-ID',
    'moment.yes': 'Ja',
    'sessions.deleteNote': 'Damit wird nur das Gespräch entfernt. Das Spiel, seine Szenen und Clips bleiben erhalten.',
    'sessions.noMatch': 'Noch kein Spiel',
    'sessions.untitled': 'Neue Sitzung',
    'sessions.deleteConfirm': 'Sitzung löschen',

    'sessions.empty': 'Noch keine Sitzungen. Laden Sie ein Spiel hoch, um zu beginnen.',

    'header.settings': 'Einstellungen',
    'settings.title': 'Einstellungen',
    'settings.close': 'Schließen',
    'settings.appLanguage': 'Sprache der App',
    'settings.appLanguageHint':
      'Die Sprache von Schaltflächen, Beschriftungen und den Antworten des Agenten.',
    'settings.followBrowser': 'Browsereinstellung folgen',
    'settings.metadataLanguage': 'Sprache der Metadaten',
    'settings.metadataLanguageHint':
      'Die Sprache, in der Szenenbeschreibungen und Spielzusammenfassungen verfasst '
      + 'werden. Gilt für künftige Analysen; bereits analysierte Spiele behalten ihre '
      + 'bisherige Sprache.',
    'settings.theme': 'Design',
    'settings.themeHint': 'Das Erscheinungsbild des Editors.',

    'header.newSession': 'Neue Sitzung',
    'header.signOut': 'Abmelden',

    'signin.title': 'Sportscut',
    'signin.blurb': 'Melden Sie sich an, um ein Spiel zu analysieren und in Kurzclips zu schneiden.',
    'signin.email': 'E-Mail',
    'signin.password': 'Passwort',
    'signin.submit': 'Anmelden',
    'signin.or': 'oder',
    'signin.google': 'Weiter mit Google',

    'auth.badCredentials': 'E-Mail und Passwort gehören zu keinem Konto.',
    'auth.noAccount': 'Kein Konto mit dieser E-Mail. Bitte wenden Sie sich an eine Administratorin oder einen Administrator.',
    'auth.unauthorizedDomain':
      'Dieser Hostname steht nicht in der Liste autorisierter Domains in Identity Platform.',
    'auth.notEnabled': 'Diese Anmeldemethode ist in diesem Projekt nicht aktiviert.',
    'auth.failed': 'Anmeldung fehlgeschlagen.',

    'greeting':
      'Laden Sie ein Spiel hoch. Ich sehe es vollständig durch, markiere jede Szene, '
      + 'die sicher genug ist, und schneide, was Sie brauchen.\n\nSagen Sie mir, was Sie wollen.',
    'action.ingest': 'Neues Spiel einlesen',
    'action.processing': 'Was läuft gerade noch?',
    'action.bestMoments': 'Zeig mir die besten Szenen',

    'ingest.chooseFile': 'Datei wählen',
    'ingest.noFile': 'Keine Datei gewählt',
    'ingest.dropHere': 'Datei hierher ziehen oder auswählen',
    'ingest.sport': 'Sportart',
    'ingest.start': 'Analyse starten',
    'ingest.uploading': 'Wird hochgeladen…',
    'ingest.analysing': 'Wird analysiert…',
    'ingest.useLastUpload': 'Letzten Upload verwenden',
    'ingest.strandedNote':
      'wurde gespeichert, aber nie analysiert. Übernehmen Sie ihn, statt erneut hochzuladen.',

    'jobs.none': 'Noch keine Aufträge.',
    'jobs.stalled': 'steht still',
    'jobs.noProgress': 'Kein Fortschritt seit',
    'jobs.deadRun':
      'Der Durchlauf für diesen Auftrag existiert nicht mehr — ein Deployment oder '
      + 'Neustart beendet ihn mittendrin, und niemand nimmt ihn wieder auf. '
      + 'Ein neuer Versuch startet von vorn.',
    'jobs.retry': 'Erneut versuchen',
    'jobs.analyseAgain': 'Erneut analysieren',
    'jobs.cancel': 'Abbrechen',
    'jobs.delete': 'Löschen',
    'jobs.deleteConfirm':
      'Damit werden das hochgeladene Video, alle erkannten Szenen, die Clips und die '
      + 'Spieldaten entfernt. Das lässt sich nicht rückgängig machen.',

    'stage.ingest': 'Einlesen',
    'stage.transcode': 'Wiedergabe',
    'stage.analysis': 'Analyse',
    'stage.clips': 'Clips',
    'stage.captions': 'Texte',

    'game.title': 'Spieldaten',
    'game.none': 'Noch keine Spieldaten — sie entstehen mit dem Abschluss der Analyse.',
    'game.moments': 'Szenen',
    'game.sport': 'Sportart',
    'game.discipline': 'Disziplin',
    'game.homeTeam': 'Heimmannschaft',
    'game.awayTeam': 'Gastmannschaft',
    'game.competition': 'Wettbewerb',
    'game.venue': 'Spielstätte',
    'game.finalScore': 'Endstand',
    'game.outcome': 'Ergebnis',
    'game.sentiment': 'Stimmung',
    'game.mood': 'Charakter',
    'game.groundedBy': 'Begegnung über Websuche ermittelt',

    'reel.title': 'Aktueller Zusammenschnitt',
    'reel.generate': 'Video erzeugen',
    'reel.reframe': 'Auf 9:16 umrahmen',
    'reel.publish': 'Veröffentlichung vorbereiten',
    'reel.play': 'Abspielen',

    'player.close': 'Schließen',
    'player.notPackaged': 'Dieses Spiel wurde noch nicht für die Wiedergabe aufbereitet.',
    'player.preparePlayback': 'Wiedergabe vorbereiten',
    'player.notReady': 'Wiedergabe ist noch nicht bereit',

    'composer.send': 'Senden',
    'composer.thinking': 'Spiel wird gesichtet…',
    'agent.label': 'Agent',
    'publish.note':
      'Sportscut stellt Clips zum Download bereit — es veröffentlicht nichts in Ihrem Namen.',
  },

  it: {
    'lang.name': 'Italiano',
    'player.forbidden': 'La CDN ha rifiutato lo stream. Ricarica la pagina per rinnovare il cookie di riproduzione; se persiste, il pacchetto va ricreato.',
    'pager.previous': 'Precedente',
    'pager.next': 'Successivo',
    'pager.of': 'di',
    'games.none': 'Ancora nessuna partita. Vengono scritte al termine di un’analisi.',
    'moments.none': 'Ancora nessuna azione. Compaiono man mano che l’analisi le trova.',
    'moments.sortBy': 'Ordine',
    'moments.filtered': 'Mostrate',
    'moments.noMatch': 'Nessun risultato per',
    'moments.showAll': 'Mostra tutte',
    'moments.half.1': 'primo tempo',
    'moments.half.2': 'secondo tempo',
    'moments.sort.score': 'Migliori prima',
    'moments.sort.time': 'Ordine di gioco',
    'reel.none': 'Il montaggio è vuoto. Aggiungi un’azione per iniziare.',
    'activity.none': 'Ancora nessuna attività per questa partita.',
    'game.summary': 'Sintesi',
    'game.matchDate': 'Data',
    'game.groundedHomeTeam': 'Squadra di casa (da ricerca web)',
    'game.groundedAwayTeam': 'Squadra ospite (da ricerca web)',
    'game.groundedCompetition': 'Competizione (da ricerca web)',
    'game.groundedVenue': 'Impianto (da ricerca web)',
    'game.notIdentified': 'Nulla sullo schermo ha identificato questa partita',
    'moment.category': 'Categoria',
    'moment.play': 'Riproduci questa azione',
    'moment.details': 'Dettagli',
    'moment.add': 'Aggiungi',
    'moment.remove': 'Rimuovi',
    'moment.summary': 'Sintesi',
    'moment.description': 'Descrizione',
    'moment.executionDetails': 'Esecuzione',
    'moment.harmony': 'Armonia',
    'moment.class': 'Azione',
    'moment.result': 'Esito',
    'moment.start': 'Inizio',
    'moment.end': 'Fine',
    'moment.peak': 'Culmine',
    'moment.participant': 'Protagonista',
    'moment.participantRole': 'Ruolo',
    'moment.actionTeam': 'Squadra in azione',
    'moment.score': 'Punteggio',
    'moment.scoreboard': 'Tabellone',
    'moment.confidence': 'Confidenza',
    'moment.excitement': 'Intensità',
    'moment.highlightScore': 'Punteggio highlight',
    'moment.evidence': 'Riscontri',
    'moment.isGoal': 'Gol',
    'moment.id': 'ID azione',
    'moment.yes': 'Sì',
    'sessions.deleteNote': 'Verrà rimossa solo la conversazione. La partita, le sue azioni e le clip restano.',
    'sessions.noMatch': 'Nessuna partita',
    'sessions.untitled': 'Nuova sessione',
    'sessions.deleteConfirm': 'Eliminare la sessione',

    'sessions.empty': 'Ancora nessuna sessione. Carica una partita per iniziarne una.',

    'header.settings': 'Impostazioni',
    'settings.title': 'Impostazioni',
    'settings.close': 'Chiudi',
    'settings.appLanguage': "Lingua dell'app",
    'settings.appLanguageHint': "La lingua di pulsanti, etichette e risposte dell'agente.",
    'settings.followBrowser': 'Segui il browser',
    'settings.metadataLanguage': 'Lingua dei metadati',
    'settings.metadataLanguageHint':
      'La lingua in cui vengono scritte le descrizioni delle azioni e i riepiloghi '
      + 'della partita. Vale per le analisi future; le partite già analizzate '
      + 'mantengono la lingua di allora.',
    'settings.theme': 'Tema',
    'settings.themeHint': "L'aspetto dell'editor.",

    'header.newSession': 'Nuova sessione',
    'header.signOut': 'Esci',

    'signin.title': 'Sportscut',
    'signin.blurb': 'Accedi per analizzare una partita e montarla in clip brevi.',
    'signin.email': 'Email',
    'signin.password': 'Password',
    'signin.submit': 'Accedi',
    'signin.or': 'oppure',
    'signin.google': 'Continua con Google',

    'auth.badCredentials': 'Email e password non corrispondono a nessun account.',
    'auth.noAccount': "Nessun account con questa email. Chiedi l'accesso a un amministratore.",
    'auth.unauthorizedDomain':
      "Questo host non è nell'elenco dei domini autorizzati di Identity Platform.",
    'auth.notEnabled': 'Questo metodo di accesso non è abilitato in questo progetto.',
    'auth.failed': 'Accesso non riuscito.',

    'greeting':
      'Carica una partita: la guardo tutta, segno ogni azione che supera la soglia '
      + 'di confidenza e monto quello che mi chiedi.\n\nDimmi cosa ti serve.',
    'action.ingest': 'Carica una nuova partita',
    'action.processing': 'Cosa è ancora in lavorazione?',
    'action.bestMoments': 'Mostrami le azioni migliori',

    'ingest.chooseFile': 'Scegli file',
    'ingest.noFile': 'Nessun file scelto',
    'ingest.dropHere': 'Trascina un file qui, oppure scegline uno',
    'ingest.sport': 'Sport',
    'ingest.start': 'Avvia analisi',
    'ingest.uploading': 'Caricamento…',
    'ingest.analysing': 'Analisi…',
    'ingest.useLastUpload': "Usa l'ultimo caricamento",
    'ingest.strandedNote':
      'è arrivato in archivio ma non è mai stato analizzato. Riprendilo invece di ricaricarlo.',

    'jobs.none': 'Nessun lavoro per ora.',
    'jobs.stalled': 'fermo',
    'jobs.noProgress': 'Nessun avanzamento da',
    'jobs.deadRun':
      'Il processo che seguiva questo lavoro non esiste più — un rilascio o un riavvio '
      + 'lo interrompe a metà e nessuno lo riprende. Riprovare lo fa ripartire da capo.',
    'jobs.retry': 'Riprova',
    'jobs.analyseAgain': 'Analizza di nuovo',
    'jobs.cancel': 'Annulla',
    'jobs.delete': 'Elimina',
    'jobs.deleteConfirm':
      'Verranno rimossi il video caricato, tutte le azioni rilevate, le clip e i dati '
      + 'della partita. Non si può annullare.',

    'stage.ingest': 'Acquisizione',
    'stage.transcode': 'Riproduzione',
    'stage.analysis': 'Analisi',
    'stage.clips': 'Clip',
    'stage.captions': 'Testi',

    'game.title': 'Dati della partita',
    'game.none': "Nessun dato ancora — vengono scritti al termine dell'analisi.",
    'game.moments': 'azioni',
    'game.sport': 'Sport',
    'game.discipline': 'Disciplina',
    'game.homeTeam': 'Squadra di casa',
    'game.awayTeam': 'Squadra ospite',
    'game.competition': 'Competizione',
    'game.venue': 'Impianto',
    'game.finalScore': 'Punteggio finale',
    'game.outcome': 'Esito',
    'game.sentiment': 'Tono',
    'game.mood': 'Carattere',
    'game.groundedBy': 'Partita identificata tramite ricerca web',

    'reel.title': 'Montaggio in corso',
    'reel.generate': 'Genera video',
    'reel.reframe': 'Inquadra 9:16',
    'reel.publish': 'Prepara la pubblicazione',
    'reel.play': 'Riproduci',

    'player.close': 'Chiudi',
    'player.notPackaged': 'Questa partita non è ancora stata preparata per la riproduzione.',
    'player.preparePlayback': 'Prepara la riproduzione',
    'player.notReady': 'La riproduzione non è ancora pronta',

    'composer.send': 'Invia',
    'composer.thinking': 'Sto guardando la partita…',
    'agent.label': 'Agente',
    'publish.note':
      'Sportscut prepara le clip da scaricare — non pubblica nulla per tuo conto.',
  },

  fr: {
    'lang.name': 'Français',
    'player.forbidden': 'Le CDN a refusé ce flux. Rechargez la page pour renouveler le cookie de lecture ; si cela persiste, le package doit être régénéré.',
    'pager.previous': 'Précédent',
    'pager.next': 'Suivant',
    'pager.of': 'sur',
    'games.none': 'Aucun match pour le moment. Ils sont écrits à la fin d’une analyse.',
    'moments.none': 'Aucune action pour le moment. Elles apparaissent au fil de l’analyse.',
    'moments.sortBy': 'Ordre',
    'moments.filtered': 'Affichées',
    'moments.noMatch': 'Aucun résultat pour',
    'moments.showAll': 'Tout afficher',
    'moments.half.1': 'première mi-temps',
    'moments.half.2': 'seconde mi-temps',
    'moments.sort.score': 'Meilleures d’abord',
    'moments.sort.time': 'Ordre du match',
    'reel.none': 'Le montage est vide. Ajoutez une action pour commencer.',
    'activity.none': 'Aucune activité pour ce match.',
    'game.summary': 'Résumé',
    'game.matchDate': 'Date',
    'game.groundedHomeTeam': 'Équipe à domicile (recherche web)',
    'game.groundedAwayTeam': 'Équipe visiteuse (recherche web)',
    'game.groundedCompetition': 'Compétition (recherche web)',
    'game.groundedVenue': 'Salle (recherche web)',
    'game.notIdentified': 'Rien à l’écran n’a permis d’identifier ce match',
    'moment.category': 'Catégorie',
    'moment.play': 'Lire cette action',
    'moment.details': 'Détails',
    'moment.add': 'Ajouter',
    'moment.remove': 'Retirer',
    'moment.summary': 'Résumé',
    'moment.description': 'Description',
    'moment.executionDetails': 'Exécution',
    'moment.harmony': 'Harmonie',
    'moment.class': 'Action',
    'moment.result': 'Issue',
    'moment.start': 'Début',
    'moment.end': 'Fin',
    'moment.peak': 'Moment clé',
    'moment.participant': 'Protagoniste',
    'moment.participantRole': 'Rôle',
    'moment.actionTeam': 'Équipe en action',
    'moment.score': 'Score',
    'moment.scoreboard': 'Bandeau de score',
    'moment.confidence': 'Confiance',
    'moment.excitement': 'Intensité',
    'moment.highlightScore': 'Score de temps fort',
    'moment.evidence': 'Indices',
    'moment.isGoal': 'But',
    'moment.id': 'ID de l’action',
    'moment.yes': 'Oui',
    'sessions.deleteNote': 'Seule la conversation est supprimée. Le match, ses actions et ses clips ne sont pas touchés.',
    'sessions.noMatch': 'Pas encore de match',
    'sessions.untitled': 'Nouvelle session',
    'sessions.deleteConfirm': 'Supprimer la session',

    'sessions.empty': 'Aucune session pour le moment. Importez un match pour en démarrer une.',

    'header.settings': 'Paramètres',
    'settings.title': 'Paramètres',
    'settings.close': 'Fermer',
    'settings.appLanguage': "Langue de l'application",
    'settings.appLanguageHint':
      "La langue des boutons, des libellés et des réponses de l'agent.",
    'settings.followBrowser': 'Suivre mon navigateur',
    'settings.metadataLanguage': 'Langue des métadonnées',
    'settings.metadataLanguageHint':
      'La langue des descriptions d’actions et des résumés de match. Vaut pour les '
      + 'analyses à venir ; les matchs déjà analysés gardent leur langue d’origine.',
    'settings.theme': 'Thème',
    'settings.themeHint': "L'apparence de l'éditeur.",

    'header.newSession': 'Nouvelle session',
    'header.signOut': 'Se déconnecter',

    'signin.title': 'Sportscut',
    'signin.blurb': 'Connectez-vous pour analyser un match et le monter en clips courts.',
    'signin.email': 'E-mail',
    'signin.password': 'Mot de passe',
    'signin.submit': 'Se connecter',
    'signin.or': 'ou',
    'signin.google': 'Continuer avec Google',

    'auth.badCredentials': 'Cet e-mail et ce mot de passe ne correspondent à aucun compte.',
    'auth.noAccount':
      'Aucun compte avec cet e-mail. Demandez un accès à un administrateur.',
    'auth.unauthorizedDomain':
      "Ce nom d'hôte ne figure pas dans les domaines autorisés d'Identity Platform.",
    'auth.notEnabled': "Cette méthode de connexion n'est pas activée sur ce projet.",
    'auth.failed': 'Échec de la connexion.',

    'greeting':
      "Importez un match : je le regarde en entier, je marque chaque action qui "
      + "dépasse le seuil de confiance, et je monte ce que vous demandez.\n\n"
      + 'Dites-moi ce qu’il vous faut.',
    'action.ingest': 'Importer un nouveau match',
    'action.processing': 'Qu’est-ce qui est encore en cours ?',
    'action.bestMoments': 'Montrez-moi les meilleures actions',

    'ingest.chooseFile': 'Choisir un fichier',
    'ingest.noFile': 'Aucun fichier choisi',
    'ingest.dropHere': 'Déposez un fichier ici, ou choisissez-en un',
    'ingest.sport': 'Sport',
    'ingest.start': "Lancer l'analyse",
    'ingest.uploading': 'Import en cours…',
    'ingest.analysing': 'Analyse en cours…',
    'ingest.useLastUpload': 'Utiliser le dernier import',
    'ingest.strandedNote':
      "est bien arrivé dans le stockage mais n'a jamais été analysé. Reprenez-le "
      + 'plutôt que de le réimporter.',

    'jobs.none': 'Aucun traitement pour le moment.',
    'jobs.stalled': 'à l’arrêt',
    'jobs.noProgress': 'Aucune progression depuis',
    'jobs.deadRun':
      "Le traitement qui suivait ce match n'existe plus — un déploiement ou un "
      + "redémarrage l'interrompt en cours, et rien ne le reprend. Réessayer le "
      + 'relance depuis le début.',
    'jobs.retry': 'Réessayer',
    'jobs.analyseAgain': 'Analyser à nouveau',
    'jobs.cancel': 'Annuler',
    'jobs.delete': 'Supprimer',
    'jobs.deleteConfirm':
      'Cela supprime la vidéo importée, toutes les actions détectées, les clips et '
      + 'les données du match. C’est irréversible.',

    'stage.ingest': 'Import',
    'stage.transcode': 'Lecture',
    'stage.analysis': 'Analyse',
    'stage.clips': 'Clips',
    'stage.captions': 'Textes',

    'game.title': 'Détails du match',
    'game.none': "Pas encore de détails — ils sont écrits à la fin de l'analyse.",
    'game.moments': 'actions',
    'game.sport': 'Sport',
    'game.discipline': 'Discipline',
    'game.homeTeam': 'Équipe à domicile',
    'game.awayTeam': 'Équipe visiteuse',
    'game.competition': 'Compétition',
    'game.venue': 'Salle',
    'game.finalScore': 'Score final',
    'game.outcome': 'Issue',
    'game.sentiment': 'Tonalité',
    'game.mood': 'Ambiance',
    'game.groundedBy': 'Rencontre identifiée par recherche web',

    'reel.title': 'Montage en cours',
    'reel.generate': 'Générer la vidéo',
    'reel.reframe': 'Recadrer en 9:16',
    'reel.publish': 'Préparer la publication',
    'reel.play': 'Lire',

    'player.close': 'Fermer',
    'player.notPackaged': "Ce match n'a pas encore été préparé pour la lecture.",
    'player.preparePlayback': 'Préparer la lecture',
    'player.notReady': "La lecture n'est pas encore prête",

    'composer.send': 'Envoyer',
    'composer.thinking': 'Analyse du match…',
    'agent.label': 'Agent',
    'publish.note':
      'Sportscut prépare des clips à télécharger — il ne publie rien à votre place.',
  },

  es: {
    'lang.name': 'Español',
    'player.forbidden': 'La CDN rechazó el stream. Recarga la página para renovar la cookie de reproducción; si continúa, hay que volver a preparar el paquete.',
    'pager.previous': 'Anterior',
    'pager.next': 'Siguiente',
    'pager.of': 'de',
    'games.none': 'Aún no hay partidos. Se escriben al terminar un análisis.',
    'moments.none': 'Aún no hay jugadas. Aparecen a medida que el análisis las encuentra.',
    'moments.sortBy': 'Orden',
    'moments.filtered': 'Mostradas',
    'moments.noMatch': 'Sin resultados para',
    'moments.showAll': 'Mostrar todas',
    'moments.half.1': 'primera parte',
    'moments.half.2': 'segunda parte',
    'moments.sort.score': 'Mejores primero',
    'moments.sort.time': 'Orden del partido',
    'reel.none': 'El montaje está vacío. Añade una jugada para empezar.',
    'activity.none': 'Aún no hay actividad para este partido.',
    'game.summary': 'Resumen',
    'game.matchDate': 'Fecha',
    'game.groundedHomeTeam': 'Equipo local (de búsqueda web)',
    'game.groundedAwayTeam': 'Equipo visitante (de búsqueda web)',
    'game.groundedCompetition': 'Competición (de búsqueda web)',
    'game.groundedVenue': 'Recinto (de búsqueda web)',
    'game.notIdentified': 'Nada en pantalla identificó este partido',
    'moment.category': 'Categoría',
    'moment.play': 'Reproducir esta jugada',
    'moment.details': 'Detalles',
    'moment.add': 'Añadir',
    'moment.remove': 'Quitar',
    'moment.summary': 'Resumen',
    'moment.description': 'Descripción',
    'moment.executionDetails': 'Ejecución',
    'moment.harmony': 'Armonía',
    'moment.class': 'Acción',
    'moment.result': 'Desenlace',
    'moment.start': 'Inicio',
    'moment.end': 'Fin',
    'moment.peak': 'Punto álgido',
    'moment.participant': 'Protagonista',
    'moment.participantRole': 'Rol',
    'moment.actionTeam': 'Equipo en acción',
    'moment.score': 'Marcador',
    'moment.scoreboard': 'Marcador en pantalla',
    'moment.confidence': 'Confianza',
    'moment.excitement': 'Intensidad',
    'moment.highlightScore': 'Puntuación de highlight',
    'moment.evidence': 'Indicios',
    'moment.isGoal': 'Gol',
    'moment.id': 'ID de la jugada',
    'moment.yes': 'Sí',
    'sessions.deleteNote': 'Solo se elimina la conversación. El partido, sus jugadas y sus clips no se ven afectados.',
    'sessions.noMatch': 'Sin partido todavía',
    'sessions.untitled': 'Nueva sesión',
    'sessions.deleteConfirm': 'Eliminar la sesión',

    'sessions.empty': 'Aún no hay sesiones. Sube un partido para empezar una.',

    'header.settings': 'Ajustes',
    'settings.title': 'Ajustes',
    'settings.close': 'Cerrar',
    'settings.appLanguage': 'Idioma de la aplicación',
    'settings.appLanguageHint': 'El idioma de botones, etiquetas y respuestas del agente.',
    'settings.followBrowser': 'Seguir mi navegador',
    'settings.metadataLanguage': 'Idioma de los metadatos',
    'settings.metadataLanguageHint':
      'El idioma en que se escriben las descripciones de jugadas y los resúmenes del '
      + 'partido. Se aplica a los análisis futuros; los partidos ya analizados '
      + 'conservan su idioma.',
    'settings.theme': 'Tema',
    'settings.themeHint': 'El aspecto del editor.',

    'header.newSession': 'Nueva sesión',
    'header.signOut': 'Cerrar sesión',

    'signin.title': 'Sportscut',
    'signin.blurb': 'Inicia sesión para analizar un partido y montarlo en clips cortos.',
    'signin.email': 'Correo electrónico',
    'signin.password': 'Contraseña',
    'signin.submit': 'Iniciar sesión',
    'signin.or': 'o',
    'signin.google': 'Continuar con Google',

    'auth.badCredentials': 'Ese correo y contraseña no corresponden a ninguna cuenta.',
    'auth.noAccount': 'No hay cuenta con ese correo. Pide acceso a un administrador.',
    'auth.unauthorizedDomain':
      'Este host no está en la lista de dominios autorizados de Identity Platform.',
    'auth.notEnabled': 'Ese método de inicio de sesión no está habilitado en este proyecto.',
    'auth.failed': 'No se pudo iniciar sesión.',

    'greeting':
      'Sube un partido: lo veo entero, marco cada jugada que supera el umbral de '
      + 'confianza y monto lo que me pidas.\n\nDime qué necesitas.',
    'action.ingest': 'Cargar un partido nuevo',
    'action.processing': '¿Qué sigue en proceso?',
    'action.bestMoments': 'Muéstrame las mejores jugadas',

    'ingest.chooseFile': 'Elegir archivo',
    'ingest.noFile': 'Ningún archivo elegido',
    'ingest.dropHere': 'Arrastra un archivo aquí, o elige uno',
    'ingest.sport': 'Deporte',
    'ingest.start': 'Iniciar análisis',
    'ingest.uploading': 'Subiendo…',
    'ingest.analysing': 'Analizando…',
    'ingest.useLastUpload': 'Usar la última subida',
    'ingest.strandedNote':
      'llegó al almacenamiento pero nunca se analizó. Retómalo en vez de subirlo otra vez.',

    'jobs.none': 'Todavía no hay trabajos.',
    'jobs.stalled': 'detenido',
    'jobs.noProgress': 'Sin avance desde hace',
    'jobs.deadRun':
      'El proceso que llevaba este trabajo ya no existe — un despliegue o un reinicio '
      + 'lo corta a medias y nadie lo retoma. Reintentar lo empieza de nuevo.',
    'jobs.retry': 'Reintentar',
    'jobs.analyseAgain': 'Analizar de nuevo',
    'jobs.cancel': 'Cancelar',
    'jobs.delete': 'Eliminar',
    'jobs.deleteConfirm':
      'Esto elimina el vídeo subido, todas las jugadas detectadas, los clips y los '
      + 'datos del partido. No se puede deshacer.',

    'stage.ingest': 'Ingesta',
    'stage.transcode': 'Reproducción',
    'stage.analysis': 'Análisis',
    'stage.clips': 'Clips',
    'stage.captions': 'Textos',

    'game.title': 'Datos del partido',
    'game.none': 'Aún no hay datos — se escriben al terminar el análisis.',
    'game.moments': 'jugadas',
    'game.sport': 'Deporte',
    'game.discipline': 'Disciplina',
    'game.homeTeam': 'Equipo local',
    'game.awayTeam': 'Equipo visitante',
    'game.competition': 'Competición',
    'game.venue': 'Recinto',
    'game.finalScore': 'Resultado final',
    'game.outcome': 'Desenlace',
    'game.sentiment': 'Tono',
    'game.mood': 'Carácter',
    'game.groundedBy': 'Encuentro identificado mediante búsqueda web',

    'reel.title': 'Montaje en curso',
    'reel.generate': 'Generar vídeo',
    'reel.reframe': 'Reencuadrar a 9:16',
    'reel.publish': 'Preparar publicación',
    'reel.play': 'Reproducir',

    'player.close': 'Cerrar',
    'player.notPackaged': 'Este partido todavía no se ha preparado para reproducción.',
    'player.preparePlayback': 'Preparar reproducción',
    'player.notReady': 'La reproducción aún no está lista',

    'composer.send': 'Enviar',
    'composer.thinking': 'Revisando el partido…',
    'agent.label': 'Agente',
    'publish.note':
      'Sportscut prepara clips para descargar — no publica nada en tu nombre.',
  },
};

export const LOCALES = Object.keys(STRINGS);

const STORAGE_KEY = 'sportscut.locale';

/**
 * The locale to start in.
 *
 * A stored choice wins, then the browser's languages in the order it lists
 * them. `en` alone resolves to en-GB because that is the base; a US browser
 * says `en-US` explicitly and gets it.
 */
export function detectLocale() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && STRINGS[stored]) return stored;
  } catch { /* private windows throw on access; fall through to the browser */ }

  for (const tag of navigator.languages || [navigator.language || '']) {
    if (STRINGS[tag]) return tag;
    const base = String(tag).split('-')[0];
    const match = LOCALES.find((l) => l === base || l.split('-')[0] === base);
    if (match) return match;
  }
  return BASE;
}

let current = BASE;

export function setLocale(locale) {
  current = STRINGS[locale] ? locale : BASE;
  try {
    localStorage.setItem(STORAGE_KEY, current);
  } catch { /* a preference that cannot be stored is still good for this tab */ }
  document.documentElement.lang = current;
  return current;
}

export function getLocale() {
  return current;
}

export function localeName(locale) {
  return STRINGS[locale]?.['lang.name'] || locale;
}

/** Translate. Falls back to en-GB, then to the key, so nothing renders blank. */
export function t(key) {
  const value = STRINGS[current]?.[key];
  if (value !== undefined) return value;
  return STRINGS[BASE][key] ?? key;
}
