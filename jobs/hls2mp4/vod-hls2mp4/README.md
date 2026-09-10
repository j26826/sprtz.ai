# HLS CMAF / CENC-CBCS Downloader & Decryptor

An ultra-lightweight, stateless streaming utility written in Rust. This application ingests HLS manifests — **CMAF / fragmented MP4** or **MPEG-TS** — dynamically fetches decryption keys using the **CPIX KMS** protocol, performs in-place decryption on AES-CBC-128 1:9 pattern encrypted payloads, and streams the result directly to **AWS S3**, **Google Cloud Storage**, or local storage **without transcoding**. fMP4 sources become a progressive `.mp4`; MPEG-TS sources become a concatenated `.ts`.

---

## 📋 Table of Contents

- [Supported Formats & Specifications](#-supported-formats--specifications)
- [System Architecture](#-system-architecture)
- [Performance & Resource Benchmarks](#-performance--resource-benchmarks)
- [Storage URI Schemes](#-storage-uri-schemes)
- [Local Setup & Execution](#-local-setup--execution)
- [Docker Execution](#-docker-execution)
- [Serverless Deployment Guides](#-serverless-deployment-guides)
  - [AWS Lambda](#aws-lambda)
  - [Google Cloud Run Jobs](#google-cloud-run-jobs)
- [Environment Configuration Reference](#-environment-configuration-reference)
  - [Where each artifact lands](#where-each-artifact-lands)
  - [Zero-based timelines](#-zero-based-timelines)
  - [HLS preview accuracy](#-hls-preview-accuracy)

---

## 🎬 Supported Formats & Specifications

| Feature | Supported Specification | Notes |
| :--- | :--- | :--- |
| **Container Format** | CMAF / Fragmented MP4 (`fMP4`) → **progressive** `.mp4`, or MPEG-TS → `.ts` | fMP4 is remuxed (stream-copy) into a progressive MP4 with full sample tables (`stts`/`ctts`/`stsc`/`stsz`/`co64`/`stss`), so the file starts at t=0 and seeks frame-accurately in every player. MPEG-TS: raw segment concatenation; TS output extension is auto-adjusted. Auto-detected via `#EXT-X-MAP`. No transcoding. |
| **Audio Muxing** | Separate CMAF audio rendition → second track | When the chosen variant references an `#EXT-X-MEDIA TYPE=AUDIO` group, the audio rendition is fetched in lockstep (1:1 by segment index) and muxed into the `.mp4`. Clear streams only. |
| **Ad Removal** | `--remove-ads` (SCTE-35) | Drops DATERANGE / CUE-OUT/IN-marked ad segments; the progressive splice is gapless (sample tables carry durations, not absolute timestamps). |
| **Ad Splitting** | `--split-on-ad` (SCTE-35) | One output file per content span instead of one joined file. Every span's timeline starts at 0 and is continuous, for CMAF *and* MPEG-TS. |
| **HLS Preview** | `--generate-preview` + `--ais-preview-uri` | Publishes a playable HLS rendition of each output alongside it, segmented on the **source's own boundaries** (frame accurate, stream-copy). MPEG-TS sources only for exact segmentation. |
| **Stream Protocols** | HLS (`.m3u8`) | Supports Master Multi-variant Playlists and Direct Media Playlists. |
| **Encryption Schemes** | `cbcs` (AES-CBC 128-bit) | Standard Apple/CMAF 1:9 pattern decryption (1 block crypt, 9 blocks skip). |
| **Clear Streams** | Unencrypted HLS | Auto-detects clear streams via `tenc` box flags and bypasses KMS/crypto completely. |
| **Key Retrieval** | CPIX XML Service | Connects over HTTPS to exchange `KID` for `CEK` via standard DASH-IF CPIX XML payloads. |
| **Variant Selection** | Automatic $O(n)$ Highest Bitrate | Auto-selects the maximum bandwidth track from multi-bitrate master manifests. |
| **Video/Audio Codecs** | Passed-through (AVC/H.264, HEVC/H.265, AAC, Opus) | Zero video re-encoding/transcoding; preserves original bitstream untouched. |

---

## 🏗️ System Architecture

                   ┌──────────────────────────────────────┐
                   │       HLS Manifest (.m3u8)          │
                   └──────────────────┬───────────────────┘
                                      │ Auto-detects Master/Variant
                                      ▼ & isolates highest bitrate track
                   ┌──────────────────────────────────────┐
                   │     fMP4 Init Segment Map (moov)     │
                   └──────────────────┬───────────────────┘
                                      │ Scans 'tenc' box for KID
                 ┌────────────────────┴────────────────────┐
                 ▼                                         ▼
       [Encrypted Stream]                          [Clear Stream]
                 │                                         │
    Extract KID & query CPIX KMS                            │
                 │                                         │
    Receive 128-bit CEK Key                                │
                 │                                         │
                 └────────────────────┬────────────────────┘
                                      │
                                      ▼
                  ┌───────────────────────────────────────┐
                  │ Download Fragment (moof + mdat)       │
                  └───────────────────┬───────────────────┘
                                      │
                                      ▼
                  ┌───────────────────────────────────────┐
                  │ In-Place Pattern Decryption           │
                  │ (AES-CBC-128-NI Hardware Acceleration)│
                  └───────────────────┬───────────────────┘
                                      │
                                      ▼
                  ┌───────────────────────────────────────┐
                  │ Async Object Store Stream             │
                  │ (s3://, gs://, file://)               │
                  └───────────────────────────────────────┘
---

## ⚡ Performance & Resource Benchmarks

| Metric | Measurement | Technical Context |
| :--- | :--- | :--- |
| **Cold Start Latency** | **< 10ms** | Binary compiles natively without VM or garbage collection (GC) overhead. |
| **Peak RAM Usage** | **~10MB - 15MB** | Zero-copy in-place array mutation prevents heap duplication during decryption. |
| **CPU Utilization** | **< 2%** (Non-I/O) | Leverages hardware-level **AES-NI** CPU instructions via the RustCrypto crate ecosystem. |
| **Binary File Size** | **~15MB** | Compiled with `rustls` (eliminates C-based OpenSSL dynamic linking). |

---

## 📦 Storage URI Schemes

The application uses the Apache Arrow `object_store` engine, allowing uniform streaming to any target URL:

- **Amazon S3:** `s3://bucket-name/path/to/output.mp4`
- **Google Cloud Storage:** `gs://bucket-name/path/to/output.mp4`
- **Local File System:** `file:///tmp/output.mp4`

Outputs write via asynchronous **Multipart Uploads** directly over the network, removing disk space constraints on serverless `/tmp` mounts.

---

## 🛠️ Local Setup & Execution

### Prerequisites
- **Rust Toolchain:** v1.80+ installed via `rustup`
- **Docker:** Optional (for containerized deployments)

### 1. Build from Source
```bash
git clone https://github.com/your-org/hls-cmaf-downloader.git
cd hls-cmaf-downloader
cargo build --release
```

### 2. Run

All configuration is supplied via command-line flags or the matching environment
variables (flags take precedence). Run `--help` to see the full list.

```bash
# Using CLI flags
./target/release/vod-hls2mp4 \
  --ext-source-uri https://example.com/media/master.m3u8 \
  --ais-source-uri s3://my-bucket/decrypted_output.mp4 \
  --cpix-endpoint https://kms.example.com/v1/cpix/getKey

# Or via environment variables (ideal for serverless)
export EXT_SOURCE_URI=https://example.com/media/master.m3u8
export AIS_SOURCE_URI=s3://my-bucket/decrypted_output.mp4
export CPIX_ENDPOINT=https://kms.example.com/v1/cpix/getKey
./target/release/vod-hls2mp4
```

For **clear (unencrypted) streams**, `--cpix-endpoint` / `CPIX_ENDPOINT` may be
omitted. If the stream turns out to be encrypted and no endpoint was provided,
the run fails with a clear error.

---

## ⚙️ Environment Configuration Reference

| Flag | Environment Variable | Required | Description |
| :--- | :--- | :--- | :--- |
| `--ext-source-uri` | `EXT_SOURCE_URI` | Yes | HLS manifest URL (master multi-variant or direct media playlist). |
| `--ais-source-uri` | `AIS_SOURCE_URI` | Yes | Destination URI for the downloaded source file, and nothing else: `s3://bucket/key`, `gs://bucket/key`, or `file:///path`. Derivatives go to `--ais-preview-uri`. |
| `--cpix-endpoint` | `CPIX_ENDPOINT` | Only for encrypted streams | CPIX KMS endpoint used to exchange the KID for the CEK. |
| `--remove-ads` | `REMOVE_ADS` | No (default `false`) | Drop SCTE-35-marked ad segments (DATERANGE `SCTE35-OUT/IN`, `CUE-OUT`/`CUE-IN`) from the output, **frame-accurately**: a segment straddling a cue is re-encoded (x264/AAC) with an exact trim; all other segments stream-copy. The splice is gapless in the progressive `.mp4`; in `.ts` output the PTS discontinuities of removed ads remain. |
| `--event-id` | `EVENT_ID` | No | Sub-folder name used in **both** destinations; defaults to the source file's stem. |
| `--proxy-1fps` | `PROXY_1FPS` | No (default `false`) | Also generate a constant-1fps H.264 MP4 proxy (`<file>_proxy_1fps.mp4`), reading the source back from storage via a signed URL. |
| `--thumbnail` | `THUMBNAIL` | No (default `false`) | Also extract the first I-frame as `<file>.jpg`. |
| `--max-parallel` | `MAX_PARALLEL` | No (default `1`) | Number of parallel segment downloads (1-64). Segments fetch concurrently but assemble strictly in playlist order, so the output is identical to a sequential run — just faster. |
| `--split-on-ad` | `SPLIT_ON_AD` | No (default `false`) | Write **one file per content span** instead of one joined file, splitting at every SCTE-35 ad break and dropping the ads. Files are numbered `<stem>-001`, `<stem>-002`, … Implies ad removal. With it `false`, the same content is emitted as a **single joined file**. Either way the output timeline starts at 0 with no discontinuities — see [Zero-based timelines](#-zero-based-timelines). |
| `--generate-preview` | `GENERATE_PREVIEW` | No (default `false`) | Also publish an HLS rendition ("preview") of every output file, so the recording plays back without fetching the whole MP4/TS. Requires `--ais-preview-uri`. Stream-copy only. |
| `--ais-preview-uri` | `AIS_PREVIEW_URI` | With any derivative flag | **Directory** URI for every consumer-facing artifact — thumbnail, 1fps proxy and HLS preview: `file:///path`, `gs://bucket/prefix`, or `s3://bucket/prefix`. Each playlist takes its source file's name with an `.m3u8` extension (`clip-001.ts` → `clip-001.m3u8`, segments `clip-001_00000.ts`, …). Required by `--thumbnail`, `--proxy-1fps` and `--generate-preview`; without it they are skipped. |

### Where each artifact lands

Output is split across **two destinations**, so the mezzanine download can stay
private while everything derived from it is published:

| Artifact | Destination |
| :--- | :--- |
| Downloaded source (`.ts` / `.mp4`) | `<ais-source-uri parent>/<event-id>/` |
| Thumbnail (`.jpg`) | `<ais-preview-uri>/<event-id>/` |
| 1fps proxy (`_proxy_1fps.mp4`) | `<ais-preview-uri>/<event-id>/` |
| HLS preview (`.m3u8` + segments) | `<ais-preview-uri>/<event-id>/` |

`--ais-source-uri` names an **object** (`gs://bucket/path/clip.mp4`), so the
source lands next to it under `<event-id>/`, which defaults to the file's stem.
`--ais-preview-uri` names a **directory** and nests the same `<event-id>/`, so
one job is one predictable path in each bucket. It may be an entirely different
bucket, and typically is — that is the point of the split.

Without `--ais-preview-uri` the download still succeeds, but `--thumbnail`,
`--proxy-1fps` and `--generate-preview` are skipped with a warning rather than
falling back to the source bucket: writing a consumer artifact into the private
mezzanine would defeat the separation.

### 🎯 Zero-based timelines

Both output modes start at PTS 0 and run continuously, for **CMAF and MPEG-TS
alike**. The two containers need different work to get there:

- **CMAF → progressive MP4** is zero-based for free: the sample tables record
  *durations*, not absolute timestamps, so dropping an ad simply removes its
  samples and the ones after it close up.
- **MPEG-TS** is byte-concatenated, which retains the source's original PTS —
  including the gap an ad break leaves behind. Each span is therefore piped
  through FFmpeg (`-c copy -avoid_negative_ts make_zero`) to rebase it. This is
  **streamed, not staged**: an earlier version buffered whole spans (89 / 82 MiB
  on a 15-minute clip) to local disk, and the piped form was verified
  byte-identical to it.

### 📺 HLS preview accuracy

Previews reproduce the **source's own segmentation** rather than chopping at a
nominal interval. The per-segment durations kept during the download become
explicit split points (FFmpeg's `segment` muxer with `-segment_times`), and those
points are the origin's segment starts — which are IDRs, so the split stays
frame accurate *and* a stream copy.

This is also what keeps `EXT-X-TARGETDURATION` honest. Asking for a nominal
`-hls_time 9`, FFmpeg can only cut at the next keyframe, so segments came out
9.93 / 9.60 / 8.00 s and the declared target rounded up to 10 against a source
that declared 9. Splitting on the real boundaries reproduces the source durations
exactly. The header is then set to `round(max EXTINF)` per
[RFC 8216 §4.3.3.1](https://www.rfc-editor.org/rfc/rfc8216#section-4.3.3.1) —
FFmpeg uses `ceil`, which would declare 10 for a 9.0666 s maximum.

Verified against a 15-minute source: 141 source segments − 22 ad = **119 preview
segments**, max EXTINF `9.066667` vs the source's `9.0666`, `TARGETDURATION:9`
matching the source.

> **MPEG-TS only.** For CMAF sources the preview falls back to nominal-duration
> segmentation, because the `segment` muxer cannot emit the `EXT-X-MAP` init
> segment that fMP4 HLS requires. The `TARGETDURATION` correction still applies,
> so the header stays spec-correct; the segment durations will not match the
> source exactly.

```bash
# one file per content span, ads dropped, 8 segments in flight, each span previewed
./target/release/vod-hls2mp4 \
  --ext-source-uri "https://example.com/media/master.m3u8" \
  --ais-source-uri gs://my-recordings/clip.mp4 \
  --ais-preview-uri gs://my-previews/clip \
  --split-on-ad --generate-preview --max-parallel 8
```

Cloud credentials are read from the standard provider environment (e.g.
`AWS_ACCESS_KEY_ID` / `AWS_REGION` for S3, `GOOGLE_APPLICATION_CREDENTIALS` for
GCS) via the `object_store` engine. On Cloud Run / Fargate, the attached service
account / task role is used automatically (no key files).

---

## 🚀 Deployment

Ships as a **distroless-static** container ([`Dockerfile`](./Dockerfile)) for
serverless batch execution on **Google Cloud Run Jobs** and **AWS Fargate ECS**.
Manifests and step-by-step instructions are in [`deploy/`](../../deploy/README.md).

The binary is fully statically linked against **musl** (crypto backend `ring`,
TLS roots bundled via webpki-roots — no OpenSSL, no glibc, no CA files needed),
so it runs on `gcr.io/distroless/static-debian12:nonroot` (~2 MB base). The
resulting image is **~9 MB**, versus ~30 MB on the glibc `distroless/cc` base.

Build **from the repo root** — the shared `ais-media-core` crate lives outside
this directory and must be in the build context:

```bash
# run from the repo root (the "." context includes ais-media-core)
docker build -f vod-hls2mp4/Dockerfile -t vod-hls2mp4 .   # from media-jobs/
```
