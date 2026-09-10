# syntax=docker/dockerfile:1
#
# hls2mp4 — the HLS → progressive MP4 / TS downloader, run as a Cloud Run Job.
#
# Vendored from ais-media-services `media-jobs/vod-hls2mp4` together with its
# `ais-media-core` sibling crate. The build context is `jobs/hls2mp4`, the
# parent of this crate: the COPY lines reference `ais-media-core` and
# `vod-hls2mp4/…` as siblings, which is the only layout where the path
# dependency in Cargo.toml resolves.
#
#   docker build -f jobs/hls2mp4/vod-hls2mp4/Dockerfile jobs/hls2mp4
#
# What differs from upstream is the runtime stage. Upstream copies a static
# ffmpeg out of a shared base image that takes about an hour to compile;
# this repo has no such image and does not want its build to. The binary only
# shells out to `ffmpeg`/`ffprobe` for the derivatives — the 1 fps analysis
# proxy and the thumbnail — so Debian's package is enough, at the cost of a
# bigger image nobody waits on: a Cloud Run Job's cold start is not on any
# request's critical path.

FROM rust:1-bookworm AS builder
WORKDIR /app
ENV CARGO_TARGET_DIR=/app/target
RUN rustup target add x86_64-unknown-linux-musl \
    && apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/*

# Shared crate (path dependency) + app manifests first, so the dependency
# compile is a cacheable layer independent of app source changes. Cloud Build
# pulls the previous :latest and passes it as --cache-from, which is what
# keeps this from being a ten-minute step on every commit.
COPY ais-media-core ./ais-media-core
COPY vod-hls2mp4/Cargo.toml vod-hls2mp4/Cargo.lock ./vod-hls2mp4/
RUN mkdir vod-hls2mp4/src \
    && echo 'fn main() {}' > vod-hls2mp4/src/main.rs \
    && cargo build --release --locked \
        --manifest-path vod-hls2mp4/Cargo.toml \
        --target x86_64-unknown-linux-musl \
    && rm -rf vod-hls2mp4/src

COPY vod-hls2mp4/src ./vod-hls2mp4/src
RUN touch vod-hls2mp4/src/main.rs \
    && cargo build --release --locked \
        --manifest-path vod-hls2mp4/Cargo.toml \
        --target x86_64-unknown-linux-musl \
    && strip /app/target/x86_64-unknown-linux-musl/release/vod-hls2mp4

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/x86_64-unknown-linux-musl/release/vod-hls2mp4 /usr/local/bin/vod-hls2mp4
ENV FFMPEG_BIN=/usr/bin/ffmpeg
RUN useradd --create-home --uid 1001 sprtz
USER sprtz
# Config is passed entirely via environment variables at run time — the media
# server sets them per execution (see mcp/media_server/runjobs.py).
ENTRYPOINT ["/usr/local/bin/vod-hls2mp4"]
