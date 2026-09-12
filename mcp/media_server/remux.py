"""Mux a separate audio rendition into a downloaded HLS recording.

JW Player and Unified Streaming publish video-only variants and put the audio
in an ``EXT-X-MEDIA`` group of its own. The download tool muxes that into a
CMAF recording as it goes, and for MPEG-TS it does not — the recording lands
silent, and so do the analysis, the preview and every cut taken from it.

This is the other half, as a Cloud Run Job on the media image: fetch the
audio playlist and its segments (small — a hundred kilobits a second, so a
three-hour match is under two hundred megabytes and fits the container's
memory-backed disk), then run one ffmpeg that reads the video over HTTPS,
the audio from disk, and stream-copies both into a new transport stream that
goes straight back to the bucket through a resumable upload. No encode, and
no video byte held here: the writable filesystem is memory.

Environment: ``JOB_ID``, ``VIDEO_URI`` (gs://), ``AUDIO_PLAYLIST_URL``,
``OUTPUT_URI`` (gs://). Exit 0 on success.
"""

from __future__ import annotations

import logging
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from media_server import ffmpeg_ops, gcs, hls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("remux")

FETCH_TIMEOUT = 60
PARALLEL_FETCHES = 8
# A CDN drops a connection now and then on three thousand fetches — the first
# real run lost its whole mux to one "Remote end closed connection". Retried
# from two seconds, doubling, with jitter so eight workers do not retry in step.
FETCH_ATTEMPTS = 5
FETCH_BACKOFF = 2.0

_session = threading.local()
UPLOAD_CHUNK = 32 * 1024 * 1024
READ_CHUNK = 8 * 1024 * 1024


def ffmpeg_command(video_url: str, audio_src: str, bearer_token: str | None) -> list[str]:
    """One stream-copy remux to stdout: video from the URL, audio from disk or a URL.

    The output is MPEG-TS because that is what the video is, and because a
    transport stream needs no seeking to write — it can go up a pipe. A
    chunk's audio is small enough to read straight from the bucket, so the
    audio input may be a URL too; the header applies per input.
    """
    audio_args = ffmpeg_ops.http_input_args(audio_src, bearer_token) \
        if audio_src.startswith("http") else []
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", *ffmpeg_ops._FFMPEG_HARDENING,
        *ffmpeg_ops.http_input_args(video_url, bearer_token), "-i", video_url,
        *audio_args, "-i", audio_src,
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        "-f", "mpegts", "pipe:1",
    ]


def fetch_playlist(url: str) -> hls.MediaPlaylist:
    response = requests.get(url, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    text = response.text
    if hls.is_master(text):
        variant = hls.pick_variant(hls.parse_master(text, url))
        if variant is None:
            raise RuntimeError("the audio playlist is a master with no variants")
        return fetch_playlist(variant.url)
    return hls.parse_media(text, url)


def _fetch(url: str) -> bytes:
    """One segment, on a per-thread keep-alive session, retried on the way."""
    session = getattr(_session, "s", None)
    if session is None:
        session = _session.s = requests.Session()
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            response = session.get(url, timeout=FETCH_TIMEOUT)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.content
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last = exc
            if isinstance(exc, requests.HTTPError) and exc.response is not None \
                    and exc.response.status_code < 500:
                raise
            delay = FETCH_BACKOFF * (2 ** attempt) * (0.5 + random.random())
            logger.warning("fetch %s failed (%s); retry %d/%d in %.1fs",
                           url.rsplit("/", 1)[-1][:60], exc, attempt + 1, FETCH_ATTEMPTS, delay)
            time.sleep(delay)
    raise RuntimeError(f"gave up fetching {url} after {FETCH_ATTEMPTS} attempts: {last}")


def download_audio(playlist: hls.MediaPlaylist, dest: Path) -> int:
    """Concatenate every audio segment, in order, into ``dest``. Returns bytes."""
    total = 0
    with dest.open("wb") as out:
        if playlist.init_url:
            total += out.write(_fetch(playlist.init_url))
        with ThreadPoolExecutor(max_workers=PARALLEL_FETCHES) as pool:
            for index, data in enumerate(pool.map(_fetch, [s.url for s in playlist.segments]), 1):
                total += out.write(data)
                if index % 500 == 0 or index == len(playlist.segments):
                    logger.info("[%d/%d] audio segments fetched", index, len(playlist.segments))
    return total


def remux_to_gcs(video_uri: str, audio_src: Path | str, output_uri: str) -> int:
    """Run ffmpeg and stream its output into the bucket. Returns bytes written.

    ``audio_src`` is a local file, or a gs:// URI read over HTTPS.
    """
    audio = gcs.https_url(str(audio_src)) if str(audio_src).startswith("gs://") else str(audio_src)
    cmd = ffmpeg_command(gcs.https_url(video_uri), audio, gcs.bearer_token())
    logger.info("running: ffmpeg ... -i <video> -i <audio> -c copy -f mpegts pipe:1")
    bucket, name = gcs.split_uri(output_uri)
    blob = gcs.client().bucket(bucket).blob(name)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr: list[bytes] = []
    drain = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
    drain.start()
    written = 0
    with blob.open("wb", chunk_size=UPLOAD_CHUNK, content_type="video/mp2t") as out:
        while True:
            data = proc.stdout.read(READ_CHUNK)
            if not data:
                break
            out.write(data)
            written += len(data)
            if written % (1024 * 1024 * 1024) < READ_CHUNK:
                logger.info("%.1f GB written", written / 1e9)
    code = proc.wait()
    drain.join(timeout=30)
    if code != 0:
        # The upload was opened before ffmpeg produced anything, so a failed
        # run leaves an empty object under the muxed name. Remove it: an
        # object that exists and is zero bytes reads as a finished mux.
        try:
            blob.delete()
        except Exception:  # noqa: BLE001
            logger.warning("could not remove the partial output %s", output_uri, exc_info=True)
        tail = b"".join(stderr).decode("utf-8", "replace").strip().splitlines()[-12:]
        raise RuntimeError("ffmpeg exited %d: %s" % (code, "\n".join(tail) or "no output"))
    return written


def main() -> int:
    job_id = os.environ.get("JOB_ID", "")
    video_uri = os.environ.get("VIDEO_URI", "")
    audio_url = os.environ.get("AUDIO_PLAYLIST_URL", "")
    output_uri = os.environ.get("OUTPUT_URI", "")
    if not (job_id and video_uri and audio_url and output_uri):
        logger.error("JOB_ID, VIDEO_URI, AUDIO_PLAYLIST_URL and OUTPUT_URI are required")
        return 2
    work = Path(tempfile.mkdtemp(prefix="remux-"))
    try:
        playlist = fetch_playlist(audio_url)
        logger.info("audio playlist: %d segments, %s", len(playlist.segments),
                    hls.container_of(playlist))
        audio_path = work / f"audio.{hls.container_of(playlist)}"
        audio_bytes = download_audio(playlist, audio_path)
        logger.info("audio fetched: %d bytes", audio_bytes)
        written = remux_to_gcs(video_uri, audio_path, output_uri)
        logger.info("muxed recording written: %d bytes to %s", written, output_uri)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("remux failed for %s: %s", job_id, exc)
        return 1
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
