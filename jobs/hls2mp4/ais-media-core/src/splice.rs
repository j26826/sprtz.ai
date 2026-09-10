//! Frame-accurate boundary splicing ("smart splice") for SCTE-35 cue points.
//!
//! Removing ads at segment granularity is only as accurate as the packager's
//! segment conditioning. For **frame accuracy** the segment that straddles a
//! cue is *re-encoded* with an exact trim (decode → cut at the splice frame →
//! libx264/AAC), while every other segment stays stream-copied — the quality
//! cost is confined to at most one segment per break edge (the deliberate,
//! sanctioned exception to the zero-transcode policy).
//!
//! Two entry points, one per container:
//! - [`reencode_trim_ts`] — MPEG-TS in/out. `-copyts` keeps the output in the
//!   source PTS domain so the piece byte-concatenates seamlessly between
//!   stream-copied segments.
//! - [`reencode_trim_cmaf`] — CMAF in (video + optional separate audio
//!   rendition), fragmented MP4 out with the video track timescale forced to
//!   the source's, ready for ingestion into the progressive muxer as a second
//!   sample description.
//!
//! The boundary segment (a few MB) is staged in a temp dir — the one narrow
//! exception to "nothing on local disk", far below serverless /tmp limits.

use std::path::Path;
use std::process::Stdio;

use anyhow::{anyhow, Context, Result};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, Command};
use tokio::task::JoinHandle;

/// Which side of the splice to keep, with offsets in **seconds relative to
/// the segment start**.
#[derive(Debug, Clone, Copy)]
pub enum Keep {
    /// Keep `[0, until)` — the break starts mid-segment.
    Before { until: f64 },
    /// Keep `[from, end)` — the break ends mid-segment.
    From { from: f64 },
}

fn ffmpeg_bin() -> String {
    std::env::var("FFMPEG_BIN").unwrap_or_else(|_| "ffmpeg".to_string())
}

fn ffprobe_bin() -> String {
    std::env::var("FFPROBE_BIN").unwrap_or_else(|_| {
        // Default to a sibling of the ffmpeg binary.
        let ff = ffmpeg_bin();
        Path::new(&ff)
            .parent()
            .map(|d| d.join("ffprobe").to_string_lossy().into_owned())
            .unwrap_or_else(|| "ffprobe".to_string())
    })
}

