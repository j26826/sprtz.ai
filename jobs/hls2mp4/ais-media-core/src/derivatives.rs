//! Post-publish derivatives of a finished output file: a constant-1fps H.264
//! proxy (`<stem>_proxy_1fps.mp4`) and a first-I-frame
//! JPEG thumbnail (`<stem>.jpg`).
//!
//! Serverless-friendly: the source object is read back **from storage** via a
//! signed HTTPS URL (`s3://`/`gs://`) or a `file:` URL — never staged on local
//! disk — and the derivative streams from FFmpeg's stdout straight back to the
//! destination as a multipart upload. Requires the bundled `ffmpeg` CLI
//! (`FFMPEG_BIN`, else `ffmpeg` on `PATH`).

use std::process::Stdio;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use crate::storage::{self, OutputTarget};

/// Signed URLs stay valid for this long — comfortably longer than either
/// derivative takes to produce (the 1fps transcode is decode-bound).
const URL_TTL: Duration = Duration::from_secs(6 * 3600);

/// The `ffmpeg` binary to invoke (`FFMPEG_BIN`, else `ffmpeg` on `PATH`).
fn ffmpeg_bin() -> String {
    std::env::var("FFMPEG_BIN").unwrap_or_else(|_| "ffmpeg".to_string())
}

/// `foo.mp4` → `foo_proxy_1fps.mp4` (always MP4).
pub fn proxy_name(source_name: &str) -> String {
    format!("{}_proxy_1fps.mp4", stem(source_name))
}

/// `foo.mp4` → `foo.jpg`.
pub fn thumbnail_name(source_name: &str) -> String {
    format!("{}.jpg", stem(source_name))
}

fn stem(name: &str) -> &str {
    name.rsplit_once('.').map_or(name, |(s, _)| s)
}

/// The flag that names the output container. Required for every derivative
/// because they all write to `pipe:1`, which has no filename extension for
/// FFmpeg to infer a format from.
const OUTPUT_FORMAT_FLAG: &str = "-f";

/// The analysis proxy's target height in pixels — §5.2's "480p / 1fps analysis
/// proxy".
///
/// STATED ONCE. This number used to live in three independent places: the
/// notifier published the label `480p`, the notifier published the type
/// `480p_1frame_mp4`, and this module scaled to nothing at all — so every proxy
/// went out labelled 480p at whatever height the source happened to be. The
/// filter below and [`proxy_resolution_label`] are both built from this
/// constant, and a test asserts the three still agree.
pub const PROXY_HEIGHT: u64 = 480;

/// §7.1 `proxy_video[].type` for this deliverable.
///
/// A literal rather than a derived string because it names the deliverable
/// CLASS from §5.2's vocabulary — the slot in the contract this file fills —
/// not a measurement of the file. What the file actually is is reported by
/// [`proxy_resolution_label`].
pub const PROXY_ENTRY_TYPE: &str = "480p_1frame_mp4";

/// The `resolution` label for a proxy generated from a source of
/// `source_height`.
///
/// Derived from the SAME rule the scale filter applies (`min(PROXY_HEIGHT,
/// ih)`) rather than restating the target, so the label describes the file
/// instead of the intent: a source already below 480p is not upscaled, and so
/// is not labelled 480p either.
///
/// `None` when the manifest declared no height — a label with nothing behind it
/// is a guess dressed as a measurement, which is the same reason
/// `SourceProfile::resolution_label` refuses to invent one.
pub fn proxy_resolution_label(source_height: Option<u64>) -> Option<String> {
    source_height.map(|h| format!("{}p", h.min(PROXY_HEIGHT)))
}

