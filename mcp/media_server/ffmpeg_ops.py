"""ffmpeg operations. Pure subprocess work, no MCP or GCS concerns.

Packaging a match for playback used to live here and now runs on Transcoder API
instead — that job wrote gigabytes of segments through a filesystem that is
really RAM, and it killed the container. What is left is the work ffmpeg is
still the right tool for here: probing an upload to decide whether it is a video
at all, one poster frame, and the short per-clip operations an editor drives,
all of which read a few megabytes over a range request and finish in seconds.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Hardening for untrusted input -------------------------------------------
#
# Everything below runs ffmpeg over a file a stranger uploaded, so the input is
# treated as hostile.
#
# The protocol allowlist is the important one. ffmpeg can be steered by the
# *contents* of a media file into opening other URLs — playlists, concat
# scripts and some container metadata all reference external resources. On GCP
# that is an SSRF straight at the metadata server (http://169.254.169.254),
# which would hand out this service account's access token. Restricting
# protocols to https/tls/crypto plus local file access closes that: plain http
# is absent, so the metadata server is unreachable, and file: cannot be reached
# from a remote input because ffmpeg refuses to cross protocol boundaries
# without -safe 0, which is never passed.
# https is layered on tcp, so tcp must be listed or every GCS read fails with
# "Protocol 'tcp' not on whitelist". Plain http is deliberately absent, and that
# is what closes the hole: GCP's metadata server is http://169.254.169.254, so
# without it a crafted file cannot steer ffmpeg into fetching this service
# account's access token. Verified against the built image — an http:// input is
# refused with "Protocol 'http' not on whitelist" while https:// still works.
_ALLOWED_PROTOCOLS = "file,https,tls,crypto,tcp"

# ffprobe has no -nostdin; passing it there makes ffprobe consume the next
# argument as its value and fail with "Option not found".
_PROBE_HARDENING: list[str] = ["-protocol_whitelist", _ALLOWED_PROTOCOLS]
_FFMPEG_HARDENING: list[str] = ["-nostdin", "-protocol_whitelist", _ALLOWED_PROTOCOLS]

# Bounds a genuine match recording stays inside. Anything outside is either a
# mistake or an attempt to exhaust the worker.
MAX_DURATION_SEC = 6 * 60 * 60
MAX_DIMENSION = 7680
MAX_PIXELS = 7680 * 4320

# Codecs the pipeline can actually copy-remux to HLS. An exotic codec is not
# necessarily malicious, but it is not something this service can serve.
ALLOWED_VIDEO_CODECS = frozenset({"h264", "hevc", "vp9", "av1", "mpeg4", "mpeg2video"})
ALLOWED_AUDIO_CODECS = frozenset({"aac", "mp3", "opus", "vorbis", "ac3", "eac3", "flac", "pcm_s16le"})


class MediaRejected(ValueError):
    """The upload is not media this service will process."""



class FfmpegError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 3600) -> str:
    logger.info("running: %s", shlex.join(cmd[:12]) + (" ..." if len(cmd) > 12 else ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise FfmpegError("\n".join(tail) or f"exit {proc.returncode}")
    return proc.stdout


# ffmpeg reads GCS objects straight over HTTPS with range seeks, so multi-GB
# sources never touch local disk — which on Cloud Run is memory.
def http_input_args(url: str, bearer_token: str | None) -> list[str]:
    args: list[str] = []
    if bearer_token:
        # ffmpeg wants the raw header line, CRLF-terminated.
        args += ["-headers", f"Authorization: Bearer {bearer_token}\r\n"]
    # Survive transient resets on a long read.
    args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    return args


@dataclass(frozen=True)
class Rendition:
    """One rung of the HLS ladder."""

    name: str
    height: int
    video_bitrate: str
    maxrate: str
    bufsize: str
    audio_bitrate: str

    @property
    def bandwidth(self) -> int:
        return int(self.video_bitrate.rstrip("k")) * 1000 + int(self.audio_bitrate.rstrip("k")) * 1000


# Deliberately short. The editor scrubs a lot and publishes little, so the ladder
# is tuned for fast start and cheap seeking rather than for maximum quality.
HLS_LADDER: tuple[Rendition, ...] = (
    Rendition("360p", 360, "800k", "856k", "1200k", "96k"),
    Rendition("540p", 540, "1400k", "1498k", "2100k", "128k"),
    Rendition("720p", 720, "2800k", "2996k", "4200k", "128k"),
)

# Two seconds keeps seek latency low when the UI jumps to a moment's start time.


def probe(path: str | Path, bearer_token: str | None = None) -> dict:
    """ffprobe a local file or an HTTPS URL into a flat dict."""
    header_args: list[str] = []
    if bearer_token and str(path).startswith("http"):
        header_args = ["-headers", f"Authorization: Bearer {bearer_token}\r\n"]
    out = _run(
        [
            "ffprobe", "-v", "error", *_PROBE_HARDENING,
            *header_args,
            "-show_entries",
            "format=duration,size,bit_rate,format_name",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,channels,sample_rate",
            "-of", "json",
            str(path),
        ],
        timeout=600,
    )
    data = json.loads(out)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    def _fps(value: str | None) -> float:
        if not value or "/" not in value:
            return 0.0
        num, den = value.split("/", 1)
        try:
            return round(float(num) / float(den), 3) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    return {
        "duration_sec": float(fmt.get("duration") or 0.0),
        "bytes": int(fmt.get("size") or 0),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "container": fmt.get("format_name", ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _fps(video.get("avg_frame_rate")),
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "audio_channels": int(audio.get("channels") or 0),
        "audio_sample_rate": int(audio.get("sample_rate") or 0),
        "has_audio": bool(audio),
    }


def cut(source: str | Path, dest: Path, start_sec: float, end_sec: float,
        reencode: bool = True, bearer_token: str | None = None) -> None:
    """Extract [start, end) from a local file or an HTTPS URL. Re-encodes by default.

    Stream copy is much faster but can only cut on a keyframe, which drifts the
    in-point by up to the GOP length — visible and wrong when the clip is built
    around a specific frame. With a URL source, -ss becomes a range seek, so a
    30-second clip out of a 3 GB match reads megabytes, not gigabytes.
    """
    duration = max(0.0, end_sec - start_sec)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = str(source)
    cmd = ["ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING,
           *(http_input_args(src, bearer_token) if src.startswith("http") else []),
           "-ss", f"{start_sec:.3f}", "-i", src,
           "-t", f"{duration:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(dest)]
    _run(cmd)


def reframe(source: Path, dest: Path, aspect: str = "9:16", blur_pad: bool = True) -> None:
    """Reframe to a vertical aspect.

    Centre-crops to the target aspect over a blurred, filled background so a
    wide court shot still reads on a phone instead of becoming letterboxed.
    """
    w, h = (1080, 1920) if aspect == "9:16" else (1080, 1080)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if blur_pad:
        vf = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=luma_radius=40:luma_power=2[bg];"
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        cmd = ["ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING, "-i", str(source), "-filter_complex", vf]
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        cmd = ["ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING, "-i", str(source), "-vf", vf]

    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)]
    _run(cmd)


def thumbnail(source: str | Path, dest: Path, at_sec: float, width: int = 640,
              bearer_token: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = str(source)
    _run([
        "ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING,
        *(http_input_args(src, bearer_token) if src.startswith("http") else []),
        "-ss", f"{at_sec:.3f}", "-i", src,
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dest),
    ], timeout=600)


def keyframe_thumbnail(source: str | Path, dest: Path, at_sec: float, width: int = 320,
                       bearer_token: str | None = None) -> bool:
    """Write the first I-frame at or after ``at_sec``. Returns whether there was one.

    ``-skip_frame nokey`` makes the decoder throw away everything that is not a
    keyframe, so the first frame to survive the seek is the first I-frame at or
    after the timestamp. Two reasons that is the frame worth keeping: it is
    whole — an inter frame decoded out of a range read carries whatever the
    encoder chose not to resend — and it costs one decode rather than a GOP of
    them, which matters when this runs once per moment across a match.

    It reads at or *after* the peak deliberately. Input ``-ss`` seeks to the
    keyframe before the timestamp, which can be a GOP earlier — several seconds
    of handball, and a different play. Landing late by a fraction of a GOP shows
    the moment; landing early by one shows the build-up to it.

    A peak inside the file's last GOP has no keyframe after it, and there the
    answer is an exact frame rather than no picture at all, so this returns
    False instead of raising and the caller falls back.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = str(source)
    try:
        _run([
            "ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING,
            *(http_input_args(src, bearer_token) if src.startswith("http") else []),
            "-ss", f"{at_sec:.3f}", "-skip_frame", "nokey", "-i", src,
            "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest),
        ], timeout=300)
    except (FfmpegError, subprocess.TimeoutExpired):
        logger.warning("no keyframe at or after %.3fs in %s", at_sec, src)
        return False
    return dest.exists() and dest.stat().st_size > 0


