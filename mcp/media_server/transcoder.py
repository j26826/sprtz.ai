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

# A real recording can be missing frames in the middle of a stream — a
# LeMieux upload has two minutes with no audio at 01:32:00 — and Transcoder
# refuses the whole encode for it: "Failed to generate output for elementary
# stream audio-aac. Media frames are missing starting at time 5520s and
# ending at time 5640s." With this it fills the gap and encodes the rest,
# which is what a preview and an analysis proxy both want: the alternative
# is no picture at all because of two silent minutes.
FILL_CONTENT_GAPS = True
# Required with it: "config.elementaryStreams[0].videoStream.h264
# .frameRateConversionStrategy is DOWNSAMPLE, must be set to DROP_DUPLICATE
# when fillContentGaps is enabled" — the API refuses the job at creation
# otherwise. Dropping or duplicating whole frames is also the honest way to
# hold a fixed rate across a gap that has no frames to blend.
FRAME_RATE_CONVERSION = "DROP_DUPLICATE"
# And a third field, refused in turn: "frameRateConversionStrategy is
# DROP_DUPLICATE, optimization should be DISABLED". Transcoder's autodetect
# optimisation reserves the right to skip work it thinks is redundant, which
# it cannot do while it is being told to duplicate frames across a gap.
OPTIMIZATION = "DISABLED"

# The analysis proxy: the same 480p picture at one frame a second, audio kept.
# Gemini samples a video at 1 fps whatever it is given, so this is the picture
# it reads anyway at a fraction of the bytes — a 3.75-hour, 6.8 GB recording
# becomes a few hundred megabytes, which fits under the model's fetch limit
# without being cut into windows. It used to be made by ffmpeg inside the
# hls2mp4 job, one core decoding the whole recording after the download; a
# Transcoder job splits the encode across its own workers and reads the
# source from the bucket, so a long recording is minutes rather than an hour
# and the download job is only a download.
PROXY_FRAME_RATE = 1
PROXY_HEIGHT = PREVIEW_HEIGHT
PROXY_WIDTH = PREVIEW_WIDTH
# At one frame a second this is half a megabit per frame, which is a
# still-quality picture; what is being bought is legibility of a score bug at
# 480p, not motion.
PROXY_BITRATE_BPS = 400_000
PROXY_AUDIO_BITRATE_BPS = 64_000
# One keyframe every ten frames. The analysis reads the file from the start,
# so seekability only matters for the range reads that cut a window out of it.
PROXY_GOP_SECONDS = 10
# Transcoder names an unsegmented mux stream `<key>.mp4` under the output
# prefix, so the proxy's URI is known before the encode has started.
PROXY_STREAM_KEY = "proxy_1fps"
PROXY_FILE_NAME = f"{PROXY_STREAM_KEY}.mp4"

# The reel: what actually goes to a channel, so it is the one encode here whose
# job is to look good rather than to be cheap to scrub.
#
# It is also the reason a reel can draw on several matches at once. A reel is
# one Transcoder job with an `Input` per source and an `EditAtom` per cut, and
# the encoder normalises whatever it is given to the single output spec below —
# so cuts from a 1080i50 broadcast and a 720p30 stream land in one file without
# this service touching a video byte. Concatenating them here with ffmpeg would
# put N range-reads across several multi-gigabyte sources through a filesystem
# that is really RAM, which is the shape that killed this container twice.
REEL_HEIGHT = 1080
REEL_WIDTH = 1920
REEL_BITRATE_BPS = 6_000_000
# One rate for every source, because the sources disagree: European broadcast
# is 25 or 50, and a cut from each in one file has to settle on something. 30 is
# the most widely accepted by YouTube, and DROP_DUPLICATE (required anyway by
# FILL_CONTENT_GAPS) is what makes the conversion honest rather than blended.
REEL_FRAME_RATE = 30
REEL_AUDIO_BITRATE_BPS = 128_000
REEL_GOP_SECONDS = 2
# Transcoder names an unsegmented mux stream `<key>.mp4` under the output
# prefix, so a reel's object URI is known before the encode has started.
REEL_STREAM_KEY = "reel"
REEL_FILE_NAME = f"{REEL_STREAM_KEY}.mp4"

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


