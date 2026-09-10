//! Generate an HLS rendition ("preview") of an already-published output file
//! and publish it to a second location.
//!
//! The source is read back over a signed HTTPS URL (or a `file:` URL), exactly
//! like [`crate::derivatives`], so the published MP4/TS is never staged
//! locally. The OUTPUT, however, unavoidably touches disk: HLS is a playlist
//! plus N segment files, and FFmpeg's `hls` muxer has to write real files --
//! it cannot emit a multi-file format down a single pipe. Segments are
//! therefore written to a tempdir, but each is uploaded and DELETED while
//! FFmpeg is still muxing the next one, so disk holds a couple of segments
//! rather than the whole rendition.
//!
//! That bound is the point. Uploading only after the muxer exited made peak
//! disk the full preview -- 463 MiB for a 24-minute asset -- which survived
//! only because Cloud Run's gen2 filesystem is disk-backed. The same code on
//! gen1, where the filesystem is in memory, would have OOM-killed the job, and
//! nothing in the code said so. Only the preview costs disk at all; the primary
//! download path stays fully streaming.
//!
//! "Same HLS profile as the source" is honoured through [`HlsProfile`]:
//! segment container (MPEG-TS vs fMP4) and target duration are taken from the
//! source playlist rather than guessed, so the preview segments line up with
//! how the origin was packaged. Everything is `-c copy` -- a preview must not
//! silently re-encode.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use anyhow::{anyhow, Context, Result};
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use crate::storage::{self, OutputTarget};

/// Signed-URL lifetime for reading the source back.
const URL_TTL: std::time::Duration = std::time::Duration::from_secs(3600);

fn ffmpeg_bin() -> String {
    std::env::var("FFMPEG_BIN").unwrap_or_else(|_| "ffmpeg".to_string())
}

/// How the SOURCE stream was packaged, so the preview matches it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum SegmentType {
    /// MPEG-TS segments (`.ts`) — an HLS playlist with no init segment.
    MpegTs,
    /// CMAF/fMP4 segments (`.m4s`) plus an `EXT-X-MAP` init segment.
    Fmp4,
}

impl SegmentType {
    fn ffmpeg_value(self) -> &'static str {
        match self {
            SegmentType::MpegTs => "mpegts",
            SegmentType::Fmp4 => "fmp4",
        }
    }
    fn extension(self) -> &'static str {
        match self {
            SegmentType::MpegTs => "ts",
            SegmentType::Fmp4 => "m4s",
        }
    }
}

/// The source playlist's shape, mirrored onto the preview.
#[derive(Debug, Clone, Copy)]
pub struct HlsProfile {
    pub segment_type: SegmentType,
    /// `EXT-X-TARGETDURATION` from the source, in seconds. Clamped to a sane
    /// floor so a malformed playlist cannot ask for 0-second segments.
    pub target_duration: f64,
}

impl HlsProfile {
    /// Derive the profile from the source: fMP4 when the media playlist had an
    /// `EXT-X-MAP` (i.e. CMAF), MPEG-TS otherwise.
    pub fn new(is_cmaf: bool, target_duration: f64) -> Self {
        Self {
            segment_type: if is_cmaf {
                SegmentType::Fmp4
            } else {
                SegmentType::MpegTs
            },
            target_duration: if target_duration.is_finite() && target_duration >= 1.0 {
                target_duration
            } else {
                6.0
            },
        }
    }
}

/// What [`generate`] published: the playlist URI plus the two facts §7.1's
/// `streaming_video` entry asks for that only the producer can answer.
///
/// COUNTED, NOT ESTIMATED. `segment_count` is the number of media parts actually
/// uploaded — filtered by the profile's own extension, so the playlist and (for
/// CMAF) the `EXT-X-MAP` init segment are excluded rather than inflating the
/// count. Deriving it downstream would mean re-fetching and re-parsing a playlist
/// this function already holds, and guessing it from the window length would be
/// wrong for exactly the runs that matter — a segment closed early by an ad break.
#[derive(Debug, Clone)]
pub struct Preview {
    pub uri: String,
    pub segment_count: usize,
    /// `EXT-X-TARGETDURATION` as declared on the GENERATED playlist, in seconds —
    /// the value patched in above from the parts actually produced.
    ///
    /// NOT THE SOURCE'S, and the two genuinely differ. `EXT-X-TARGETDURATION` is an
    /// upper BOUND, not the segment length, and an origin may declare it loosely:
    /// the live source this was found on declares 10 while every one of its parts
    /// is 6.4 s. Reporting that 10 as the preview's `segment_duration` described a
    /// playlist that declares 8 and is cut at ~6.4 — a plausible-looking number
    /// belonging to a different playlist, which is the kind that survives review.
    pub target_duration: f64,
}

