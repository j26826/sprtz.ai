"""HLS playlist reading, for the live capture.

Pure functions over playlist text. The recorder in ``live_capture`` polls a
live media playlist and needs three things from it: which variant to follow,
the media-sequence number of every segment so a gap is a gap in the numbers
rather than a guess, and the wall-clock time each segment covers so a chunk
can be named by when it happened.

No library. What the live capture needs is a dozen tags, m3u8 parsers differ
in how they treat the ones that matter here (``EXT-X-DISCONTINUITY`` and
``EXT-X-PROGRAM-DATE-TIME`` between segments), and a parser that is forty
lines long can be read in full when a stream misbehaves at 2am.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def attributes(line: str) -> dict[str, str]:
    """The ``KEY=VALUE,KEY="quoted"`` attribute list after a tag's colon."""
    _, _, rest = line.partition(":")
    return {k: v.strip('"') for k, v in _ATTR.findall(rest)}


@dataclass(frozen=True)
class Variant:
    url: str
    bandwidth: int
    resolution: str = ""
    codecs: str = ""
    # The EXT-X-MEDIA group this variant's audio lives in, when the audio is
    # a separate rendition rather than muxed into the variant's own segments.
    audio_group: str = ""


@dataclass(frozen=True)
class AudioRendition:
    group: str
    url: str
    name: str = ""
    default: bool = False


def parse_master(text: str, base_url: str) -> list[Variant]:
    """Every ``EXT-X-STREAM-INF`` entry of a multivariant playlist, resolved."""
    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = attributes(line)
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            variants.append(Variant(
                url=urljoin(base_url, line),
                bandwidth=int(pending.get("BANDWIDTH") or pending.get("AVERAGE-BANDWIDTH") or 0),
                resolution=pending.get("RESOLUTION", ""),
                codecs=pending.get("CODECS", ""),
                audio_group=pending.get("AUDIO", ""),
            ))
            pending = None
    return variants


def parse_audio_renditions(text: str, base_url: str) -> list[AudioRendition]:
    """Every ``EXT-X-MEDIA:TYPE=AUDIO`` entry that names its own playlist.

    A rendition without a URI is audio muxed into the variant's segments and
    needs nothing done; one with a URI is a second stream to fetch.
    """
    found: list[AudioRendition] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#EXT-X-MEDIA"):
            continue
        attrs = attributes(line)
        if attrs.get("TYPE", "").upper() != "AUDIO" or not attrs.get("URI"):
            continue
        found.append(AudioRendition(
            group=attrs.get("GROUP-ID", ""),
            url=urljoin(base_url, attrs["URI"]),
            name=attrs.get("NAME", ""),
            default=attrs.get("DEFAULT", "").upper() == "YES",
        ))
    return found


def separate_audio_url(text: str, base_url: str, variant: Variant | None = None) -> str:
    """The playlist of the audio a variant relies on, or "" when it carries its own.

    JW Player and Unified Streaming publish video-only variants with the audio
    in an EXT-X-MEDIA group — a download of the variant alone is silent. The
    default rendition of the variant's group wins; failing that, the first.
    """
    if variant is None:
        variant = pick_variant(parse_master(text, base_url))
    if variant is None or not variant.audio_group:
        return ""
    group = [r for r in parse_audio_renditions(text, base_url) if r.group == variant.audio_group]
    if not group:
        return ""
    chosen = next((r for r in group if r.default), group[0])
    return chosen.url


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def pick_variant(variants: list[Variant]) -> Variant | None:
    """The highest-bandwidth rendition, the same rule the downloader applies.

    The analysis samples one frame a second at 480p, so a lower rung would do
    for it — but the same capture is also what a download is cut from, and a
    cut taken from the lowest rung is one nobody publishes.
    """
    return max(variants, key=lambda v: v.bandwidth) if variants else None


@dataclass
class Segment:
    seq: int
    url: str
    duration: float
    # Wall-clock start, from EXT-X-PROGRAM-DATE-TIME or carried forward from
    # the last one seen by adding durations. None when the playlist never says.
    pdt: datetime | None = None
    discontinuity: bool = False


@dataclass
class MediaPlaylist:
    target_duration: float = 6.0
    media_sequence: int = 0
    segments: list[Segment] = field(default_factory=list)
    init_url: str = ""
    endlist: bool = False

    @property
    def last_seq(self) -> int | None:
        return self.segments[-1].seq if self.segments else None


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    """A media playlist with every segment numbered and timed.

    Sequence numbers come from ``EXT-X-MEDIA-SEQUENCE`` plus position, which is
    what makes two polls of a sliding window comparable: a segment is the same
    segment when its number is, whatever its URL looks like.
    """
    playlist = MediaPlaylist()
    seq = 0
    duration: float | None = None
    pdt: datetime | None = None
    explicit_pdt = False
    discontinuity = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION"):
            playlist.target_duration = float(line.partition(":")[2] or 6)
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            seq = int(line.partition(":")[2] or 0)
            playlist.media_sequence = seq
        elif line.startswith("#EXT-X-MAP"):
            uri = attributes(line).get("URI", "")
            if uri:
                playlist.init_url = urljoin(base_url, uri)
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
            pdt = parse_pdt(line.partition(":")[2])
            explicit_pdt = pdt is not None
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            if not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
                discontinuity = True
        elif line.startswith("#EXTINF"):
            value = line.partition(":")[2].split(",", 1)[0].strip()
            try:
                duration = float(value)
            except ValueError:
                duration = playlist.target_duration
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.endlist = True
        elif line.startswith("#"):
            continue
        else:
            if duration is None:
                # A URI with no EXTINF before it is not a media segment.
                continue
            playlist.segments.append(Segment(
                seq=seq, url=urljoin(base_url, line), duration=duration,
                pdt=pdt, discontinuity=discontinuity,
            ))
            seq += 1
            # Carry the clock forward so a playlist that stamps only its
            # first segment still times every one of them.
            if pdt is not None:
                pdt = pdt + timedelta(seconds=duration)
            explicit_pdt = False
            duration = None
            discontinuity = False
    del explicit_pdt
    return playlist


def parse_pdt(value: str) -> datetime | None:
    """ISO 8601 as playlists write it, including a trailing Z."""
    value = value.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def container_of(playlist: MediaPlaylist) -> str:
    """``mp4`` for a CMAF stream (it has an init map), ``ts`` otherwise.

    The same rule the downloader uses. It decides both the chunk file's
    extension and whether the init segment has to be written ahead of the
    media parts when they are joined.
    """
    return "mp4" if playlist.init_url else "ts"