/// FFmpeg arguments for the constant-1fps H.264 MP4 proxy.
///
/// Fragmented output so MP4 can stream to a pipe (no seek-back); every frame is
/// its own fragment boundary at 1 fps, keeping the proxy seekable.
///
/// `scale=-2:'min(480,ih)'` is what makes the published `480p` true. The height
/// is capped at [`PROXY_HEIGHT`] and the width is derived (`-2` rounds it to an
/// even number, which yuv420p/libx264 require), so the source's aspect ratio
/// survives the downscale. `min` rather than a flat target because a source
/// ALREADY below 480p must not be upscaled: that invents pixels and inflates
/// the very file whose purpose is to be cheap to decode.
///
/// AUDIO IS KEPT, and it is not incidental to this file's job. The clipper
/// publishes exactly one `proxy_video` entry, and N8N's preview selection takes
/// the first proxy whose type does not say `1frame` — finding none, it falls
/// back to this one. So this doubles as the preview a producer plays, and the
/// `-an` that used to sit here is what made every one of them silent.
///
/// No `-c:a`: the MP4 muxer's default audio encoder is already AAC and its
/// default bitrate is what we would have pinned, so naming either only risks
/// overriding a source that wants something else (mono, or a lower rate).
/// `-vf` decimates video alone, so the audio track stays continuous under 1 fps
/// video.
///
/// THE ENCODER THIS LEANS ON IS NOT GATED EVERYWHERE. This module is shared:
/// [`generate_1fps_proxy`] is called by `live-hls2mp4` AND `vod-hls2mp4`, so both
/// now encode audio here, but only the former's Dockerfile has the capability
/// stage that fails the build on a missing `require encoder aac`. `vod-hls2mp4`
/// copies FFmpeg straight out of the base image with no check, so there a build
/// that dropped the encoder would lose the proxy at run time instead — silently,
/// since derivatives are best-effort. That exposure is NOT new: both crates
/// already reach `splice::reencode_trim_*`, which encodes AAC unconditionally.
/// Do not read one Dockerfile's assertion as covering the other.
const PROXY_1FPS_ARGS: &[&str] = &[
    "-vf",
    "fps=1,scale=-2:'min(480,ih)'",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "frag_keyframe+empty_moov+default_base_moof",
    OUTPUT_FORMAT_FLAG,
    "mp4",
];

/// FFmpeg arguments for the single-frame JPEG thumbnail.
///
/// A live MPEG-TS source carries an audio stream that an image muxer cannot
/// hold, so `-an` drops it explicitly rather than relying on FFmpeg's automatic
/// stream selection to infer that from the muxer's default codecs.
///
/// `image2` does write a lone image to `pipe:1` (verified against both a
/// full FFmpeg and the shared minimal build), but the muxer *and* the `mjpeg`
/// encoder have to be compiled in. `ffmpeg/Dockerfile` is
/// `--disable-everything` plus an allow-list, so neither is implied:
/// `live-hls2mp4/Dockerfile` asserts both at build time, because a build
/// missing either loses the thumbnail silently at run time — a run that exits 0
/// with the deliverable absent.
const THUMBNAIL_ARGS: &[&str] = &[
    "-an",
    "-frames:v",
    "1",
    "-c:v",
    "mjpeg",
    "-q:v",
    "3",
    OUTPUT_FORMAT_FLAG,
    "image2",
];

/// Generates the constant-1fps H.264 MP4 proxy of `source_name` (read from
/// `src_dir_uri`) as `<stem>_proxy_1fps.mp4` under `out_dir_uri`.
///
/// The two directories are separate because the source recording and its
/// consumer-facing derivatives live in different buckets: the mezzanine stays
/// private, the proxy is published.
pub async fn generate_1fps_proxy(
    src_dir_uri: &str,
    out_dir_uri: &str,
    source_name: &str,
) -> Result<String> {
    let out_name = proxy_name(source_name);
    run_ffmpeg_to_object(
        src_dir_uri,
        out_dir_uri,
        source_name,
        &out_name,
        PROXY_1FPS_ARGS,
    )
    .await
    .with_context(|| format!("generating the 1fps proxy of {source_name}"))?;
    Ok(out_name)
}

/// Extracts the first I-frame of `source_name` (read from `src_dir_uri`) as a
/// JPEG named `<stem>.jpg` under `out_dir_uri`. Outputs of these tools always
/// begin at a keyframe, so the first decoded frame is the first I-frame.
pub async fn generate_thumbnail(
    src_dir_uri: &str,
    out_dir_uri: &str,
    source_name: &str,
) -> Result<String> {
    let out_name = thumbnail_name(source_name);
    run_ffmpeg_to_object(
        src_dir_uri,
        out_dir_uri,
        source_name,
        &out_name,
        THUMBNAIL_ARGS,
    )
    .await
    .with_context(|| format!("generating the thumbnail of {source_name}"))?;
    Ok(out_name)
}