/// Build an HLS rendition of `source_name` (already published under
/// `src_dir_uri`) and publish playlist + segments to `out_dir_uri`.
///
/// Naming follows the source file: `clip.ts` yields `clip.m3u8` alongside
/// `clip_00000.ts`, `clip_00001.ts`, ... so a preview is always identifiable
/// from the artifact it previews.
///
/// Returns the playlist URI.
/// `boundaries` are EXACT cumulative split points in seconds (excluding 0 and
/// the total). Supplying them reproduces the SOURCE's own segmentation rather
/// than asking FFmpeg to chop every `target_duration` seconds: those points
/// are the origin's segment starts, which are IDRs (the source declares
/// `EXT-X-INDEPENDENT-SEGMENTS`), so the split is frame accurate AND still a
/// stream copy.
///
/// This is also what keeps `EXT-X-TARGETDURATION` honest. With a nominal
/// `-hls_time 9` FFmpeg can only cut at the next keyframe, so segments came
/// out 9.93 / 9.60 / 8.00 s and the declared target rounded UP to 10 against
/// the source's 9. Splitting on the real boundaries reproduces the source
/// durations exactly, so the declared target matches.
pub async fn generate(
    src_dir_uri: &str,
    source_name: &str,
    out_dir_uri: &str,
    profile: HlsProfile,
    boundaries: &[f64],
) -> Result<Preview> {
    let stem = source_name
        .rsplit_once('.')
        .map(|(s, _)| s)
        .unwrap_or(source_name)
        .to_string();

    let inputs = storage::ffmpeg_input_urls(
        src_dir_uri,
        std::slice::from_ref(&source_name.to_string()),
        URL_TTL,
    )
    .await
    .context("resolving the source object to an FFmpeg input")?;
    let input = inputs
        .urls
        .first()
        .ok_or_else(|| anyhow!("no input URL for {source_name}"))?;

    let dir = tempdir()?;
    let playlist_path = dir.join(format!("{stem}.m3u8"));
    let seg_pattern = dir.join(format!("{stem}_%05d.{}", profile.segment_type.extension()));

    let bin = ffmpeg_bin();
    let mut cmd = Command::new(&bin);
    cmd.args([
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,pipe",
    ]);
    if let Some(header) = &inputs.header {
        cmd.args(["-headers", header]);
    }
    // Stream copy: a preview that re-encodes is not a preview of the asset,
    // it is a different asset.
    cmd.args(["-i", input]).args(["-c", "copy"]);
    let exact = !boundaries.is_empty() && profile.segment_type == SegmentType::MpegTs;
    if exact {
        // `segment` (not `hls`) because it is the only muxer that takes an
        // explicit list of split points, and it can still emit the m3u8.
        let times = boundaries
            .iter()
            .map(|t| format!("{t:.6}"))
            .collect::<Vec<_>>()
            .join(",");
        cmd.args(["-f", "segment"])
            .args(["-segment_times", &times])
            .args(["-segment_list_type", "m3u8"])
            .args(["-segment_format", "mpegts"])
            .arg("-segment_list")
            .arg(&playlist_path)
            .arg(&seg_pattern);
    } else {
        // No boundary list (or fMP4, where the hls muxer owns the init
        // segment): fall back to a nominal duration. FFmpeg still cuts only on
        // keyframes, so the declared target duration may round up.
        cmd.args(["-f", "hls"])
            .args(["-hls_time", &format!("{:.3}", profile.target_duration)])
            .args(["-hls_playlist_type", "vod"])
            .args(["-hls_segment_type", profile.segment_type.ffmpeg_value()])
            // 0 = keep every segment (VOD, not a sliding window).
            .args(["-hls_list_size", "0"])
            .args(["-hls_flags", "independent_segments"])
            .arg("-hls_segment_filename")
            .arg(&seg_pattern);
        if profile.segment_type == SegmentType::Fmp4 {
            cmd.args(["-hls_fmp4_init_filename", &format!("{stem}_init.mp4")]);
        }
        cmd.arg(&playlist_path);
    }

    // Resolved BEFORE ffmpeg runs: segments ship while it is still muxing, so
    // the destination has to be known up front -- and an unusable destination
    // should fail before doing the muxing work rather than after.
    let target = OutputTarget::from_uri(out_dir_uri)
        .with_context(|| format!("resolving preview destination {out_dir_uri}"))?;

    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        // An upload error now returns while ffmpeg is still running; without
        // this the child would be orphaned instead of reaped.
        .kill_on_drop(true)
        .spawn()
        .with_context(|| format!("spawning {bin} (installed / FFMPEG_BIN set?)"))?;
    let mut stderr = child.stderr.take().expect("stderr piped");
    let err_task = tokio::spawn(async move {
        let mut buf = String::new();
        let _ = stderr.read_to_string(&mut buf).await;
        buf
    });

    // Media parts only: the playlist and, for CMAF, the EXT-X-MAP init segment
    // are published too but are not segments, so counting every uploaded file
    // would over-report by one or two.
    let segment_ext = profile.segment_type.extension();
    let mut segment_count = 0usize;
    // Every object already uploaded, so a later failure can take them back out.
    let mut published: Vec<String> = Vec::new();

    // Ship each finished segment while ffmpeg muxes the next. Both muxers write
    // strictly increasing indices and never revisit a file, so every segment
    // except the highest-numbered one is complete; that last one is still being
    // written and is held back until ffmpeg exits.
    let status = loop {
        if let Some(status) = child.try_wait().context("polling ffmpeg (hls)")? {
            break status;
        }
        let mut ready = segment_files(&dir, &stem, segment_ext).await?;
        ready.pop();
        for name in ready {
            if let Err(e) = upload_and_remove(&target, &dir.join(&name), &name).await {
                abandon(out_dir_uri, &published, &dir).await;
                return Err(e);
            }
            segment_count += 1;
            published.push(name);
        }
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    };
    let err = err_task.await.unwrap_or_default();
    if !status.success() {
        abandon(out_dir_uri, &published, &dir).await;
        return Err(anyhow!("ffmpeg (hls preview) failed: {}", err.trim()));
    }

    // Reconcile EXT-X-TARGETDURATION with the spec (and with the source).
    //
    // RFC 8216 4.3.3.1: the target duration is the maximum segment duration
    // ROUNDED TO THE NEAREST INTEGER. FFmpeg is conservative and uses ceil, so
    // a source whose longest segment is 9.0666 s declares 9 while FFmpeg
    // declares 10 -- describing byte-identical segmentation with a different
    // integer. The segments are already frame accurate; only the header
    // disagreed, so rewrite it rather than re-cut the media.
    // The target duration the GENERATED playlist ends up declaring. Computed here
    // and REPORTED, rather than echoing the profile's: see Preview.target_duration.
    let mut declared_target = profile.target_duration;
    if let Ok(text) = tokio::fs::read_to_string(&playlist_path).await {
        let max_extinf = text
            .lines()
            .filter_map(|l| l.strip_prefix("#EXTINF:"))
            .filter_map(|v| v.trim_end_matches(',').split(',').next())
            .filter_map(|v| v.trim().parse::<f64>().ok())
            .fold(0.0f64, f64::max);
        if max_extinf > 0.0 {
            let rounded = max_extinf.round().max(1.0) as u64;
            declared_target = rounded as f64;
            let patched = text
                .lines()
                .map(|l| {
                    if l.starts_with("#EXT-X-TARGETDURATION:") {
                        format!("#EXT-X-TARGETDURATION:{rounded}")
                    } else {
                        l.to_string()
                    }
                })
                .collect::<Vec<_>>()
                .join("\n");
            let _ = tokio::fs::write(&playlist_path, patched + "\n").await;
        }
    }

    // Whatever is still on disk is complete now: the held-back last segment,
    // and for CMAF the EXT-X-MAP init segment. The playlist goes LAST --
    // publishing one that still references unwritten segments would be worse
    // than publishing nothing.
    let playlist_name = format!("{stem}.m3u8");
    for name in segment_files(&dir, &stem, segment_ext).await? {
        if let Err(e) = upload_and_remove(&target, &dir.join(&name), &name).await {
            abandon(out_dir_uri, &published, &dir).await;
            return Err(e);
        }
        segment_count += 1;
        published.push(name);
    }
    let mut entries = tokio::fs::read_dir(&dir)
        .await
        .context("listing generated HLS files")?;
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name == playlist_name {
            continue;
        }
        let name = name.to_string();
        if let Err(e) = upload_and_remove(&target, &path, &name).await {
            abandon(out_dir_uri, &published, &dir).await;
            return Err(e);
        }
        published.push(name);
    }

    // Checked before the playlist goes up, so a preview that produced no media
    // never publishes a playlist pointing at nothing. The old check counted
    // FILES, which a lone playlist would have satisfied.
    if segment_count == 0 {
        abandon(out_dir_uri, &published, &dir).await;
        return Err(anyhow!("HLS preview produced no segments"));
    }
    if let Err(e) = upload_and_remove(&target, &playlist_path, &playlist_name).await {
        abandon(out_dir_uri, &published, &dir).await;
        return Err(e);
    }
    cleanup(dir).await;
    Ok(Preview {
        uri: format!("{}/{stem}.m3u8", out_dir_uri.trim_end_matches('/')),
        segment_count,
        target_duration: declared_target,
    })
}

