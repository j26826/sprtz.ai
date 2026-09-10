# ais-media-core

Shared library for the stream-copy HLS tools — the common foundation behind
[`vod-hls2mp4`](../vod-hls2mp4) (VOD) and [`live-hls2mp4`](../live-hls2mp4)
(live events). Consumed via a path dependency (`ais-media-core = { path =
"../ais-media-core" }`); there is no Cargo workspace, so each app still builds
independently.

## Modules

| Module | Responsibility |
| :--- | :--- |
| `crypto` | fMP4 `tenc` KID parsing, DASH-IF CPIX key exchange, in-place `cbcs` (1:9) pattern decryption, KID→UUID formatting. |
| `mp4mux` | Streaming CMAF → **progressive MP4** remux (stream-copy): `ftyp`, one `mdat` per segment (video + muxed audio-rendition samples), and a closing `moov` with full sample tables — zero-based, gapless-splicing, frame-accurately seekable output written with no seek-back. |
| `fmp4` | Fragmented-MP4 box surgery: `tfdt`/`sidx` timeline zero-basing, `track_ID` remapping, two-traf fragment merge, `mfhd` renumbering, `mfra`/`tfra` seek-trailer assembly. Available for fragmented output paths. |
| `scte35` | The SCTE-35 ad-break state machine (`AdState`): DATERANGE `SCTE35-OUT/IN` with auto-return windows, inline `CUE-OUT`/`CONT`/`IN`; a bare `SCTE35-CMD` only opens a break when it carries a positive duration. |
| `storage` | `object_store`-backed output over `s3://` / `gs://` / `file://`: `single_object_writer` (one file, used by VOD), `OutputTarget` (a directory that mints per-segment writers and renames temps, used by live), signed GET URLs (`ffmpeg_input_urls`) and object deletion for the TS restamp merge. `parse_store` is the shared URI→backend resolver. |
| `net` | `fetch_bytes` — a retrying HTTP GET (`error_for_status` + short backoff) shared by both tools' playlist/segment downloads. |
