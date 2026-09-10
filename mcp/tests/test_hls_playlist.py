"""Reading HLS playlists, the live recorder's one input."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_server import hls  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480,CODECS="avc1.4d401f,mp4a.40.2"
480/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080
https://cdn.example.com/live/1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720/index.m3u8
"""

MEDIA_TS = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:1204
#EXT-X-PROGRAM-DATE-TIME:2026-09-10T12:00:00.000Z
#EXTINF:6.000,
seg1204.ts
#EXTINF:6.000,
seg1205.ts
#EXT-X-DISCONTINUITY
#EXTINF:4.500,
seg1206.ts
"""

MEDIA_CMAF = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.000,
s7.m4s
#EXTINF:4.000,
s8.m4s
#EXT-X-ENDLIST
"""


class TestMaster:
    def test_the_highest_bandwidth_rendition_is_chosen(self):
        variants = hls.parse_master(MASTER, "https://cdn.example.com/live/master.m3u8")
        assert len(variants) == 3
        best = hls.pick_variant(variants)
        assert best.bandwidth == 4500000
        assert best.resolution == "1920x1080"

    def test_relative_and_absolute_urls_both_resolve(self):
        variants = hls.parse_master(MASTER, "https://cdn.example.com/live/master.m3u8")
        assert variants[0].url == "https://cdn.example.com/live/480/index.m3u8"
        assert variants[1].url == "https://cdn.example.com/live/1080/index.m3u8"

    def test_a_media_playlist_is_not_a_master(self):
        assert hls.is_master(MASTER)
        assert not hls.is_master(MEDIA_TS)


class TestMedia:
    def test_segments_are_numbered_from_the_media_sequence(self):
        pl = hls.parse_media(MEDIA_TS, "https://cdn.example.com/live/1080/index.m3u8")
        assert [s.seq for s in pl.segments] == [1204, 1205, 1206]
        assert pl.last_seq == 1206
        assert pl.target_duration == 6.0

    def test_the_clock_carries_forward_from_one_stamp(self):
        pl = hls.parse_media(MEDIA_TS, "https://cdn.example.com/live/1080/index.m3u8")
        t0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        assert pl.segments[0].pdt == t0
        assert (pl.segments[2].pdt - t0).total_seconds() == 12.0

    def test_a_discontinuity_marks_only_the_segment_after_it(self):
        pl = hls.parse_media(MEDIA_TS, "https://cdn.example.com/live/1080/index.m3u8")
        assert [s.discontinuity for s in pl.segments] == [False, False, True]

    def test_cmaf_has_an_init_map_and_is_mp4(self):
        pl = hls.parse_media(MEDIA_CMAF, "https://cdn.example.com/live/v/index.m3u8")
        assert pl.init_url == "https://cdn.example.com/live/v/init.mp4"
        assert hls.container_of(pl) == "mp4"
        assert pl.endlist

    def test_ts_has_no_init_map_and_is_ts(self):
        pl = hls.parse_media(MEDIA_TS, "https://cdn.example.com/live/1080/index.m3u8")
        assert hls.container_of(pl) == "ts"
        assert not pl.endlist

    def test_a_missing_clock_is_none_not_now(self):
        text = MEDIA_TS.replace("#EXT-X-PROGRAM-DATE-TIME:2026-09-10T12:00:00.000Z\n", "")
        pl = hls.parse_media(text, "https://cdn.example.com/x.m3u8")
        assert all(s.pdt is None for s in pl.segments)


class TestParsePdt:
    def test_z_and_offset_forms_both_read_as_utc(self):
        z = hls.parse_pdt("2026-09-10T12:00:00Z")
        off = hls.parse_pdt("2026-09-10T14:00:00+02:00")
        assert z == off
        assert z.tzinfo is not None

    def test_garbage_is_none(self):
        assert hls.parse_pdt("yesterday") is None
        assert hls.parse_pdt("") is None


DEMUXED_MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-aacl-100",NAME="English",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="manifest-audio_0=100848.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-aacl-100",NAME="Commentary",DEFAULT=NO,URI="manifest-audio_1=100848.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480,CODECS="avc1.4d401f",AUDIO="audio-aacl-100"
manifest-video_0=1100000.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4060000,RESOLUTION=1920x1080,CODECS="avc1.640028",AUDIO="audio-aacl-100"
manifest-video_0=3945178.m3u8
"""


class TestASeparateAudioRendition:
    """JW Player and Unified Streaming keep the audio out of the variant.

    A download of the variant alone is a silent recording; the group the
    variant names is where the sound is.
    """

    def test_the_variant_names_its_audio_group(self):
        variants = hls.parse_master(DEMUXED_MASTER, "https://cdn.example.com/x/manifest.m3u8")
        assert hls.pick_variant(variants).audio_group == "audio-aacl-100"
        assert hls.pick_variant(hls.parse_master(MASTER, "https://cdn.example.com/")).audio_group == ""

    def test_the_renditions_are_read_and_resolved(self):
        found = hls.parse_audio_renditions(DEMUXED_MASTER, "https://cdn.example.com/x/manifest.m3u8")
        assert [r.name for r in found] == ["English", "Commentary"]
        assert found[0].default and not found[1].default
        assert found[0].url == "https://cdn.example.com/x/manifest-audio_0=100848.m3u8"

    def test_the_default_rendition_of_the_variants_group_is_chosen(self):
        url = hls.separate_audio_url(DEMUXED_MASTER, "https://cdn.example.com/x/manifest.m3u8")
        assert url.endswith("manifest-audio_0=100848.m3u8")

    def test_a_muxed_master_needs_nothing(self):
        assert hls.separate_audio_url(MASTER, "https://cdn.example.com/") == ""

    def test_a_rendition_without_a_uri_is_muxed_audio(self):
        text = DEMUXED_MASTER.replace(',URI="manifest-audio_0=100848.m3u8"', "").replace(
            ',URI="manifest-audio_1=100848.m3u8"', "")
        assert hls.parse_audio_renditions(text, "https://cdn.example.com/") == []
        assert hls.separate_audio_url(text, "https://cdn.example.com/") == ""
