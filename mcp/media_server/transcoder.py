"""Google Cloud Transcoder API — the HLS package for review playback.

ffmpeg used to do this in-process, and the shape of that job was wrong for the
place it ran. A copy-remux writes as many gigabytes of segments as the source is
long, Cloud Run's writable filesystem is memory, and the drain emptying it had to
outrun the writer or the container died holding the backlog. It did die,
repeatedly. A real 480p encode was never even on the table there: hours of CPU
against a one-hour request ceiling.

Transcoder API moves all of it off this service. It reads the source from GCS and
writes the package to GCS itself, so no video byte passes through this container
— the work left here is creating a job and asking how it is doing. That turns the
memory ceiling into someone else's problem and makes a real 480p rendition
affordable, which is what a preview wants: nobody needs a 3.4 GB match at full
bitrate to decide whether a moment is worth cutting.

The API is asynchronous by design and this module does not paper over that.
:func:`create_preview_job` returns as soon as the job is accepted and
:func:`job_state` reports on it; blocking a request until a match-length encode
finished would only move the one-hour ceiling from ffmpeg onto an idle HTTP
connection.

The Google client is imported inside the accessors like every other Google import
here — they cost ~100s at module scope on Cloud Run, and the service has to answer
its health check long before that.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
# Transcoder is regional and serves fewer regions than Cloud Run, so where it
# runs is configurable rather than pinned to wherever this service happens to be.
LOCATION = os.environ.get(
    "TRANSCODER_LOCATION", os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
)

# One 480p rendition. A ladder is what you build for public delivery; this stream
# exists so an editor can scrub a match and judge a moment, and every extra
# rendition is encode minutes spent on a picture nobody watches.
PREVIEW_HEIGHT = 480
PREVIEW_WIDTH = 854  # 16:9 at 480p, even-numbered as H.264 requires
PREVIEW_BITRATE_BPS = 1_200_000
PREVIEW_FRAME_RATE = 25
AUDIO_BITRATE_BPS = 96_000
SEGMENT_SECONDS = 6

MASTER_PLAYLIST = "master.m3u8"

_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        from google.cloud.video import transcoder_v1

        _client = transcoder_v1.TranscoderServiceClient()
    return _client


def parent() -> str:
    return f"projects/{PROJECT_ID}/locations/{LOCATION}"


def output_uri(bucket: str, job_id: str) -> str:
    # Transcoder treats the output as a directory prefix and writes the playlists
    # and segments directly beneath it, so the trailing slash is load-bearing.
    return f"gs://{bucket}/jobs/{job_id}/hls/"


def build_preview_config(out_uri: str) -> Any:
    """A single-rendition 480p HLS package."""
    from google.cloud.video import transcoder_v1
    from google.protobuf import duration_pb2

    segment = duration_pb2.Duration(seconds=SEGMENT_SECONDS)

    return transcoder_v1.types.JobConfig(
        elementary_streams=[
            transcoder_v1.types.ElementaryStream(
                key="video-480p",
                video_stream=transcoder_v1.types.VideoStream(
                    h264=transcoder_v1.types.VideoStream.H264CodecSettings(
                        height_pixels=PREVIEW_HEIGHT,
                        width_pixels=PREVIEW_WIDTH,
                        bitrate_bps=PREVIEW_BITRATE_BPS,
                        frame_rate=PREVIEW_FRAME_RATE,
                        # One keyframe per segment. Seeking to a moment's in
                        # point is the only thing this stream is for, and
                        # without a keyframe at the segment boundary the player
                        # starts late — the feature failing quietly rather than
                        # loudly.
                        gop_duration=segment,
                    ),
                ),
            ),
            transcoder_v1.types.ElementaryStream(
                key="audio-aac",
                audio_stream=transcoder_v1.types.AudioStream(
                    codec="aac",
                    bitrate_bps=AUDIO_BITRATE_BPS,
                ),
            ),
        ],
        mux_streams=[
            transcoder_v1.types.MuxStream(
                key="hls-480p",
                container="ts",
                elementary_streams=["video-480p", "audio-aac"],
                segment_settings=transcoder_v1.types.SegmentSettings(
                    segment_duration=segment,
                    # Without this the container is written as one file and
                    # there is no HLS package to serve.
                    individual_segments=True,
                ),
            ),
        ],
        manifests=[
            transcoder_v1.types.Manifest(
                file_name=MASTER_PLAYLIST,
                type_=transcoder_v1.types.Manifest.ManifestType.HLS,
                mux_streams=["hls-480p"],
            ),
        ],
        output=transcoder_v1.types.Output(uri=out_uri),
    )


def create_preview_job(source_uri: str, hls_bucket: str, job_id: str) -> dict[str, Any]:
    """Start the 480p HLS encode. Returns as soon as it is accepted."""
    from google.cloud.video import transcoder_v1

    out_uri = output_uri(hls_bucket, job_id)
    job = transcoder_v1.types.Job(
        input_uri=source_uri,
        output_uri=out_uri,
        config=build_preview_config(out_uri),
        # Let finished jobs age out on their own. The package lives in GCS; the
        # job record is only interesting while it is running or has just failed.
        ttl_after_completion_days=7,
        labels={"sprtz_job": job_id[:63]},
    )
    created = client().create_job(parent=parent(), job=job)
    logger.info("transcoder job %s created for %s", created.name, job_id)
    return {
        "transcoder_job": created.name,
        "output_uri": out_uri,
        "master_playlist_uri": f"{out_uri}{MASTER_PLAYLIST}",
    }


# Transcoder's own names for where a job has got to, mapped to the two things a
# caller actually needs to decide: keep waiting, or stop.
_TERMINAL = {"SUCCEEDED", "FAILED"}


def job_state(name: str) -> dict[str, Any]:
    """Where a transcoder job has got to.

    ``name`` is the full resource name returned by :func:`create_preview_job`.
    """
    job = client().get_job(name=name)
    state = job.state.name
    result: dict[str, Any] = {
        "transcoder_job": name,
        "state": state,
        "done": state in _TERMINAL,
        "succeeded": state == "SUCCEEDED",
    }
    if job.error and job.error.message:
        # A failed encode reports why, and that reason is the only thing that
        # distinguishes a corrupt upload from a misconfigured job.
        result["error"] = job.error.message
    return result
