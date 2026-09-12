"""Firestore access and embedding generation.

This module is the only place that knows the document shape, so the agents, the
API and the UI all agree on one contract.

The Google client libraries are imported inside the accessors rather than at
module scope. They are startlingly expensive to import on Cloud Run — measured
on a live revision, `google.cloud.firestore` alone took 45s and `google.genai`
another 27s, about 100s before the process could bind a port, against 6s on a
developer machine. Importing them lazily lets the server answer its health
check in a couple of seconds and pay that cost on the first tool call instead,
where it is warm for the life of the instance.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from google.cloud import firestore

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-south1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "768"))
RERANK_MODEL = os.environ.get("RERANK_MODEL", "gemini-3.6-flash")
# Its own location, because the model and the location move together: the
# newer Flash generation is served only through Vertex's `global` location in
# this project, while the embedding model is regional and stays on LOCATION.
# The fallback is LOCATION rather than an opinion: unset means "wherever the
# regional client already goes", which is what every test and local run gets.
# Terraform is what points it at `global`.
RERANK_LOCATION = os.environ.get("RERANK_LOCATION", LOCATION)
# How many candidates to pull from the vector index per result asked for. The
# reranker can only reorder what retrieval gave it, so over-fetching is what
# actually buys the quality; 4x is where the gain flattens on this corpus.
RERANK_OVERFETCH = int(os.environ.get("RERANK_OVERFETCH", "4"))
RERANK_MAX_CANDIDATES = int(os.environ.get("RERANK_MAX_CANDIDATES", "60"))

_db: "firestore.Client | None" = None
_genai_client: Any = None


def db() -> "firestore.Client":
    global _db
    if _db is None:
        from google.cloud import firestore

        _db = firestore.Client(project=PROJECT_ID or None)
    return _db


def genai_client() -> Any:
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    return _genai_client


_rerank_client = None


def rerank_client() -> Any:
    """A client for the rerank model's location.

    The regional client itself when reranking is not going anywhere different
    — one object, one seam, so whatever a test injects there is what the
    reranker uses. A separate client only when the locations differ.
    """
    global _rerank_client
    if RERANK_LOCATION == LOCATION:
        return genai_client()
    if _rerank_client is None:
        from google import genai

        _rerank_client = genai.Client(vertexai=True, project=PROJECT_ID, location=RERANK_LOCATION)
    return _rerank_client


def now() -> datetime:
    return datetime.now(UTC)


# --- Embeddings ---------------------------------------------------------------


def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed with gemini-embedding-001 at the width the Firestore index expects.

    The index dimension is fixed at creation, so output_dimensionality must match
    EMBEDDING_DIMENSIONS exactly or every write is rejected at query time rather
    than at write time.
    """
    if not texts:
        return []

    from google.genai import types

    vectors: list[list[float]] = []
    # The endpoint caps how many inputs one call may carry.
    for start in range(0, len(texts), 20):
        chunk = texts[start : start + 20]
        response = genai_client().models.embed_content(
            model=EMBEDDING_MODEL,
            contents=chunk,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBEDDING_DIMENSIONS,
            ),
        )
        vectors.extend(list(e.values) for e in response.embeddings)
    return vectors


def action_play_text(moment: dict[str, Any]) -> str:
    """What gets embedded for a moment.

    Semantic search has to answer "double save by the keeper" and "who scored
    from the wing", so the vector has to carry the outcome, the participant and
    their role — not just the type label and the prose. A description alone
    matches on narration and misses the structured facts beside it.
    """
    parts = [
        moment.get("label") or moment.get("action_class") or "",
        moment.get("category") or "",
        moment.get("action_result") or "",
        moment.get("participant_role") or "",
        moment.get("participant") or "",
        # Which side did it — "Denmark's goals" is a query. The scoreline is
        # not embedded: "24-23" as text matches nothing anyone would type, and
        # a number in the vector dilutes the words that do.
        moment.get("action_team") or "",
        # The sentence a person would actually type when looking for this.
        moment.get("summary") or "",
        moment.get("description") or "",
        # How it was performed. In a sport judged on form rather than on
        # outcome, this is most of what anyone searches by — "clean take-off",
        # "horse fighting the contact" live here and nowhere else.
        moment.get("execution_details") or "",
        moment.get("harmony_index") or "",
        # Who was in the arena. `participant` is the handball question — a
        # shirt number read off a jersey — and an equestrian moment leaves it
        # empty, because the pair is joined from the ride windows in code
        # rather than read per moment. Without these two the vector has no
        # idea whose round it is, so "Loretta Joynson's half-pass" could only
        # be answered by the reranker, and only if the play had already
        # surfaced on its own meaning.
        moment.get("rider") or "",
        moment.get("horse") or "",
    ]
    return ". ".join(p.strip() for p in parts if p and p.strip())


def as_action_play(doc: dict[str, Any]) -> dict[str, Any]:
    """A stored moment in ActionPlay form.

    Times are MM:SS into the match. The stored confidence is a 0-1 probability
    and this shape wants a 0-100 score, so it is scaled here rather than stored
    twice and allowed to disagree.
    """
    return {
        "type": "ActionPlay",
        "timeOffsetStart": format_timecode(doc.get("startSec", 0.0)),
        "timeOffsetEnd": format_timecode(doc.get("endSec", 0.0)),
        "actionCategory": doc.get("category", ""),
        "actionClass": doc.get("label", ""),
        "actionResult": doc.get("actionResult", ""),
        "participant": doc.get("participant", ""),
        "participantRole": doc.get("participantRole", ""),
        "team1": doc.get("team1", ""),
        "team2": doc.get("team2", ""),
        "scoreTeam1": doc.get("scoreTeam1"),
        "scoreTeam2": doc.get("scoreTeam2"),
        "actionTeam": doc.get("actionTeam", ""),
        "summary": doc.get("summary", ""),
        "description": doc.get("description", ""),
        "executionDetails": doc.get("executionDetails", ""),
        "harmonyIndex": doc.get("harmonyIndex", ""),
        "confidenceScore": round(float(doc.get("confidence", 0.0)) * 100),
    }


