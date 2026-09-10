# jobs/hls2mp4

The HLS downloader the media server runs as a **Cloud Run Job** — for an upload
that is an HLS URL rather than a file. Vendored from
`ais-media-services/media-jobs/{vod-hls2mp4,ais-media-core}` unchanged apart
from `vod-hls2mp4/Dockerfile`, whose runtime stage takes ffmpeg from Debian
rather than from a shared base image this repo does not build. Upstream's
README in `vod-hls2mp4/` is the reference for every flag.

How it is used here:

- `EXT_SOURCE_URI` — the HLS URL the editor gave.
- `AIS_SOURCE_URI` — `gs://<uploads>/hls/<job>/source.mp4`; the tool writes the
  source next to it under `source/` and switches the extension to `.ts` for an
  MPEG-TS stream, so the media server lists that prefix afterwards rather than
  assuming a name.
- `PROXY_1FPS=true` with `AIS_PREVIEW_URI=gs://<media>/jobs/<job>/proxy` — the
  constant-1 fps 480p H.264 proxy. Supported, but **not requested here**: it is
  one core decoding the whole recording after the download, so the media
  service makes the proxy on Transcoder instead (`make_analysis_proxy`).
  Audio is kept. It is a fraction of the bytes, which is what Gemini's
  whole-object fetch limit cares about.

Build context is this directory, not the crate: see the Dockerfile header.