/// Runs `ffmpeg -i <signed-url-of-source> <args> pipe:1`, reading the source
/// from `src_dir_uri` and streaming stdout to `out_name` under `out_dir_uri`
/// (which may be a different bucket). The object is only committed when FFmpeg
/// exits cleanly.
async fn run_ffmpeg_to_object(
    src_dir_uri: &str,
    out_dir_uri: &str,
    source_name: &str,
    out_name: &str,
    args: &[&str],
) -> Result<()> {
    let inputs = storage::ffmpeg_input_urls(
        src_dir_uri,
        std::slice::from_ref(&source_name.to_string()),
        URL_TTL,
    )
    .await
    .context("resolving the source object to an FFmpeg input")?;
    let input = inputs.urls.first().ok_or_else(|| anyhow!("no input URL"))?;

    let bin = ffmpeg_bin();
    let mut cmd = Command::new(&bin);
    cmd.args([
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,pipe",
    ]);
    // Bearer auth for plain (unsigned) GCS object URLs.
    if let Some(header) = &inputs.header {
        cmd.args(["-headers", header]);
    }
    let mut child = cmd
        .args(["-i", input])
        .args(args)
        .arg("pipe:1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("spawning {bin} (installed / FFMPEG_BIN set?)"))?;

    // Drain stderr concurrently so the pipe never fills and blocks FFmpeg.
    let mut stderr = child.stderr.take().expect("stderr piped");
    let err_task = tokio::spawn(async move {
        let mut buf = String::new();
        let _ = stderr.read_to_string(&mut buf).await;
        buf
    });

    let mut stdout = child.stdout.take().expect("stdout piped");
    let target = OutputTarget::from_uri(out_dir_uri)?;
    let mut writer = target.writer(out_name);
    tokio::io::copy(&mut stdout, &mut writer)
        .await
        .context("streaming the derivative to storage")?;

    let status = child.wait().await?;
    let errs = err_task.await.unwrap_or_default();
    if !status.success() {
        // Dropping the writer without shutdown leaves no truncated object.
        return Err(ffmpeg_failure(&status.to_string(), &errs));
    }
    tokio::io::AsyncWriteExt::shutdown(&mut writer).await?;
    Ok(())
}

/// The error for a failed FFmpeg run, on ONE line.
///
/// Callers log this to stderr, where a log collector splits on newlines and
/// files every line after the first as its own entry — so FFmpeg's multi-line
/// stderr would strand the actual cause in separate entries, away from the
/// `-> WARNING:` line that names the derivative and the source object. That is
/// how the lost-thumbnail run read: the warning entry ended at "ffmpeg failed
/// (exit status: 234):" and the reason sat in four unrelated-looking entries.
fn ffmpeg_failure(status: &str, errs: &str) -> anyhow::Error {
    anyhow!("ffmpeg failed ({status}): {}", one_line(errs))
}

/// Flattens multi-line FFmpeg stderr into one ` | `-joined line so the whole
/// diagnosis survives as a single log entry. Empty input yields `<no output>`
/// rather than an empty tail that reads as a message with no reason.
fn one_line(errs: &str) -> String {
    let joined = errs
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join(" | ");
    if joined.is_empty() {
        "<no output>".to_string()
    } else {
        joined
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The value following `flag` in an argument list.
    fn arg_value<'a>(args: &'a [&'a str], flag: &str) -> Option<&'a str> {
        let i = args.iter().position(|a| *a == flag)?;
        args.get(i + 1).copied()
    }

    /// Every derivative writes to `pipe:1`, which carries no filename extension
    /// for FFmpeg to infer a container from, so an explicit `-f` is mandatory.
    #[test]
    fn every_derivative_names_its_output_format() {
        for args in [PROXY_1FPS_ARGS, THUMBNAIL_ARGS] {
            assert!(
                arg_value(args, OUTPUT_FORMAT_FLAG).is_some(),
                "{args:?} must pass {OUTPUT_FORMAT_FLAG} <format>"
            );
        }
    }

    /// The published height, the label rule and the filter that enforces them
    /// must all name the same number.
    ///
    /// REGRESSION GUARD, and the bug it guards was live: the filter was `fps=1`
    /// alone — no scaling whatsoever — while the notifier published
    /// `resolution: "480p"` and `type: "480p_1frame_mp4"` unconditionally. Every
    /// proxy of a 1080p source went out as a 1080p file labelled 480p, and a
    /// consumer had no way to tell it was being misinformed.
    #[test]
    fn the_proxy_label_matches_the_filter_that_produces_it() {
        let vf = arg_value(PROXY_1FPS_ARGS, "-vf").expect("the proxy sets -vf");
        assert!(vf.contains("fps=1"), "the proxy must be decimated to 1 fps: {vf}");
        assert!(
            vf.contains(&format!("min({PROXY_HEIGHT},ih)")),
            "the scale filter must cap the height at PROXY_HEIGHT: {vf}"
        );
        assert!(
            vf.contains("scale=-2:"),
            "the width must be derived and even, not fixed: {vf}"
        );
        assert!(
            PROXY_ENTRY_TYPE.starts_with(&format!("{PROXY_HEIGHT}p")),
            "the §7.1 type must name the height this module produces"
        );
    }

    /// The label reports the FILE, not the target.
    ///
    /// The cap is the whole point: `min` in the filter means a sub-480p source
    /// is passed through untouched, so labelling it `480p` would reintroduce the
    /// same lie in a narrower case. An undeclared height yields no label rather
    /// than a default.
    #[test]
    fn proxy_resolution_label_reports_what_was_produced() {
        assert_eq!(proxy_resolution_label(Some(1080)).as_deref(), Some("480p"));
        assert_eq!(proxy_resolution_label(Some(480)).as_deref(), Some("480p"));
        assert_eq!(proxy_resolution_label(Some(360)).as_deref(), Some("360p"));
        assert_eq!(proxy_resolution_label(None), None);
    }

    /// The thumbnail is one MJPEG frame written to a pipe. Regression guard for
    /// the run that produced no `.jpg`: the shared FFmpeg build had neither the
    /// `image2` muxer nor the `mjpeg` encoder, and `-f`/`-c:v` are what
    /// `live-hls2mp4/Dockerfile` asserts the build provides — so if either value
    /// changes here, that assertion must change with it.
    #[test]
    fn thumbnail_args_request_one_mjpeg_frame_to_a_pipe_capable_muxer() {
        assert_eq!(arg_value(THUMBNAIL_ARGS, "-frames:v"), Some("1"));
        assert_eq!(arg_value(THUMBNAIL_ARGS, "-c:v"), Some("mjpeg"));
        assert!(THUMBNAIL_ARGS.contains(&"-an"));

        // `image2` writes a lone image to a pipe; `image2pipe`/`mjpeg` are the
        // other muxers that can. Anything else needs a numbered filename
        // pattern and fails on `pipe:1` with "Invalid argument".
        let muxer = arg_value(THUMBNAIL_ARGS, OUTPUT_FORMAT_FLAG).expect("thumbnail sets -f");
        assert!(
            ["image2", "image2pipe", "mjpeg"].contains(&muxer),
            "{muxer} cannot write a single image to pipe:1"
        );
    }

    /// THE PROXY CARRIES AUDIO. Worth a guard because `-an` sat here from the
    /// module's first commit and nothing caught it: every proxy went out silent,
    /// and N8N serves this same file as the producer's preview (its selector
    /// looks for a proxy whose type does not say `1frame`, finds none, and falls
    /// back to this one).
    ///
    /// Asserts the ABSENCE of `-an` rather than the presence of a `-c:a`,
    /// because the fix is to let the muxer's own AAC default apply — pinning a
    /// codec or bitrate here would override a source that wants neither.
    #[test]
    fn the_proxy_keeps_the_audio_track() {
        assert!(
            !PROXY_1FPS_ARGS.contains(&"-an"),
            "the proxy doubles as the preview; disabling audio makes it silent"
        );
        // The thumbnail is the opposite case and must stay that way — an image
        // muxer cannot hold an audio stream at all.
        assert!(THUMBNAIL_ARGS.contains(&"-an"));
    }

    /// The proxy must stay fragmented: a plain MP4 muxer seeks back to patch the
    /// `moov`, which `pipe:1` cannot do.
    #[test]
    fn proxy_args_stream_fragmented_mp4() {
        assert_eq!(arg_value(PROXY_1FPS_ARGS, OUTPUT_FORMAT_FLAG), Some("mp4"));
        let flags = arg_value(PROXY_1FPS_ARGS, "-movflags").expect("proxy sets -movflags");
        assert!(flags.contains("empty_moov"), "{flags} must avoid seek-back");
        assert!(flags.contains("frag_keyframe"), "{flags} must fragment");
    }

    /// FFmpeg's stderr is multi-line; a log collector would file every line
    /// after the first as a separate entry, stranding the cause away from the
    /// warning that names the derivative.
    #[test]
    fn ffmpeg_stderr_collapses_to_one_line() {
        let raw = "[AVFormatContext @ 0x1] Requested output format 'image2' is not known.\n\
                   [out#0 @ 0x2] Error initializing the muxer for pipe:1: Invalid argument\n\
                   \n\
                   Error opening output files: Invalid argument\n";
        let collapsed = one_line(raw);
        assert!(!collapsed.contains('\n'), "{collapsed} must be one line");
        assert_eq!(
            collapsed,
            "[AVFormatContext @ 0x1] Requested output format 'image2' is not known. | \
             [out#0 @ 0x2] Error initializing the muxer for pipe:1: Invalid argument | \
             Error opening output files: Invalid argument"
        );
    }

    /// The whole point of the reporting change: one log entry that names the
    /// derivative, the source object AND the underlying cause. Built from the
    /// real stderr of execution `live-hls2mp4-x6fgp`, whose thumbnail went
    /// missing while the run exited 0.
    #[test]
    fn a_failed_derivative_reports_its_name_object_and_cause_on_one_line() {
        let source = "lc-2min-20260806T062518-20260806T062548Z-20260806T062718Z.ts";
        let stderr = "[AVFormatContext @ 0x7f9756bb02c0] Requested output format 'image2' is not known.\n\
                      [out#0 @ 0x7f9755d32180] Error initializing the muxer for pipe:1: Invalid argument\n\
                      Error opening output file pipe:1.\n\
                      Error opening output files: Invalid argument";
        let err = ffmpeg_failure("exit status: 234", stderr)
            .context(format!("generating the thumbnail of {source}"));

        // Exactly how live-hls2mp4's publish loop renders it.
        let line = format!(
            "-> WARNING: {err:#} (omitted from the reported outputs; the recording is unaffected)"
        );

        assert!(!line.contains('\n'), "must be one log entry: {line}");
        assert!(
            line.contains("thumbnail"),
            "must name the derivative: {line}"
        );
        assert!(line.contains(source), "must name the source object: {line}");
        assert!(
            line.contains("Requested output format 'image2' is not known."),
            "must carry the underlying cause: {line}"
        );
        println!("{line}");
    }

    #[test]
    fn empty_ffmpeg_stderr_is_reported_explicitly() {
        assert_eq!(one_line(""), "<no output>");
        assert_eq!(one_line("  \n \n"), "<no output>");
    }

    #[test]
    fn derivative_names() {
        assert_eq!(proxy_name("ev-a-b.mp4"), "ev-a-b_proxy_1fps.mp4");
        assert_eq!(proxy_name("ev-a-b.ts"), "ev-a-b_proxy_1fps.mp4");
        assert_eq!(thumbnail_name("ev-a-b.mp4"), "ev-a-b.jpg");
        assert_eq!(thumbnail_name("ev-a-b.ts"), "ev-a-b.jpg");
        assert_eq!(thumbnail_name("noext"), "noext.jpg");
    }
}
