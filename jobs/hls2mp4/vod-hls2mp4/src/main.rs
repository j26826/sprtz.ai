use std::sync::Arc;

use anyhow::{anyhow, Context, Result};
use chrono::{DateTime, Duration, Utc};
use clap::{ArgAction, Parser};
use m3u8_rs::MediaPlaylist;
use reqwest::Client;
use tokio::io::AsyncWriteExt;

use ais_media_core::hls;
use ais_media_core::notify::{self, StatusWriter};
use ais_media_core::scte35::{AdState, SegmentClass};
use ais_media_core::splice::{self, Keep};
use ais_media_core::{crypto, derivatives, hlspreview, mp4mux, net, storage, task, tsprobe};

/// Stateless HLS CMAF/CENC-CBCS downloader, decryptor, and cloud uploader.
///
/// Every option can be supplied on the command line or via the matching
/// environment variable, which suits both CLI and serverless (Lambda /
/// Cloud Run) invocation.
#[derive(Debug, Parser)]
#[command(version, about, long_about = None)]
struct Args {
    /// HLS manifest URL (master multi-variant or direct media playlist).
    #[arg(long, env = "EXT_SOURCE_URI")]
    ext_source_uri: String,

    /// Destination URI for the downloaded SOURCE file, and nothing else:
    /// s3://bucket/key, gs://bucket/key, or file:///path.
    ///
    /// Only the `.ts` / `.mp4` built from the HLS input lands here. Every
    /// consumer-facing artifact (thumbnail, 1fps proxy, HLS preview) goes to
    /// `--ais-preview-uri`, so the mezzanine bucket can stay private.
    #[arg(long, env = "AIS_SOURCE_URI")]
    ais_source_uri: String,

    /// CPIX KMS endpoint. Required only for encrypted streams; clear streams
    /// ignore it.
    #[arg(long, env = "CPIX_ENDPOINT")]
    cpix_endpoint: Option<String>,

    /// Drop ad segments (SCTE-35 DATERANGE / CUE-OUT / CUE-IN markers) from the
    /// output instead of downloading them.
    ///
    /// For CMAF (the usual case) the surviving content is remuxed into a
    /// progressive MP4 whose sample tables are built by APPENDING sample
    /// durations, so the excised ads leave no gap: the output timeline is
    /// continuous and the media clock is already "PDT corrected". MPEG-TS is
    /// streamed through the rebaser under the default `--restamp`, so it comes
    /// out zero-based and continuous too; with `--restamp=false` the segments
    /// are byte-concatenated with their original PTS intact and a removed ad
    /// does leave a timestamp jump. (Measured on a 24-minute TS asset:
    /// `start_time` 0.000 with restamp, 10.000 without.)
    ///
    /// Accepts `--remove-ads`, `--remove-ads=true|false`, or `REMOVE_ADS=true`.
    #[arg(
        long,
        env = "REMOVE_ADS",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = false,
        default_missing_value = "true"
    )]
    remove_ads: bool,

    /// Split the output at ad breaks: emit ONE FILE PER CONTENT SPAN instead of
    /// a single joined file. Ads are dropped either way -- this only decides
    /// whether the surviving spans are welded together or kept apart.
    ///
    ///   false (default): one joined file, ads excised, timeline continuous.
    ///   true:            `<stem>-001.mp4`, `<stem>-002.mp4`, ... one per span,
    ///                    each independently playable with its own moov.
    ///
    /// Implies `--remove-ads`: you cannot split on a break you did not detect.
    /// Accepts `--split-on-ad`, `--split-on-ad=true|false`, or
    /// `SPLIT_ON_AD=true`.
    #[arg(
        long,
        env = "SPLIT_ON_AD",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = false,
        default_missing_value = "true"
    )]
    split_on_ad: bool,

    /// Rewrite container timestamps so each output file starts at zero and runs
    /// continuously (MPEG-TS only; see below).
    ///
    /// `restamp` decides the TIMELINE; `--split-on-ad` decides the FILE LAYOUT.
    /// They are independent, and this crate derives its concat axis from the
    /// existing flag rather than adding a second name for it —
    /// `concat == !split_on_ad`:
    ///
    ///   concat (default), restamp   -> ONE joined file, rebased to zero
    ///   concat (default), no restamp-> ONE joined file, original PTS kept
    ///   split-on-ad, restamp        -> one file per span, EACH rebased to zero
    ///   split-on-ad, no restamp     -> one file per span, original PTS kept
    ///
    /// DEFAULTS TO TRUE, unlike live-hls2mp4 where it defaults to false. That
    /// asymmetry is deliberate: this job has ALWAYS streamed MPEG-TS through
    /// the rebaser, so `false` would silently change the mezzanine that
    /// downstream consumers already depend on. The flag exposes the existing
    /// behaviour rather than introducing it.
    ///
    /// CMAF is unaffected either way: progressive MP4 sample tables store
    /// durations, not absolute timestamps, so the output is zero-based and
    /// continuous by construction and there is nothing to restamp.
    ///
    /// Accepts `--restamp`, `--restamp=true|false`, or `RESTAMP=true`.
    #[arg(
        long,
        env = "RESTAMP",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = true,
        default_missing_value = "true"
    )]
    restamp: bool,

    /// Also publish an HLS rendition ("preview") of every output file, so the
    /// result can be played back adaptively without downloading the whole MP4.
    /// Requires --ais-preview-uri. Accepts `--generate-preview`,
    /// `--generate-preview=true|false`, or `GENERATE_PREVIEW=true`.
    #[arg(
        long,
        env = "GENERATE_PREVIEW",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = false,
        default_missing_value = "true"
    )]
    generate_preview: bool,

    /// Directory URI for every CONSUMER-FACING artifact: `file:///path`,
    /// `gs://bucket/prefix` or `s3://bucket/prefix`.
    ///
    /// Receives the thumbnail, the 1fps proxy and the HLS preview — everything
    /// derived from the download, as opposed to the download itself, which goes
    /// to `--ais-source-uri`. Each playlist takes its source file's name with an
    /// `.m3u8` extension (`clip.ts` -> `clip.m3u8`), and its segments are
    /// packaged with the SAME profile as the source stream (MPEG-TS vs fMP4,
    /// and the source's target duration) rather than a fixed default.
    ///
    /// Required when any of `--thumbnail`, `--proxy-1fps` or
    /// `--generate-preview` is set; without it those artifacts are skipped.
    #[arg(long, env = "AIS_PREVIEW_URI")]
    ais_preview_uri: Option<String>,

    /// Name of the sub-folder the artifacts are published into. Used in BOTH
    /// destinations: `<ais-source-uri>/<event-id>/` for the source file and
    /// `<ais-preview-uri>/<event-id>/` for the derivatives. Defaults to the
    /// source file's stem.
    #[arg(long, env = "EVENT_ID")]
    event_id: Option<String>,

    /// Also generate a constant-1fps H.264 MP4 proxy of the
    /// published file, named `<file>_proxy_1fps.mp4` alongside it.
    /// Accepts `--proxy-1fps`, `--proxy-1fps=true|false`, or `PROXY_1FPS=true`.
    #[arg(
        long = "proxy-1fps",
        env = "PROXY_1FPS",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = false,
        default_missing_value = "true"
    )]
    proxy_1fps: bool,

    /// Also extract the first I-frame of the published file as a JPEG named
    /// `<file>.jpg` alongside it.
    /// Accepts `--thumbnail`, `--thumbnail=true|false`, or `THUMBNAIL=true`.
    #[arg(
        long,
        env = "THUMBNAIL",
        action = ArgAction::Set,
        num_args = 0..=1,
        default_value_t = false,
        default_missing_value = "true"
    )]
    thumbnail: bool,

    /// Number of parallel segment downloads (distributed processing). Segments
    /// are fetched concurrently but assembled strictly in playlist order, so
    /// the output is byte-identical to a sequential run.
    #[arg(long, env = "MAX_PARALLEL", default_value_t = 1, value_parser = clap::value_parser!(u32).range(1..=64))]
    max_parallel: u32,

    /// HTTPS base the preview bucket is served from, without a trailing slash
    /// (e.g. `https://preview.example.com`). Each deliverable that lives in the
    /// preview bucket is reported with a `cdn_url` built from this base beside
    /// its `gs://` uri; deliverables anywhere else get none.
    ///
    /// Unset omits `cdn_url` entirely, which is the correct state when no CDN
    /// fronts the bucket — a base pointing at a CDN that serves something else
    /// would yield URLs that 404, so there is no default.
    #[arg(long, env = "CDN_BASE_URL")]
    cdn_base_url: Option<String>,

    /// The inbound §4 task envelope, verbatim JSON (`{"header":…,"data":…}`).
    ///
    /// Everything this job ECHOES rather than acts on arrives here: job / brand /
    /// agent / ext-job ids, the signature, `ai_flags`, `pass_through`, and — for a
    /// `job_type: "vod"` task — `media_asset.metadata`. Together with
    /// `--status-uri` it switches on §7 reporting: the envelope cannot be stamped
    /// with an identity the job does not have, so a run without a task reports
    /// nothing and logs instead.
    ///
    /// The orchestrator (clipping-scheduler) has been sending this to the vod job
    /// since the G8 branch was written; until now clap did not declare it, so it
    /// was silently ignored and no status document was ever written.
    #[arg(long, env = "TASK_JSON")]
    task_json: Option<String>,

    /// Output directory URI for the §7 status documents — the dedicated status
    /// bucket a storage trigger watches: `gs://bucket/prefix`,
    /// `s3://bucket/prefix`, or `file:///path`. One JSON object is written per
    /// state change. Reporting is off unless this and `--task-json` are both set.
    #[arg(long, env = "STATUS_URI")]
    status_uri: Option<String>,

    /// Seconds one status-document write may take before it is abandoned. Set to
    /// 0 to remove the bound and wait indefinitely instead — 0 disables the
    /// timeout, it does not abandon writes at once.
    #[arg(long, env = "STATUS_WRITE_TIMEOUT_SECS", default_value_t = 30)]
    status_write_timeout_secs: u64,

    /// Which occurrence of the dispatching schedule this run covers, reported
    /// under §7 `data.status.occurrence_index`.
    ///
    /// DECLARED EVEN THOUGH §4.4 HAS NO RECURRENCE, because the orchestrator sends
    /// OCCURRENCE_INDEX to this job on every dispatch and an undeclared env var is
    /// silently dropped by clap. Reading it means the report says what the
    /// dispatcher says; hard-coding 0 here would make the two disagree the moment
    /// they ever differed. A VOD capture is created once and never updated, so in
    /// practice the value IS 0 — which is also the default for a run with no
    /// orchestrator at all.
    #[arg(long, env = "OCCURRENCE_INDEX", default_value_t = 0)]
    occurrence_index: u32,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    // The §4 task, decoded BEFORE anything else runs. A task that was supplied and
    // cannot be read fails the process here rather than producing a download whose
    // reports carry no identity — see task::Task::parse.
    let task = match args.task_json.as_deref() {
        Some(raw) => Some(task::Task::parse(raw)?),
        None => None,
    };
    // §7 reporting (log-only for standalone runs). Shared behind an `Arc` for the
    // same reason the live clipper's is: the terminal document is written from
    // here, outside the frame that did the work.
    //
    // The fallback identity is EVENT_ID, which the scheduler sets to the job id —
    // and the download's own sub-folder name, so a run whose task carries no
    // job_id still writes documents under the prefix its media landed in.
    let reporter = Arc::new(StatusWriter::from_task(
        args.status_uri.as_deref(),
        args.event_id.as_deref().unwrap_or_default(),
        args.occurrence_index,
        args.status_write_timeout_secs,
        task.as_ref(),
    ));
    match run(&args, &reporter).await {
        Ok(completion_reported) => {
            // Only when the run did not already report §7.3, which is itself the
            // completion — see `run`.
            if !completion_reported {
                reporter.completed().await;
            }
            Ok(())
        }
        Err(e) => {
            // The terminal document is written BEFORE the error propagates, so a
            // job that dies is reported as failed rather than leaving the
            // orchestrator's record at in_progress forever (which is what every
            // vod dispatch did before this).
            //
            // Bound to a local rather than passed as `&format!(...)`: `failed`
            // copies the string before its first await today, so a temporary
            // would survive, but that is its business and not something this
            // call site should depend on.
            let details = format!("{e:#}");
            reporter.failed(notify::ERR_DOWNLOAD_FAILED, &details).await;
            Err(e)
        }
    }
}