def build_preview_config(out_uri: str, audio: bool = True) -> Any:
    """A single-rendition 480p HLS package. ``audio=False`` for a silent source."""
    from google.cloud.video import transcoder_v1
    from google.protobuf import duration_pb2

    segment = duration_pb2.Duration(seconds=SEGMENT_SECONDS)

    streams = [
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
                        frame_rate_conversion_strategy=FRAME_RATE_CONVERSION,
                    ),
                ),
            ),
    ]
    if audio:
        streams.append(_audio_stream("audio-aac", AUDIO_BITRATE_BPS))

    return transcoder_v1.types.JobConfig(
        elementary_streams=streams,
        mux_streams=[
            transcoder_v1.types.MuxStream(
                key="hls-480p",
                container="ts",
                elementary_streams=[s.key for s in streams],
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


def proxy_output_uri(bucket: str, job_id: str) -> str:
    return f"gs://{bucket}/jobs/{job_id}/proxy/"


def _audio_stream(key: str, bitrate_bps: int) -> Any:
    from google.cloud.video import transcoder_v1

    return transcoder_v1.types.ElementaryStream(
        key=key,
        audio_stream=transcoder_v1.types.AudioStream(codec="aac", bitrate_bps=bitrate_bps),
    )


def build_proxy_config(out_uri: str, audio: bool = True) -> Any:
    """One 480p, 1 fps MP4 with the audio kept — the file the analysis reads.

    ``audio=False`` for a source with no audio track: Transcoder asked for an
    AAC stream from one fails rather than writing a silent file.
    """
    from google.cloud.video import transcoder_v1
    from google.protobuf import duration_pb2

    streams = [
        transcoder_v1.types.ElementaryStream(
            key="video-1fps",
            video_stream=transcoder_v1.types.VideoStream(
                h264=transcoder_v1.types.VideoStream.H264CodecSettings(
                    height_pixels=PROXY_HEIGHT,
                    width_pixels=PROXY_WIDTH,
                    bitrate_bps=PROXY_BITRATE_BPS,
                    frame_rate=PROXY_FRAME_RATE,
                    gop_duration=duration_pb2.Duration(seconds=PROXY_GOP_SECONDS),
                    frame_rate_conversion_strategy=FRAME_RATE_CONVERSION,
                ),
            ),
        ),
    ]
    if audio:
        streams.append(_audio_stream("audio-aac", PROXY_AUDIO_BITRATE_BPS))
    return transcoder_v1.types.JobConfig(
        elementary_streams=streams,
        mux_streams=[
            transcoder_v1.types.MuxStream(
                key=PROXY_STREAM_KEY,
                container="mp4",
                elementary_streams=[s.key for s in streams],
            ),
        ],
        output=transcoder_v1.types.Output(uri=out_uri),
    )


def create_proxy_job(source_uri: str, media_bucket: str, job_id: str,
                     audio: bool = True) -> dict[str, Any]:
    """Start the 1 fps analysis proxy encode. Returns as soon as it is accepted."""
    from google.cloud.video import transcoder_v1

    out_uri = proxy_output_uri(media_bucket, job_id)
    job = transcoder_v1.types.Job(
        input_uri=source_uri,
        output_uri=out_uri,
        config=build_proxy_config(out_uri, audio=audio),
        fill_content_gaps=FILL_CONTENT_GAPS,
        optimization=OPTIMIZATION,
        ttl_after_completion_days=7,
        labels={"sprtz_job": job_id[:63], "sprtz_kind": "proxy"},
    )
    created = client().create_job(parent=parent(), job=job)
    logger.info("transcoder proxy job %s created for %s", created.name, job_id)
    return {
        "transcoder_job": created.name,
        "output_uri": out_uri,
        "analysis_uri": f"{out_uri}{PROXY_FILE_NAME}",
    }


def create_preview_job(source_uri: str, hls_bucket: str, job_id: str,
                       audio: bool = True) -> dict[str, Any]:
    """Start the 480p HLS encode. Returns as soon as it is accepted."""
    from google.cloud.video import transcoder_v1

    out_uri = output_uri(hls_bucket, job_id)
    job = transcoder_v1.types.Job(
        input_uri=source_uri,
        output_uri=out_uri,
        config=build_preview_config(out_uri, audio=audio),
        fill_content_gaps=FILL_CONTENT_GAPS,
        optimization=OPTIMIZATION,
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


def reel_output_uri(bucket: str, reel_id: str) -> str:
    return f"gs://{bucket}/reels/{reel_id}/"


def _ms_duration(ms: int) -> Any:
    """Milliseconds as a protobuf Duration, exactly.

    Duration carries nanoseconds, so a cut point survives the trip whole. This
    is what makes the stored millisecond a real boundary rather than a number
    that gets rounded to the nearest second on the way to the encoder.
    """
    from google.protobuf import duration_pb2

    ms = max(0, int(ms))
    return duration_pb2.Duration(seconds=ms // 1000, nanos=(ms % 1000) * 1_000_000)


def build_reel_config(out_uri: str, cuts: list[dict[str, Any]],
                      sources: dict[str, str], audio: bool = True) -> Any:
    """One MP4 of the cuts, in order, drawn from however many sources they name.

    ``cuts`` are dicts with ``jobId``, ``startMs`` and ``endMs``; ``sources``
    maps a job id to its ``gs://`` source. Each distinct source becomes an
    ``Input`` and each cut an ``EditAtom`` naming it — the edit list is the
    concatenation, in list order.
    """
    from google.cloud.video import transcoder_v1
    from google.protobuf import duration_pb2

    # One Input per distinct source, in first-use order so the config reads in
    # the same order as the reel does.
    keys: dict[str, str] = {}
    inputs = []
    for cut in cuts:
        job_id = cut.get("jobId") or ""
        if job_id in keys or job_id not in sources:
            continue
        keys[job_id] = f"in{len(keys)}"
        inputs.append(transcoder_v1.types.Input(key=keys[job_id], uri=sources[job_id]))

    atoms = []
    for i, cut in enumerate(cuts):
        key = keys.get(cut.get("jobId") or "")
        if not key:
            continue
        atoms.append(transcoder_v1.types.EditAtom(
            key=f"atom{i}",
            inputs=[key],
            start_time_offset=_ms_duration(cut.get("startMs") or 0),
            end_time_offset=_ms_duration(cut.get("endMs") or 0),
        ))

    streams = [
        transcoder_v1.types.ElementaryStream(
            key="video-reel",
            video_stream=transcoder_v1.types.VideoStream(
                h264=transcoder_v1.types.VideoStream.H264CodecSettings(
                    height_pixels=REEL_HEIGHT,
                    width_pixels=REEL_WIDTH,
                    bitrate_bps=REEL_BITRATE_BPS,
                    frame_rate=REEL_FRAME_RATE,
                    gop_duration=duration_pb2.Duration(seconds=REEL_GOP_SECONDS),
                    frame_rate_conversion_strategy=FRAME_RATE_CONVERSION,
                ),
            ),
        ),
    ]
    if audio:
        streams.append(_audio_stream("audio-reel", REEL_AUDIO_BITRATE_BPS))

    return transcoder_v1.types.JobConfig(
        inputs=inputs,
        edit_list=atoms,
        elementary_streams=streams,
        mux_streams=[
            transcoder_v1.types.MuxStream(
                key=REEL_STREAM_KEY,
                container="mp4",
                elementary_streams=[s.key for s in streams],
            ),
        ],
        output=transcoder_v1.types.Output(uri=out_uri),
    )


def create_reel_job(reel_id: str, cuts: list[dict[str, Any]], sources: dict[str, str],
                    media_bucket: str, audio: bool = True) -> dict[str, Any]:
    """Start the reel encode. Returns as soon as it is accepted.

    ``input_uri`` is deliberately not set on the Job: the sources live in the
    config's ``inputs`` because there is more than one of them, and a Job
    carrying both would be ambiguous about which the edit list refers to.
    """
    from google.cloud.video import transcoder_v1

    out_uri = reel_output_uri(media_bucket, reel_id)
    job = transcoder_v1.types.Job(
        output_uri=out_uri,
        config=build_reel_config(out_uri, cuts, sources, audio=audio),
        fill_content_gaps=FILL_CONTENT_GAPS,
        optimization=OPTIMIZATION,
        ttl_after_completion_days=7,
        labels={"sprtz_reel": reel_id[:63], "sprtz_kind": "reel"},
    )
    created = client().create_job(parent=parent(), job=job)
    logger.info("transcoder reel job %s created for %s", created.name, reel_id)
    return {
        "transcoder_job": created.name,
        "output_uri": out_uri,
        "reel_uri": f"{out_uri}{REEL_FILE_NAME}",
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
