"""Mux a separate audio rendition into a downloaded HLS recording.

JW Player and Unified Streaming publish video-only variants and put the audio
in an ``EXT-X-MEDIA`` group of its own. The download tool muxes that into a
CMAF recording as it goes, and for MPEG-TS it does not — the recording lands
silent, and so do the analysis, the preview and every clip cut from it.

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
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from media_server import ffmpeg_ops, gcs, hls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("remux")

FETCH_TIMEOUT = 60
PARALLEL_FETCHES = 8
UPLOAD_CHUNK = 32 * 1024 * 1024
READ_CHUNK = 8 * 1024 * 1024


def ffmpeg_command(video_url: str, audio_path: str, bearer_token: str | None) -> list[str]:
    """One stream-copy remux to stdout: video from the URL, audio from disk.

    The output is MPEG-TS because that is what the video is, and because a
    transport stream needs no seeking to write — it can go up a pipe.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", *ffmpeg_ops._FFMPEG_HARDENING,
        *ffmpeg_ops.http_input_args(video_url, bearer_token), "-i", video_url,
        "-i", audio_path,
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
    response = requests.get(url, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response.content


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


def remux_to_gcs(video_uri: str, audio_path: Path, output_uri: str) -> int:
    """Run ffmpeg and stream its output into the bucket. Returns bytes written."""
    cmd = ffmpeg_command(gcs.https_url(video_uri), str(audio_path), gcs.bearer_token())
    logger.info("running: ffmpeg ... -i <video> -i %s -c copy -f mpegts pipe:1", audio_path.name)
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
