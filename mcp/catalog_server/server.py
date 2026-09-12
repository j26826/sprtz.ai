"""mcp-catalog — Firestore, embeddings and semantic search.

Private Cloud Run service. Every write the agents make to the job's data goes
through here, which is also what makes the UI's realtime listeners update.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from catalog_server import store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mcp-catalog")

mcp = FastMCP("sprtz-catalog")


def _fail(exc: Exception, **context: Any) -> dict:
    logger.exception("tool failed: %s", context)
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}", **context}


@mcp.tool
def create_job(job_id: str, owner_uid: str, title: str, sport: str, gcs_uri: str,
               original_name: str, size_bytes: int, content_type: str = "",
               metadata_language: str = "en", context_urls: list[str] | None = None,
               kind: str = "upload", hls_url: str = "", event_start: str = "",
               event_end: str = "", chunk_sec: int = 0,
               title_source: str = "derived", stall_minutes: float = 0) -> dict:
    """Open a new analysis job for an uploaded video, an HLS URL, or a live event.

    Args:
        job_id: Identifier to create the job under.
        owner_uid: Identity Platform uid of the owner.
        title: Human-readable title.
        sport: Sport in the video, for example "handball".
        gcs_uri: gs:// URI of the uploaded source. Empty for hls and live.
        original_name: The file name the user uploaded.
        size_bytes: Size of the upload.
        content_type: Content type the client declared, checked at ingest.
        metadata_language: ISO 639-1 code the analysis should write in.
        context_urls: Pages the editor says are about this recording, for grounding.
        kind: "upload", "hls" (a playlist to download first) or "live" (a
            playlist to record between two times).
        hls_url: The playlist URL, for hls and live.
        event_start: ISO 8601 start of a live event.
        event_end: ISO 8601 end of a live event.
        chunk_sec: Live chunk length; the deployment default when 0.
        title_source: "editor" when a person typed the title, "derived" when it
            was taken off a filename or a URL. The editor's own name wins over
            the title the game record would compose for itself.
        stall_minutes: For a live event, how long a stream that was flowing may
            produce nothing before the event is finished. 0 waits for event_end.
    """
    try:
        return {"status": "success", **store.create_job(
            job_id, owner_uid, title, sport, gcs_uri, original_name, size_bytes,
            content_type, metadata_language, context_urls or [],
            kind, hls_url, event_start, event_end, chunk_sec,
            title_source=title_source, stall_minutes=stall_minutes)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def get_job(job_id: str) -> dict:
    """Read a job document.

    Args:
        job_id: Identifier of the job.
    """
    try:
        return store.get_job(job_id)
    except KeyError as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_jobs(owner_uid: str, limit: int = 20, status: str = "") -> dict:
    """List an owner's recent jobs, newest first.

    Args:
        owner_uid: Ignored. Jobs are shared across the desk.
        limit: Most jobs to return.
        status: Optional filter. "running" means anything the pipeline still
            owes an answer for; otherwise an exact status such as "ready".
    """
    try:
        jobs = store.list_jobs(owner_uid, limit=limit, status=status)
        return {"status": "success", "jobs": jobs, "count": len(jobs)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, owner_uid=owner_uid)


@mcp.tool
def update_job_status(job_id: str, status: str, stage: str = "", error: str = "",
                      progress: int = -1) -> dict:
    """Move a job to a new status, and optionally a new stage.

    Args:
        job_id: Identifier of the job.
        status: New status, e.g. uploaded, transcoding, analyzing, analyzed, ready, failed.
        stage: Current pipeline stage. Empty string leaves it unchanged.
        error: Failure message. Empty string leaves it unchanged.
        progress: Percent complete 0-100. Pass -1 to leave unchanged.
    """
    try:
        return {"status": "success", **store.update_job_status(
            job_id, status,
            stage=stage or None,
            error=error or None,
            progress=None if progress < 0 else progress,
        )}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def delete_job(job_id: str) -> dict:
    """Delete a job and every record hanging off it.

    Removes the moments, events and the game record as well as the job
    itself. The source video is the media server's to delete.

    Args:
        job_id: Job to delete.
    """
    try:
        return {"status": "success", **store.delete_job(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def clear_analysis(job_id: str) -> dict:
    """Drop a job's moments and game record so it can be analysed again.

    Args:
        job_id: Job to reset.
    """
    try:
        return {"status": "success", **store.clear_analysis(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def request_cancel(job_id: str) -> dict:
    """Ask a running job to stop at its next stage boundary.

    Args:
        job_id: Job to cancel.
    """
    try:
        return {"status": "success", **store.request_cancel(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def cancel_requested(job_id: str) -> dict:
    """Whether a stop has been asked for. Stages check this between steps.

    Args:
        job_id: Job to check.
    """
    try:
        return {"status": "success", "job_id": job_id,
                "cancelling": store.cancel_requested(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_media_info(job_id: str, media: dict, segment_count: int) -> dict:
    """Persist the probe results onto a job.

    Args:
        job_id: Identifier of the job.
        media: Output of the media server's probe_media tool.
        segment_count: How many analysis segments the video will be split into.
    """
    try:
        return {"status": "success", **store.record_media_info(job_id, media, segment_count)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_playback(job_id: str, playback_url: str, poster_url: str,
                    renditions: list[str], segment_seconds: int) -> dict:
    """Save the CDN HLS URL for a job so the editor can play it.

    Args:
        job_id: Identifier of the job.
        playback_url: CDN URL of the HLS master playlist.
        poster_url: CDN URL of the poster image.
        renditions: Rendition names in the ladder.
        segment_seconds: HLS segment duration.
    """
    try:
        return {"status": "success", **store.record_playback(
            job_id, playback_url, poster_url, renditions, segment_seconds)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_teams(job_id: str, home: str, away: str) -> dict:
    """Save the two teams as read from the score bug.

    Args:
        job_id: Identifier of the job.
        home: Home team, the first side on the bug.
        away: Away team, the second side on the bug.
    """
    try:
        return {"status": "success", **store.record_teams(job_id, home, away)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def emit_event(job_id: str, stage: str, level: str, message: str, data: dict) -> dict:
    """Append a line to the job's live activity feed.

    Args:
        job_id: Identifier of the job.
        stage: Pipeline stage the event belongs to.
        level: One of info, warning, error.
        message: Human-readable line shown in the editor.
        data: Any structured detail to attach.
    """
    try:
        return {"status": "success", **store.emit_event(job_id, stage, level, message, data)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def upsert_moments(job_id: str, moments: list[dict]) -> dict:
    """Save key moments, generating an embedding for each.

    Args:
        job_id: Identifier of the job.
        moments: Moment records. Each may carry embed_text to control what is embedded.
    """
    try:
        return {"status": "success", "job_id": job_id, "saved": store.upsert_moments(job_id, moments)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def record_moment_thumbnails(job_id: str, thumbnails: dict) -> dict:
    """Attach the thumbnail written for each moment to its record.

    Args:
        job_id: Identifier of the job.
        thumbnails: moment_id -> gs:// URI of the moment's thumbnail.
    """
    try:
        saved = store.record_moment_thumbnails(job_id, thumbnails)
        return {"status": "success", "job_id": job_id, "saved": saved}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def rename_job(job_id: str, title: str) -> dict:
    """Rename a job, and its game record with it.

    One recording, one name. An editor who renames a match has said which name
    they want, so the analysis keeps it rather than composing over it next time.

    Args:
        job_id: The job.
        title: The new name. Blank is refused — a match with no name is worse
            than one named after its file.
    """
    try:
        return {"status": "success", **store.rename_job(job_id, title)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def update_job_context(job_id: str, context_urls: list[str]) -> dict:
    """Replace the pages an editor says are about this job.

    Args:
        job_id: The job.
        context_urls: http(s) URLs. The whole list; it replaces what was there.
    """
    try:
        return {"status": "success", **store.update_job_context(job_id, context_urls or [])}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_top_moments(limit: int = 20, sport: str = "", job_ids: list[str] | None = None) -> dict:
    """The key moments across every game on the desk, best first, each naming its game.

    Args:
        limit: Most moments to return.
        sport: Keep only games of this sport, e.g. "equestrian". Empty for all.
        job_ids: Keep only these games. Empty for every game on the desk.

    Returns:
        dict with `moments` (each carrying `game`), `running` (job ids still
        analysing, whose moments do not exist yet) and `games_searched`.
    """
    try:
        return {"status": "success", **store.list_top_moments(limit=limit, sport=sport, job_ids=job_ids or [])}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool
def update_moment_identity(job_id: str, identities: list[dict]) -> dict:
    """Set who was riding on moments that already exist.

    Args:
        job_id: The job the moments belong to.
        identities: [{"moment_id", "rider", "horse", "start_number", "ride_order",
            "identity_source"}, ...]. Moments not listed are left untouched.

    Returns:
        dict with `updated`, the number of moments patched.
    """
    try:
        return {"status": "success", "updated": store.update_moment_identity(job_id, identities)}
    except Exception as exc:  # noqa: BLE001 — reported to the caller, never raised past the tool
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool
def list_moments(job_id: str, limit: int, min_score: float) -> dict:
    """List a job's key moments, highest scoring first.

    Args:
        job_id: Identifier of the job.
        limit: Maximum number to return.
        min_score: Lowest highlight score to include.
    """
    try:
        return {"status": "success", "job_id": job_id,
                "moments": store.list_moments(job_id, limit, min_score)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def update_live_booking(job_id: str, event_start: str = "", event_end: str = "",
                        hls_url: str = "", title: str = "", sport: str = "",
                        metadata_language: str = "", stall_minutes: float = 0,
                        context_urls: list[str] | None = None) -> dict:
    """Change a live event's booking, before it starts.

    Args:
        job_id: Identifier of the job.
        event_start: New ISO 8601 start, or empty to leave it.
        event_end: New ISO 8601 end, or empty to leave it.
        hls_url: New playlist URL, or empty to leave it.
        title: New title, or empty to leave it.
        sport: New sport, or empty to leave it.
        metadata_language: New metadata language, or empty to leave it.
        stall_minutes: New stall limit, or 0 to leave it.
        context_urls: Replacement context links, or null to leave them.
    """
    try:
        return {"status": "success", **store.update_live_booking(
            job_id, event_start=event_start, event_end=event_end, hls_url=hls_url,
            title=title, sport=sport, metadata_language=metadata_language,
            stall_minutes=stall_minutes, context_urls=context_urls)}
    except (KeyError, ValueError) as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def get_config(name: str) -> dict:
    """Read one deployment configuration document, such as "youtube".

    Args:
        name: Which configuration to read.
    """
    try:
        return {"status": "success", "name": name, "config": store.get_config(name)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, name=name)


@mcp.tool
def set_config(name: str, values: dict) -> dict:
    """Merge fields into one deployment configuration document.

    Args:
        name: Which configuration to write.
        values: Fields to set. Existing fields not named here are left alone.
    """
    try:
        return {"status": "success", **store.set_config(name, values)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, name=name)


@mcp.tool
def clear_config(name: str, fields: list[str]) -> dict:
    """Blank named fields of a configuration document.

    Args:
        name: Which configuration to write.
        fields: Field names to clear.
    """
    try:
        return {"status": "success", **store.clear_config(name, fields)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, name=name)


@mcp.tool
def get_moment(job_id: str, moment_id: str) -> dict:
    """Read one moment by id.

    Args:
        job_id: Identifier of the job.
        moment_id: Identifier of the moment.
    """
    try:
        moment = store.get_moment(job_id, moment_id)
        if moment is None:
            return {"status": "error", "error": "No such moment.",
                    "job_id": job_id, "moment_id": moment_id}
        return {"status": "success", "moment": moment}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id, moment_id=moment_id)


@mcp.tool
def list_action_plays(job_id: str, limit: int = 500, min_score: float = 0.0) -> dict:
    """Every detected moment in a job as ActionPlay records, in match order.

    Args:
        job_id: Job whose moments to list.
        limit: Most records to return.
        min_score: Drop anything below this highlight score.
    """
    try:
        plays = store.list_action_plays(job_id, limit=limit, min_score=min_score)
        return {"status": "success", "job_id": job_id, "action_plays": plays,
                "count": len(plays)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def upsert_game(job_id: str, game: dict, embed_text: str = "", class_id: str = "") -> dict:
    """Save the match-level record and index it for game search.

    Args:
        job_id: Job the game belongs to.
        game: GameDetails fields.
        embed_text: What to embed. Falls back to the teams and summary.
        class_id: One competition of a recording that held several. Empty is
            the whole recording, which is how every sport but equestrian and
            every single-class day is stored.
    """
    try:
        return {"status": "success",
                **store.upsert_game(job_id, game, embed_text, class_id=class_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_games(job_id: str) -> dict:
    """Every competition one recording holds, in running order.

    A live URL points at an arena, so a day's capture can cross several
    classes; each is an event of its own. One entry for a recording that held
    one competition, which is every sport but equestrian.

    Args:
        job_id: Identifier of the job.
    """
    try:
        return {"status": "success", "job_id": job_id, "games": store.list_games(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def get_game(job_id: str) -> dict:
    """Read the overall game details for a job.

    Args:
        job_id: Job whose game record to read.
    """
    try:
        return {"status": "success", "game": store.get_game(job_id)}
    except KeyError as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_game_rides(job_id: str, class_id: str = "") -> dict:
    """A competition day's rides in running order, as the game record holds
    them: rider, horse, start and end, test type, judges' marks, total, rank,
    score check and where the score came from. One document read.

    Args:
        job_id: Job whose rides to read.
        class_id: One competition of it, or empty for the whole recording.
    """
    try:
        return {"status": "success", "job_id": job_id,
                "rides": store.get_rides(job_id, class_id=class_id)}
    except KeyError as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def get_event_tree(job_id: str, class_id: str = "") -> dict:
    """One event as a tree: the event, each ride in running order (a rider on
    one horse), and the moments that happened during each ride.

    Moments outside every ride are listed under ``unassignedMoments``. A sport
    without rides returns no riders and every moment unassigned.

    Args:
        job_id: Job whose event to read.
        class_id: One competition of a day that held several; empty is the
            first of them, which is the whole recording when it held one.
    """
    try:
        return {"status": "success", **store.event_tree(job_id, class_id=class_id)}
    except KeyError as exc:
        return {"status": "error", "error": str(exc), "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def knn_search_games(query: str, owner_uid: str, limit: int = 5) -> dict:
    """Find whole matches by meaning — teams, competition, venue, how it felt.

    This searches games, not the moments inside them. Use it for "the Sweden
    Denmark match" or "that intense final"; use knn_search_moments for a play.

    Args:
        query: Plain-language description of the match.
        owner_uid: Ignored. Games are shared across the desk.
        limit: Most games to return.
    """
    try:
        games = store.knn_search_games(query, owner_uid, limit)
        return {"status": "success", "games": games, "count": len(games)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, query=query)


@mcp.tool
def match_games_by_title(query: str, limit: int = 5) -> dict:
    """Find games a question names outright, by comparing the text of the title.

    Use this before knn_search_games when the question contains what looks like
    a fixture — "moments of FAG v TVB — DAIKIN HBL". A name is what a vector
    search is worst at: abbreviations and a sponsor sit near every other
    fixture in the same league, so meaning-search answers with a plausible
    neighbour instead of the match that was asked for.

    Args:
        query: The editor's question, or the title itself.
        limit: Most games to return.
    """
    try:
        games = store.match_games_by_title(query, limit)
        return {"status": "success", "games": games, "count": len(games)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, query=query)


@mcp.tool
def knn_search_moments(query: str, job_id: str, limit: int, owner_uid: str = "",
                       sport: str = "", job_ids: list[str] | None = None,
                       rerank: bool = True) -> dict:
    """Find moments whose meaning matches a plain-language query.

    Embedding search retrieves candidates, then Gemini 2.5 Flash reranks them by
    how well they actually answer the query. Each result carries `similarity`
    (the vector score), `rerank_score` and `rerank_reason` (the model's judgement
    and why), and `rank` (final position). A null `rerank_score` means the
    reranker was unavailable and the vector order stands.

    Args:
        query: What to look for, in plain language.
        job_id: Job to search within. Empty string searches the owner's whole library.
        limit: Maximum results.
        owner_uid: Required only for a library-wide search.
        sport: Only moments from games of this sport, e.g. "equestrian". Library-wide only.
        job_ids: Only moments from these games. One id is answered by the per-job index.
        rerank: Set false to skip reranking and return raw vector order.
    """
    try:
        moments = store.knn_search_moments(query, job_id, owner_uid, limit, rerank=rerank, sport=sport, job_ids=job_ids or [])
        return {
            "status": "success",
            "query": query,
            "reranked": rerank and any(m.get("rerank_score") is not None for m in moments),
            "moments": moments,
        }
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, query=query, job_id=job_id)


# The startup probe's target, and the whole of what Cloud Run judges this
# service by. It was deleted as a neighbour of the clip tools when those were
# removed — the region sliced ran to the next `@mcp.tool`, and this sits
# between two of them. The service came up, served /mcp perfectly, answered
# every /healthz with 404, and the revision never went healthy: twelve minutes
# of "Still modifying..." and then a failed deploy.
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-catalog"})


@mcp.tool
def set_source(job_id: str, gcs_uri: str, analysis_uri: str = "", original_name: str = "",
               size_bytes: int = 0, content_type: str = "") -> dict:
    """Record where a job's video ended up, once a download has produced it.

    Args:
        job_id: The job.
        gcs_uri: gs:// URI of the source object.
        analysis_uri: gs:// URI the analysis should read instead (the 1 fps proxy).
        original_name: File name of the source.
        size_bytes: Size of the source.
        content_type: Content type of the source.
    """
    try:
        return {"status": "success", **store.set_source(
            job_id, gcs_uri, analysis_uri, original_name, size_bytes, content_type)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_live_jobs() -> dict:
    """Every live event that is scheduled or running, with its live state."""
    try:
        return {"status": "success", "jobs": store.list_live_jobs()}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool
def update_live(job_id: str, patch: dict) -> dict:
    """Patch fields under a live job's `live` map.

    Args:
        job_id: The live event's job.
        patch: Field -> value, applied under `live.`.
    """
    try:
        return {"status": "success", **store.update_live(job_id, patch or {})}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def list_live_chunks(job_id: str) -> dict:
    """The chunks the live recorder has closed for a job, in order.

    Args:
        job_id: The live event's job.
    """
    try:
        return {"status": "success", "chunks": store.list_live_chunks(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def claim_live_chunk(job_id: str, index: int) -> dict:
    """Take one captured chunk for analysis; `claimed` is false if someone already has.

    Args:
        job_id: The live event's job.
        index: The chunk's index.
    """
    try:
        return {"status": "success", **store.claim_live_chunk(job_id, index)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id, index=index)


@mcp.tool
def finish_live_chunk(job_id: str, index: int, moments: int = 0, error: str = "",
                      continuity: dict | None = None, summary: str = "",
                      competition: str = "", venue: str = "", discipline: str = "",
                      discipline_confidence: float = 0.0, muxed_uri: str = "",
                      ride_fragments: list[dict] | None = None,
                      not_confirmed: list[dict] | None = None) -> dict:
    """Record the outcome of analysing one live chunk.

    Args:
        job_id: The live event's job.
        index: The chunk's index.
        moments: How many moments were saved from it.
        error: Why it failed, if it did.
        continuity: What the check against the previous chunk found.
        summary: The segment summary the analysis wrote.
        competition: Competition read off the picture, if any.
        venue: Venue read off the picture, if any.
        discipline: Discipline code the chunk reported, if any.
        discipline_confidence: Its confidence.
        muxed_uri: The chunk with its audio muxed in, when the tick made one.
        ride_fragments: The rides this chunk saw, absolute and not yet stitched.
        not_confirmed: What this chunk looked for and did not find.
    """
    try:
        return {"status": "success", **store.finish_live_chunk(
            job_id, index, moments, error, continuity, summary, competition, venue,
            discipline, discipline_confidence, muxed_uri=muxed_uri,
            ride_fragments=ride_fragments, not_confirmed=not_confirmed)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id, index=index)


@mcp.tool
def list_stalled_jobs(minutes: int = 15) -> dict:
    """Running jobs nothing has written to for `minutes` — runs that died with their process.

    Args:
        minutes: How long without a write counts as dead.
    """
    try:
        return {"status": "success", "jobs": store.list_stalled_jobs(minutes)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool
def note_recovery(job_id: str, reason: str = "") -> dict:
    """Count an automatic restart on a job.

    Args:
        job_id: The job being restarted.
        reason: Why.
    """
    try:
        return {"status": "success", **store.note_recovery(job_id, reason)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id)


@mcp.tool
def reset_live_chunk(job_id: str, index: int, stale_after_minutes: int = 0) -> dict:
    """Return a failed live chunk to `captured` for another attempt, up to a limit.

    Args:
        job_id: The live event's job.
        index: The chunk's index.
        stale_after_minutes: Also reset a chunk still `analyzing` from a claim older
            than this — the tick that claimed it died with its process.
    """
    try:
        return {"status": "success", **store.reset_live_chunk(job_id, index, stale_after_minutes=stale_after_minutes)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, job_id=job_id, index=index)


def main() -> None:
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