async fn run(cmd: &mut Command, what: &str) -> Result<Vec<u8>> {
    let out = cmd
        .stdin(Stdio::null())
        .output()
        .await
        .with_context(|| format!("spawning {what}"))?;
    if !out.status.success() {
        return Err(anyhow!(
            "{what} failed ({}):\n{}",
            out.status,
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(out.stdout)
}

/// Container `start_time` (seconds) of the given media file, via ffprobe —
/// the base FFmpeg normalizes input timestamps against.
async fn start_time_secs(path: &Path) -> Result<f64> {
    let out = run(
        Command::new(ffprobe_bin()).args([
            "-v",
            "error",
            "-show_entries",
            "format=start_time",
            "-of",
            "default=nw=1:nk=1",
            path.to_str().context("non-utf8 temp path")?,
        ]),
        "ffprobe",
    )
    .await?;
    String::from_utf8_lossy(&out)
        .trim()
        .parse::<f64>()
        .context("no container start_time in boundary segment")
}

/// The output-side trim options for `keep` plus the `-output_ts_offset` that
/// restores the source timestamp domain. FFmpeg normalizes input timestamps
/// to zero, so the trim bounds are segment-relative; an output-side `-ss`
/// additionally re-zeroes the kept output, hence the `from`-shifted offset.
fn trim_args(keep: Keep, base: f64) -> Vec<String> {
    match keep {
        Keep::Before { until } => vec![
            "-to".into(),
            format!("{until:.6}"),
            "-output_ts_offset".into(),
            format!("{base:.6}"),
        ],
        Keep::From { from } => vec![
            "-ss".into(),
            format!("{from:.6}"),
            "-output_ts_offset".into(),
            format!("{:.6}", base + from),
        ],
    }
}

/// The source segment's `(video_pid, audio_pid)`, so the re-encoded piece can
/// reuse them — demuxers ignore mid-stream packets on unknown PIDs, so a
/// piece muxed on FFmpeg's default PIDs would be invisible after
/// concatenation.
async fn ts_pids(path: &Path) -> Result<(Option<String>, Option<String>)> {
    let out = run(
        Command::new(ffprobe_bin()).args([
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,id",
            "-of",
            "csv=p=0",
            path.to_str().context("non-utf8 temp path")?,
        ]),
        "ffprobe",
    )
    .await?;
    let (mut video, mut audio) = (None, None);
    for line in String::from_utf8_lossy(&out).lines() {
        let mut parts = line.trim().split(',');
        match (parts.next(), parts.next()) {
            (Some("video"), Some(pid)) if video.is_none() => video = Some(pid.to_string()),
            (Some("audio"), Some(pid)) if audio.is_none() => audio = Some(pid.to_string()),
            _ => {}
        }
    }
    Ok((video, audio))
}

/// Re-encodes the kept portion of one MPEG-TS segment with a frame-accurate
/// trim, preserving the source PTS domain and stream PIDs so the result
/// splices seamlessly between stream-copied segments. Returns the TS bytes.
pub async fn reencode_trim_ts(segment: &[u8], keep: Keep) -> Result<Vec<u8>> {
    let dir = tempdir()?;
    let in_path = dir.join("seg.ts");
    tokio::fs::write(&in_path, segment).await?;

    // Restore the source PTS domain after the (normalized-domain) trim so the
    // piece byte-concatenates seamlessly between stream-copied segments.
    let base = start_time_secs(&in_path).await?;
    let (video_pid, audio_pid) = ts_pids(&in_path).await?;

    let mut cmd = Command::new(ffmpeg_bin());
    cmd.args(["-hide_banner", "-loglevel", "error", "-i"])
        .arg(&in_path)
        .args(trim_args(keep, base))
        .args([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]);
    // Output stream order is video then audio; pin both to the source PIDs.
    if let Some(v) = &video_pid {
        cmd.args(["-streamid", &format!("0:{v}")]);
    }
    if let Some(a) = &audio_pid {
        cmd.args(["-streamid", &format!("1:{a}")]);
    }
    cmd.args(["-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"]);
    let bytes = run(&mut cmd, "ffmpeg (TS boundary re-encode)").await?;
    cleanup(dir).await;
    println!(
        "-> boundary piece ({:?}): {} bytes re-encoded",
        keep,
        bytes.len()
    );
    if bytes.is_empty() {
        return Err(anyhow!("boundary re-encode produced no output"));
    }
    Ok(bytes)
}

/// Re-encodes the kept portion of one CMAF segment pair with a frame-accurate
/// trim, producing a **fragmented MP4** (init + fragments) whose video track
/// timescale matches `video_timescale`, ready for
/// `mp4mux::ProgressiveMp4::push_encoded`. `audio`, when present, is the
/// paired audio-rendition segment; both tracks are trimmed at the same point
/// and re-encoded (x264 / AAC).
pub async fn reencode_trim_cmaf(
    video_init: &[u8],
    video_seg: &[u8],
    audio: Option<(&[u8], &[u8])>,
    keep: Keep,
    video_timescale: u32,
) -> Result<Vec<u8>> {
    let dir = tempdir()?;
    // Init + fragment concatenated = a self-contained playable fMP4.
    let v_path = dir.join("v.mp4");
    let mut v = video_init.to_vec();
    v.extend_from_slice(video_seg);
    tokio::fs::write(&v_path, v).await?;
    let a_path = if let Some((a_init, a_seg)) = audio {
        let p = dir.join("a.mp4");
        let mut a = a_init.to_vec();
        a.extend_from_slice(a_seg);
        tokio::fs::write(&p, a).await?;
        Some(p)
    } else {
        None
    };

    // The piece's absolute timestamps are irrelevant to the progressive muxer
    // (sample tables carry durations), but keep the domain consistent anyway.
    let base = start_time_secs(&v_path).await?;

    let out_path = dir.join("out.mp4");
    let mut cmd = Command::new(ffmpeg_bin());
    cmd.args(["-hide_banner", "-loglevel", "error", "-i"]).arg(&v_path);
    if let Some(a) = &a_path {
        cmd.arg("-i").arg(a);
    }
    cmd.args(trim_args(keep, base));
    cmd.args([
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]);
    if a_path.is_some() {
        cmd.args(["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"]);
    }
    cmd.args([
        "-video_track_timescale",
        &video_timescale.to_string(),
        "-movflags",
        "empty_moov+default_base_moof+frag_keyframe",
        "-f",
        "mp4",
    ])
    .arg(&out_path);
    run(&mut cmd, "ffmpeg (CMAF boundary re-encode)").await?;

    let bytes = tokio::fs::read(&out_path)
        .await
        .context("reading re-encoded boundary piece")?;
    cleanup(dir).await;
    Ok(bytes)
}

/// A per-call scratch dir under the system temp root.
fn tempdir() -> Result<std::path::PathBuf> {
    let dir = std::env::temp_dir().join(format!(
        "ais-splice-{}-{:x}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

async fn cleanup(dir: std::path::PathBuf) {
    let _ = tokio::fs::remove_dir_all(dir).await;
}

/// A STREAMING MPEG-TS timeline rebaser: segments go in, a continuous
/// zero-based TS comes out, and nothing is ever held on disk or in full in
/// memory.
///
/// Byte-concatenating TS segments preserves each segment's original PTS/DTS,
/// so the result starts at whatever the source clock happened to be and keeps
/// a jump wherever an ad break was excised. A clip must start at zero and play
/// without a seek-back, so the timeline has to be rewritten.
///
/// The first implementation staged the whole span to a tempdir and used
/// ffmpeg's concat demuxer. That works but buffers the entire span (measured:
/// 89 MiB and 82 MiB on a 900 s window), which is exactly the wrong shape for
/// a small Cloud Run task. Piping the concatenation through ffmpeg on
/// stdin/stdout instead was verified to produce BYTE-IDENTICAL output on the
/// same input -- same size, same duration, same zero start, clean decode
/// across the splice -- while holding nothing.
///
/// `-avoid_negative_ts make_zero` rebases the start; the mpegts demuxer's
/// discontinuity handling collapses the excised-ad gaps (verified: a 44.8 s
/// gapped concatenation of 25.6 s of content comes out 25.6 s long). Pure
/// stream copy -- no transcode.
///
/// The CMAF path needs none of this: `mp4mux` builds its sample tables by
/// APPENDING durations, so it is already continuous and zero-based.
pub struct TsRebaser {
    child: Child,
    stdin: Option<ChildStdin>,
    /// Pumps ffmpeg's stdout STRAIGHT INTO the caller's sink, returning it so
    /// `finish` can hand it back. Held so `finish` can wait for the tail of the
    /// stream after stdin closes.
    ///
    /// It forwards rather than collects, and that distinction is the whole
    /// point: an earlier version did `read_to_end` into a `Vec` and returned
    /// the bytes for the caller to write, which held the ENTIRE rebased span in
    /// memory. On a 512Mi Cloud Run job a 24-minute asset (~365 MiB) was
    /// OOM-killed by the kernel the moment the last segment went in — after
    /// every segment had downloaded successfully, so the logs showed a clean
    /// `[248/248]` and then signal 9. Local runs never showed it because memory
    /// there is effectively unbounded. Peak now stays flat regardless of asset
    /// length, which is what this type was written for.
    ///
    /// Returns the byte count alongside the sink so `finish` can still reject an
    /// empty rebase. Counting is the only way to notice now — the buffering
    /// version could just look at the `Vec`.
    pump: JoinHandle<Result<(BoxedSink, u64)>>,
}

/// The caller's destination for rebased bytes: any async writer, type-erased so
/// the pump task can own it for its lifetime and give it back at `finish`.
pub type BoxedSink = Box<dyn tokio::io::AsyncWrite + Unpin + Send>;

impl TsRebaser {
    /// Spawn the rebaser. Output is drained concurrently by an internal task:
    /// writing stdin without reading stdout deadlocks as soon as the pipe
    /// buffer fills, which for real segments is immediate.
    pub fn new(sink: BoxedSink) -> Result<Self> {
        let mut child = Command::new(ffmpeg_bin())
            .args(["-hide_banner", "-loglevel", "error", "-i", "pipe:0"])
            .args([
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-f",
                "mpegts",
                "pipe:1",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("spawning ffmpeg (TS rebase)")?;
        let stdin = child.stdin.take().context("ffmpeg stdin")?;
        let mut stdout = child.stdout.take().context("ffmpeg stdout")?;
        let pump = tokio::spawn(async move {
            let mut sink = sink;
            // Constant-memory forward: tokio::io::copy uses a fixed internal
            // buffer, so nothing scales with the length of the span.
            let written = tokio::io::copy(&mut stdout, &mut sink)
                .await
                .context("streaming ffmpeg stdout to the output")?;
            Ok((sink, written))
        });
        Ok(Self {
            child,
            stdin: Some(stdin),
            pump,
        })
    }

    /// Feed one segment in.
    pub async fn push(&mut self, bytes: &[u8]) -> Result<()> {
        let stdin = self.stdin.as_mut().context("rebaser already finished")?;
        stdin.write_all(bytes).await.context("writing ffmpeg stdin")
    }

    /// Close the input, flush ffmpeg's tail into the sink, and hand the sink
    /// back so the caller can keep writing to it or shut it down.
    ///
    /// The rebased bytes are ALREADY in the sink by the time this returns —
    /// there is nothing to write here. That is the contract change from the
    /// buffering version, and it is why the caller no longer wraps this in
    /// `write_all(...)`.
    pub async fn finish(mut self) -> Result<BoxedSink> {
        // Dropping stdin signals EOF; ffmpeg then flushes its tail.
        drop(self.stdin.take());
        let (sink, written) = self.pump.await.context("joining ffmpeg pump")??;
        let status = self.child.wait().await.context("waiting for ffmpeg")?;
        if !status.success() {
            let mut err = String::new();
            if let Some(mut e) = self.child.stderr.take() {
                let mut b = Vec::new();
                let _ = e.read_to_end(&mut b).await;
                err = String::from_utf8_lossy(&b).into_owned();
            }
            return Err(anyhow!("ffmpeg (TS rebase) failed: {}", err.trim()));
        }
        // AFTER the status check on purpose: when ffmpeg fails it usually also
        // writes nothing, and its stderr says far more about why than "produced
        // no output" would. Reaching here means ffmpeg claimed success while
        // emitting an empty span -- silent on its own, and it would otherwise
        // leave a zero-byte object behind and exit 0.
        if written == 0 {
            return Err(anyhow!("TS rebase produced no output"));
        }
        Ok(sink)
    }
}