/// Which §7 document one finished span reports.
///
/// SEPARATED FROM THE LOOP SO THE RULE IS TESTABLE. Which shape goes on the wire
/// is the whole of this clipper's §7 behaviour, and inline in the derivative loop
/// it could only be exercised by running a real download.
#[derive(Debug, PartialEq, Eq)]
enum SpanReport {
    /// §7.3: `job` + `status` + `output`, and NO segment block — the deliverable
    /// and the completion in one document.
    Completion,
    /// A §7.1-shaped segment carrying the reason this span ended.
    Segment(&'static str),
}

/// `span` is 0-based; `total` is how many spans the download produced.
///
/// ONE SPAN IS §7.3, however the run was REQUESTED. A `--split-on-ad` download of
/// a source with no ad break yields a single file, and that is one asset in, one
/// file out — there is nothing for a segment block to tell apart, and §2.1 says a
/// VOD job notifies "on completion". Keying on the flag instead of the outcome
/// would emit a numbered segment for a job that produced exactly one thing.
///
/// N SPANS ARE SEGMENTS, which §7 draws no example for: emitting §7.3 N times
/// would publish N completions for one job with nothing to order or distinguish
/// them. Every span but the last was closed by the ad break that follows it; the
/// last ran to the end of the requested window — the same two reasons, and the
/// same spellings, the live clipper reports, so a consumer needs no VOD-specific
/// vocabulary.
fn span_report(span: usize, total: usize) -> SpanReport {
    if total <= 1 {
        return SpanReport::Completion;
    }
    if span + 1 == total {
        SpanReport::Segment(notify::CLOSE_SCHEDULE_END)
    } else {
        SpanReport::Segment(notify::CLOSE_AD_BREAK_START)
    }
}

/// What `SourceProfile::measured_frame_rate` should carry, given what the
/// manifest DECLARED and what each probe MEASURED.
///
/// A DECLARED RATE IS NEVER OVERRULED (product ruling). `video_profile` prefers
/// `measured_frame_rate` over `frame_rate`, so returning a measurement when the
/// manifest already stated one would change a value that publishes correctly
/// today. That is right for the live clipper, which re-times what it recorded
/// off a sliding edge, but not here: a VOD span is a STREAM COPY of the declared
/// variant, so the origin's own `FRAME-RATE` describes these exact bytes.
///
/// WHICH PROBE ANSWERS IS THE CONTAINER'S CHOICE, not a preference order: a CMAF
/// fragment carries no PES stamps for `tsprobe` to read, and MPEG-TS builds no
/// sample tables for the progressive builder to count. Only one of the two can
/// ever have an answer, so this picks it rather than falling back between them —
/// a fallback would let a stale value from the wrong container leak through.
fn reported_frame_rate(
    declared: Option<f64>,
    is_cmaf: bool,
    cmaf_measured: Option<f64>,
    ts_measured: Option<f64>,
) -> Option<f64> {
    if declared.is_some() {
        return None;
    }
    if is_cmaf {
        cmaf_measured
    } else {
        ts_measured
    }
}

/// The whole download: resolve, fetch, assemble, derive, and report each output
/// file as it is published.
///
/// Returns whether the TERMINAL `completed` document was already written from in
/// here. A single-span run reports §7.3 — one document carrying the output AND
/// the completion — so a second `completed` from the caller would publish a
/// lifecycle message for a job that had already finished. A split run reports one
/// segment per span and leaves the completion to the caller.
async fn run(args: &Args, reporter: &Arc<StatusWriter>) -> Result<bool> {
    println!("Initializing lightweight serverless downloader context...");
    let client = Client::builder().build()?;

    // 1. Resolve the source to its media playlists AND the attributes the §7.1
    //    `media_profile` is built from — resolution, codecs, frame rate, average
    //    bandwidth, and the EXT-X-MEDIA audio / CC declarations.
    //
    //    THE SAME RESOLVER THE LIVE CLIPPER USES (`ais_media_core::hls`). This
    //    file used to carry its own copy of the master-playlist walk, which
    //    selected the identical variant but threw every attribute away except the
    //    audio URI — so there was nothing left to describe the media with. Two
    //    requests either way: the master, then the chosen variant.
    let renditions = hls::resolve_renditions(&client, &args.ext_source_uri).await?;
    let media_playlist = match hls::fetch_playlist(&client, &renditions.video).await? {
        hls::Fetched::Media(playlist) => *playlist,
        // resolve_renditions already picked a variant, so a master here means the
        // origin served one in its place; there is no second variant to descend to.
        hls::Fetched::Master => {
            return Err(anyhow!(
                "the selected variant {} is itself a master playlist",
                renditions.video
            ))
        }
    };
    let resolved_base_url = renditions.video.clone();
    let audio_url = renditions.audio.clone();
    let mut source_profile = notify::SourceProfile::of(&renditions);

    // 2. Detect the container and prepare the output. Both paths are
    //    byte-concat only (no transcoding): CMAF/fMP4 fragments -> .mp4,
    //    MPEG-TS segments -> .ts.
    let map_tag = media_playlist.segments.iter().find_map(|s| s.map.clone());
    let (header_bytes, cek_option, effective_output) = match map_tag {
        Some(map) => {
            // CMAF / fragmented MP4: prepend the init map, honour encryption.
            let init_url = hls::resolve_url(&resolved_base_url, &map.uri)?;
            println!(
                "-> CMAF/fMP4 stream. Downloading initialization map from: {}",
                init_url
            );
            let init_bytes = net::fetch_bytes(&client, &init_url, 3)
                .await
                .context("downloading initialization map")?;

            let cek_option = match crypto::parse_tenc(&init_bytes)? {
                Some(tenc) => {
                    let kid_uuid = crypto::format_to_uuid(&tenc.kid);
                    println!("-> Detected Encryption. Extracted KID: {}", kid_uuid);
                    let iv = tenc.constant_iv.ok_or_else(|| {
                        anyhow!(
                            "stream uses per-sample IVs (senc box, default_Per_Sample_IV_Size={}), \
                             which is not supported; only cbcs with a constant IV is handled.",
                            tenc.per_sample_iv_size
                        )
                    })?;
                    let cpix_endpoint = args.cpix_endpoint.as_deref().ok_or_else(|| {
                        anyhow!("Stream is encrypted but no --cpix-endpoint / CPIX_ENDPOINT was provided.")
                    })?;
                    let cek =
                        crypto::fetch_key_from_cpix(&client, cpix_endpoint, &kid_uuid).await?;
                    println!("-> Acquired plain CEK via CPIX handshake.");
                    Some((cek, iv))
                }
                None => {
                    println!("-> Stream is CLEAR. Bypassing KMS verification.");
                    None
                }
            };
            // The CMAF output is a progressive MP4, so the destination must NAME
            // one. The orchestrator passes `--ais-source-uri` as a bucket PREFIX
            // (`gs://<bucket>/<job_id>`), which has no extension at all: the
            // MPEG-TS branch below already forces `.ts` on it, and without the
            // same treatment here a CMAF source landed as an extensionless object
            // that the §7 `master_video.url` then named. An input that already
            // ends in `.mp4` is left exactly as it was.
            let output = force_extension(&args.ais_source_uri, "mp4");
            if output != args.ais_source_uri {
                println!("-> CMAF output adjusted to {output}");
            }
            (init_bytes, cek_option, output)
        }
        None => {
            // MPEG-TS: no init map. Concatenate segments into a single .ts.
            if media_playlist.segments.iter().any(|s| s.key.is_some()) {
                return Err(anyhow!(
                    "MPEG-TS stream is AES-128 encrypted (#EXT-X-KEY), which is not yet supported."
                ));
            }
            let output = force_extension(&args.ais_source_uri, "ts");
            if output != args.ais_source_uri {
                println!(
                    "-> MPEG-TS stream detected. Concatenating to .ts; output adjusted to {}",
                    output
                );
            } else {
                println!(
                    "-> MPEG-TS stream detected. Concatenating segments to .ts (no re-encoding)."
                );
            }
            (Vec::new(), None, output)
        }
    };

    // 2b. CMAF keeps audio in a separate rendition; fetch its playlist + init
    //     so the audio track is muxed into the .mp4. Clear streams only — the
    //     CEK path decrypts video in place and separate encrypted audio isn't
    //     handled.
    let is_cmaf = !header_bytes.is_empty();
    let audio = match (&audio_url, &cek_option, is_cmaf) {
        (Some(aurl), None, true) => match fetch_audio_rendition(&client, aurl).await {
            Ok(x) => {
                println!("-> Muxing separate audio rendition into the .mp4.");
                Some(x)
            }
            Err(e) => {
                eprintln!("-> Audio mux setup failed ({e}); writing video only.");
                None
            }
        },
        (Some(_), Some(_), true) => {
            eprintln!("-> Encrypted stream: separate audio muxing is not supported; video only.");
            None
        }
        _ => None,
    };

    // CMAF is remuxed to a **progressive** MP4 (real sample tables, moov at
    // the end) — fragmented output seeks poorly in players like VLC. Still
    // pure stream-copy; see `ais_media_core::mp4mux`.
    let (audio_init, audio_playlist) = match audio {
        Some((init, pl)) => (Some(init), Some(pl)),
        None => (None, None),
    };
    let mut mp4: Option<mp4mux::ProgressiveMp4> = None;
    let mut leading_bytes: Vec<u8> = Vec::new();
    if is_cmaf {
        println!("-> Writing a progressive MP4 (sample tables, seekable everywhere).");
        let (builder, ftyp) = mp4mux::ProgressiveMp4::new(&header_bytes, audio_init.as_deref())?;
        mp4 = Some(builder);
        leading_bytes = ftyp;
    }

    // 3. Initialize Cloud Streaming Upload Pipeline. Artifacts split across TWO
    //    destinations, both nesting under the SAME `<event-id or file stem>/`
    //    sub-folder so a job's output is one predictable path in each:
    //      publish_dir - the source file only (private mezzanine)
    //      public_dir  - thumbnail, 1fps proxy, HLS preview (consumer-facing)
    //    `public_dir` is None without --ais-preview-uri, which is why every
    //    derivative below is guarded rather than assumed.
    let (parent_uri, file_name) = split_object_uri(&effective_output)?;
    let sub_folder = args
        .event_id
        .clone()
        .unwrap_or_else(|| stem(&file_name).to_string());
    // Both sides resolve the per-event folder idempotently, NOT by appending:
    // a base that ALREADY ends in the event id is left alone. Appending
    // unconditionally buried a job one level deeper as
    // `<bucket>/<job_id>/<job_id>/`, which happens whenever the caller has
    // already named the folder — the clipping-scheduler does exactly that for
    // --ais-preview-uri, and a hand-run `--ais-source-uri gs://b/<job>/clip.mp4`
    // does it for the source. `storage::event_folder` is the same helper the
    // live clipper uses; VOD simply never routed through it.
    let publish_dir = storage::event_folder(&parent_uri, &sub_folder);
    let public_dir = args
        .ais_preview_uri
        .as_deref()
        .map(|uri| storage::event_folder(uri, &sub_folder));
    let final_uri = format!("{publish_dir}/{file_name}");
    // One output per content span. Without --split-on-ad there is exactly one,
    // so the joined and split paths are the SAME code -- they differ only in
    // how many spans Pass 1 produced.
    //
    // Naming: the joined file keeps the requested name; split spans get
    // `<stem>-001.<ext>` etc. so they sort correctly and never collide with it.
    let span_name = |span: usize| -> String {
        if !args.split_on_ad {
            return file_name.clone();
        }
        let st = stem(&file_name);
        let ext = file_name.rsplit_once('.').map(|(_, e)| e).unwrap_or("mp4");
        format!("{st}-{:03}.{ext}", span + 1)
    };
    let span_uri = |span: usize| -> String { format!("{publish_dir}/{}", span_name(span)) };
    println!(
        "-> Establishing secure streaming connection to: {}",
        span_uri(0)
    );
    let mut cur_span: usize = 0;
    // MPEG-TS is rebased by STREAMING it through ffmpeg (see splice::TsRebaser)
    // -- segments go in as they arrive, nothing is staged. CMAF streams
    // straight through: mp4mux is already zero-based and continuous.
    // For MPEG-TS the writer is OWNED by the rebaser: ffmpeg's stdout is
    // forwarded into it directly, so the span never exists in memory. `finish`
    // hands it back. CMAF keeps its own writer, since mp4mux emits ready bytes.
    let mut out_stream: Option<splice::BoxedSink> = None;
    // The rebaser is what makes MPEG-TS zero-based; without `--restamp` the
    // segments are byte-concatenated with their original PTS intact, so a
    // removed ad leaves a timestamp jump (which is exactly what the flag is
    // for). CMAF never needs it.
    let restamp_ts = args.restamp && !is_cmaf;
    let mut ts_rebaser = if restamp_ts {
        Some(splice::TsRebaser::new(Box::new(
            storage::single_object_writer(&span_uri(0))?,
        ))?)
    } else {
        out_stream = Some(Box::new(storage::single_object_writer(&span_uri(0))?));
        None
    };
    let mut written_uris: Vec<String> = Vec::new();

    // FRAME RATE MEASURED FROM THE BYTES, for the sources that declare none.
    //
    // `FRAME-RATE` is OPTIONAL in RFC 8216 and plenty of origins omit it — the
    // §7 `media_profile` then carried no frame rate at all, because nothing in a
    // manifest can supply one honestly (`CODECS` bounds macroblocks per second,
    // not frames; `BANDWIDTH` says nothing). The live clipper already measures
    // its own, so a VOD capture of the same source reported less than a live one
    // did. This closes that.
    //
    // FED ONLY BY CONTENT SEGMENTS (product ruling). An ad segment is dropped
    // and never reaches the output, so its stamps describe bytes nobody
    // publishes; a BOUNDARY segment is re-encoded by the frame-accurate trim, so
    // its stamps describe the re-encode rather than the source. Both are skipped
    // — see the feed site, which is gated on `w.keep.is_none()`.
    let mut ts_fps = tsprobe::FrameRateProbe::new();
    // The CMAF equivalent, read off the progressive builder's own sample tables
    // (timescale × samples ÷ total ticks) rather than probed — the tables are
    // already being built, so it costs nothing. Captured before `finish`
    // consumes the builder.
    let mut cmaf_fps: Option<f64> = None;

    // Leading bytes: the progressive `ftyp` for CMAF, nothing for MPEG-TS.
    if let Some(w) = out_stream.as_mut() {
        w.write_all(&leading_bytes).await?;
    }

    // The job is now doing the work it was dispatched for. Reported BEFORE the
    // first fetch, so an orchestrator watching the status bucket sees the job
    // start rather than only learning it existed when it finished.
    reporter.set_stage(notify::STAGE_DOWNLOAD);
    reporter.recording().await;

    // 4. Stream, Decrypt (CMAF only), and Upload Segments in real-time
    // SCTE-35 ad removal (`--remove-ads`): segment wall-clock times come from
    // EXT-X-PROGRAM-DATE-TIME when present and are extrapolated from segment
    // durations otherwise (VOD timelines are epoch-based, matching the
    // DATERANGE START-DATEs).
    // Splitting on ad breaks requires detecting them, so it implies removal.
    let remove_ads = args.remove_ads || args.split_on_ad;
    let mut ad_state = remove_ads.then(AdState::new);
    let mut clock: Option<DateTime<Utc>> = None;
    let mut skipped_ads: u32 = 0;
    if ad_state.is_some() {
        println!("-> Ad removal enabled: SCTE-35-marked segments will be dropped.");
    }
    if args.split_on_ad {
        println!("-> Split on ad: one output file per content span.");
    }
    let total_segments = media_playlist.segments.len();

    // Pass 1 — classify every segment against the (static) VOD playlist and
    // build the download work list. Ad segments are never fetched; boundary
    // segments carry their frame-accurate trim.
    struct Work {
        index: usize,
        label: String,
        video_url: String,
        audio_url: Option<String>,
        keep: Option<Keep>,
        // Seconds of content this segment actually contributes: its EXTINF,
        // or the trimmed remainder at an ad boundary. Cumulated per span to
        // give the preview EXACT, keyframe-aligned split points.
        kept_secs: f64,
        // Which contiguous run of programme this segment belongs to. Bumped
        // whenever content resumes after a dropped ad, so the writer knows
        // where one output file ends and the next begins. Always 0 unless
        // --split-on-ad is set.
        span: usize,
        // Wall-clock bounds of the part of this segment that was KEPT — the
        // trimmed remainder at an ad boundary, not the whole EXTINF. Reduced per
        // span into the §7 `start_time` / `end_time`.
        //
        // Meaningful only when the playlist declares EXT-X-PROGRAM-DATE-TIME
        // (`playlist_has_pdt`); without it the clock below is an extrapolation
        // from the Unix epoch, which is a real instant and must never be
        // published as one. See where the report is built.
        kept_start: DateTime<Utc>,
        kept_end: DateTime<Utc>,
    }
    // Whether the source states its own wall clock at all. A VOD playlist need
    // not, and §7's start/end are wall-clock fields: absent PDT means the run
    // reports a duration and no bounds, rather than bounds anchored on 1970.
    let playlist_has_pdt = media_playlist
        .segments
        .iter()
        .any(|s| s.program_date_time.is_some());
    let mut work: Vec<Work> = Vec::new();
    // Span bookkeeping: `span` is the current run, `in_ad` remembers whether
    // the previous segment was dropped so the FIRST content segment after a
    // break opens a new file (and a break before any content does not create
    // an empty leading span).
    let mut span: usize = 0;
    let mut in_ad = false;
    for (i, segment) in media_playlist.segments.iter().enumerate() {
        let seg_start = segment
            .program_date_time
            .map(|pdt| pdt.with_timezone(&Utc))
            .or(clock)
            .unwrap_or(DateTime::<Utc>::UNIX_EPOCH);
        let seg_end = seg_start + Duration::milliseconds((segment.duration * 1000.0) as i64);
        clock = Some(seg_end);

        // A cue landing inside the segment classifies as EntersAd/ExitsAd —
        // the straddling segment is re-encoded with a frame-accurate trim.
        let class = match ad_state.as_mut() {
            Some(ad) => ad.classify(segment, seg_start, seg_end),
            None => SegmentClass::Content,
        };
        if class == SegmentClass::Ad {
            skipped_ads += 1;
            in_ad = true;
            println!(
                "[{}/{}] Skipping ad segment: {}",
                i + 1,
                total_segments,
                segment.uri
            );
            continue;
        }

        // A boundary segment (the break starts or ends inside it) is
        // re-encoded with a frame-accurate trim; everything else stream-copies.
        let keep = match class {
            SegmentClass::EntersAd { splice } => {
                println!(
                    "[{}/{}] Ad break starts mid-segment; frame-accurate trim at {}",
                    i + 1,
                    total_segments,
                    splice.to_rfc3339()
                );
                Some(Keep::Before {
                    until: (splice - seg_start).num_milliseconds() as f64 / 1000.0,
                })
            }
            SegmentClass::ExitsAd { splice } => {
                println!(
                    "[{}/{}] Ad break ends mid-segment; frame-accurate trim at {}",
                    i + 1,
                    total_segments,
                    splice.to_rfc3339()
                );
                Some(Keep::From {
                    from: (splice - seg_start).num_milliseconds() as f64 / 1000.0,
                })
            }
            _ => None,
        };

        let video_url = hls::resolve_url(&resolved_base_url, &segment.uri)?;
        // Paired audio segment — same index, VOD playlists list the
        // renditions 1:1.
        let audio_seg_url = if mp4.is_some() {
            match &audio_playlist {
                Some(ap) => {
                    let aseg = ap.segments.get(i).ok_or_else(|| {
                        anyhow!(
                            "audio playlist has no segment #{} to pair (audio: {}, video: {})",
                            i + 1,
                            ap.segments.len(),
                            total_segments
                        )
                    })?;
                    Some(hls::resolve_url(
                        audio_url.as_deref().expect("mux implies url"),
                        &aseg.uri,
                    )?)
                }
                None => None,
            }
        } else {
            None
        };
        // Content resuming after a break starts a new span. Guarded on
        // `!work.is_empty()` so a break BEFORE any content does not leave an
        // empty span 0 and an off-by-one in the file numbering.
        if in_ad {
            if args.split_on_ad && !work.is_empty() {
                span += 1;
            }
            in_ad = false;
        }
        // A trimmed boundary segment contributes only the kept remainder — in
        // seconds AND at the two ends of its wall clock, which is why the bounds
        // are derived from the same `keep` rather than from the whole EXTINF.
        let (kept_secs, kept_start, kept_end) = match keep {
            None => (segment.duration as f64, seg_start, seg_end),
            Some(Keep::Before { until }) => (
                until,
                seg_start,
                seg_start + Duration::milliseconds((until * 1000.0) as i64),
            ),
            Some(Keep::From { from }) => (
                (segment.duration as f64 - from).max(0.0),
                seg_start + Duration::milliseconds((from * 1000.0) as i64),
                seg_end,
            ),
        };
        work.push(Work {
            index: i,
            label: segment.uri.clone(),
            video_url,
            audio_url: audio_seg_url,
            keep,
            kept_secs,
            span,
            kept_start,
            kept_end,
        });
    }

    // NOTHING SURVIVED PASS 1 IS A FAILURE, NOT AN EMPTY SUCCESS.
    //
    // A playlist whose every segment classified as an ad — or one with no
    // segments at all — leaves `work` empty. Everything downstream still runs:
    // `derive_from` is built from `0..=cur_span`, so it is NEVER empty and the
    // reporting loop always fires at least once; `written_uris` is pushed
    // unconditionally. Without this guard the run would finalize an object with
    // no media in it and publish a §7.3 COMPLETION naming it — telling AI Studio
    // the job succeeded and handing it a `master_video` entry for content that
    // does not exist. A false success is worse than the silence this job used to
    // produce, because a consumer cannot tell it from a real one.
    //
    // Checked HERE, between the passes, because it is the last point where the
    // answer is knowable and nothing has been written yet: Pass 1 has classified
    // every segment, and Pass 2 has not created the output object. Returning an
    // error routes through `main`, which reports `failed` with
    // ERR_DOWNLOAD_FAILED — the honest outcome, and the one the sweep and the
    // notifier already know how to handle.
    if work.is_empty() {
        return Err(anyhow!(
            "no content segments survived: {total_segments} segment(s) in the playlist, \
             {skipped_ads} removed as ads, 0 kept — nothing to publish"
        ));
    }

    // Pass 2 — distributed download: up to `--max-parallel` segment fetches in
    // flight at once (a video segment and its paired audio fetch together),
    // consumed strictly in playlist order so the output assembles
    // deterministically regardless of completion order.
    let parallel = (args.max_parallel.max(1) as usize).min(work.len().max(1));
    if parallel > 1 {
        println!("-> Distributed download: {parallel} parallel segment fetches");
    }
    type FetchedPair = Result<(Vec<u8>, Option<Vec<u8>>)>;
    let mut inflight: std::collections::VecDeque<tokio::task::JoinHandle<FetchedPair>> =
        std::collections::VecDeque::new();
    let mut next_spawn = 0usize;
    for w in &work {
        // Keep the pipeline full up to the concurrency cap.
        while inflight.len() < parallel && next_spawn < work.len() {
            let nw = &work[next_spawn];
            let client = client.clone();
            let video_url = nw.video_url.clone();
            let audio_url = nw.audio_url.clone();
            inflight.push_back(tokio::spawn(async move {
                let v = net::fetch_bytes(&client, &video_url, 3)
                    .await
                    .with_context(|| format!("downloading segment {video_url}"))?;
                let a = match &audio_url {
                    Some(u) => Some(
                        net::fetch_bytes(&client, u, 3)
                            .await
                            .with_context(|| format!("downloading audio segment {u}"))?,
                    ),
                    None => None,
                };
                Ok((v, a))
            }));
            next_spawn += 1;
        }

        let (mut seg_bytes, audio_bytes) = inflight
            .pop_front()
            .expect("pipeline primed")
            .await
            .context("segment download task failed")??;
        println!(
            "[{}/{}] Streaming segment to cloud: {}",
            w.index + 1,
            total_segments,
            w.label
        );

        if let Some((ref cek, ref iv)) = cek_option {
            crypto::decrypt_cmaf_cbcs_inplace(&mut seg_bytes, cek, iv)?;
        }

        // Measure from CONTENT segments only — `w.keep.is_none()` is exactly
        // "this segment is stream-copied whole", i.e. neither an ad (those never
        // become a `Work` at all) nor a boundary piece the trim re-encodes.
        // Fed after decryption, because encrypted payload has no readable PES
        // stamps. MPEG-TS only: a CMAF fragment carries no PES, and its rate is
        // taken from the builder instead.
        if !is_cmaf && w.keep.is_none() {
            ts_fps.feed(&seg_bytes);
        }

        // CMAF: remux the fragment into the progressive MP4 (samples land in
        // an `mdat`, timing/size tables accumulate for the final `moov`).
        // MPEG-TS is concatenated as-is.
        let out_bytes = match (w.keep, mp4.as_mut()) {
            // Stream-copy paths.
            (None, Some(builder)) => builder
                .push(&seg_bytes, audio_bytes.as_deref())
                .with_context(|| format!("remuxing segment {}", w.label))?,
            (None, None) => seg_bytes,
            // Frame-accurate boundary re-encode.
            (Some(keep), Some(builder)) => {
                let piece = splice::reencode_trim_cmaf(
                    &header_bytes,
                    &seg_bytes,
                    match (&audio_init, &audio_bytes) {
                        (Some(init), Some(seg)) => Some((init.as_slice(), seg.as_slice())),
                        _ => None,
                    },
                    keep,
                    builder.video_timescale(),
                )
                .await
                .context("re-encoding the boundary segment")?;
                builder
                    .push_encoded(&piece)
                    .context("ingesting the re-encoded boundary piece")?
            }
            (Some(keep), None) => splice::reencode_trim_ts(&seg_bytes, keep)
                .await
                .context("re-encoding the boundary segment")?,
        };

        // Span rollover: close the file this segment does NOT belong to and
        // open the next. Each span gets a fresh mp4 builder, which is what
        // makes every split file independently playable and zero-based.
        if w.span != cur_span {
            if is_cmaf {
                if let (Some(builder), Some(w)) = (&mp4, out_stream.as_mut()) {
                    w.write_all(&builder.finish()?).await?;
                }
                let (builder, ftyp) =
                    mp4mux::ProgressiveMp4::new(&header_bytes, audio_init.as_deref())?;
                if let Some(w) = out_stream.as_mut() {
                    w.shutdown().await?;
                }
                written_uris.push(span_uri(cur_span));
                cur_span = w.span;
                let mut w: splice::BoxedSink =
                    Box::new(storage::single_object_writer(&span_uri(cur_span))?);
                w.write_all(&ftyp).await?;
                out_stream = Some(w);
                mp4 = Some(builder);
            } else {
                if let Some(r) = ts_rebaser.take() {
                    // The span is already fully written; finish only returns
                    // the writer so it can be closed.
                    let mut sink = r.finish().await?;
                    sink.shutdown().await?;
                }
                if let Some(w) = out_stream.as_mut() {
                    w.shutdown().await?;
                }
                written_uris.push(span_uri(cur_span));
                cur_span = w.span;
                if restamp_ts {
                    // A fresh rebaser per span is what makes EACH file
                    // zero-based -- the `--split-on-ad` + `--restamp` corner.
                    ts_rebaser = Some(splice::TsRebaser::new(Box::new(
                        storage::single_object_writer(&span_uri(cur_span))?,
                    ))?);
                } else {
                    out_stream = Some(Box::new(storage::single_object_writer(&span_uri(
                        cur_span,
                    ))?));
                }
            }
            println!(
                "-> Span {} complete; writing {}",
                cur_span,
                span_uri(cur_span)
            );
        }

        // Rebaser first: with `--restamp` on MPEG-TS it OWNS the writer, so
        // `out_stream` is None. Ordering it this way also means TS without
        // restamp falls through to the plain writer instead of silently
        // discarding every chunk (which an `is_cmaf` test alone would do).
        if let Some(r) = ts_rebaser.as_mut() {
            // Straight into ffmpeg's stdin; its stdout is drained concurrently.
            r.push(&out_bytes).await?;
        } else if let Some(w) = out_stream.as_mut() {
            // CMAF, or MPEG-TS byte-concatenated with its original PTS.
            w.write_all(&out_bytes).await?;
        }
    }

    // 5. Finalize the Multipart Cloud Upload
    if skipped_ads > 0 {
        println!("-> Removed {skipped_ads} ad segment(s).");
    }
    println!("-> Finalizing cloud upload stream...");
    if let Some(builder) = &mp4 {
        // Read the measured rate off the sample tables BEFORE `finish` consumes
        // them. `or` so the first span's measurement is kept: every span of one
        // capture comes from the same variant, and a later span that produced
        // too few samples to answer must not blank an answer already had.
        cmaf_fps = cmaf_fps.or_else(|| builder.video_frame_rate());
    }
    if let Some(builder) = &mp4 {
        // Close the progressive MP4 with its sample-table `moov` (small: the
        // sample tables, not the media).
        let w = out_stream.as_mut().context("CMAF writer open")?;
        w.write_all(&builder.finish()?).await?;
    }
    if let Some(r) = ts_rebaser.take() {
        // MPEG-TS: flush ffmpeg's tail into the sink, then close it.
        let mut sink = r.finish().await?;
        sink.shutdown().await?;
    }
    if let Some(w) = out_stream.as_mut() {
        w.shutdown().await?;
    }
    written_uris.push(span_uri(cur_span));
    if args.split_on_ad {
        println!("-> Wrote {} content span(s):", written_uris.len());
        for u in &written_uris {
            println!("     {u}");
        }
    }

    // 6. Derivatives: a 1fps proxy and/or first-I-frame thumbnail, generated by
    // reading the published file back from storage (signed URL) and streaming
    // the result alongside it. Failures don't undo a good download — they are
    // reported and the exit stays clean.
    // One set per span. In split mode the joined `file_name` was never
    // written, so deriving from it would just fail on a missing object.
    let derive_from: Vec<String> = (0..=cur_span).map(span_name).collect();
    // Exact, keyframe-aligned split points per span: the running sum of each
    // kept segment's duration, minus the final total (a trailing boundary
    // would ask for an empty segment). Reproducing the SOURCE's boundaries is
    // what makes the preview frame accurate and its TARGETDURATION match.
    // Built by mapping over the spans rather than indexing into a pre-sized Vec:
    // the index form was the crate's one standing `clippy -D warnings` failure
    // (needless_range_loop), and this function is being edited anyway.
    let span_boundaries: Vec<Vec<f64>> = (0..=cur_span)
        .map(|sp| {
            let mut t = 0.0f64;
            let mut cuts: Vec<f64> = work
                .iter()
                .filter(|w| w.span == sp)
                .map(|w| {
                    t += w.kept_secs;
                    t
                })
                .collect();
            cuts.pop(); // the end of the last segment is the end of the file
            cuts
        })
        .collect();
    // Everything below reads the source from `publish_dir` and writes to
    // `public_dir`. All three are skipped without a public destination:
    // publishing a consumer artifact back into the private mezzanine bucket
    // would defeat the split.
    let wants_public = args.proxy_1fps || args.thumbnail || args.generate_preview;
    if public_dir.is_none() && wants_public {
        eprintln!(
            "-> WARNING: --thumbnail / --proxy-1fps / --generate-preview need \
             --ais-preview-uri; skipping them."
        );
    }
    // A deliverable gets a `cdn_url` only when its object really lives in the
    // CDN-fronted bucket, which is decided by comparing the object's bucket to
    // the PREVIEW destination's. That is why a mezzanine in a private bucket
    // correctly gets none, and why a wrong CDN_BASE_URL cannot leak a private
    // object — it can only mint a preview URL that does not resolve.
    let cdn_base = notify::cdn_base(args.cdn_base_url.as_deref());
    let served_bucket = public_dir.as_deref().map(notify::bucket_of).unwrap_or("");
    if !cdn_base.is_empty() && !served_bucket.is_empty() {
        println!("-> Serving preview deliverables from {cdn_base}/ (bucket {served_bucket})");
    }
    let profile = hlspreview::HlsProfile::new(is_cmaf, media_playlist.target_duration as f64);
    reporter.set_stage(notify::STAGE_ASSEMBLY);

    // FILL THE FRAME RATE ONLY WHERE THE MANIFEST LEFT A HOLE (product ruling).
    //
    // `SourceProfile::video_profile` prefers `measured_frame_rate` over
    // `frame_rate`, so setting this unconditionally would OVERRIDE a declared
    // rate — changing a value that already publishes correctly today. That is
    // right for the live clipper, which re-times what it recorded off a sliding
    // edge, but not here: a VOD span is a STREAM COPY of the declared variant,
    // so the origin's own `FRAME-RATE` describes these exact bytes and stands.
    // The measurement therefore fills a gap and never overrules a statement.
    //
    // Measured either way (the probe is fed and the builder read regardless),
    // because the cost is a pass over bytes already in hand and having the
    // answer ready costs nothing. Only the REPORTING is conditional.
    let measured = reported_frame_rate(
        source_profile.frame_rate,
        is_cmaf,
        cmaf_fps,
        ts_fps.frame_rate(),
    );
    if let Some(fps) = measured {
        println!("-> Frame rate measured from the media: {fps:.3} fps (manifest declared none)");
    }
    source_profile.measured_frame_rate = measured;

    // WHICH §7 SHAPE THIS RUN REPORTS, decided by what it actually produced
    // rather than by the --split-on-ad flag: a split that found no ad break
    // yields one file, and that is a §7.3 completion however it was requested.
    let single_span = derive_from.len() == 1;

    for (sp, name) in derive_from.iter().enumerate() {
        // Per span, so a split output derives each piece from its own file.
        let cuts = span_boundaries.get(sp).map(|v| v.as_slice()).unwrap_or(&[]);
        // §7.1 `master_video` — the downloaded file itself. Reported for EVERY
        // span, including one whose derivatives were skipped or failed: the
        // mezzanine is what the job was asked for, and a segment notification
        // that omitted it would announce a clip with no asset.
        let source_uri = format!("{publish_dir}/{name}");
        let mut outputs = notify::Outputs {
            maxed_mp4: Some(notify::OutputFile {
                file_size: object_size(&publish_dir, name).await,
                resolution: source_profile.resolution_label(),
                // Faithful because the output is a STREAM COPY of the declared
                // variant — no re-encode outside the frame-accurate ad trim — so
                // the source's declaration still describes these bytes.
                media_profile: source_profile.video_profile(source_profile.video_codec()),
                cdn_url: notify::cdn_url(&source_uri, &cdn_base, served_bucket),
                uri: source_uri,
            }),
            ..Default::default()
        };

        if let Some(public_dir) = public_dir.as_deref() {
            // The three derivatives are INDEPENDENT -- one source object in,
            // three different objects out -- so they run concurrently per span
            // rather than one whole pass after another.
            //
            // The 1fps proxy dominates: it decodes every frame to emit one per
            // second, and measured 52-295 s (4 vCPU to 1) against ~28 s for the
            // preview and ~1 s for the thumbnail. Running them together costs
            // about what the proxy costs alone instead of the sum of all three.
            // The preview is I/O-bound (a stream copy) and the thumbnail decodes
            // a single frame, so neither takes much CPU from the proxy even at
            // `--cpu=1`.
            //
            // `join!`, NOT `try_join!`: a failed derivative is reported and
            // skipped exactly as it was when these ran in sequence. try_join
            // would cancel its siblings on the first error, turning one missing
            // thumbnail into a missing preview and proxy as well.
            //
            // Each branch RETURNS its `outputs` field rather than assigning it:
            // concurrent futures cannot share `&mut outputs`. A derivative that
            // failed contributes `None` and is simply absent from the reported
            // group — the download is unaffected and the exit stays clean.
            //
            // Log lines from the three interleave now instead of appearing in a
            // fixed order. Nothing parses them -- the wire contract is the
            // status JSON, which is assembled by field.
            let proxy = async {
                if !args.proxy_1fps {
                    return None;
                }
                match derivatives::generate_1fps_proxy(&publish_dir, public_dir, name).await {
                    Ok(p) => {
                        println!("-> Generated 1fps proxy: {public_dir}/{p}");
                        let uri = format!("{public_dir}/{p}");
                        Some(notify::OutputFile {
                            cdn_url: notify::cdn_url(&uri, &cdn_base, served_bucket),
                            file_size: object_size(public_dir, &p).await,
                            // The source height capped at the proxy's target, by
                            // the same rule the scale filter applies — and the
                            // same call `live-hls2mp4` makes for its own proxy
                            // entry. REPORTED RATHER THAN LEFT UNSET: a live
                            // segment publishes this field, so omitting it here
                            // made one clipper's `proxy_video` entry a different
                            // shape from the other's for no reason a consumer
                            // could see. The `480p_1frame_mp4` type label is a
                            // TARGET; this is what the bytes actually came out as,
                            // which is why a shorter source reports its own height
                            // rather than a fixed 480p.
                            resolution: derivatives::proxy_resolution_label(source_profile.height),
                            // NOT the source profile: this file IS re-encoded, at
                            // 1 fps, so the variant's declared frame rate and
                            // bitrate describe the input and not these bytes.
                            media_profile: Vec::new(),
                            uri,
                        })
                    }
                    Err(e) => {
                        eprintln!(
                            "-> WARNING: 1fps proxy failed for {name}: {e:#} (omitted from the \
                             reported outputs; the download is unaffected)"
                        );
                        None
                    }
                }
            };
            let thumbnail = async {
                if !args.thumbnail {
                    return None;
                }
                match derivatives::generate_thumbnail(&publish_dir, public_dir, name).await {
                    Ok(t) => {
                        println!("-> Generated thumbnail: {public_dir}/{t}");
                        let uri = format!("{public_dir}/{t}");
                        Some(notify::OutputRef {
                            // A still image has no playlist and no audio, so none
                            // of §7.1's streaming_video fields apply.
                            segment_count: None,
                            segment_duration: None,
                            audio_languages: Vec::new(),
                            video_bitrate: None,
                            cdn_url: notify::cdn_url(&uri, &cdn_base, served_bucket),
                            file_size: object_size(public_dir, &t).await,
                            // The thumbnail is extracted with no scale filter, so
                            // the variant's DECLARED dimensions describe it
                            // exactly rather than approximately.
                            width: source_profile.width,
                            height: source_profile.height,
                            uri,
                        })
                    }
                    Err(e) => {
                        eprintln!(
                            "-> WARNING: thumbnail failed for {name}: {e:#} (omitted from the \
                             reported outputs; the download is unaffected)"
                        );
                        None
                    }
                }
            };
            // HLS preview: one playlist per output file, packaged with the
            // SOURCE's profile (TS vs fMP4, and its target duration) so the
            // preview matches how the origin was segmented, not a house default.
            let preview = async {
                if !args.generate_preview {
                    return None;
                }
                match hlspreview::generate(&publish_dir, name, public_dir, profile, cuts).await {
                    // `generate` returns the preview's own facts rather than
                    // just its uri: the GENERATED playlist's target duration
                    // is not the source's (an origin may declare 10 while
                    // every part is 6.4 s), so reporting the source's would
                    // describe a different playlist. Same line shape as the
                    // live clipper, so one log format reads across both.
                    Ok(p) => {
                        println!(
                            "-> Generated HLS preview: {} ({} segments, target {}s)",
                            p.uri, p.segment_count, p.target_duration
                        );
                        Some(notify::OutputRef {
                            cdn_url: notify::cdn_url(&p.uri, &cdn_base, served_bucket),
                            segment_count: Some(p.segment_count as u64),
                            segment_duration: Some(p.target_duration.round() as u64),
                            // Declared by the manifest, carried through the same
                            // SourceProfile the media_profile is built from, so
                            // one fact has one source.
                            audio_languages: source_profile.audio_languages.to_vec(),
                            video_bitrate: source_profile.average_bandwidth,
                            uri: p.uri,
                            // §7.1's streaming_video entry has no file_size — the
                            // size that matters for a playlist is its segments'.
                            file_size: None,
                            // A playlist has no pixel dimensions of its own.
                            width: None,
                            height: None,
                        })
                    }
                    Err(e) => {
                        eprintln!("-> WARNING: HLS preview failed for {name}: {e:#}");
                        None
                    }
                }
            };
            let (proxy, thumbnail, preview) = tokio::join!(proxy, thumbnail, preview);
            outputs.mp4_1fps = proxy;
            outputs.thumbnail = thumbnail;
            outputs.hls_preview = preview;
        }

        // §7.1 boundaries and duration for THIS span. The duration is the sum of
        // what was kept (exact, and defined even for a source with no PDT); the
        // bounds are the first kept start and the last kept end, published only
        // when the source declared a wall clock at all.
        let mut span_work = work.iter().filter(|w| w.span == sp).peekable();
        let mut duration = 0.0f64;
        let mut first_start: Option<DateTime<Utc>> = None;
        let mut last_end: Option<DateTime<Utc>> = None;
        for w in span_work.by_ref() {
            duration += w.kept_secs;
            first_start.get_or_insert(w.kept_start);
            last_end = Some(w.kept_end);
        }
        let (start, end) = match playlist_has_pdt {
            true => (first_start, last_end),
            false => (None, None),
        };
        match span_report(sp, derive_from.len()) {
            SpanReport::Completion => reporter.vod_completed(duration, outputs).await,
            // 1-based, so the first span is `sequence: 1` exactly as the live
            // clipper's first content run is.
            SpanReport::Segment(close_reason) => {
                reporter
                    .vod_segment(
                        &notify::VodSpan {
                            sequence: sp as u32 + 1,
                            start,
                            end,
                            duration_sec: duration,
                            close_reason,
                        },
                        outputs,
                    )
                    .await
            }
        }
    }

    // Report what was ACTUALLY written. Announcing `final_uri` in split mode
    // named a file that does not exist, which is worse than unhelpful for
    // anything parsing this line.
    if args.split_on_ad {
        println!(
            "Process complete. {} content span(s) stored under: {publish_dir}",
            written_uris.len()
        );
    } else {
        println!("Process complete. Asset securely stored at: {final_uri}");
    }
    Ok(single_span)
}

/// Size of a published object, or `None` when it could not be read.
///
/// Storage metadata, NOT a media probe: one HEAD against an object that has
/// already been written, so it cannot change what was produced. Absent — never 0
/// — on failure, so a consumer can tell "unknown" from "empty file".
async fn object_size(output_dir: &str, name: &str) -> Option<u64> {
    match storage::object_size(output_dir, name).await {
        Ok(size) => Some(size),
        Err(e) => {
            eprintln!("-> WARNING: could not read the size of {name}: {e:#}");
            None
        }
    }
}

/// Splits an object URI into `(parent_dir_uri, file_name)`.
fn split_object_uri(uri: &str) -> Result<(String, String)> {
    let trimmed = uri.trim_end_matches('/');
    let scheme_end = trimmed.find("://").map(|i| i + 3).unwrap_or(0);
    match trimmed[scheme_end..].rfind('/') {
        Some(i) => {
            let split = scheme_end + i;
            Ok((
                trimmed[..split].to_string(),
                trimmed[split + 1..].to_string(),
            ))
        }
        None => Err(anyhow!(
            "output URI '{uri}' has no path component to derive a sub-folder from"
        )),
    }
}

/// `foo.mp4` → `foo`.
fn stem(name: &str) -> &str {
    name.rsplit_once('.').map_or(name, |(s, _)| s)
}

/// Forces the final path component of a URI to use `ext` (e.g. swaps
/// `video.mp4` -> `video.ts`). Appends the extension when none is present.
fn force_extension(uri: &str, ext: &str) -> String {
    if uri.to_ascii_lowercase().ends_with(&format!(".{ext}")) {
        return uri.to_string();
    }
    match uri.rfind('.') {
        // Only treat the trailing '.' as an extension if it is in the last path segment.
        Some(dot) if !uri[dot..].contains('/') => format!("{}.{ext}", &uri[..dot]),
        _ => format!("{uri}.{ext}"),
    }
}

/// Fetches a CMAF audio rendition's init segment and media playlist (whose
/// segments pair 1:1 by index with the video playlist's).
async fn fetch_audio_rendition(
    client: &Client,
    audio_url: &str,
) -> Result<(Vec<u8>, MediaPlaylist)> {
    let audio_playlist = match hls::fetch_playlist(client, audio_url)
        .await
        .context("downloading audio rendition playlist")?
    {
        hls::Fetched::Media(playlist) => *playlist,
        hls::Fetched::Master => return Err(anyhow!("audio rendition is not a media playlist")),
    };
    let a_map = audio_playlist
        .segments
        .iter()
        .find_map(|s| s.map.clone())
        .ok_or_else(|| anyhow!("audio rendition has no #EXT-X-MAP init segment"))?;
    let a_init_url = hls::resolve_url(audio_url, &a_map.uri)?;
    let a_init = net::fetch_bytes(client, &a_init_url, 3)
        .await
        .context("downloading audio initialization map")?;
    Ok((a_init, audio_playlist))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// THE ORCHESTRATOR'S OWN STRINGS, parsed by this binary's clap.
    ///
    /// clipping-scheduler launches the VOD job with `REMOVE_ADS: "true"` and
    /// `SPLIT_ON_AD: "<false|true>"` as ENV STRINGS, and a bool flag declared
    /// `ArgAction::Set` with `default_missing_value = "true"` is exactly the shape
    /// where `"false"` could plausibly be read as "present, therefore true". If it
    /// were, every published-asset job would silently split. This parses the real
    /// strings and pins both product cases.
    /// The process environment is GLOBAL and Rust runs tests on parallel
    /// threads, so any test that sets a var races every other test that reads
    /// one. `Args::try_parse_from` reads env at parse time — that is the whole
    /// point of this test — so the hazard is real the moment a second test
    /// parses `Args`. The lock makes env-touching tests serial with each other
    /// while leaving the rest of the suite parallel.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn the_scheduler_env_strings_parse_to_the_product_modes() {
        use clap::Parser as _;

        // Held for the whole test: the vars must not change under a concurrent
        // reader between the two parses below. `unwrap_or_else(|e| e.into_inner())`
        // rather than `unwrap()` so one failing test does not cascade into
        // "poisoned lock" failures in every other env test.
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());