def still_frame(source: str | Path, dest: Path, at_sec: float, width: int = 320,
                bearer_token: str | None = None) -> None:
    """Write the frame at exactly ``at_sec``, decoding from the keyframe before it.

    The fallback for :func:`keyframe_thumbnail`, and separate from
    :func:`thumbnail` because that one carries ``-q:v`` for JPEG and this writes
    PNG, where the flag means nothing.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = str(source)
    _run([
        "ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING,
        *(http_input_args(src, bearer_token) if src.startswith("http") else []),
        "-ss", f"{at_sec:.3f}", "-i", src,
        "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest),
    ], timeout=300)


def burn_text(source: Path, dest: Path, text: str, duration_sec: float = 1.5) -> None:
    """Burn a hook line over the opening of a clip."""
    safe = text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    vf = (
        f"drawtext=text='{safe}':fontsize=h/14:fontcolor=white:borderw=4:bordercolor=black@0.8:"
        f"x=(w-text_w)/2:y=h*0.12:enable='lt(t,{duration_sec})'"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-hide_banner", "-y", *_FFMPEG_HARDENING, "-i", str(source), "-vf", vf,
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-c:a", "copy", "-movflags", "+faststart", str(dest)])


def validate(info: dict, *, declared_content_type: str = "") -> list[str]:
    """Check probe output against what this service will process.

    Returns the reasons to reject, empty if the file is acceptable. Separated
    from :func:`probe` so it can be unit-tested against fixture dictionaries
    without invoking ffprobe, and so callers can log every reason at once
    rather than surfacing them one failed upload at a time.

    This runs on probe output rather than on the client's claims, because a
    filename and a Content-Type are both attacker-controlled: the only evidence
    that an upload is a video is that a decoder could read it as one.
    """
    reasons: list[str] = []

    if not info.get("video_codec"):
        reasons.append("no video stream found — the file is not a video")

    duration = float(info.get("duration_sec") or 0.0)
    if duration <= 0:
        reasons.append("no readable duration; the file is truncated or not media")
    elif duration > MAX_DURATION_SEC:
        reasons.append(
            f"duration {duration / 3600:.1f}h exceeds the {MAX_DURATION_SEC / 3600:.0f}h limit"
        )

    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        reasons.append("no readable video dimensions")
    else:
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            reasons.append(f"{width}x{height} exceeds the {MAX_DIMENSION}px limit on a side")
        # Guards a decompression bomb: a small file can declare an enormous
        # frame and exhaust memory on the first decode.
        if width * height > MAX_PIXELS:
            reasons.append(f"{width}x{height} exceeds the total pixel limit")

    codec = (info.get("video_codec") or "").lower()
    if codec and codec not in ALLOWED_VIDEO_CODECS:
        reasons.append(f"video codec {codec!r} is not supported")

    audio = (info.get("audio_codec") or "").lower()
    if audio and audio not in ALLOWED_AUDIO_CODECS:
        reasons.append(f"audio codec {audio!r} is not supported")

    # A mismatch is not proof of an attack — browsers guess Content-Type from
    # the extension — but a file claiming to be video that decodes as something
    # else is worth refusing rather than feeding to the rest of the pipeline.
    if declared_content_type and not declared_content_type.startswith("video/"):
        reasons.append(f"declared content type {declared_content_type!r} is not a video type")

    return reasons
