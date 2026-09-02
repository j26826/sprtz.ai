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
RERANK_MODEL = os.environ.get("RERANK_MODEL", "gemini-2.5-flash")
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
    addressable and billable for ever, so the moments, clips and events have to
    go explicitly. The game record lives in its own top-level collection and is
    not a subcollection at all, which is exactly the sort of thing a cascade you
    imagined into existence would miss.

    The source video is not deleted here — that is the media server's bucket and
    its job, and doing it from two places is how you end up doing it neither.
    """
    removed = {"moments": 0, "clips": 0, "events": 0, "game": 0}
    for name in ("moments", "clips", "events"):
        removed[name] = _delete_collection(job_ref(job_id).collection(name))

    game = game_ref(job_id).get()
    if game.exists:
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
        "clips": _delete_collection(job_ref(job_id).collection("clips")),
    }
    if game_ref(job_id).get().exists:
        game_ref(job_id).delete()
        removed["game"] = 1

    job_ref(job_id).update({
        "counts": {"moments": 0, "clips": 0},
        "error": None,
        "progress": 0,
        "status": "uploaded",
        "stage": "ingest",
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


def game_ref(job_id: str):
    return db().collection("games").document(job_id)


def upsert_game(job_id: str, game: dict[str, Any], embed_text: str = "") -> dict[str, Any]:
    """Write the match-level record with its own embedding."""
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
        "embedding": Vector(vector),
        "updatedAt": now(),
    }
    game_ref(job_id).set(payload)
    return {"job_id": job_id, "indexed": True}


def _game_out(data: dict[str, Any]) -> dict[str, Any]:
    """Firestore document -> the GameDetails shape, without the 768-float vector."""
    return {
        "type": "GameDetails",
        "jobId": data.get("jobId", ""),
        "title": data.get("title", ""),
        "sport": data.get("sport", ""),
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


def get_game(job_id: str) -> dict[str, Any]:
    snapshot = game_ref(job_id).get()
    if not snapshot.exists:
        raise KeyError(f"No game record for job {job_id!r}.")
    return _game_out(snapshot.to_dict())


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
    }


def create_job(job_id: str, owner_uid: str, title: str, sport: str, gcs_uri: str,
               original_name: str, size_bytes: int, content_type: str = "",
               metadata_language: str = "en") -> dict[str, Any]:
    payload = {
        "ownerUid": owner_uid,
        "title": title,
        "sport": sport,
        # What the analysis writes its prose in. Stored on the job so re-reading
        # it years later still says which language its descriptions are in.
        "metadataLanguage": metadata_language or "en",
        "status": "uploaded",
        "stage": "ingest",
        "progress": 0,
        "source": {
            "gcsUri": gcs_uri,
            "originalName": original_name,
            "bytes": size_bytes,
            # What the client claimed. Kept so ingest can compare it against
            # what the file actually decodes as; never trusted on its own.
            "contentType": content_type,
        },
        "media": {},
        "playback": {},
        "counts": {"moments": 0, "clips": 0},
        "error": None,
        "createdAt": now(),
        "updatedAt": now(),
    }
    job_ref(job_id).set(payload)
    return {"job_id": job_id, **payload}


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
                "segmentIndexes": moment.get("segment_indexes", []),
                "embedding": Vector(vector),
                "createdAt": now(),
            },
        )
    batch.commit()

    job_ref(job_id).update({"counts.moments": firestore.Increment(len(moments)), "updatedAt": now()})
    return len(moments)


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
        "segment_indexes": data.get("segmentIndexes", []),
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
described in its own words — not whether its type label resembles the query.

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

    lines = []
    for i, moment in enumerate(candidates):
        detail = moment.get("description") or moment.get("label", "")
        extra = []
        if moment.get("scoreboard"):
            extra.append(f"scoreboard {moment['scoreboard']}")
        if moment.get("is_goal"):
            extra.append("resulted in a goal")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"{i}. [{moment.get('label', 'Unknown')}] {detail}{suffix}")

    try:
        response = genai_client().models.generate_content(
            model=RERANK_MODEL,
            contents=_RERANK_PROMPT.format(
                query=query, count=len(candidates), candidates="\n".join(lines)
            ),
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_RerankResult,
                max_output_tokens=8192,
                thinking_config=types.ThinkingConfig(thinking_budget=2048),
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, _RerankResult) or not parsed.ranked:
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


def knn_search_moments(query: str, job_id: str = "", owner_uid: str = "",
                       limit: int = 10, rerank: bool = True) -> list[dict[str, Any]]:
    """Nearest-neighbour search over moment embeddings, reranked by Gemini.

    Scoped to one job when job_id is given, otherwise across the owner's whole
    library via a collection-group query. When ``rerank`` is set the index is
    over-fetched and the candidates are reordered by relevance.
    """
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
    from google.cloud.firestore_v1.vector import Vector

    vector = embed([query], task_type="RETRIEVAL_QUERY")[0]

    if job_id:
        base = job_ref(job_id).collection("moments")
    else:
        # Library-wide, across every job. A vector index with an equality
        # prefix only serves queries carrying that equality, so this needs an
        # index on the embedding alone — moments_knn_all in firestore.tf.
        base = db().collection_group("moments")

    fetch = min(limit * RERANK_OVERFETCH, RERANK_MAX_CANDIDATES) if rerank else limit

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

    if not rerank:
        return candidates[:limit]

    ranked = _rerank(query, candidates, limit)
    for position, moment in enumerate(ranked):
        moment["rank"] = position + 1
    return ranked


# --- Clips --------------------------------------------------------------------


def upsert_clips(job_id: str, clips: list[dict[str, Any]]) -> int:
    if not clips:
        return 0

    owner_uid = get_job(job_id).get("ownerUid", "")
    batch = db().batch()
    collection = job_ref(job_id).collection("clips")
    for clip in clips:
        doc_id = clip["clip_id"]
        batch.set(
            collection.document(doc_id),
            {
                "clipId": doc_id,
                "jobId": job_id,
                "ownerUid": owner_uid,
                "momentId": clip.get("moment_id"),
                "startSec": clip["start_sec"],
                "endSec": clip["end_sec"],
                "durationSec": clip["duration_sec"],
                "aspect": clip.get("aspect", "9:16"),
                "platforms": clip.get("platforms", []),
                "hookText": clip.get("hook_text", ""),
                "title": clip.get("title", ""),
                "captions": clip.get("captions", {}),
                "hashtags": clip.get("hashtags", []),
                "score": clip.get("score", 0.0),
                "rationale": clip.get("rationale", ""),
                "status": "suggested",
                "renderUri": None,
                "thumbnailUri": None,
                "createdAt": now(),
                "updatedAt": now(),
            },
            merge=True,
        )
    batch.commit()
    job_ref(job_id).update({"counts.clips": len(clips), "updatedAt": now()})
    return len(clips)


def _clip_out(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "clip_id": data.get("clipId"),
        "job_id": data.get("jobId"),
        "moment_id": data.get("momentId"),
        "start_sec": data.get("startSec", 0.0),
        "end_sec": data.get("endSec", 0.0),
        "duration_sec": data.get("durationSec", 0.0),
        "aspect": data.get("aspect", "9:16"),
        "platforms": data.get("platforms", []),
        "hookText": data.get("hookText", ""),
        "title": data.get("title", ""),
        "captions": data.get("captions", {}),
        "hashtags": data.get("hashtags", []),
        "score": data.get("score", 0.0),
        "rationale": data.get("rationale", ""),
        "status": data.get("status", "suggested"),
        "renderUri": data.get("renderUri"),
        "thumbnailUri": data.get("thumbnailUri"),
    }


def list_clips(job_id: str, limit: int = 100) -> list[dict[str, Any]]:
    from google.cloud import firestore

    query = (
        job_ref(job_id)
        .collection("clips")
        .order_by("score", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [_clip_out(d.to_dict()) for d in query.stream()]


_CLIP_WRITABLE = {
    "title", "hookText", "captions", "hashtags", "startSec", "endSec",
    "durationSec", "aspect", "platforms", "status", "renderUri", "thumbnailUri",
}


def delete_clip(job_id: str, clip_id: str) -> dict[str, Any]:
    """Drop a clip from the reel.

    The moment it was cut from is untouched: a clip is a suggestion about a
    moment, and rejecting the suggestion is not a claim that the moment did not
    happen. Removing the moment as well would also lose its embedding, and with
    it the ability to find the play again later.
    """
    ref = job_ref(job_id).collection("clips").document(clip_id)
    existed = ref.get().exists
    if existed:
        ref.delete()
    return {"job_id": job_id, "clip_id": clip_id, "deleted": existed}


def update_clip(job_id: str, clip_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update, rejecting fields callers must not rewrite.

    Score, momentId and ownerUid are derived, not editable — an agent that
    rewrote them would silently break ranking and the tenant boundary.
    """
    rejected = sorted(set(patch) - _CLIP_WRITABLE)
    clean = {k: v for k, v in patch.items() if k in _CLIP_WRITABLE}
    if not clean:
        return {"status": "error", "error": "Nothing writable in the patch.", "rejected": rejected}

    clean["updatedAt"] = now()
    if "startSec" in clean and "endSec" in clean:
        clean["durationSec"] = round(float(clean["endSec"]) - float(clean["startSec"]), 2)

    job_ref(job_id).collection("clips").document(clip_id).update(clean)
    return {"status": "success", "clip_id": clip_id, "updated": sorted(clean), "rejected": rejected}