        // Required args, so parsing gets as far as the flags under test.
        std::env::set_var("EXT_SOURCE_URI", "https://origin.example/vod/master.m3u8");
        std::env::set_var("AIS_SOURCE_URI", "gs://bucket/job-1");

        // VOD SOURCE: remove ads, do NOT split. One ad-free file.
        std::env::set_var("REMOVE_ADS", "true");
        std::env::set_var("SPLIT_ON_AD", "false");
        let vod = Args::try_parse_from(["vod-hls2mp4"]).expect("vod-source env parses");
        assert!(vod.remove_ads, "REMOVE_ADS=true must remove ads");
        assert!(
            !vod.split_on_ad,
            "SPLIT_ON_AD=false must NOT split — a published asset is one deliverable"
        );
        // The timeline default this branch relies on rather than sending.
        assert!(vod.restamp, "RESTAMP defaults to true on the vod job");
        // And the shape that follows from one span.
        assert_eq!(span_report(0, 1), SpanReport::Completion);

        // CATCH-UP: remove ads AND split, one file per content run.
        std::env::set_var("SPLIT_ON_AD", "true");
        let catchup = Args::try_parse_from(["vod-hls2mp4"]).expect("catch-up env parses");
        assert!(catchup.remove_ads);
        assert!(catchup.split_on_ad, "SPLIT_ON_AD=true must split");

