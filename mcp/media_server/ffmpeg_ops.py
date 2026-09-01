"""ffmpeg operations. Pure subprocess work, no MCP or GCS concerns."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class FfmpegError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 3600) -> str:
    logger.info("running: %s", shlex.join(cmd[:12]) + (" ..." if len(cmd) > 12 else ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise FfmpegError("\n".join(tail) or f"exit {proc.returncode}")
    return proc.stdout


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
HLS_SEGMENT_SECONDS = 2


def probe(path: str | Path) -> dict:
    """ffprobe a local file into a flat dict."""
    out = _run(
        [
            "ffprobe", "-v", "error",
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


def transcode_hls(source: Path, out_dir: Path, source_height: int = 0) -> dict:
    """Produce a multi-rendition HLS package with a master playlist.

    Renditions above the source height are skipped — upscaling costs money and
    delivers nothing. `-master_pl_name` writes the master alongside the variant
    playlists so the whole directory can be copied to GCS as-is.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    ladder = [r for r in HLS_LADDER if not source_height or r.height <= source_height]
    if not ladder:
        # Source is smaller than the lowest rung; encode one rendition at source height.
        ladder = [HLS_LADDER[0]]

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-y", "-i", str(source)]

    # Split the decoded video once and scale each branch, so the source is
    # decoded a single time regardless of ladder depth.
    splits = "".join(f"[v{i}]" for i in range(len(ladder)))
    filters = [f"[0:v]split={len(ladder)}{splits}"]
    for i, r in enumerate(ladder):
        filters.append(f"[v{i}]scale=-2:{r.height}[v{i}out]")
    cmd += ["-filter_complex", ";".join(filters)]

    var_parts: list[str] = []
    for i, r in enumerate(ladder):
        cmd += [
            "-map", f"[v{i}out]",
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", r.video_bitrate,
            f"-maxrate:v:{i}", r.maxrate,
            f"-bufsize:v:{i}", r.bufsize,
            "-preset", "veryfast",
            "-profile:v", "main",
            "-sc_threshold", "0",
            # Keyframe cadence must divide the segment length or seeks land late.
            "-g", str(HLS_SEGMENT_SECONDS * 50),
            "-keyint_min", str(HLS_SEGMENT_SECONDS * 50),
        ]
        cmd += ["-map", "a:0?", f"-c:a:{i}", "aac", f"-b:a:{i}", r.audio_bitrate, "-ac", "2"]
        var_parts.append(f"v:{i},a:{i},name:{r.name}")

    cmd += [
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "%v_%05d.ts"),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(var_parts),
        str(out_dir / "%v.m3u8"),
    ]

    _run(cmd, timeout=6 * 3600)

    return {
        "master_playlist": "master.m3u8",
        "renditions": [r.name for r in ladder],
        "segment_seconds": HLS_SEGMENT_SECONDS,
        "files": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
    }


def cut(source: Path, dest: Path, start_sec: float, end_sec: float, reencode: bool = True) -> None:
    """Extract [start, end). Re-encodes by default.

    Stream copy is much faster but can only cut on a keyframe, which drifts the
    in-point by up to the GOP length — visible and wrong when the clip is built
    around a specific frame.
    """
    duration = max(0.0, end_sec - start_sec)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-y", "-ss", f"{start_sec:.3f}", "-i", str(source),
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
        cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(source), "-filter_complex", vf]
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(source), "-vf", vf]

    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)]
    _run(cmd)


def thumbnail(source: Path, dest: Path, at_sec: float, width: int = 640) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-hide_banner", "-y", "-ss", f"{at_sec:.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dest),
    ], timeout=600)


def burn_text(source: Path, dest: Path, text: str, duration_sec: float = 1.5) -> None:
    """Burn a hook line over the opening of a clip."""
    safe = text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    vf = (
        f"drawtext=text='{safe}':fontsize=h/14:fontcolor=white:borderw=4:bordercolor=black@0.8:"
        f"x=(w-text_w)/2:y=h*0.12:enable='lt(t,{duration_sec})'"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-hide_banner", "-y", "-i", str(source), "-vf", vf,
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-c:a", "copy", "-movflags", "+faststart", str(dest)])