def format_timecode(seconds: float) -> str:
    """Seconds to MM:SS, or H:MM:SS once a match runs past the hour."""
    total = max(0, round(float(seconds or 0)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def delete_job(job_id: str) -> dict[str, Any]:
    """Remove a job and everything hanging off it.

    Firestore does not cascade: deleting a document leaves its subcollections
    addressable and billable for ever, so the moments and events have to go
    explicitly. `clips` is in the list because jobs analysed before clip
    generation was withdrawn still hold one; nothing writes it any more. The game record lives in its own top-level collection and is
    not a subcollection at all, which is exactly the sort of thing a cascade you
    imagined into existence would miss.

    The source video is not deleted here — that is the media server's bucket and
    its job, and doing it from two places is how you end up doing it neither.
    """
    removed = {"moments": 0, "clips": 0, "events": 0, "chunks": 0, "game": 0}
    for name in ("moments", "clips", "events", "chunks"):
        removed[name] = _delete_collection(job_ref(job_id).collection(name))

    # Every game record of this job: a recording split into its classes has
    # one per class, and a delete that took only the first would leave the
    # rest unreachable, still indexed, and still on the desk for a job that no
    # longer exists.
    for doc in game_docs(job_id) or []:
        doc.reference.delete()
        removed["game"] += 1
    if not removed["game"] and game_ref(job_id).get().exists:
        game_ref(job_id).delete()
        removed["game"] = 1

    job_ref(job_id).delete()
    return {"job_id": job_id, "deleted": True, **removed}


def _delete_collection(collection, batch_size: int = 300) -> int:
    """Delete every document in a collection, a page at a time.

    Paged because a match yields hundreds of moments and a single batch has a
    limit; unbounded, this is the call that fails on exactly the biggest job.
    """
    total = 0
    while True:
        docs = list(collection.limit(batch_size).stream())
        if not docs:
            return total
        batch = db().batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        total += len(docs)


def clear_analysis(job_id: str) -> dict[str, Any]:
    """Drop a job's findings so it can be analysed again from scratch.

    Re-running without this leaves the previous run's moments in place and the
    new ones land beside them: the same play twice, with different ids, and a
    moment count that grows every time anyone retries.
    """
    removed = {
        "moments": _delete_collection(job_ref(job_id).collection("moments")),
        # Left behind by a job analysed before clip generation was withdrawn.
        "clips": _delete_collection(job_ref(job_id).collection("clips")),
    }
    cleared = 0
    for doc in game_docs(job_id) or []:
        doc.reference.delete()
        cleared += 1
    if not cleared and game_ref(job_id).get().exists:
        game_ref(job_id).delete()
        cleared = 1
    if cleared:
        removed["game"] = cleared

    job_ref(job_id).update({
        "counts": {"moments": 0},
        "error": None,
        "progress": 0,
        "status": "uploaded",
        "stage": "ingest",
        # A cancel is a flag the stages read, and it outlived the run it
        # stopped: a job cancelled in the morning refused every re-run after
        # it, reporting "Cancelled before the analysis started" a second
        # after the editor asked for one. Starting again is the one moment
        # the flag certainly no longer applies.
        "cancelRequested": False,
        "updatedAt": now(),
    })
    return {"job_id": job_id, "cleared": True, **removed}


def request_cancel(job_id: str) -> dict[str, Any]:
    """Ask a running job to stop.

    A flag rather than a kill: the run is a sequence of calls on Agent Runtime
    with no handle to interrupt, so the stages check this between steps and stop
    at the next boundary. That means cancelling a segment analysis takes effect
    when that segment finishes, not instantly.
    """
    job_ref(job_id).update({
        "cancelRequested": True,
        "status": "cancelling",
        "updatedAt": now(),
    })
    return {"job_id": job_id, "cancelling": True}


def cancel_requested(job_id: str) -> bool:
    snapshot = job_ref(job_id).get()
    return bool((snapshot.to_dict() or {}).get("cancelRequested")) if snapshot.exists else False


# --- Games --------------------------------------------------------------------
#
# A game record is indexed separately from its moments, in its own top-level
# collection with its own vector index. That separation is the point: "find the
# Sweden Denmark match" and "find the double save" are different questions over
# different units, and one index holding both would return moments to someone
# asking for a game and vice versa, because a match summary and the moments
# inside it share most of their vocabulary.


# A recording is not a competition. A live URL points at an arena, and the
# camera runs through class after class — so one job can hold several events,
# and each of them is a game record of its own: its own name, judges, start
# list, rides and Equipe page.
#
# The id says which: `{job}` while a recording holds one competition, which is
# every handball match and every single-class day, and `{job}__{classId}` once
# it holds more. Nothing already on the desk changes id, so nothing needs
# migrating and the single-game path is exactly what it was.
CLASS_SEPARATOR = "__"


def game_id(job_id: str, class_id: str | int = "") -> str:
    """The document id for one job's game, or for one class of it."""
    return f"{job_id}{CLASS_SEPARATOR}{class_id}" if class_id else job_id


def game_ref(job_id: str, class_id: str | int = ""):
    return db().collection("games").document(game_id(job_id, class_id))


def job_of(game_doc_id: str) -> str:
    """The job a game document belongs to, from its id."""
    return str(game_doc_id).split(CLASS_SEPARATOR, 1)[0]


def game_docs(job_id: str) -> list[Any]:
    """Every game document of one job, in the order the classes ran.

    Read by the `jobId` field rather than by guessing ids: the field is written
    on every record, and a query answers whether a recording was split without
    the caller having to know the class ids to ask for.
    """
    from google.cloud import firestore

    docs = list(db().collection("games")
                .where(filter=firestore.FieldFilter("jobId", "==", job_id)).stream())
    return sorted(docs, key=_class_order)


def _class_order(doc: Any) -> tuple:
    data = doc.to_dict() or {}
    # By when the class was due, then by the id, so the order is the running
    # order and is stable for two classes with the same start.
    return (str(data.get("classStartAt") or ""), str(doc.id))


def canonical_game(job_id: str) -> str:
    """The class a question about the whole recording is answered by.

    The first to run. A recording that was never split is its own answer, and
    this is what every reader that knows only a job id falls back to.
    """
    docs = game_docs(job_id)
    return docs[0].id if docs else job_id


def upsert_game(job_id: str, game: dict[str, Any], embed_text: str = "",
                class_id: str | int = "") -> dict[str, Any]:
    """Write the match-level record with its own embedding.

    ``class_id`` names one competition inside a recording that held several.
    Empty is the whole recording, which is every sport but equestrian and every
    day that ran one class.
    """
    from google.cloud.firestore_v1.vector import Vector

    owner_uid = get_job(job_id).get("ownerUid", "")
    text = embed_text.strip() or " ".join(
        str(game.get(k, "")) for k in ("title", "sport", "home_team", "away_team", "summary")
    )
    vector = embed([text], task_type="RETRIEVAL_DOCUMENT")[0]

    payload = {
        "jobId": job_id,
        "ownerUid": owner_uid,
        "title": game.get("title", ""),
        "sport": game.get("sport", ""),
        # Which form of the sport, for one that has several, and how sure the
        # reading was. Stored as the label: it is what is displayed and what
        # someone searches by, and the sport profile normalises it back to a
        # code when it needs one.
        "discipline": game.get("discipline", ""),
        "disciplineConfidence": game.get("discipline_confidence", 0.0),
        # Movements the analysis looked for across this match and did not find,
        # with the notes that rejected them. This payload is built field by
        # field rather than dumped, so anything not named here is dropped
        # silently — which is why a schema field alone is not enough to store
        # something.
        "notConfirmed": game.get("not_confirmed", []),
        # The day's rounds, in running order. Stored on the game rather than as
        # a subcollection: they are always read with it, there are tens rather
        # than thousands, and filtering them is a Python pass over a list the
        # caller already has — the same reasoning as list_jobs and its status.
        "rides": game.get("rides", []),
        # What the published record added. Kept apart from the observed fields
        # above it for the same reason the grounded* fields are: a reader must
        # always be able to tell a caption from a search result.
        "showTitle": game.get("show_title", ""),
        "location": game.get("location", ""),
        "equipeUrl": game.get("equipe_url", ""),
        "judges": game.get("judges", []),
        "startList": game.get("start_list", []),
        "scheduleAnchors": game.get("schedule_anchors", 0),
        "scheduleOffsetSec": game.get("schedule_offset_sec"),
        # What the search was told and what it asked. Stored so an editor can
        # see why a record grounded where it did — the wrong-class case was
        # invisible without them.
        "contextUrls": game.get("context_urls", []),
        "groundingQueries": game.get("grounding_queries", []),
        "homeTeam": game.get("home_team", ""),
        "awayTeam": game.get("away_team", ""),
        "competition": game.get("competition", ""),
        "venue": game.get("venue", ""),
        "finalScore": game.get("final_score", ""),
        "eventOutcome": game.get("event_outcome", ""),
        "sentiment": game.get("sentiment", ""),
        "mood": game.get("mood", ""),
        "summary": game.get("summary", ""),
        "momentCount": game.get("moment_count", 0),
        "highlightCount": game.get("highlight_count", 0),
        # Grounded values are kept apart from observed ones so a reader can
        # always tell a caption from a search result.
        "grounded": game.get("grounded", False),
        "groundedCompetition": game.get("grounded_competition", ""),
        "groundedVenue": game.get("grounded_venue", ""),
        "groundedHomeTeam": game.get("grounded_home_team", ""),
        "groundedAwayTeam": game.get("grounded_away_team", ""),
        "matchDate": game.get("match_date", ""),
        "groundingSources": game.get("grounding_sources", []),
        # Which competition of the recording this is. Absent on a record that
        # is the whole recording, which is how a reader tells the two apart.
        "classId": str(class_id or ""),
        # What the published record says about this class beyond its name: how
        # the organiser numbered it, where it ran, the test and the movements
        # it was marked on, and whether the results were final when they were
        # read. A placing that can still move is not a placing.
        "classNo": game.get("class_no", ""),
        "arena": game.get("arena", ""),
        "testName": game.get("test_name", ""),
        "testMovements": game.get("test_movements", []),
        "resultsFinal": bool(game.get("results_final", False)),
        "classStartAt": game.get("class_start_at", ""),
        "classUrl": game.get("class_url", ""),
        # "schedule" when the published timetable placed these rides, "caption"
        # when what was read in the arena moved them. A boundary that was
        # published and one that was observed are different kinds of fact.
        "classDecidedBy": game.get("class_decided_by", ""),
        "showId": game.get("show_id", 0),
        "showUrl": game.get("show_url", ""),
        "embedding": Vector(vector),
        "updatedAt": now(),
    }
    game_ref(job_id, class_id).set(payload)
    return {"job_id": job_id, "game_id": game_id(job_id, class_id), "indexed": True}


def _game_out(data: dict[str, Any]) -> dict[str, Any]:
    """Firestore document -> the GameDetails shape, without the 768-float vector."""
    return {
        "type": "GameDetails",
        "classId": data.get("classId", ""),
        "className": data.get("classId") and data.get("title", "") or "",
        "classNo": data.get("classNo", ""),
        "classUrl": data.get("classUrl", ""),
        "arena": data.get("arena", ""),
        "testName": data.get("testName", ""),
        "resultsFinal": data.get("resultsFinal", False),
        "showTitle": data.get("showTitle", ""),
        "showUrl": data.get("showUrl", ""),
        "equipeUrl": data.get("equipeUrl", ""),
        "judges": data.get("judges", []),
        "jobId": data.get("jobId", ""),
        "title": data.get("title", ""),
        "sport": data.get("sport", ""),
        "discipline": data.get("discipline", ""),
        "disciplineConfidence": data.get("disciplineConfidence", 0.0),
        "homeTeam": data.get("homeTeam", ""),
        "awayTeam": data.get("awayTeam", ""),
        "competition": data.get("competition", ""),
        "venue": data.get("venue", ""),
        "finalScore": data.get("finalScore", ""),
        "eventOutcome": data.get("eventOutcome", ""),
        "sentiment": data.get("sentiment", ""),
        "mood": data.get("mood", ""),
        "summary": data.get("summary", ""),
        "momentCount": data.get("momentCount", 0),
        "grounded": data.get("grounded", False),
        "groundedCompetition": data.get("groundedCompetition", ""),
        "groundedVenue": data.get("groundedVenue", ""),
        "groundedHomeTeam": data.get("groundedHomeTeam", ""),
        "groundedAwayTeam": data.get("groundedAwayTeam", ""),
        "matchDate": data.get("matchDate", ""),
        "groundingSources": data.get("groundingSources", []),
    }


def _chunks(items: list[str], size: int = 30) -> list[list[str]]:
    """Firestore's `in` takes at most thirty values."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def get_games_by_ids(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The game record for each job id, keyed by job id. Missing ones are absent.

    An equality filter with no ordering, so the automatic single-field index
    serves it and nothing new is declared in firestore.tf.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    wanted = [j for j in dict.fromkeys(job_ids or []) if j]
    found: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(wanted):
        for doc in db().collection("games").where(filter=FieldFilter("jobId", "in", chunk)).stream():
            data = doc.to_dict() or {}
            job_id = data.get("jobId") or job_of(doc.id)
            entry = {
                "job_id": job_id,
                "class_id": data.get("classId", ""),
                "title": data.get("title", ""),
                "sport": data.get("sport", ""),
                "discipline": data.get("discipline", ""),
            }
            # A recording split into classes has several records under one job
            # id, and this is keyed by job. Keying by the document instead
            # would change what every caller gets back; what matters to them is
            # a name for the recording, so the first class to run supplies it
            # rather than whichever document happened to stream last.
            previous = found.get(job_id)
            if previous is None or _earlier(data, previous.get("_at", "")):
                entry["_at"] = str(data.get("classStartAt") or "")
                found[job_id] = entry
    for entry in found.values():
        entry.pop("_at", None)
    return found


def _earlier(data: dict[str, Any], against: str) -> bool:
    at = str(data.get("classStartAt") or "")
    if not against:
        return False
    return bool(at) and at < against


def delete_game(job_id: str, class_id: str | int = "") -> dict[str, Any]:
    """Remove one game record, leaving the job and its moments alone.

    For the one case that needs it: a recording stored as a single event that
    turns out to have held several. The whole-day record has to go as the
    classes are written, or the desk shows the day twice — once whole and once
    in pieces.
    """
    ref = game_ref(job_id, class_id)
    existed = ref.get().exists
    if existed:
        ref.delete()
    return {"job_id": job_id, "game_id": game_id(job_id, class_id), "deleted": bool(existed)}


def get_game(job_id: str, class_id: str | int = "") -> dict[str, Any]:
    """One game record. Without a class, the recording's first competition."""
    snapshot = game_ref(job_id, class_id).get()
    if not snapshot.exists and not class_id:
        # A recording that was split has no record under the bare job id; the
        # first class is what a question about "the match" is asking for.
        snapshot = db().collection("games").document(canonical_game(job_id)).get()
    if not snapshot.exists:
        raise KeyError(f"No game record for job {job_id!r}.")
    return _game_out(snapshot.to_dict())


def list_games(job_id: str) -> list[dict[str, Any]]:
    """Every competition this recording holds, in running order.

    One entry for a day that held one class, several for a day that held
    several — which is what a live URL pointed at an arena produces.
    """
    out = []
    for doc in game_docs(job_id):
        data = doc.to_dict() or {}
        game = _game_out(data)
        game["gameId"] = doc.id
        game["classId"] = data.get("classId", "")
        game["className"] = data.get("title", "")
        game["classStartAt"] = data.get("classStartAt", "")
        game["classUrl"] = data.get("classUrl", "")
        game["rideCount"] = len(data.get("rides") or [])
        out.append(game)
    return out


def get_rides(job_id: str, class_id: str | int = "") -> list[dict[str, Any]]:
    """The competition day's rides, exactly as the game record stores them.

    Read from the raw document because _game_out, the shape the agents'
    context wants for a game, leaves the rides behind — which is how
    list_rides answered "no rides recorded" for every event that had them.
    One document read; the moments are not touched.

    Without a class this is every ride of the recording, gathered from each of
    its competitions in running order — a question about the day is about the
    day, whether or not it was one class.
    """
    snapshot = game_ref(job_id, class_id).get()
    if snapshot.exists:
        return [dict(r) for r in (snapshot.to_dict() or {}).get("rides") or []
                if isinstance(r, dict)]
    if class_id:
        raise KeyError(f"No game record for job {job_id!r} class {class_id!r}.")

    # No record under the bare job id: the recording was split into its
    # classes, so the day's rides are the classes' rides in running order. The
    # direct read above is what an unsplit recording costs — one document,
    # as before.
    docs = game_docs(job_id)
    if not docs:
        raise KeyError(f"No game record for job {job_id!r}.")
    rides: list[dict[str, Any]] = []
    for doc in docs:
        data = doc.to_dict() or {}
        for ride in data.get("rides") or []:
            if isinstance(ride, dict):
                rides.append({**ride, "class_id": data.get("classId", ""),
                              "class_name": data.get("title", "")})
    return rides


def event_tree(job_id: str, moment_limit: int = 2000,
               class_id: str | int = "") -> dict[str, Any]:
    """The event, its rides, and the moments in each — see event_tree.py.

    Reads the raw game document rather than get_game, because _game_out is the
    shape the agents' context wants and leaves the rides behind. A job with no
    game record yet still answers, with every moment unassigned.
    """
    from catalog_server.event_tree import build_event_tree

    job = get_job(job_id)
    snapshot = game_ref(job_id, class_id).get()
    if not snapshot.exists and not class_id:
        snapshot = db().collection("games").document(canonical_game(job_id)).get()
    game = (snapshot.to_dict() or {}) if snapshot.exists else {}
    docs = (
        job_ref(job_id).collection("moments")
        .order_by("startSec")
        .limit(moment_limit)
        .stream()
    )
    moments = [d.to_dict() or {} for d in docs]
    # One class of a day is one event: its own rides, and only the moments that
    # happened inside them. A moment carries the class it was in, so the split
    # is a filter rather than a second grouping rule that could disagree with
    # the one the rides were split by.
    this_class = str(game.get("classId") or "")
    if this_class:
        moments = [m for m in moments if str(m.get("classId") or "") == this_class]
    return build_event_tree(job_id, job, game, moments)


def _title_key(text: str) -> str:
    """Letters and digits only, for comparing a title with a sentence.

    Titles are composed in code — "SWE v DEN — EHF Euro" — and typed back by
    hand, where the em dash becomes a hyphen and the spacing drifts. Case and
    punctuation are noise for this comparison; the letters are not.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_games_by_title(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Find games whose title appears in ``query``, longest title first.

    A name is what a vector search is worst at. "FAG v TVB — DAIKIN HBL" is
    abbreviations and a sponsor, so its embedding sits near every other fixture
    in the same league, and `knn_search_games` answers with a plausible
    neighbour rather than the match that was named. Comparing the text answers
    it exactly or not at all, which is the right failure for a name.

    Reads the collection with a field mask so the 768-float embeddings stay in
    Firestore: this scans every game, and the vectors are almost all of the
    bytes.
    """
    asked = _title_key(query)
    if not asked:
        return []

    fields = ["jobId", "title", "homeTeam", "awayTeam",
              "groundedHomeTeam", "groundedAwayTeam"]
    hits: list[tuple[int, str]] = []
    for doc in db().collection("games").select(fields).stream():
        data = doc.to_dict() or {}
        for name in _game_names(data):
            key = _title_key(name)
            # One word is not a name: a team called "Lions" would answer any
            # question mentioning lions.
            if len(key.split()) > 1 and key in asked:
                hits.append((len(key), data.get("jobId") or doc.id))
                break

    # Longest first, so a title is not answered by a fixture whose own title is
    # a prefix of it.
    hits.sort(key=lambda hit: hit[0], reverse=True)

    games: list[dict[str, Any]] = []
    for _, job_id in hits[:limit]:
        try:
            games.append(get_game(job_id))
        except KeyError:
            continue
    return games


def _game_names(data: dict[str, Any]) -> list[str]:
    """The ways one game might be named: its title, then its fixture."""
    home = data.get("homeTeam") or data.get("groundedHomeTeam") or ""
    away = data.get("awayTeam") or data.get("groundedAwayTeam") or ""
    names = [data.get("title") or ""]
    if home and away:
        names += [f"{home} v {away}", f"{home} vs {away}", f"{home} {away}"]
    return [n for n in names if n]


def knn_search_games(query: str, owner_uid: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """Find whole matches by meaning, across every game.

    Unfiltered, which is also what makes the index simple: a vector index with
    an equality prefix only serves queries carrying that equality, so dropping
    the owner filter needs an index on the vector alone — `games_knn_all` in
    firestore.tf.

    ``owner_uid`` is accepted and ignored so an old caller is not silently
    answered with a filtered list it did not ask for.
    """
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
    from google.cloud.firestore_v1.vector import Vector

    vector = embed([query], task_type="RETRIEVAL_QUERY")[0]
    results = (
        db().collection("games")
        .find_nearest(
            vector_field="embedding",
            query_vector=Vector(vector),
            distance_measure=DistanceMeasure.COSINE,
            limit=limit,
        )
        .stream()
    )
    return [_game_out(doc.to_dict()) for doc in results]


# --- Jobs ---------------------------------------------------------------------


def job_ref(job_id: str):
    return db().collection("jobs").document(job_id)


def update_job_context(job_id: str, context_urls: list[str]) -> dict[str, Any]:
    """Replace the job's context links. The whole list, not a merge."""
    job_ref(job_id).update({"contextUrls": list(context_urls or []), "updatedAt": now()})
    return {"job_id": job_id, "context_urls": list(context_urls or [])}


def rename_job(job_id: str, title: str) -> dict[str, Any]:
    """Give a job the name an editor typed, and give it to its game record too.

    One name, in both places. The game composes its own title from what was
    read on screen and that is usually the better one — but an editor who
    renames a match has said which name they want, and a desk that answers with
    two different names for one recording is a desk nobody trusts. So this
    writes both, and marks the job so a later analysis keeps the chosen name
    rather than composing over it.

    ``updatedAt`` is deliberately not touched. The watchdog reads it to decide
    whether a run has died, and the editor shows a running job that has been
    silent for fifteen minutes as stalled — typing a new name is not progress,
    and it must not make a dead run look alive for another quarter of an hour.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("A title cannot be empty.")

    job_ref(job_id).update({"title": title, "titleSource": "editor"})
    # The game record is a separate top-level document and may not exist yet:
    # it is written when the analysis has something to say. When it arrives it
    # will read titleSource off the job and keep this name.
    # Every class of the recording, not just the first. A day split into three
    # competitions is three records of one recording, and renaming the
    # recording that answers with one name for one class and another for the
    # next is the exact failure this function exists to prevent.
    #
    # A class keeps its own name where it has one: the class *is* what that
    # record is called, and "FAIRFAX SADDLES PSG FREESTYLE GOLD" is a better
    # name for it than anything typed about the day. What the rename gives it
    # is the day's name as its show title.
    renamed = 0
    for doc in game_docs(job_id) or []:
        data = doc.to_dict() or {}
        patch: dict[str, Any] = {"updatedAt": now()}
        if data.get("classId"):
            patch["showTitle"] = title
        else:
            patch["title"] = title
        doc.reference.update(patch)
        renamed += 1
    if not renamed:
        game = db().collection("games").document(job_id)
        if game.get().exists:
            game.update({"title": title, "updatedAt": now()})
            renamed = 1
    return {"job_id": job_id, "title": title, "renamed_game": bool(renamed)}


def update_live_booking(job_id: str, event_start: str = "", event_end: str = "",
                        hls_url: str = "", title: str = "", sport: str = "",
                        metadata_language: str = "", stall_minutes: float = 0,
                        context_urls: list[str] | None = None) -> dict[str, Any]:
    """Correct a live event that has not started yet.

    A booking is made hours ahead, and the window and the playlist URL are the
    two things most likely to be wrong by the time it comes round — a class
    running late, a link whose token has turned over. Before this the only
    remedy was to delete the event and book it again, which threw away the
    title and the context links with it.

    Refused once the event is running: the window is what the recorder was
    started with, and the chunks are numbered and timed against it. The caller
    checks that too — this checks again because the check and the write are
    otherwise two moments apart, and the tick fires every minute.

    ``updatedAt`` is left alone for the same reason `rename_job` leaves it: the
    watchdog reads it, and editing a booking is not a sign of life from a run.
    """
    snapshot = job_ref(job_id).get()
    if not snapshot.exists:
        raise KeyError(f"No such job: {job_id}")
    job = snapshot.to_dict() or {}
    if job.get("kind") != "live":
        raise ValueError("This match is not a live event.")
    if job.get("status") != "scheduled":
        raise ValueError("This event has already started; it can no longer be rescheduled.")

    patch: dict[str, Any] = {}
    if event_start:
        patch["live.eventStart"] = event_start
    if event_end:
        patch["live.eventEnd"] = event_end
    if hls_url:
        patch["hlsUrl"] = hls_url
        patch["source.originalName"] = hls_url
    if title:
        patch["title"] = title
        patch["titleSource"] = "editor"
    if sport:
        patch["sport"] = sport
    if metadata_language:
        patch["metadataLanguage"] = metadata_language
    if stall_minutes:
        patch["live.stallMinutes"] = float(stall_minutes)
    if context_urls is not None:
        patch["contextUrls"] = list(context_urls)
    if not patch:
        return {"job_id": job_id, "changed": []}

    job_ref(job_id).update(patch)
    if title:
        game = db().collection("games").document(job_id)
        if game.get().exists:
            game.update({"title": title, "updatedAt": now()})
    return {"job_id": job_id, "changed": sorted(patch)}


def get_job(job_id: str) -> dict[str, Any]:
    snapshot = job_ref(job_id).get()
    if not snapshot.exists:
        raise KeyError(f"No job {job_id!r}.")
    return {"job_id": job_id, **snapshot.to_dict()}


# Statuses that mean the pipeline still owes the editor an answer. Kept here so
# the agent, the API and the UI cannot drift on what "still processing" means.
RUNNING_STATUSES = ("uploaded", "transcoding", "analyzing")


def list_jobs(owner_uid: str = "", limit: int = 20, status: str = "") -> list[dict[str, Any]]:
    """Recent jobs, newest first.

    Every job, not one owner's. Matches are shared across the desk, so the
    question "what is still processing?" is about the desk rather than about
    whoever happens to be asking.

    ``owner_uid`` is kept in the signature and ignored, so a caller that still
    passes one is not silently answered with a filtered list it did not ask for
    — and so the parameter can be given a meaning again without a signature
    change if the product ever grows tenants.

    The status filter is applied here rather than in the query: it would need an
    index of its own per status, and over a page of jobs it costs nothing.
    """
    wanted = (
        RUNNING_STATUSES if status == "running"
        else (status,) if status
        else ()
    )
    query = (
        db().collection("jobs")
        .order_by("createdAt", direction="DESCENDING")
        # Over-read when filtering so a page of finished jobs cannot hide the
        # running ones underneath it.
        .limit(limit * 4 if wanted else limit)
    )

    jobs: list[dict[str, Any]] = []
    for snapshot in query.stream():
        doc = snapshot.to_dict() or {}
        if wanted and doc.get("status") not in wanted:
            continue
        jobs.append(_job_summary(snapshot.id, doc))
        if len(jobs) >= limit:
            break
    return jobs


def _job_summary(job_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """The fields worth spending tokens on. The full document is get_job's job."""
    created = doc.get("createdAt")
    # The agent reads this to tell a live run from one that died with its
    # process: a status alone cannot distinguish them.
    updated = doc.get("updatedAt")
    return {
        "job_id": job_id,
        "title": doc.get("title") or doc.get("source", {}).get("originalName") or job_id,
        "sport": doc.get("sport", ""),
        "status": doc.get("status", "unknown"),
        "stage": doc.get("stage", ""),
        "progress": doc.get("progress", 0),
        "error": doc.get("error"),
        "duration_sec": doc.get("media", {}).get("durationSec"),
        "counts": doc.get("counts", {}),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
        # Where the video comes from. A live event has a window rather than a
        # file, and the agent answers "what is running" differently for it.
        "kind": doc.get("kind", "upload"),
        **({"live": doc.get("live")} if doc.get("kind") == "live" else {}),
    }


def create_job(job_id: str, owner_uid: str, title: str, sport: str, gcs_uri: str,
               original_name: str, size_bytes: int, content_type: str = "",
               metadata_language: str = "en",
               context_urls: list[str] | None = None,
               kind: str = "upload", hls_url: str = "",
               event_start: str = "", event_end: str = "",
               chunk_sec: int = 0,
               title_source: str = "derived",
               stall_minutes: float = 0) -> dict[str, Any]:
    """Open a job. Three kinds, told apart by where the video comes from.

    ``upload`` has its source in the bucket already. ``hls`` has only a URL and
    gets its source when the ingest stage has downloaded it. ``live`` has a URL
    and a time window, no source at all, and is driven by the live tick rather
    than by the analysis pipeline — it starts as ``scheduled``, not
    ``uploaded``, because there is nothing to analyse until the event starts.
    """
    kind = kind if kind in ("upload", "hls", "live") else "upload"
    payload = {
        "ownerUid": owner_uid,
        "title": title,
        # Whether a person typed this name or it was taken off a filename. The
        # game record composes its own title from what was read on screen, and
        # that is the better name for a handball upload called
        # GAME_2026_03_11_FINAL.mp4 — but not for one an editor sat down and
        # named. "editor" wins over the composed title; "derived" does not.
        "titleSource": "editor" if title_source == "editor" else "derived",
        "sport": sport,
        "kind": kind,
        "hlsUrl": hls_url,
        # What the analysis writes its prose in. Stored on the job so re-reading
        # it years later still says which language its descriptions are in.
        "metadataLanguage": metadata_language or "en",
        # Pages the editor says are about this recording. Evidence for
        # grounding, not a fetch target: the Equipe pages it will usually name
        # are JavaScript shells, and what they steer is the search.
        "contextUrls": list(context_urls or []),
        "status": "scheduled" if kind == "live" else "uploaded",
        "stage": "live" if kind == "live" else "ingest",
        "progress": 0,
        "source": {
            "gcsUri": gcs_uri,
            "originalName": original_name,
            "bytes": size_bytes,
            # What the client claimed. Kept so ingest can compare it against
            # what the file actually decodes as; never trusted on its own.
            "contentType": content_type,
            # Where the analysis reads from when that is not the source: the
            # 1 fps proxy an HLS download produces. Empty means the source.
            "analysisUri": "",
        },
        "media": {},
        "playback": {},
        "counts": {"moments": 0},
        "error": None,
        "createdAt": now(),
        "updatedAt": now(),
    }
    if kind == "live":
        payload["live"] = {
            "eventStart": event_start,
            "eventEnd": event_end,
            "chunkSec": int(chunk_sec or 0),
            # Minutes a stream that was flowing may stop before the event is
            # finished. Copied from the editor's settings when it was booked,
            # like the metadata language: what an event was recorded under
            # does not change because someone's preference did afterwards.
            "stallMinutes": max(0.0, float(stall_minutes or 0)),
            "state": "scheduled",
            "chunksCaptured": 0,
            "chunksAnalysed": 0,
            "capture": {},
            "tickLockUntil": None,
        }
    job_ref(job_id).set(payload)
    return {"job_id": job_id, **payload}


def set_source(job_id: str, gcs_uri: str, analysis_uri: str = "", original_name: str = "",
               size_bytes: int = 0, content_type: str = "") -> dict[str, Any]:
    """Record where a job's video ended up, for a source that arrived later.

    An HLS job is created with a URL and no object; this is how the ingest
    stage hands it the object once the download has finished. Fields are
    written one by one so a value that is not known is left as it was.
    """
    patch: dict[str, Any] = {"source.gcsUri": gcs_uri, "updatedAt": now()}
    if analysis_uri:
        patch["source.analysisUri"] = analysis_uri
    if original_name:
        patch["source.originalName"] = original_name
    if size_bytes:
        patch["source.bytes"] = int(size_bytes)
    if content_type:
        patch["source.contentType"] = content_type
    job_ref(job_id).update(patch)
    return {"job_id": job_id, **patch}


# --- Live events ----------------------------------------------------------------

LIVE_ACTIVE_STATES = ("scheduled", "live")


def list_live_jobs() -> list[dict[str, Any]]:
    """Every live event that is not over, for the tick to decide about.

    An equality filter alone, which the automatic single-field index serves;
    the state filter is applied here because it is an ``in`` over a handful of
    documents, not a page of them.
    """
    from google.cloud.firestore_v1 import FieldFilter

    query = db().collection("jobs").where(filter=FieldFilter("kind", "==", "live"))
    jobs: list[dict[str, Any]] = []
    for snapshot in query.stream():
        doc = snapshot.to_dict() or {}
        live = doc.get("live") or {}
        if live.get("state") not in LIVE_ACTIVE_STATES:
            continue
        jobs.append({**_job_summary(snapshot.id, doc), "hls_url": doc.get("hlsUrl", ""),
                     "live": live})
    return jobs


def update_live(job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Patch fields under a job's ``live`` map, leaving the rest alone."""
    update: dict[str, Any] = {f"live.{key}": value for key, value in patch.items()}
    update["updatedAt"] = now()
    job_ref(job_id).update(update)
    return {"job_id": job_id, **update}


def _chunk_ref(job_id: str, index: int):
    return job_ref(job_id).collection("chunks").document(f"{int(index):04d}")


def list_live_chunks(job_id: str) -> list[dict[str, Any]]:
    """Every chunk the recorder has closed, in order."""
    query = job_ref(job_id).collection("chunks").order_by("index")
    return [snapshot.to_dict() or {} for snapshot in query.stream()]


def claim_live_chunk(job_id: str, index: int) -> dict[str, Any]:
    """Move one chunk from ``captured`` to ``analyzing``, exactly once.

    A transaction, because two ticks can overlap — the scheduler fires on the
    minute whatever the last tick is still doing — and the same five minutes
    analysed twice lands every moment in it twice.
    """
    from google.cloud import firestore

    ref = _chunk_ref(job_id, index)

    @firestore.transactional
    def claim(transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        if (snapshot.to_dict() or {}).get("status") != "captured":
            return False
        transaction.update(ref, {"status": "analyzing", "claimedAt": now()})
        return True

    claimed = claim(db().transaction())
    return {"job_id": job_id, "index": int(index), "claimed": bool(claimed)}


def finish_live_chunk(job_id: str, index: int, moments: int = 0, error: str = "",
                      continuity: dict[str, Any] | None = None,
                      summary: str = "", competition: str = "", venue: str = "",
                      discipline: str = "", discipline_confidence: float = 0.0,
                      muxed_uri: str = "",
                      ride_fragments: list[dict[str, Any]] | None = None,
                      not_confirmed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record the analysis of one chunk, and count it on the job.

    ``muxed_uri`` is the chunk with its audio muxed in, when the tick made
    one; it is kept so a retried chunk is not muxed twice.

    ``ride_fragments`` are the rides this chunk saw, in absolute time and not
    yet stitched — a ride crosses chunks, so the tick fuses the whole day from
    every chunk's fragments. ``not_confirmed`` is what the chunk looked for and
    did not find. Both empty for a sport that is not judged in rounds.
    """
    patch: dict[str, Any] = {
        "status": "failed" if error else "analysed",
        "moments": int(moments),
        "error": error or None,
        "continuity": continuity or {},
        "summary": summary,
        "competition": competition,
        "venue": venue,
        "discipline": discipline,
        "disciplineConfidence": float(discipline_confidence or 0.0),
        "rideFragments": list(ride_fragments or []),
        "notConfirmed": list(not_confirmed or []),
        "analysedAt": now(),
    }
    if muxed_uri:
        patch["muxedUri"] = muxed_uri
    _chunk_ref(job_id, index).update(patch)
    from google.cloud import firestore

    # The moments are **not** counted here. `upsert_moments` already added them
    # when it wrote them, and the tick always writes a chunk's moments before it
    # marks the chunk analysed — so counting the same moments again on the way
    # past made every live event's total exactly twice what was stored. It was
    # invisible because nothing compares the two: 1206 moments against 603
    # documents reads as a busy day, and the desk, the agent and the finish
    # message all quote the counter.
    #
    # The write that knows how many landed is the one that does the landing.
    # This one only knows what it was told.
    job_ref(job_id).update({
        "live.chunksAnalysed": firestore.Increment(1),
        "updatedAt": now(),
    })
    return {"job_id": job_id, "index": int(index), **patch}


def record_teams(job_id: str, home: str, away: str) -> dict[str, Any]:
    """Save who is playing, as read off the score bug.

    A match-level fact rather than a per-moment one: it does not change, and
    storing one agreed answer keeps the UI from showing a different pairing
    depending on which moment it happens to read.
    """
    patch = {"teams": {"home": home, "away": away}, "updatedAt": now()}
    job_ref(job_id).update(patch)
    return {"job_id": job_id, **patch}


def update_job_status(job_id: str, status: str, stage: str | None = None,
                      error: str | None = None, progress: int | None = None) -> dict[str, Any]:
    """Patch a job's status, stage, error or progress.

    An empty status leaves the status alone. Progress updates arrive far more
    often than status changes — once per analysed segment — and they have no
    opinion about the status, so writing "" over it would blank the field the
    whole UI reads.
    """
    patch: dict[str, Any] = {"updatedAt": now()}
    if status:
        patch["status"] = status
    if stage is not None:
        patch["stage"] = stage
    if error is not None:
        patch["error"] = error

    if progress is not None:
        # Progress only ever goes forward. Playback and analysis run
        # concurrently and occupy different bands of the bar — 5-20 and 20-80 —
        # so whichever finishes last wrote its number last, and an encode that
        # ended after the analysis had reached 80% pulled the bar back to 20.
        #
        # Zero is the exception, because it is how a run says it is starting
        # over rather than how it reports being early.
        if progress <= 0:
            patch["progress"] = progress
        else:
            snapshot = job_ref(job_id).get()
            current = (snapshot.to_dict() or {}).get("progress", 0) if snapshot.exists else 0
            patch["progress"] = max(int(current or 0), progress)

    job_ref(job_id).update(patch)
    return {"job_id": job_id, **patch}


def record_media_info(job_id: str, media: dict[str, Any], segment_count: int) -> dict[str, Any]:
    patch = {
        "media": {
            "durationSec": media.get("duration_sec", 0.0),
            "width": media.get("width", 0),
            "height": media.get("height", 0),
            "fps": media.get("fps", 0.0),
            "videoCodec": media.get("video_codec", ""),
            "audioCodec": media.get("audio_codec", ""),
            "bitrate": media.get("bitrate", 0),
            "bytes": media.get("bytes", 0),
            "segmentCount": segment_count,
        },
        "updatedAt": now(),
    }
    job_ref(job_id).update(patch)
    return {"job_id": job_id, "media": patch["media"]}


def record_playback(job_id: str, playback_url: str, poster_url: str,
                    renditions: list[str], segment_seconds: int) -> dict[str, Any]:
    """Store the CDN HLS URL the editor plays."""
    patch = {
        "playback": {
            "hlsUrl": playback_url,
            "posterUrl": poster_url,
            "renditions": renditions,
            "segmentSeconds": segment_seconds,
            "readyAt": now(),
        },
        "updatedAt": now(),
    }
    job_ref(job_id).update(patch)
    return {"job_id": job_id, **patch}


def emit_event(job_id: str, stage: str, level: str, message: str,
               data: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = {
        "jobId": job_id,
        "ts": now(),
        "stage": stage,
        "level": level,
        "message": message,
        "data": data or {},
    }
    _, ref = job_ref(job_id).collection("events").add(doc)
    return {"event_id": ref.id, **{k: v for k, v in doc.items() if k != "ts"}}


# --- Moments ------------------------------------------------------------------


def upsert_moments(job_id: str, moments: list[dict[str, Any]]) -> int:
    """Write moments with their embeddings in one batch.

    Embeddings are generated here rather than by the caller so the vector width
    can never drift from the index.
    """
    if not moments:
        return 0

    from google.cloud import firestore
    from google.cloud.firestore_v1.vector import Vector

    owner_uid = get_job(job_id).get("ownerUid", "")
    texts = [m.pop("embed_text", None) or action_play_text(m) for m in moments]
    vectors = embed(texts, task_type="RETRIEVAL_DOCUMENT")

    batch = db().batch()
    collection = job_ref(job_id).collection("moments")
    for moment, vector in zip(moments, vectors, strict=True):
        doc_id = moment["moment_id"]
        batch.set(
            collection.document(doc_id),
            {
                "momentId": doc_id,
                "jobId": job_id,
                "ownerUid": owner_uid,
                "momentType": moment["moment_type"],
                "category": moment.get("category", ""),
                "label": moment.get("label", ""),
                "startSec": moment["start_sec"],
                "endSec": moment["end_sec"],
                "peakSec": moment["peak_sec"],
                "confidence": moment.get("confidence", 0.0),
                # The welfare gate. This payload is field-by-field, so a moment
                # type flagged for review would arrive here and be dropped —
                # detected, stored, and indistinguishable from anything else.
                "requiresHumanReview": bool(moment.get("requires_human_review", False)),
                # Who was in the arena. Joined from the ride windows, and
                # identitySource says whether the name was read off a graphic
                # or inferred from the published start list — a caption must
                # never present the second as the first.
                "rider": moment.get("rider", ""),
                "horse": moment.get("horse", ""),
                "startNumber": moment.get("start_number", ""),
                "rideOrder": moment.get("ride_order"),
                "identitySource": moment.get("identity_source", ""),
                "excitement": moment.get("excitement", 0.0),
                "highlightScore": moment.get("highlight_score", 0.0),
                "description": moment.get("description", ""),
                "evidence": moment.get("evidence", []),
                "scoreboard": moment.get("scoreboard"),
                "isGoal": moment.get("is_goal", False),
                "summary": moment.get("summary", ""),
                "actionResult": moment.get("action_result", ""),
                "participant": moment.get("participant", ""),
                "participantRole": moment.get("participant_role", ""),
                "team1": moment.get("team1", ""),
                "team2": moment.get("team2", ""),
                # None, not 0: nil-nil is a real score and unknown is not.
                "scoreTeam1": moment.get("score_team1"),
                "scoreTeam2": moment.get("score_team2"),
                "actionTeam": moment.get("action_team", ""),
                "executionDetails": moment.get("execution_details", ""),
                "harmonyIndex": moment.get("harmony_index", ""),
                "segmentIndexes": moment.get("segment_indexes", []),
                "embedding": Vector(vector),
                "createdAt": now(),
            },
        )
    batch.commit()

    job_ref(job_id).update({"counts.moments": firestore.Increment(len(moments)), "updatedAt": now()})
    return len(moments)


def record_moment_thumbnails(job_id: str, thumbnails: dict[str, str]) -> int:
    """Attach a thumbnail URI to each named moment.

    A patch rather than part of `upsert_moments`, because the picture is cut
    after the moments exist — the frame is taken at a peak that only the saved
    record knows. `update` rather than `set` for the same reason: this must
    touch one field and leave the embedding and the ActionPlay alone.

    A moment that has since been deleted is skipped rather than recreated as a
    document holding nothing but a thumbnail.
    """
    if not thumbnails:
        return 0

    from google.api_core.exceptions import NotFound

    collection = job_ref(job_id).collection("moments")
    wanted = {mid: uri for mid, uri in thumbnails.items() if mid and uri}
    if not wanted:
        return 0

    batch = db().batch()
    for moment_id, uri in wanted.items():
        batch.update(collection.document(moment_id), {"thumbUri": uri})

    try:
        batch.commit()
        saved = len(wanted)
    except NotFound:
        # A batch is all or nothing, so one moment deleted between the analysis
        # and the cut would cost every thumbnail in the request. Retry singly.
        saved = 0
        for moment_id, uri in wanted.items():
            try:
                collection.document(moment_id).update({"thumbUri": uri})
                saved += 1
            except NotFound:
                logger.info("moment %s is gone; its thumbnail is orphaned", moment_id)

    job_ref(job_id).update({"updatedAt": now()})
    return saved


def update_moment_identity(job_id: str, identities: list[dict[str, Any]]) -> int:
    """Set who was riding on moments that already exist.

    Moments are written before the game record is built, and the ride they
    belong to can only be named after grounding — the start list is what names
    a round no graphic did. So this patches the identity fields on stored
    moments in place, the way record_moment_thumbnails patches a thumbnail.
    Nothing else on the moment is touched, and a moment the list does not
    mention is left exactly as it was.
    """
    collection = job_ref(job_id).collection("moments")
    batch = db().batch()
    count = 0
    for row in identities:
        moment_id = str(row.get("moment_id") or "")
        if not moment_id:
            continue
        patch = {
            "rider": row.get("rider", ""),
            "horse": row.get("horse", ""),
            "startNumber": row.get("start_number", ""),
            "rideOrder": row.get("ride_order"),
            "identitySource": row.get("identity_source", ""),
        }
        # Which competition of the day this moment happened in. Only written
        # when it is known: a recording of one class has none, and writing an
        # empty one over a moment that has it would unfile it.
        if row.get("class_id"):
            patch["classId"] = str(row["class_id"])
        batch.update(collection.document(moment_id), patch)
        count += 1
        # Firestore batches cap at 500 writes.
        if count % 400 == 0:
            batch.commit()
            batch = db().batch()
    if count % 400:
        batch.commit()
    if count:
        job_ref(job_id).update({"updatedAt": now()})
    return count


def _moment_out(data: dict[str, Any]) -> dict[str, Any]:
    """Firestore document -> the snake_case shape the agents use.

    The embedding is deliberately dropped: it is 768 floats that no caller needs
    and that would otherwise land in a model's context.
    """
    return {
        "moment_id": data.get("momentId"),
        "job_id": data.get("jobId"),
        "moment_type": data.get("momentType"),
        "category": data.get("category", ""),
        "label": data.get("label", ""),
        "start_sec": data.get("startSec", 0.0),
        "end_sec": data.get("endSec", 0.0),
        "peak_sec": data.get("peakSec", 0.0),
        "confidence": data.get("confidence", 0.0),
        "requires_human_review": bool(data.get("requiresHumanReview", False)),
        "rider": data.get("rider", ""),
        "horse": data.get("horse", ""),
        "start_number": data.get("startNumber", ""),
        "ride_order": data.get("rideOrder"),
        "class_id": data.get("classId", ""),
        "identity_source": data.get("identitySource", ""),
        "excitement": data.get("excitement", 0.0),
        "highlight_score": data.get("highlightScore", 0.0),
        "description": data.get("description", ""),
        "evidence": data.get("evidence", []),
        "scoreboard": data.get("scoreboard"),
        "is_goal": data.get("isGoal", False),
        "summary": data.get("summary", ""),
        "action_result": data.get("actionResult", ""),
        "participant": data.get("participant", ""),
        "participant_role": data.get("participantRole", ""),
        "team1": data.get("team1", ""),
        "team2": data.get("team2", ""),
        "score_team1": data.get("scoreTeam1"),
        "score_team2": data.get("scoreTeam2"),
        "action_team": data.get("actionTeam", ""),
        "execution_details": data.get("executionDetails", ""),
        "harmony_index": data.get("harmonyIndex", ""),
        "segment_indexes": data.get("segmentIndexes", []),
        "thumb_uri": data.get("thumbUri", ""),
    }


def list_action_plays(job_id: str, limit: int = 500, min_score: float = 0.0) -> list[dict[str, Any]]:
    """Every moment in the job as ActionPlay records, in match order.

    Ordered by time rather than by score because this is a record of what
    happened, not a shortlist — `list_moments` is the ranked view.

    The score threshold is applied here rather than in the query. Firestore
    wants the first order_by to be the field an inequality filters on, so
    "highlightScore >= x ordered by startSec" is not one query it will run
    without a composite index — and it fails at read time with a link to create
    one, which is a bad way to find out. Ordering by startSec alone needs only
    the single-field index every collection already has.
    """
    from google.cloud import firestore

    # Over-read when filtering: taking `limit` documents first and then dropping
    # the low-scoring ones would return fewer than asked for, and on a match
    # where the early moments score badly it would return almost nothing.
    query = (
        job_ref(job_id)
        .collection("moments")
        .order_by("startSec", direction=firestore.Query.ASCENDING)
        .limit(limit * 4 if min_score > 0 else limit)
    )

    plays: list[dict[str, Any]] = []
    for snapshot in query.stream():
        doc = snapshot.to_dict() or {}
        if min_score > 0 and float(doc.get("highlightScore") or 0.0) < min_score:
            continue
        plays.append(as_action_play(doc))
        if len(plays) >= limit:
            break
    return plays


# --- Deployment configuration -------------------------------------------------
#
# One document per integration under `config`, written from the editor's
# settings panel through the API. It holds secrets — a YouTube refresh token is
# a standing permission to post to someone's channel — so nothing here is ever
# projected into an agent's context or returned to the browser whole; the API
# says whether a field is set, never what it is.

CONFIG_COLLECTION = "config"


def get_config(name: str) -> dict[str, Any]:
    """Read one configuration document, empty when it has never been written."""
    doc = db().collection(CONFIG_COLLECTION).document(name).get()
    return doc.to_dict() or {} if doc.exists else {}


def set_config(name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Merge fields into one configuration document.

    A merge rather than a replace so connecting a channel does not clear the
    client it was connected with, and an empty string is a real value — that is
    how a field is cleared.
    """
    payload = {**values, "updatedAt": now()}
    db().collection(CONFIG_COLLECTION).document(name).set(payload, merge=True)
    return {"name": name, "fields": sorted(values)}


def clear_config(name: str, fields: list[str]) -> dict[str, Any]:
    """Blank the named fields, leaving the rest of the document alone."""
    if not fields:
        return {"name": name, "cleared": []}
    payload: dict[str, Any] = {f: "" for f in fields}
    payload["updatedAt"] = now()
    db().collection(CONFIG_COLLECTION).document(name).set(payload, merge=True)
    return {"name": name, "cleared": sorted(fields)}


def get_moment(job_id: str, moment_id: str) -> dict[str, Any] | None:
    """One moment, by id. None when the job does not hold it.

    Downloading or publishing a moment starts from its in and out points, and
    those come from the record rather than from whoever asked. A caller may
    trim around them — that is what the publish preview does — but the record
    is what the trim is bounded against, so "this moment" cannot become an
    hour of the match under a moment's name.
    """
    doc = job_ref(job_id).collection("moments").document(moment_id).get()
    return _moment_out(doc.to_dict()) if doc.exists else None


def list_moments(job_id: str, limit: int = 100, min_score: float = 0.0) -> list[dict[str, Any]]:
    from google.cloud import firestore

    query = (
        job_ref(job_id)
        .collection("moments")
        .where(filter=firestore.FieldFilter("highlightScore", ">=", min_score))
        .order_by("highlightScore", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [_moment_out(d.to_dict()) for d in query.stream()]


class _RerankedItem(BaseModel):
    index: int = Field(description="The candidate's number, exactly as shown in the list.")
    relevance: float = Field(
        ge=0.0, le=1.0,
        description=(
            "How well this moment answers the query. 1.0 is exactly what was asked "
            "for; 0.0 is unrelated. Judge the moment's description, not its type label."
        ),
    )
    reason: str = Field(description="One short clause saying why, for the editor to see.")


class _RerankResult(BaseModel):
    ranked: list[_RerankedItem] = Field(
        description="Every candidate, most relevant first. Do not omit or invent candidates."
    )


_RERANK_PROMPT = """\
An editor searching a sports video library asked:

    "{query}"

Below are {count} candidate moments retrieved by embedding similarity. Embedding \
similarity matches on wording, so some of these will be about the right kind of \
play but the wrong one, and some will be right despite sharing no vocabulary with \
the query.

Score every candidate on how well it answers what the editor actually asked for, \
and return them ordered most relevant first. Judge what happens in the moment, \
described in its own words — not whether its type label resembles the query. Each \
candidate also says which match or class it is in and, where known, who was \
competing; when the query names a match, a team, a rider or a horse, that is part \
of what it asks for.

Give every candidate a score. Score generously only when the moment genuinely \
answers the query; a list where everything scores above 0.8 is not a ranking.

Candidates:

{candidates}
"""


def _rerank(query: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Reorder vector-search candidates with Gemini 2.5 Flash.

    Retrieval and ranking answer different questions: the embedding index finds
    moments worded like the query, which is not the same as moments that answer
    it. A search for "the keeper kept them in it" retrieves anything mentioning a
    keeper; the reranker is what puts the double save above the routine catch.

    Falls back to the vector order on any failure — a degraded ranking is a far
    better outcome than a search that returns nothing.
    """
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    from google.genai import types

    lines = [_candidate_line(i, moment) for i, moment in enumerate(candidates)]

    try:
        response = rerank_client().models.generate_content(
            model=RERANK_MODEL,
            contents=_RERANK_PROMPT.format(
                query=query, count=len(candidates), candidates="\n".join(lines)
            ),
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                # JSON Schema rather than the class: given the class, the
                # newer Flash models write ``relevance`` as an unbounded run
                # of digits until the token cap, and the result is unparseable
                # — which here degrades silently to vector order.
                response_json_schema=_RerankResult.model_json_schema(),
                max_output_tokens=8192,
                thinking_config=types.ThinkingConfig(thinking_budget=2048),
            ),
        )
        parsed = _RerankResult.model_validate_json(
            (getattr(response, "text", "") or "").strip())
        if not parsed.ranked:
            raise ValueError("reranker returned nothing usable")
    except Exception:  # noqa: BLE001
        logger.warning("rerank failed; falling back to vector order", exc_info=True)
        for moment in candidates:
            moment["rerank_score"] = None
            moment["rerank_reason"] = None
        return candidates[:limit]

    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in parsed.ranked:
        if not 0 <= item.index < len(candidates) or item.index in seen:
            # The model hallucinated or repeated an index; skip rather than
            # surface someone else's moment under this score.
            continue
        seen.add(item.index)
        moment = dict(candidates[item.index])
        moment["rerank_score"] = round(item.relevance, 4)
        moment["rerank_reason"] = item.reason
        ranked.append(moment)

    # Anything the model dropped keeps its vector position, behind what it ranked.
    for i, moment in enumerate(candidates):
        if i not in seen:
            leftover = dict(moment)
            leftover["rerank_score"] = None
            leftover["rerank_reason"] = None
            ranked.append(leftover)

    return ranked[:limit]


def _merge_top(per_job: dict[str, list[dict[str, Any]]], games: dict[str, dict[str, Any]],
               *, sport: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """The best moments across several games, as one list.

    Each game's own listing is already in score order; this joins the game on,
    drops the sports not asked for, and takes the top of the union. Pure, so
    it can be tested without a database — the fan-out that fills per_job is
    the only part that reads one.
    """
    want = (sport or "").strip().lower()
    out: list[dict[str, Any]] = []
    for job_id, moments in per_job.items():
        game = games.get(job_id, {"job_id": job_id, "title": "", "sport": "", "discipline": ""})
        if want and (game.get("sport", "") or "").lower() != want:
            continue
        for m in moments:
            m = dict(m)
            m["game"] = game
            out.append(m)
    out.sort(key=lambda m: float(m.get("highlight_score") or 0.0), reverse=True)
    return out[:limit]


def list_top_moments(limit: int = 20, sport: str = "", job_ids: list[str] | None = None,
                     max_games: int = 60) -> dict[str, Any]:
    """The key moments across the desk, best first, each naming its game.

    A fan-out of the per-job listing rather than one collection-group query:
    that listing is served by an index that exists, ordering a collection group
    by score is not, and the desk is tens of games, not thousands. Games still
    analysing are reported rather than silently counted as empty — "no
    moments" and "not finished yet" are different answers.
    """
    if job_ids:
        jobs = [{"job_id": j, "status": get_job(j).get("status", "")} for j in job_ids[:max_games]]
    else:
        jobs = [{"job_id": j.get("job_id") or j.get("id"), "status": j.get("status", "")}
                for j in list_jobs(limit=max_games)]
    running = [j["job_id"] for j in jobs if j["status"] in RUNNING_STATUSES]
    per_job = {j["job_id"]: list_moments(j["job_id"], limit=limit, min_score=0.0) for j in jobs if j["job_id"]}
    games = get_games_by_ids(list(per_job))
    return {
        "moments": _merge_top(per_job, games, sport=sport, limit=limit),
        "running": running,
        "games_searched": len(per_job),
    }


def _filter_candidates(candidates: list[dict[str, Any]], *, sport: str = "",
                       job_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Narrow a candidate set by sport or by game, in Python.

    In Python because the library-wide vector index is on the embedding alone —
    a `where` on a collection-group vector query needs the field in the index,
    and moments do not carry their sport anyway; it comes from the game joined
    on beside them. Same reasoning as the status filter in list_jobs: over-read
    first, then narrow.
    """
    wanted = {j for j in (job_ids or []) if j}
    want_sport = (sport or "").strip().lower()
    out = []
    for m in candidates:
        if wanted and m.get("job_id") not in wanted:
            continue
        if want_sport and (m.get("game", {}).get("sport", "") or "").lower() != want_sport:
            continue
        out.append(m)
    return out


def _candidate_line(index: int, moment: dict[str, Any]) -> str:
    """One candidate as the reranker sees it: what it is, and where it is.

    The game is named on every line. An editor searching the whole library
    asks for "the double save in the Sweden match" and "the pat after the
    freestyle", and a ranker that only sees the play cannot use either half.
    """
    detail = moment.get("summary") or moment.get("description") or moment.get("label", "")
    extra = []
    game = moment.get("game") or {}
    where = " / ".join(x for x in (game.get("title"), game.get("discipline") or game.get("sport")) if x)
    if where:
        extra.append(f"in {where}")
    who = " / ".join(x for x in (moment.get("rider"), moment.get("horse")) if x)
    if who:
        extra.append(who)
    if moment.get("scoreboard"):
        extra.append(f"scoreboard {moment['scoreboard']}")
    if moment.get("is_goal"):
        extra.append("resulted in a goal")
    suffix = f" ({'; '.join(extra)})" if extra else ""
    return f"{index}. [{moment.get('label', 'Unknown')}] {detail}{suffix}"


def knn_search_moments(query: str, job_id: str = "", owner_uid: str = "",
                       limit: int = 10, rerank: bool = True,
                       sport: str = "", job_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Nearest-neighbour search over moment embeddings, reranked by Gemini.

    Scoped to one job when job_id is given, otherwise across the owner's whole
    library via a collection-group query. When ``rerank`` is set the index is
    over-fetched and the candidates are reordered by relevance.
    """
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
    from google.cloud.firestore_v1.vector import Vector

    vector = embed([query], task_type="RETRIEVAL_QUERY")[0]

    # A search "across everything, but only this one game" is a per-job search
    # and gets the per-job index, which answers exactly; the post-filter below
    # is for a sport or for several games, where over-reading is the only way.
    chosen = [j for j in (job_ids or []) if j]
    if not job_id and len(chosen) == 1:
        job_id = chosen[0]
        chosen = []
    filtering = bool(sport) or bool(chosen)

    if job_id:
        base = job_ref(job_id).collection("moments")
    else:
        # Library-wide, across every job. A vector index with an equality
        # prefix only serves queries carrying that equality, so this needs an
        # index on the embedding alone — moments_knn_all in firestore.tf.
        base = db().collection_group("moments")

    fetch = min(limit * RERANK_OVERFETCH, RERANK_MAX_CANDIDATES) if rerank else limit
    if filtering:
        # A filter discards most of what the index returns, so ask for more —
        # a sport that is a tenth of the library would otherwise fill one page
        # from ten pages of the other sport's nearest neighbours.
        fetch = min(fetch * 4, RERANK_MAX_CANDIDATES * 4)

    results = base.find_nearest(
        vector_field="embedding",
        query_vector=Vector(vector),
        distance_measure=DistanceMeasure.COSINE,
        limit=fetch,
        distance_result_field="vector_distance",
    ).get()

    candidates = []
    for doc in results:
        data = doc.to_dict()
        moment = _moment_out(data)
        distance = data.get("vector_distance")
        # Cosine distance in [0, 2]; report a similarity so callers can threshold
        # without knowing the measure.
        moment["similarity"] = round(1.0 - float(distance) / 2.0, 4) if distance is not None else None
        candidates.append(moment)

    # The game, joined on before anything ranks or filters. A result across the
    # library means nothing without the match it is in, and the ranker needs it
    # for the same reason the reader does.
    games = get_games_by_ids([m.get("job_id", "") for m in candidates])
    for m in candidates:
        m["game"] = games.get(m.get("job_id", ""), {"job_id": m.get("job_id", ""), "title": "", "sport": "", "discipline": ""})
    candidates = _filter_candidates(candidates, sport=sport, job_ids=chosen)

    if not rerank:
        return candidates[:limit]

    ranked = _rerank(query, candidates, limit)
    for position, moment in enumerate(ranked):
        moment["rank"] = position + 1
    return ranked


# --- Recovery ---------------------------------------------------------------------

# A run whose last write is older than this is dead: progress is written at
# least once per analysed segment, and nothing takes this long between them.
STALL_MINUTES = 15
# How many times the watchdog restarts one job before it gives up and says so.
MAX_RECOVERIES = 2
# How many times a failed live chunk is analysed again before it stays failed.
MAX_CHUNK_ATTEMPTS = 3


def list_stalled_jobs(minutes: int = STALL_MINUTES, limit: int = 50) -> list[dict[str, Any]]:
    """Uploaded, transcoding or analysing jobs nothing has written to lately.

    Ordered by creation and filtered here rather than by an inequality on
    ``updatedAt``: a running status plus a range on another field is a
    composite index, and this reads a page of recent jobs once a minute.
    Live events are driven by their own tick and left out.
    """
    from datetime import timedelta

    cutoff = now() - timedelta(minutes=minutes)
    query = (
        db().collection("jobs")
        .order_by("createdAt", direction="DESCENDING")
        .limit(200)
    )
    stalled: list[dict[str, Any]] = []
    for snapshot in query.stream():
        doc = snapshot.to_dict() or {}
        if doc.get("kind") == "live" or doc.get("status") not in RUNNING_STATUSES:
            continue
        updated = doc.get("updatedAt")
        if updated is None or not hasattr(updated, "tzinfo"):
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if updated > cutoff:
            continue
        stalled.append({**_job_summary(snapshot.id, doc), "recovery": doc.get("recovery") or {}})
        if len(stalled) >= limit:
            break
    return stalled


def note_recovery(job_id: str, reason: str) -> dict[str, Any]:
    """Count one automatic restart on the job, and say why."""
    snapshot = job_ref(job_id).get()
    current = ((snapshot.to_dict() or {}).get("recovery") or {}) if snapshot.exists else {}
    attempts = int(current.get("attempts") or 0) + 1
    recovery = {"attempts": attempts, "lastAttemptAt": now(), "lastReason": reason}
    job_ref(job_id).update({"recovery": recovery, "updatedAt": now()})
    return {"job_id": job_id, "attempts": attempts, "max_attempts": MAX_RECOVERIES}


def reset_live_chunk(job_id: str, index: int, stale_after_minutes: int = 0) -> dict[str, Any]:
    """Put a failed chunk back to ``captured`` so the tick analyses it again.

    A transaction for the same reason the claim is one, and bounded: a chunk
    that has failed ``MAX_CHUNK_ATTEMPTS`` times stays failed and is reported
    as missing rather than retried for the rest of the event.

    With ``stale_after_minutes`` it also resets a chunk that is still
    ``analyzing`` from a claim older than that: the tick that claimed it died
    with its process — a deploy replaced the engine — and nothing else would
    ever ask for that chunk again.
    """
    from datetime import timedelta

    from google.cloud import firestore

    ref = _chunk_ref(job_id, index)

    def _stale(doc: dict[str, Any]) -> bool:
        if not stale_after_minutes or doc.get("status") != "analyzing":
            return False
        claimed = doc.get("claimedAt")
        if claimed is None:
            return True
        return claimed < now() - timedelta(minutes=stale_after_minutes)

    @firestore.transactional
    def reset(transaction) -> tuple[bool, int]:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False, 0
        doc = snapshot.to_dict() or {}
        attempts = int(doc.get("attempts") or 1)
        if attempts >= MAX_CHUNK_ATTEMPTS:
            return False, attempts
        if doc.get("status") != "failed" and not _stale(doc):
            return False, attempts
        transaction.update(ref, {"status": "captured", "attempts": attempts + 1,
                                 "error": None, "resetAt": now()})
        return True, attempts + 1

    done, attempts = reset(db().transaction())
    return {"job_id": job_id, "index": int(index), "reset": bool(done), "attempts": attempts}