/// This preview's media segments currently on disk, in muxer order.
///
/// Matched by stem AND extension so the CMAF init segment (`<stem>_init.mp4`)
/// and the playlist are left to the final sweep: the init segment is not a
/// media part and must not inflate `segment_count`.
async fn segment_files(dir: &Path, stem: &str, ext: &str) -> Result<Vec<String>> {
    let prefix = format!("{stem}_");
    let mut out = Vec::new();
    let mut entries = tokio::fs::read_dir(dir)
        .await
        .context("listing generated HLS files")?;
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name.starts_with(&prefix) && path.extension().and_then(|e| e.to_str()) == Some(ext) {
            out.push(name.to_string());
        }
    }
    // Indices are zero-padded (`%05d`), so lexicographic order IS muxer order.
    out.sort();
    Ok(out)
}

/// Streams one generated file to `target` as `name`, then removes the local
/// copy -- the removal is what keeps peak disk flat.
///
/// Streams rather than `fs::read`, so a segment is never held whole in memory
/// even though at preview sizes it would fit.
async fn upload_and_remove(target: &OutputTarget, path: &Path, name: &str) -> Result<()> {
    use tokio::io::AsyncWriteExt;
    let mut file = tokio::fs::File::open(path)
        .await
        .with_context(|| format!("reading generated {name}"))?;
    let mut w = target.writer(name);
    tokio::io::copy(&mut file, &mut w)
        .await
        .with_context(|| format!("uploading {name}"))?;
    w.shutdown()
        .await
        .with_context(|| format!("finalizing {name}"))?;
    let _ = tokio::fs::remove_file(path).await;
    Ok(())
}

/// Gives up on a partly-published preview: removes what was already uploaded,
/// then the tempdir.
///
/// Without the playlist these objects are unreferenced, so they are not a
/// correctness problem -- but abandoning hundreds of orphan segments in a
/// consumer-facing bucket on every failed run is its own mess. Best effort: the
/// caller is already returning the real error, which must not be replaced by a
/// cleanup failure.
async fn abandon(out_dir_uri: &str, published: &[String], dir: &Path) {
    if !published.is_empty() {
        if let Err(e) = storage::delete_objects(out_dir_uri, published).await {
            eprintln!("-> HLS preview: warning: could not remove the partial upload: {e}");
        }
    }
    cleanup(dir.to_path_buf()).await;
}

fn tempdir() -> Result<PathBuf> {
    let base = std::env::temp_dir().join(format!(
        "hlspreview-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&base)
        .with_context(|| format!("creating temp dir {}", base.display()))?;
    Ok(base)
}

async fn cleanup(dir: PathBuf) {
    let _ = tokio::fs::remove_dir_all(&dir).await;
}

#[allow(dead_code)]
fn _assert_path_used(_p: &Path) {}