        for k in [
            "EXT_SOURCE_URI",
            "AIS_SOURCE_URI",
            "REMOVE_ADS",
            "SPLIT_ON_AD",
        ] {
            std::env::remove_var(k);
        }
    }

    /// A PUBLISHED VOD ASSET — the `remove_ads=true, split_on_ad=false` case —
    /// reports §7.3 and nothing else.
    #[test]
    fn one_span_reports_the_section_7_3_completion() {
        assert_eq!(span_report(0, 1), SpanReport::Completion);
    }

    /// AND SO DOES A SPLIT THAT FOUND NO BREAK. The shape follows what the run
    /// PRODUCED, not what it was asked to do: a catch-up window containing no ad
    /// break yields one file, which is a completion, not segment 1 of 1.
    #[test]
    fn a_split_that_produced_one_file_still_reports_a_completion() {
        // Same call the loop makes for a --split-on-ad run whose source had no
        // ad break: total is 1 because Pass 1 produced one span.
        assert_eq!(span_report(0, 1), SpanReport::Completion);
        // Defensive: a download that produced nothing never enters the loop, so
        // this is unreachable in practice — pinned so it can never become a
        // panic or a segment with no file behind it.
        assert_eq!(span_report(0, 0), SpanReport::Completion);
    }

    /// A CATCH-UP SPLIT: every span but the last closed on the break that follows
    /// it, the last on the end of the window. Both spellings are the live
    /// clipper's own, so one consumer reads both clippers.
    #[test]
    fn split_spans_close_on_the_break_except_the_last() {
        assert_eq!(
            span_report(0, 2),
            SpanReport::Segment(notify::CLOSE_AD_BREAK_START)
        );
        assert_eq!(
            span_report(1, 2),
            SpanReport::Segment(notify::CLOSE_SCHEDULE_END)
        );

        // Many: only the final index may report schedule_end.
        let total = 5;
        for span in 0..total - 1 {
            assert_eq!(
                span_report(span, total),
                SpanReport::Segment(notify::CLOSE_AD_BREAK_START),
                "span {span} of {total} is not the last and must close on the break"
            );
        }
        assert_eq!(
            span_report(total - 1, total),
            SpanReport::Segment(notify::CLOSE_SCHEDULE_END)
        );
    }

    /// EXACTLY ONE COMPLETION PER RUN, whichever shape it took — the property
    /// that keeps `run`'s return value (which suppresses the caller's second
    /// `completed`) honest at every span count.
    #[test]
    fn a_run_reports_exactly_one_completion() {
        for total in 1..=6 {
            let completions = (0..total)
                .filter(|&s| span_report(s, total) == SpanReport::Completion)
                .count();
            let expected = usize::from(total == 1);
            assert_eq!(
                completions, expected,
                "{total} span(s) produced {completions} §7.3 completion(s)"
            );
        }
    }

    /// A MANIFEST THAT SPOKE IS NEVER CONTRADICTED. `video_profile` prefers
    /// `measured_frame_rate`, so a measurement returned here would silently
    /// replace a declared rate that publishes correctly today — the exact
    /// regression the ruling forbids. Asserted for BOTH containers, and with a
    /// measurement that disagrees, so a future "prefer the more accurate one"
    /// edit fails here rather than in production §7 output.
    #[test]
    fn a_declared_frame_rate_is_never_overruled_by_a_measurement() {
        assert_eq!(
            reported_frame_rate(Some(25.0), false, None, Some(29.97)),
            None,
            "MPEG-TS: a declared 25 fps was overruled by the probe"
        );
        assert_eq!(
            reported_frame_rate(Some(25.0), true, Some(29.97), None),
            None,
            "CMAF: a declared 25 fps was overruled by the builder"
        );
    }

    /// THE HOLE THIS CLOSES: `FRAME-RATE` is optional in RFC 8216, and an origin
    /// that omits it used to yield a §7.1 `media_profile` with no frame rate at
    /// all — a VOD capture reporting less than a live capture of the same source.
    #[test]
    fn an_undeclared_frame_rate_is_filled_from_the_media() {
        assert_eq!(
            reported_frame_rate(None, false, None, Some(29.97)),
            Some(29.97)
        );
        assert_eq!(
            reported_frame_rate(None, true, Some(50.0), None),
            Some(50.0)
        );
    }

    /// EACH CONTAINER READS ITS OWN PROBE ONLY. Only one of the two can ever have
    /// an answer for a given run, but this pins that the choice is the container's
    /// rather than a fallback between them: were it `cmaf.or(ts)`, a CMAF run
    /// whose builder could not answer would publish an MPEG-TS reading of
    /// different bytes as though it described the output.
    #[test]
    fn the_container_selects_the_probe_rather_than_falling_back() {
        assert_eq!(
            reported_frame_rate(None, true, None, Some(29.97)),
            None,
            "CMAF fell back to the MPEG-TS probe"
        );
        assert_eq!(
            reported_frame_rate(None, false, Some(50.0), None),
            None,
            "MPEG-TS fell back to the CMAF builder"
        );
    }

    /// NEITHER SOURCE ANSWERED: the run publishes no frame rate, which is what
    /// every VOD run did before this fallback existed. Silence is the correct
    /// output — a fabricated default would be indistinguishable from a measured
    /// one downstream.
    #[test]
    fn nothing_is_reported_when_nothing_could_be_measured() {
        assert_eq!(reported_frame_rate(None, false, None, None), None);
        assert_eq!(reported_frame_rate(None, true, None, None), None);
    }

    /// The orchestrator passes `--ais-source-uri` as a bucket PREFIX
    /// (`gs://<bucket>/<job_id>`), not an object URI. Both container branches must
    /// name a file from it, or the mezzanine lands as an extensionless object that
    /// the §7 `master_video.url` then points at.
    ///
    /// The MPEG-TS half has always forced `.ts`; the CMAF half did not, which is
    /// the case this pins. A job id contains no dot, so nothing in it can be
    /// mistaken for an extension.
    #[test]
    fn a_bucket_prefix_destination_gains_the_container_extension() {
        let prefix = "gs://sbox2wrkr-ais-source/71a22135-7af0-46ca-bacf-c8e572a5b96f";
        assert_eq!(
            force_extension(prefix, "mp4"),
            "gs://sbox2wrkr-ais-source/71a22135-7af0-46ca-bacf-c8e572a5b96f.mp4"
        );
        assert_eq!(
            force_extension(prefix, "ts"),
            "gs://sbox2wrkr-ais-source/71a22135-7af0-46ca-bacf-c8e572a5b96f.ts"
        );
        // An object URI that already names the right container is left alone,
        // and one naming the other container is corrected rather than appended to.
        assert_eq!(
            force_extension("gs://b/j/clip.mp4", "mp4"),
            "gs://b/j/clip.mp4"
        );
        assert_eq!(
            force_extension("gs://b/j/clip.mp4", "ts"),
            "gs://b/j/clip.ts"
        );
        // A dot in a PARENT path segment is not an extension.
        assert_eq!(
            force_extension("gs://b/v1.2/job-9", "mp4"),
            "gs://b/v1.2/job-9.mp4"
        );
    }

    /// The per-job folder is resolved idempotently at BOTH destinations, so a base
    /// that already ends in the event id is used as-is. The scheduler passes such a
    /// base for `--ais-preview-uri`, and appending unconditionally buried a job one
    /// level deeper as `<bucket>/<job>/<job>/`.
    #[test]
    fn the_event_folder_is_not_doubled_when_the_base_already_names_it() {
        let job = "71a22135-7af0-46ca-bacf-c8e572a5b96f";
        assert_eq!(
            storage::event_folder(&format!("gs://preview/{job}"), job),
            format!("gs://preview/{job}")
        );
        // The mezzanine's parent is the bare bucket, so there the folder IS added.
        assert_eq!(
            storage::event_folder("gs://sbox2wrkr-ais-source", job),
            format!("gs://sbox2wrkr-ais-source/{job}")
        );
    }

    /// `span_name`'s numbering, asserted through the naming rule it implements:
    /// the joined output keeps the requested name, split spans are `-001`, `-002`,
    /// … so they sort correctly and never collide with it. The §7 `sequence` is
    /// the span index + 1, which is what ties `<stem>-001.mp4` to `sequence: 1`.
    #[test]
    fn split_span_names_are_one_based_and_sort() {
        let name = |span: usize| format!("{}-{:03}.{}", "clip", span + 1, "mp4");
        assert_eq!(name(0), "clip-001.mp4");
        assert_eq!(name(9), "clip-010.mp4");
        let mut names: Vec<String> = (0..11).map(name).collect();
        let sorted = {
            let mut c = names.clone();
            c.sort();
            c
        };
        names.sort_by_key(|n| n.clone());
        assert_eq!(names, sorted, "zero padding keeps lexical order == numeric");
    }
}
