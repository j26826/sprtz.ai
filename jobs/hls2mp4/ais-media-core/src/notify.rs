//! Interim status documents for the orchestration tier.
//!
//! The recorder runs as a fire-and-forget batch job, so without this its
//! progress is only visible in the container logs. Each state change — capture
//! started, ad break opened / closed, content run published, terminal
//! status — is written as **one small JSON object** under `STATUS_URI`, the
//! dedicated status bucket. A storage trigger (Eventarc) on that bucket drives
//! the clipping-notifier workflow, which owns the database writes and the
//! downstream `/status` notification.
//!
//! The clipper therefore stays fully cloud-agnostic: it makes **no HTTP calls,
//! no database writes and no Pub/Sub publishes** — it only writes objects, the
//! same thing it already does with media. (Callbacks were rejected because an
//! AWS Step Functions task token is single-use.)
//!
//! One object per event: Eventarc fires per finalized object, so every document
//! is written and closed on its own (see [`StatusWriter::object_name`] for the
//! naming scheme).
//!
//! **Every write is best-effort.** A failure is logged and dropped — reporting
//! never interrupts or fails a recording, and a write that hangs is abandoned
//! after `--status-write-timeout-secs` so a stalled upload cannot stall the
//! capture loop at an emission point. Without `STATUS_URI` / `JOB_ID`
//! (standalone runs) the writer is disabled and logs the document it would have
//! written instead.

use std::future::Future;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex as StdMutex;
use std::time::Duration;

use anyhow::Result;
use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Serialize, Serializer};
use serde_json::Value as JsonValue;
use tokio::io::AsyncWriteExt;
use uuid::Uuid;

use crate::storage;
use crate::task::{Task, JOB_TYPE_LIVE, JOB_TYPE_PACKAGING};

/// INTERNAL event discriminators — one per state change the recorder makes.
///
/// NOT ON THE WIRE. §7 determines the kind of a notification from block presence,
/// so these never reach the envelope; they exist so this module can decide which
/// blocks to emit and so the interim event log stays readable.
pub const TYPE_STATUS: &str = "status";
pub const TYPE_SEGMENT_CLOSED: &str = "segment_closed";
/// The two timeline markers: a segment's boundaries reported as they are
/// observed, ahead of the assembled `segment_closed`. Both report the segment
/// `in_progress` and carry no `output` — see [`Identity::segment_started_event`].
pub const TYPE_SEGMENT_STARTED: &str = "segment_started";
pub const TYPE_SEGMENT_ENDED: &str = "segment_ended";
pub const TYPE_AD_BREAK_START: &str = "ad_break_start";
pub const TYPE_AD_BREAK_END: &str = "ad_break_end";

/// §7 `data.segment.type` — the only two values the contract defines.
pub const SEGMENT_TYPE_SEGMENT: &str = "segment";
pub const SEGMENT_TYPE_AD: &str = "ad";

/// Lifecycle `status` values carried by a `type=status` event.
pub const STATUS_IN_PROGRESS: &str = "in_progress";
pub const STATUS_COMPLETED: &str = "completed";
pub const STATUS_FAILED: &str = "failed";

/// Coarse pipeline stage, reported as `error.stage` when a run fails.
pub const STAGE_SETUP: &str = "setup";
pub const STAGE_CAPTURE: &str = "capture";
pub const STAGE_ASSEMBLY: &str = "assembly";
/// The VOD clipper's equivalent of [`STAGE_CAPTURE`]: it FETCHES a published
/// playlist rather than recording a live edge, and naming the stage for what it
/// actually did is what makes an `error.stage` legible to whoever reads it.
pub const STAGE_DOWNLOAD: &str = "download";
/// The packager's equivalent, for the same reason: it neither records nor
/// downloads — it TRANSCODES an ABR ladder and writes HLS manifests, and a failure
/// reported as `capture` or `download` would send whoever reads it looking at the
/// wrong half of the system. It covers the whole run because the packaging pipeline
/// is one call with no reportable phases of its own (see `vod-packager`'s `main`).
pub const STAGE_PACKAGE: &str = "package";

/// `error.code` reported for a terminal failure of the recording.
pub const ERR_CAPTURE_FAILED: &str = "capture_failed";
/// The VOD clipper's terminal failure, for the same reason [`STAGE_DOWNLOAD`]
/// exists: "capture failed" would describe a live recording that never ran.
pub const ERR_DOWNLOAD_FAILED: &str = "download_failed";
/// The packager's terminal failure. Same rule again: nothing was captured and
/// nothing was downloaded, so neither of the other two codes describes what broke.
/// The `<stage>_failed` spelling of both siblings is kept, so a consumer that
/// pattern-matches the code family keeps working without learning a third shape.
pub const ERR_PACKAGE_FAILED: &str = "package_failed";
/// THE RUNTIME STOPPED THE RUN, NOT THE MEDIA. Cloud Run sends SIGTERM about ten
/// seconds before SIGKILL whenever it recycles an instance, and a job killed in that
/// window has produced no deliverable and reported nothing — so without a document of
/// its own the orchestrator's record sits at `in_progress` for ever with no
/// notification ever arriving. It is deliberately NOT one of the `<stage>_failed`
/// codes: nothing about the packaging broke, so a consumer (or an operator reading
/// the code) must be able to tell "this asset cannot be packaged" from "this attempt
/// was evicted and is worth retrying unchanged". The stage beside it still says WHERE
/// the run was when it was stopped.
pub const ERR_INTERRUPTED: &str = "interrupted";

/// Why a content-run file was closed, reported as the segment's close reason.
///
/// DEFINED HERE RATHER THAN IN THE RECORDER because they are wire values on the
/// §7 segment block, and both clippers now emit that block — `live-hls2mp4`
/// re-exports them from `recorder` so its own call sites read unchanged.
pub const CLOSE_AD_BREAK_START: &str = "ad_break_start";
pub const CLOSE_SCHEDULE_END: &str = "schedule_end";

/// Kind infix of a `segment_id`: a published content run vs an ad marker. They
/// share one sequence space (see [`Sequencer`]) but land as different record
/// kinds, so the kind is part of the id.
const SEGMENT_KIND_CONTENT: &str = "seg";
const SEGMENT_KIND_AD: &str = "ad";

/// `capture_position.container` — how the INGESTED media was packaged. The
/// source's packaging, not the output's: an MPEG-TS source is concatenated to
/// `.ts`, but a CMAF source is remuxed to a progressive `.mp4`, and reporting
/// `mp4` there would describe what the recorder wrote rather than what it read
/// (which is what a resume has to line up with).
pub const CONTAINER_TS: &str = "ts";
pub const CONTAINER_CMAF: &str = "cmaf";

/// Coarse progress: recording started / finished successfully. The recorder has
/// no finer progress model (the event window's length is the only yardstick).
const PROGRESS_STARTED: u8 = 0;
const PROGRESS_DONE: u8 = 100;

/// The **single** sequence space shared by content segments and ad markers: they
/// land in one `clip_segments` collection that is unique on
/// `(schedule_id, sequence)`, so the two kinds must never reuse a number.
///
/// Numbers are allocated in **boundary order** — as a content run closes and as
/// an ad break opens — not when the notification is emitted: content-run events
/// are deferred until their derivatives exist, while ad events fire live, so
/// allocating at emit time would number every ad before every content run.
///
/// IT LIVES BESIDE THE ENVELOPE, not in the recorder, because the number it
/// allocates is a wire field (`segment.sequence`, and the `segment_id` derived
/// from it). The VOD clipper numbers its spans from the same space and never
/// records anything, so a sequencer reachable only through the live recorder
/// would have forced a second, silently divergent counter.
#[derive(Debug, Default)]
pub struct Sequencer {
    next: u32,
    /// Number held by the ad break currently open, so its end event reuses the
    /// number its start allocated (one break = one marker = one number).
    open_ad_break: Option<u32>,
    /// Number held by the content run currently open, for the same reason: the
    /// start marker and the eventual close are one segment and must share one
    /// number, because `segment_id` is derived from it.
    open_content_run: Option<u32>,
}

impl Sequencer {
    fn allocate(&mut self) -> u32 {
        self.next += 1;
        self.next
    }

    /// Allocates the content run's number as it OPENS, so the start marker can
    /// name the segment before any of it has been recorded.
    ///
    /// Ordering is unchanged by allocating here rather than at the close. A run
    /// is closed BY the break that follows it, so a run always opens before that
    /// break opens, and the two alternate — the run still takes the lower number
    /// either way. What allocating at open does change is that the number is
    /// spent on a run that opened, not on one that produced a file; the caller
    /// therefore allocates here only when it is going to announce the run.
    pub fn content_opened(&mut self) -> u32 {
        let sequence = self.allocate();
        self.open_content_run = Some(sequence);
        sequence
    }

    /// The number of the content run being closed — the one its open allocated,
    /// or a fresh one when the run was never announced (the CONCAT mode that
    /// keeps a single file across breaks, and any run closed without a start
    /// marker). Called by `Recorder::finalize_current` only.
    pub fn content_closed(&mut self) -> u32 {
        self.open_content_run
            .take()
            .unwrap_or_else(|| self.allocate())
    }

    /// Allocates the ad break's single number, at its start.
    pub fn ad_break_opened(&mut self) -> u32 {
        let sequence = self.allocate();
        self.open_ad_break = Some(sequence);
        sequence
    }

    /// The number for the open break's end event — the one its start allocated,
    /// or a fresh one when the break was already under way as capture began (no
    /// start event was ever emitted for it).
    pub fn ad_break_closed(&mut self) -> u32 {
        self.open_ad_break.take().unwrap_or_else(|| self.allocate())
    }
}

/// The orchestrated job's identity — everything the recorder ECHOES rather than
/// derives, read from the §4 task it was launched with (see [`crate::task`]).
/// `job_id` keys every event; the rest is present only when a task was supplied,
/// since a standalone run belongs to no job.
#[derive(Debug, Clone, Default)]
pub struct Identity {
    pub job_id: String,
    pub signature: Option<String>,
    /// The §4 `job.job_type` this run was dispatched as — `live` / `vod` /
    /// `packaging` (see [`crate::task`]).
    ///
    /// MODELED, THOUGH IT IS ALSO INSIDE THE ECHO, and that is the exception the
    /// echo's own rule allows for: `job` carries the seven fields this struct used
    /// to duplicate purely to hand them BACK, while this one is BRANCHED ON — §7
    /// defines a different field set per job type (`status.occurrence_index` on the
    /// live shapes only, a different `output` group set for packaging), so the
    /// envelope has to know which shape it is rendering. It is the same value the
    /// echo carries, read from the same block ([`crate::task::TaskJob::job_type`] is
    /// derived from `job_raw`), so the two cannot disagree.
    ///
    /// Empty for a standalone run, which was dispatched as nothing. An empty (or
    /// unknown) type renders the shapes §7 defines for every job type and none of
    /// the ones it qualifies — see [`Event::envelope`].
    pub job_type: String,
    /// The §4 `job.media_id` — modeled for the same reason `job_type` is: it is
    /// BRANCHED ON. A manual clip's segment id is this value rather than a derived
    /// one, because the Video Editor created the row it names before dispatching
    /// the job. Empty on every other path, where the id is minted as before.
    pub media_id: String,
    /// Whether this run is a manual clip (§4 `job.job_sub_type`). Branched on by
    /// [`Identity::segment_id`], and by the recorder for the split, marker and
    /// derivative rules a manual clip does not follow.
    pub manual: bool,
    /// The occurrence of a recurring schedule this run covers.
    ///
    /// ORCHESTRATOR CONFIG, NOT CALLER DATA. §4 does not carry it and the recorder
    /// cannot derive it — one dispatch covers exactly one occurrence and only the
    /// orchestrator knows which — so it arrives as `OCCURRENCE_INDEX` alongside the
    /// window and the destinations. It is reported under §7 `data.status`, which is
    /// worker-owned and purely derived, never inside the echoed `job`.
    ///
    /// Optional because `0` is a real occurrence, so "no task at all" has to stay
    /// distinguishable from "the first one".
    pub occurrence_index: Option<u32>,
    /// The §4 `data.job` block echoed onto every §7 message, structurally unchanged
    /// — no field added, removed, renamed or rewritten (§7: "Never mutated by the
    /// worker"). Not byte-identical; see the `task` module header.
    ///
    /// THIS IS THE WHOLE ECHO. It replaces the eight fields this struct used to
    /// carry one-per-value (`brand_id`, `agent_id`, `ext_job_id`, `ext_event_id`,
    /// `ai_flags`, `pass_through`, and the reconstructed `schedule` block): every one
    /// of them existed only to be handed back, so modeling them meant a §4 field
    /// could not reach N8N until this file grew to match it, and any field never
    /// modeled was silently dropped from the caller's own request.
    ///
    /// `Value::Null` for a standalone run, which has no job to echo.
    pub job: JsonValue,
}

/// How far capture got, in MEDIA time (§7.2 / §9 `capture_position`).
///
/// The `at` of a status document is the moment the document was WRITTEN — for a
/// terminal document that is after assembly and the derivatives, so it trails
/// the last frame recorded by however long that took (65 s on the run this was
/// added for). Nothing else on the envelope says where the media stopped, so a
/// consumer had to fall back to the SCHEDULED end, which is only as accurate as
/// the schedule. This is that missing answer, and per §4.3 it is also the
/// checkpoint a resumed capture restarts from.
///
/// `last_pdt` stays a `DateTime` rather than the pre-formatted string the
/// envelope uses elsewhere: the schedule-end ad close reads the media time back
/// out of it (see [`StatusWriter::capture_position`]), so it is formatted at the
/// wire edge only.
#[derive(Debug, Clone, Serialize)]
pub struct CapturePosition {
    /// HLS media sequence of the last chunk INGESTED — the last one the capture
    /// loop consumed from the manifest, ad chunks included (they advance the
    /// media position even though they are dropped, and a resume must not
    /// re-read them).
    pub last_media_sequence: u64,
    /// The PDT that chunk ended at, so `(sequence, pdt)` is a resume point.
    #[serde(serialize_with = "serialize_rfc3339")]
    pub last_pdt: DateTime<Utc>,
    /// `BANDWIDTH` of the selected variant. Omitted — not zeroed — for a source
    /// that is a media playlist with no master, where there is no variant to
    /// report: dropping the whole position over an unknowable bandwidth would
    /// lose the media time, which is the part downstream cannot derive.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub variant_bandwidth: Option<u64>,
    /// [`CONTAINER_TS`] / [`CONTAINER_CMAF`].
    pub container: &'static str,
}

/// The part of a [`CapturePosition`] that is fixed for the whole run: which
/// variant is being ingested, in which container. Kept beside the capture loop
/// so each ingested chunk only has to supply the part that moves.
#[derive(Debug, Clone, Copy)]
pub struct StreamProfile {
    pub variant_bandwidth: Option<u64>,
    pub container: &'static str,
}

impl StreamProfile {
    /// The position reached by ingesting media sequence `last_media_sequence`,
    /// which ended at `last_pdt`.
    pub fn at(&self, last_media_sequence: u64, last_pdt: DateTime<Utc>) -> CapturePosition {
        CapturePosition {
            last_media_sequence,
            last_pdt,
            variant_bandwidth: self.variant_bandwidth,
            container: self.container,
        }
    }
}

/// Failure detail carried on a `status=failed` event.
#[derive(Debug, Serialize)]
pub struct EventError {
    pub code: String,
    pub details: String,
    pub stage: String,
}

/// A single object deliverable of a published segment.
/// One `media_profile` entry — a track group of the published file (§7.1).
///
/// EVERY FIELD IS OPTIONAL AND ABSENT WHEN UNKNOWN. The values are read from the
/// master playlist's variant declaration, and a manifest may declare any subset
/// (`RESOLUTION`, `CODECS`, `FRAME-RATE` are each independently optional). A
/// half-known profile is still useful to AI Studio; a profile padded with zeros
/// would be a lie about the media, so absence is preserved all the way out.
#[derive(Debug, Clone, Serialize)]
pub struct MediaProfile {
    pub group_type: &'static str,
    /// Audio-only descriptors (§7.1): which rendition this is and in what
    /// language, plus the channel layout. `audio_kind` is the fixed `PRM` the
    /// contract uses for a primary rendition; the rest are DECLARED by the
    /// manifest's EXT-X-MEDIA entry.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_kind: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_group: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    /// CC-only: the caption format, read from `INSTREAM-ID` rather than assumed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub format: Option<&'static str>,
    /// CC entries carry `offset_sec` at the TOP level (they have no track_info).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offset_sec: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub track_info: Option<TrackInfo>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub channel_layouts: Vec<ChannelLayout>,
}

/// One entry of an audio track's `channel_layouts` (§7.1): which physical channel
/// sits at which locator. Derived from the manifest's `CHANNELS` COUNT — HLS
/// declares how many channels there are, not which is which, so only the standard
/// mono / stereo layouts are named and anything else is left unstated rather than
/// guessed.
#[derive(Debug, Clone, Serialize)]
pub struct ChannelLayout {
    pub channel_locator: String,
    pub audio_channel: &'static str,
}

impl MediaProfile {
    /// A video track group.
    fn video(track_info: TrackInfo) -> Self {
        Self {
            group_type: "video",
            audio_kind: None,
            audio_group: None,
            language: None,
            format: None,
            offset_sec: None,
            track_info: Some(track_info),
            channel_layouts: Vec::new(),
        }
    }
}

/// The per-track detail of a [`MediaProfile`].
///
/// The camelCase names are deliberate and must not be "tidied" to snake_case:
/// §7.1's example and the previously-working worker's payload both use
/// `bitRate` / `frameRate` / `frameSize` / `sampleRate` inside `track_info`
/// while every wrapper around them stays snake_case. AI Studio reads these keys.
#[derive(Debug, Clone, Serialize)]
pub struct TrackInfo {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub codec: Option<String>,
    #[serde(rename = "bitRate", skip_serializing_if = "Option::is_none")]
    pub bit_rate: Option<u64>,
    #[serde(rename = "frameRate", skip_serializing_if = "Option::is_none")]
    pub frame_rate: Option<FrameRate>,
    #[serde(rename = "frameSize", skip_serializing_if = "Option::is_none")]
    pub frame_size: Option<FrameSize>,
    /// Audio sample rate. ALWAYS ABSENT today and that is deliberate: HLS declares
    /// no sample rate anywhere, so reporting one would mean opening the media.
    #[serde(rename = "sampleRate", skip_serializing_if = "Option::is_none")]
    pub sample_rate: Option<u64>,
    pub offset_sec: u32,
}

/// A rational frame rate, as §7.1 draws it (`{numerator, denominator}`).
#[derive(Debug, Clone, Serialize)]
pub struct FrameRate {
    pub numerator: u32,
    pub denominator: u32,
}

/// Pixel dimensions of a video track.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct FrameSize {
    pub width: u64,
    pub height: u64,
}

#[derive(Debug, Serialize)]
pub struct OutputFile {
    pub uri: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_size: Option<u64>,
    /// §7.1 `resolution` — the declared height as a label (e.g. `720p`). Absent
    /// when the manifest declared no RESOLUTION; never guessed from the bitrate.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<String>,
    /// §7.1 `media_profile`. Empty when the source declared nothing usable,
    /// which the notifier forwards as-is rather than substituting a shape.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub media_profile: Vec<MediaProfile>,
    /// Public HTTPS URL for the same object, when it lives in the CDN-fronted
    /// bucket. `None` — and so absent from the wire — for anything the CDN does
    /// not serve. See [`cdn_url`].
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
}

/// An object deliverable with no size reported (the thumbnail image).
#[derive(Debug, Default, Serialize)]
pub struct OutputRef {
    pub uri: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    /// Size of the published object, when it could be read back.
    ///
    /// Storage metadata, NOT a media probe: it is the same best-effort
    /// `object_size` lookup the mezzanine and the proxy already report, so it
    /// costs one HEAD and cannot change what was written. §7.1 gives `thumbnail`
    /// a `file_size`, and the notifier previously had to publish a hardcoded 0
    /// because nothing upstream reported one. Absent — never 0 — when the lookup
    /// failed, so a consumer can tell "unknown" from "empty file".
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_size: Option<u64>,
    /// Pixel dimensions of the published image (§7.1 thumbnail width/height).
    ///
    /// The thumbnail is extracted with no scale filter, so these ARE the source
    /// variant's declared dimensions rather than a separate measurement. Absent
    /// when the manifest declared no RESOLUTION.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u64>,
    /// §7.1 `streaming_video.segment_count` / `.segment_duration`.
    ///
    /// Only meaningful for the HLS preview, and only the producer can answer them
    /// without re-fetching and re-parsing the playlist it just wrote: the count is
    /// what was actually uploaded, and the duration is the target the playlist
    /// declares. Absent on every other deliverable, which is why they live here as
    /// options rather than in a type of their own.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_duration: Option<u64>,
    /// §7.1 `streaming_video.audio_languages` — every language the variant's
    /// audio GROUP advertises, from the manifest's EXT-X-MEDIA entries.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub audio_languages: Vec<String>,
    /// §7.1 `streaming_video.video_bitrate` — the variant's AVERAGE-BANDWIDTH.
    ///
    /// SAME IMPRECISION AS master_video's bitRate, recorded here too so it is not
    /// rediscovered as a bug: the manifest declares this per VARIANT with audio
    /// multiplexed in, so the video track is credited with the audio's share.
    /// There is deliberately no `audio_bitrate` beside it — HLS declares none, and
    /// the digits inside a packager-chosen GROUP-ID are a naming convention rather
    /// than a value the manifest asserts.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_bitrate: Option<u64>,
}

/// Namespace for the v5 `segment_id` hash. A fixed, arbitrary UUID whose only job
/// is to never change: `(namespace, name)` is what makes the derived id
/// reproducible across runs and across builds, so editing this constant would
/// silently renumber every future segment.
const SEGMENT_ID_NAMESPACE: Uuid = Uuid::from_bytes([
    0x6b, 0xa7, 0xb8, 0x14, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
]);

/// One packaged HLS presentation, as it is reported in §7.1's `streaming_video`.
///
/// A SECOND SHAPE BESIDE [`OutputRef`], and deliberately not a reuse of it. The
/// clippers publish at most ONE streaming deliverable per segment — the
/// `--generate-preview` rendition — so `Outputs::hls_preview` is a single option and
/// [`Outputs::to_output`] can hardcode its `clear_hls_source` type. A packaging run
/// publishes N presentations of the SAME media, one per requested HLS version, and
/// each carries its own type (`clear_hlsv3` … `clear_hlsv7`). Widening `hls_preview`
/// into a list with a per-entry type would have re-serialized both clippers' output
/// through a changed code path for no gain to them; this rides beside it instead, so
/// live and VOD emit exactly the bytes they emitted before (see the golden fixture).
///
/// `segment_count` IS DELIBERATELY ABSENT from this struct. §7.1 defines the field
/// and the packager cannot answer it honestly: the pipeline reports CHUNKS (its unit
/// of transcode parallelism), not segments, and one chunk holds several segments. A
/// count computed as `duration / segment_seconds` would be an estimate that
/// disagrees with the playlist it names — worse than absence, because absence is
/// readable as "unknown" while a wrong number is not. The producer would have to
/// re-fetch and re-parse the manifest it just wrote to answer it, which is the only
/// way it will ever be added.
#[derive(Debug, Default, Serialize)]
pub struct StreamingOutput {
    /// §7.1 `streaming_video.type` — `clear_hlsv3` … `clear_hlsv7`. Chosen by the
    /// producer from the format it actually packaged, never inferred here: this
    /// crate has no view of the packager's output formats.
    pub entry_type: &'static str,
    /// The manifest's own URI (`gs://…/index-v7.m3u8`), which is what
    /// `manifest_url` carries on the wire.
    pub uri: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    /// Playable duration of the presentation. Carried per entry rather than taken
    /// from the event's `duration_sec` (as the clippers' entries are), because a
    /// packaging document reports no job-level duration — see
    /// [`Identity::packaging_completed_event`].
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    /// §7.1 `segment_duration` — the target the packager segmented on.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_duration: Option<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub audio_languages: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_bitrate: Option<u64>,
}

/// The deliverable set of a published content run: the recorded file plus the
/// derivatives that were generated for it.
#[derive(Debug, Default, Serialize)]
pub struct Outputs {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub maxed_mp4: Option<OutputFile>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mp4_1fps: Option<OutputFile>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail: Option<OutputRef>,
    /// HLS rendition of `maxed_mp4`, when --generate-preview is set.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hls_preview: Option<OutputRef>,
    /// Packaged HLS presentations, one per requested output format.
    ///
    /// EMPTY FOR BOTH CLIPPERS, which is what keeps their envelopes byte-identical
    /// across this addition: an empty vec is skipped on the internal event log and
    /// contributes no `streaming_video` entry, so the golden fixture's LIFECYCLE /
    /// SEGMENT / AD lines do not move. Only `vod-packager` fills it.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub streaming: Vec<StreamingOutput>,
}

/// One span of a split VOD capture, as it is reported.
///
/// A STRUCT AND NOT SIX ARGUMENTS: the boundaries are optional and the duration
/// is not derived from them, so a positional call site had two `Option`s of the
/// same type next to a bare `f64` — the shape most likely to be silently
/// transposed. Naming them also keeps the reporting call inside clippy's
/// argument-count limit.
#[derive(Debug, Clone)]
pub struct VodSpan {
    /// 1-based, so the first span is `sequence: 1` exactly as the live clipper's
    /// first content run is.
    pub sequence: u32,
    /// First kept instant, present only when the playlist carried
    /// `EXT-X-PROGRAM-DATE-TIME` — see [`Identity::vod_segment_event`].
    pub start: Option<DateTime<Utc>>,
    /// Last kept instant, on the same condition as `start`.
    pub end: Option<DateTime<Utc>>,
    /// MEASURED from what was kept, so it is defined even with no PDT at all.
    pub duration_sec: f64,
    /// Why this span ended: [`CLOSE_AD_BREAK_START`] for every span but the
    /// last, [`CLOSE_SCHEDULE_END`] for the last.
    pub close_reason: &'static str,
}

/// One interim event. Field order is the wire order; everything that does not
/// apply to the event's `type` is omitted rather than sent null.
#[derive(Debug, Serialize)]
pub struct Event {
    pub job_id: String,
    #[serde(rename = "type")]
    pub event_type: &'static str,
    pub at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress: Option<u8>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<EventError>,
    /// Where capture reached in media time. Only on `type=status` — it is a
    /// property of the job's progress, not of one clip — and absent until the
    /// first chunk has been ingested (nothing was captured, so there is no
    /// position to report; a zeroed one would read as "started at sequence 0").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub capture_position: Option<CapturePosition>,
    /// Identity of the clip record this event is about (see
    /// [`Identity::segment_id`]). Absent on `type=status`, which is about the
    /// job, not a clip.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_pdt: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_pdt: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_sec: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub close_reason: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub outputs: Option<Outputs>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    /// The occurrence this run covers, reported under §7 `data.status`.
    ///
    /// Omitted rather than sent as `0` when the run was launched without one: a
    /// consumer flooring a missing value at 0 is a different statement from an
    /// invented 0, which would make a real first occurrence indistinguishable from
    /// no answer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub occurrence_index: Option<u32>,
    /// The §4 `data.job` block, echoed verbatim onto the §7 envelope.
    ///
    /// ONE FIELD REPLACING SEVEN. `brand_id`, `agent_id`, `ext_job_id`,
    /// `ext_event_id`, `ai_flags`, `pass_through` and the reconstructed `schedule`
    /// each used to travel here separately, purely to be handed back — so every §4
    /// addition needed a matching field on this struct before it could reach a
    /// consumer, and anything unmodeled was dropped from the caller's own request.
    ///
    /// `Value::Null` for a standalone run. Skipped on the interim event log when
    /// null so those records stay as they were.
    #[serde(skip_serializing_if = "JsonValue::is_null")]
    pub job: JsonValue,
    /// Which §7 shape this document has to be rendered in — see
    /// [`Identity::job_type`].
    ///
    /// NOT SERIALIZED, deliberately. It is not a new fact: `job.job_type` is already
    /// on the wire inside the echo, and it is the field consumers route on. Writing
    /// it a second time here would put the same statement in two places on the
    /// interim event log, which is the shape §7 spent `data.event_type` removing.
    #[serde(skip)]
    pub job_type: String,
}

/// `Some(trimmed)` for a non-empty string, `None` otherwise — the §4 envelope
/// writes an absent id as `""`, and an empty id must stay absent on the wire rather
/// than becoming a field whose value is the empty string.
fn non_empty(s: &str) -> Option<String> {
    match s.trim() {
        "" => None,
        v => Some(v.to_string()),
    }
}

impl Identity {
    /// Identity of one clip record, as a UUID derived from
    /// `(job_id, kind, sequence)`.
    ///
    /// A UUID BECAUSE THE CONTRACT SAYS SO: the §7.1 segment example and the §9
    /// data model both specify `segment_id` as a uuid, and the AI Studio tables it
    /// lands in are uuid-typed — a `<job>-seg-<n>` string is readable but
    /// unstorable there, which is what made the earlier shape unusable downstream.
    ///
    /// VERSION 5, NOT 4, and that distinction is the point. The id must stay a
    /// pure function of the run so the SAME segment always yields the SAME id:
    /// clipping-notifier treats a redelivered status document as safe to
    /// reprocess (Eventarc retries, Pub/Sub is at-least-once) and §10 idempotency
    /// rests on a stable key. A random v4 would mint a fresh id each time a
    /// restarted clipper re-closed the same segment, splitting one clip into
    /// several downstream. Hashing the name keeps determinism across the change
    /// of shape.
    ///
    /// `kind` separates a content run from the ad marker sharing its sequence
    /// space. `job_id` is never empty: standalone runs fall back to the event id
    /// (see [`StatusWriter::from_args`]), so the id is always attributable.
    /// A MANUAL CLIP DOES NOT MINT ONE. The Video Editor created the media_asset
    /// row this clip fills and named it `media_id`; a derived id would address a
    /// row that does not exist, so the inbound value is returned verbatim. It is
    /// exactly one segment per manual job (§7), so there is nothing for the
    /// sequence to disambiguate.
    ///
    /// Guarded on the id being present rather than on `manual` alone: a manual task
    /// that carried no `media_id` has nothing to use, and falling through to the
    /// derived id keeps such a job reporting an attributable segment instead of
    /// an empty one.
    fn segment_id(&self, kind: &str, sequence: u32) -> String {
        if self.manual {
            if let Some(id) = non_empty(&self.media_id) {
                return id;
            }
        }
        let name = format!("{}:{}:{}", self.job_id, kind, sequence);
        Uuid::new_v5(&SEGMENT_ID_NAMESPACE, name.as_bytes()).to_string()
    }

    /// The envelope every event shares: job identity, discriminator, and the
    /// emission time.
    fn event(&self, event_type: &'static str, at: DateTime<Utc>) -> Event {
        Event {
            job_id: self.job_id.clone(),
            event_type,
            at: rfc3339(at),
            status: None,
            progress: None,
            error: None,
            capture_position: None,
            segment_id: None,
            sequence: None,
            start_pdt: None,
            end_pdt: None,
            duration_sec: None,
            close_reason: None,
            outputs: None,
            signature: self.signature.clone(),
            occurrence_index: self.occurrence_index,
            job: self.job.clone(),
            job_type: self.job_type.clone(),
        }
    }

    /// A lifecycle event: `in_progress` / `completed` / `failed` (+ the failure
    /// detail on the latter), and where capture had reached in media time when
    /// the document was written.
    fn status_event(
        &self,
        at: DateTime<Utc>,
        status: &'static str,
        progress: u8,
        error: Option<EventError>,
        capture_position: Option<CapturePosition>,
    ) -> Event {
        Event {
            status: Some(status),
            progress: Some(progress),
            error,
            capture_position,
            ..self.event(TYPE_STATUS, at)
        }
    }

    /// The segment has OPENED: its start, and nothing else yet.
    ///
    /// Same `segment_id` the eventual [`Self::segment_closed_event`] will carry —
    /// both derive it from `SEGMENT_KIND_CONTENT` and the run's number, which the
    /// sequencer now holds from open to close, so the three messages about one
    /// segment share one id.
    ///
    /// No `end_pdt` and no `outputs`, and that is the whole discriminator: the
    /// envelope renders `end_epoch`/`end_time` from `end_pdt`, so their absence
    /// IS "this is a start marker". Nothing new is added to the wire for it.
    fn segment_started_event(
        &self,
        at: DateTime<Utc>,
        sequence: u32,
        start: DateTime<Utc>,
    ) -> Event {
        Event {
            segment_id: Some(self.segment_id(SEGMENT_KIND_CONTENT, sequence)),
            sequence: Some(sequence),
            start_pdt: Some(rfc3339(start)),
            ..self.event(TYPE_SEGMENT_STARTED, at)
        }
    }

    /// The segment's boundary is now known — reported BEFORE assembly, so the
    /// timeline is complete while the deliverables are still being built.
    ///
    /// Carries the full boundary set but still no `outputs`: the file exists only
    /// once assembly finishes, and naming media that is not there is what
    /// `segment_closed` is for.
    fn segment_ended_event(
        &self,
        at: DateTime<Utc>,
        sequence: u32,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
        close_reason: &'static str,
    ) -> Event {
        Event {
            segment_id: Some(self.segment_id(SEGMENT_KIND_CONTENT, sequence)),
            sequence: Some(sequence),
            start_pdt: Some(rfc3339(start)),
            end_pdt: Some(rfc3339(end)),
            duration_sec: Some((end - start).num_milliseconds() as f64 / 1000.0),
            close_reason: Some(close_reason),
            ..self.event(TYPE_SEGMENT_ENDED, at)
        }
    }

    /// A published content run: its boundaries, duration, why it closed, and the
    /// deliverables written for it.
    fn segment_closed_event(
        &self,
        at: DateTime<Utc>,
        sequence: u32,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
        close_reason: &'static str,
        outputs: Outputs,
    ) -> Event {
        Event {
            segment_id: Some(self.segment_id(SEGMENT_KIND_CONTENT, sequence)),
            sequence: Some(sequence),
            start_pdt: Some(rfc3339(start)),
            end_pdt: Some(rfc3339(end)),
            duration_sec: Some((end - start).num_milliseconds() as f64 / 1000.0),
            close_reason: Some(close_reason),
            outputs: Some(outputs),
            ..self.event(TYPE_SEGMENT_CLOSED, at)
        }
    }

    /// §7.3 — the VOD capture's completion: `job` + `status` + `output`, and NO
    /// segment block, which is exactly the shape §7.3 draws.
    ///
    /// A VOD download of a published asset produces ONE deliverable set for the
    /// whole asset, so there is no run to number and nothing for a segment block
    /// to identify. `Envelope` emits `segment` exactly when `segment_id` is set,
    /// so leaving it unset IS the §7.3 shape — no second code path.
    ///
    /// `duration_sec` is MEASURED from what was written, never the `duration` the
    /// §4 task declared about the source: an ad-removed download is shorter than
    /// the asset it came from, so echoing the declared value would be wrong on
    /// exactly the jobs this clipper exists for.
    fn vod_completed_event(&self, at: DateTime<Utc>, duration_sec: f64, outputs: Outputs) -> Event {
        Event {
            status: Some(STATUS_COMPLETED),
            progress: Some(PROGRESS_DONE),
            duration_sec: Some(duration_sec),
            outputs: Some(outputs),
            ..self.event(TYPE_STATUS, at)
        }
    }

    /// §7.4 — the packaging run's completion: `job` + `status` + `output`, and NO
    /// segment block. Structurally identical to [`Self::vod_completed_event`], and
    /// that is the point: one asset in, one presentation set out, so there is
    /// nothing to number and nothing for a segment block to tell apart.
    ///
    /// THE ONLY DOCUMENT A PACKAGING RUN EMITS. §6: VOD and packaging jobs "emit no
    /// intermediate lifecycle notifications … exactly one terminal notification".
    /// So unlike `vod-hls2mp4` — which reports `in_progress` as it starts fetching —
    /// nothing precedes this, and a failed run emits `failed` in its place rather
    /// than after it. There is no state a consumer could miss by seeing only one
    /// document, because the run has no observable intermediate state: the pipeline
    /// is a single call that either produces a playable presentation or does not.
    ///
    /// NO DURATION IS REPORTED AT THE JOB LEVEL, which is why this takes only the
    /// outputs. `duration_sec` on the sibling event is what the VOD clipper MEASURED
    /// from the bytes it kept; a packaging run's equivalent is the source duration
    /// `ffprobe` read, which describes the INPUT. Where it does describe an output —
    /// each packaged presentation plays for that long — it is carried per entry on
    /// [`StreamingOutput::duration`], next to the manifest it is a fact about. A
    /// job-level duration here would have to be one of the N presentations' or a
    /// restatement of the input, and neither is a property of "the job".
    fn packaging_completed_event(&self, at: DateTime<Utc>, outputs: Outputs) -> Event {
        Event {
            status: Some(STATUS_COMPLETED),
            progress: Some(PROGRESS_DONE),
            outputs: Some(outputs),
            ..self.event(TYPE_STATUS, at)
        }
    }

    /// One span of a SPLIT VOD capture — the catch-up case, where the source is a
    /// `vbegin`/`vend` window and every content run between its ad breaks is a
    /// clip in its own right (see the scheduler's `vod_split_mode`).
    ///
    /// IT CARRIES A SEGMENT BLOCK AND §7.3 DOES NOT, deliberately. §7.3 draws the
    /// one-asset-in, one-file-out case and has no way to tell one span from
    /// another; a split capture emitting that shape N times would publish N
    /// completions for one job with nothing to order or distinguish them. So a
    /// split span reports as §7.1 does — numbered, bounded, one record each — and
    /// only an unsplit capture uses [`Self::vod_completed_event`].
    ///
    /// THE BOUNDARIES ARE OPTIONAL, unlike the live segment's. They are real
    /// wall-clock instants only when the playlist carried `EXT-X-PROGRAM-DATE-TIME`
    /// (a catch-up window does; a plain published asset need not), and inventing
    /// them from the download's own clock would report when the FETCH happened as
    /// if it were when the content aired. Absent bounds leave `duration` as the
    /// only measure, which is why it is passed rather than derived.
    fn vod_segment_event(&self, at: DateTime<Utc>, span: &VodSpan, outputs: Outputs) -> Event {
        Event {
            segment_id: Some(self.segment_id(SEGMENT_KIND_CONTENT, span.sequence)),
            sequence: Some(span.sequence),
            start_pdt: span.start.map(rfc3339),
            end_pdt: span.end.map(rfc3339),
            duration_sec: Some(span.duration_sec),
            close_reason: Some(span.close_reason),
            outputs: Some(outputs),
            ..self.event(TYPE_SEGMENT_CLOSED, at)
        }
    }

    /// An ad-break boundary. `start_pdt` is absent when the break was already
    /// under way as capture began (no OUT cue was ever seen). Both boundaries of
    /// one break carry the same `sequence`, so they also carry the same
    /// `segment_id` — one break is one marker record.
    ///
    /// An end event carries no `close_reason`: a break normally ends because
    /// content cued back in, which is the type itself. See
    /// [`Identity::ad_break_ended_by_schedule_event`] for the one case that does.
    fn ad_break_event(
        &self,
        at: DateTime<Utc>,
        event_type: &'static str,
        sequence: u32,
        start_pdt: Option<DateTime<Utc>>,
        end_pdt: Option<DateTime<Utc>>,
    ) -> Event {
        Event {
            segment_id: Some(self.segment_id(SEGMENT_KIND_AD, sequence)),
            sequence: Some(sequence),
            start_pdt: start_pdt.map(rfc3339),
            end_pdt: end_pdt.map(rfc3339),
            // THE OPEN/CLOSED DISCRIMINATOR, NOW ON THE WIRE. §7.2 draws an ad
            // marker with `close_reason: "ad_break_end"`, and it has to: with
            // `data.event_type` gone, an ad block is otherwise identical whether the
            // break just opened or just closed — same `type: "ad"`, same absence of
            // `output` — and the notifier must publish only once it has CLOSED while
            // ms-api records a distinct row for each.
            //
            // `event_type` is already exactly the string §7.2 wants
            // (`ad_break_start` / `ad_break_end`), so it is the value, not a mapping
            // of it. `ad_break_ended_by_schedule_event` overrides this with
            // `schedule_end`, which is also a CLOSE — so a consumer reads
            // "opened" as `ad_break_start` and treats every other reason as closed,
            // rather than enumerating the close reasons.
            close_reason: Some(event_type),
            ..self.event(event_type, at)
        }
    }

    /// The end of a break that the capture window ran out inside (§4.1): the
    /// ordinary end document plus the `close_reason` that says no cue-in ended
    /// it. `end_pdt` is mandatory here — a break closed for this reason without
    /// one would be exactly the open-ended marker this exists to prevent.
    fn ad_break_ended_by_schedule_event(
        &self,
        at: DateTime<Utc>,
        sequence: u32,
        start_pdt: Option<DateTime<Utc>>,
        end_pdt: DateTime<Utc>,
    ) -> Event {
        Event {
            close_reason: Some(CLOSE_SCHEDULE_END),
            ..self.ad_break_event(at, TYPE_AD_BREAK_END, sequence, start_pdt, Some(end_pdt))
        }
    }
}

// ---------------------------------------------------------------------------
// §7 notification envelope
// ---------------------------------------------------------------------------
//
// THE DOCUMENT WRITTEN TO STORAGE **IS** THE NOTIFICATION. Everything the
// consumer receives is decided here, by the component that actually knows the
// media, and nothing downstream reshapes it: clipping-notifier publishes the
// `{header, data}` block verbatim and ms-api reads the same block back. Before
// this, the clipper wrote a flat internal document and the notifier assembled the
// §7 envelope from it in Workflows YAML — which meant the contract lived in a
// language with no types and no tests, the notifier had to re-fetch the job record
// to fill fields the clipper already knew, and "what did we send?" could only be
// answered by reading a published message.
//
// `Event` is kept as the INTERNAL representation and converted here at the wire
// edge. That is deliberate: every emission point, the sequence space and the
// ad-pair id logic stay exactly as they were and keep their tests, while the shape
// on the wire changes in one place that can be asserted whole.

// `data.event_type` IS GONE. §7: the kind of notification is "determined
// structurally by block presence — there is no separate `data.type` /
// top-level status field, and N8N routes on blocks".
//
// It existed because block presence alone cannot tell `ad_break_start` from
// `ad_break_end` — both are `segment.type == "ad"` with no `output` — and the
// notifier must publish an ad only once the break has CLOSED, while ms-api
// records a distinct row for each. That discriminator is still on the wire, in a
// field §7 does define: `segment.close_reason`. The complete routing key is the
// PAIR:
//
//   segment absent                                  -> lifecycle
//   segment.type == "segment"                        -> content run (output beside it)
//   segment.type == "ad" && close_reason == "ad_break_start" -> break opened
//   segment.type == "ad" && close_reason == "ad_break_end"   -> break closed
//
// Consumers route on that pair. Nothing needs a field outside the contract.

/// The §7 envelope: `{header, data}`.
#[derive(Debug, Serialize)]
pub struct Envelope {
    pub header: Header,
    pub data: Data,
}

/// §7 `header`. `type` is `notification` for every message this worker writes;
/// health pings are a separate branch and are not produced here.
#[derive(Debug, Serialize)]
pub struct Header {
    #[serde(rename = "type")]
    pub header_type: &'static str,
    pub signature: String,
    #[serde(rename = "postedDate")]
    pub posted_date: String,
}

/// §7 `data`.
///
/// THE KIND OF NOTIFICATION IS STRUCTURAL. §7: "determined by block presence —
/// there is no separate `data.type` / top-level status field, and N8N routes on
/// blocks, never on `job.action`." So a segment message is one with `segment.type
/// == "segment"` and an `output`; an ad marker has `segment.type == "ad"` and no
/// output; a lifecycle message has neither. The `data.id` / `data.status`-string /
/// `data.type` / `data.event_type` discriminators this struct used to carry are all
/// gone — every one of them was a second way to say what the blocks already say,
/// and a consumer that trusted one over the other could disagree with the payload.
#[derive(Debug, Serialize)]
pub struct Data {
    /// The task's `job` block, echoed BYTE-FOR-BYTE. Never constructed here.
    pub job: JsonValue,
    pub status: Status,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment: Option<Segment>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output: Option<OutputBlock>,
    /// Where capture reached in MEDIA time. ADDITIVE to §7 and carried only on
    /// lifecycle messages — it is a property of the job's progress, not of a clip,
    /// it is the only media-time report the worker makes about itself, and per §10
    /// it is the checkpoint a resumed capture restarts from.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub capture_position: Option<CapturePosition>,
    pub created_at: String,
}

/// §7 `data.status` — worker-owned lifecycle, present on every message.
///
/// EVERY FIELD HERE IS DERIVED BY THE WORKER. None of it comes from the task, which
/// is why it sits beside the echoed `job` rather than inside it: the job block is
/// the caller's, and stamping worker state into it would mutate the thing §7
/// requires be handed back untouched.
#[derive(Debug, Serialize)]
pub struct Status {
    /// §6: `queued` / `in_progress` / `completed` / `failed`.
    pub state: &'static str,
    /// LIVE ONLY, and absent — not zeroed — on every other job type.
    ///
    /// §6's field table marks it `(live)`, and the two shapes that are not live draw
    /// a status block of exactly `{state, progress, error, updated_at}`: §7.3 (vod
    /// completion) and §7.4 (packaging completion). §7.1, §7.2 and §7.5 — all live —
    /// are the three that draw it, so it is rendered for `job_type: "live"` and
    /// suppressed otherwise (see [`Event::envelope`]).
    ///
    /// THE GATE IS THE JOB TYPE, NOT WHETHER A VALUE EXISTS. Every dispatch carries
    /// an `OCCURRENCE_INDEX` — a vod or packaging run is launched with 0 — so
    /// `Option::is_some` would answer "was one supplied", which is true on all three
    /// job types and therefore not the question §7 is asking.
    ///
    /// Still MANDATORY on the live shapes: `0` is a real occurrence and the live
    /// examples draw the key unconditionally, so a live document with no index
    /// reported reports 0 rather than dropping the field.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub occurrence_index: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress: Option<u8>,
    /// Rendered as `null` rather than omitted: §7 draws `"error": null` on every
    /// healthy message, so a consumer can read the key unconditionally.
    pub error: Option<EventError>,
    pub updated_at: String,
}

/// §7 `segment.status` — the state of THIS segment, beside the job-level
/// `data.status`.
///
/// TWO STATUSES, TWO SUBJECTS, and they legitimately disagree: a closed content
/// run is `completed` while the job it belongs to is still `in_progress` for the
/// rest of its window. §7.1 draws exactly that (`data.status.state` in_progress,
/// `segment.status.state` completed), which is why this is a separate block
/// rather than a copy of [`Status`].
///
/// It carries three of `Status`'s fields and not the other two. `progress` is a
/// job-level notion — a segment is open or closed, never 40% closed — and
/// `updated_at` would restate `data.status.updated_at`, since both are stamped
/// from the same event.
#[derive(Debug, Serialize)]
pub struct SegmentStatus {
    /// Rendered as `null` rather than omitted, matching [`Status::error`] and
    /// §7's drawing, so a consumer can read the key unconditionally.
    pub error: Option<EventError>,
    /// The segment's place in the RUNNING ORDER of the window — the same number
    /// as [`Segment::sequence`], because content runs and ad markers share one
    /// numbering space: a first content run is 1 and the ad after it is 2.
    ///
    /// Named `occurrence_index` by §7 and carried here despite duplicating
    /// `sequence`, because the field is the contract's; reporting the job's
    /// occurrence instead would answer a different question than the one the
    /// name is being used to ask.
    pub occurrence_index: u32,
    /// §6 vocabulary. `completed` for a closed run or a closed ad break;
    /// `in_progress` for an ad break that has only opened — it is a boundary
    /// that has not found its other edge yet, and calling that `completed`
    /// would report a break as over while it is still running.
    pub state: &'static str,
}

/// §7 `data.segment` — live only, absent on a lifecycle message.
///
/// One block for both kinds: a content run (`type: "segment"`, carrying an `output`
/// beside it) and an ad marker (`type: "ad"`, with no output). They share every
/// field, so they share the struct — the `type` and the presence of `output` are
/// what distinguish them, exactly as §7's routing table draws it.
#[derive(Debug, Serialize)]
pub struct Segment {
    pub segment_id: String,
    #[serde(rename = "type")]
    pub segment_type: &'static str,
    pub sequence: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub close_reason: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_epoch: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_epoch: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_time: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_time: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    pub status: SegmentStatus,
}

/// `data.output`, in whichever group set the job type defines.
///
/// TWO GROUP SETS, NOT ONE, because §7 defines two. The capture shapes (§7.1 live
/// segment, §7.3 vod completion) draw `master_video` / `streaming_video` /
/// `proxy_video` / `thumbnail`; §7.4 packaging draws `streaming_video` /
/// `source_convert` / `thumbnail` / `keyframes` — no mezzanine and no proxy, because
/// a packaging run does not produce either. Publishing them as empty lists said the
/// packager had produced none of something it cannot produce at all.
///
/// UNTAGGED, so the variant name never reaches the wire: `data.output` is the group
/// object itself in both cases, exactly as §7 draws it, and the kind of notification
/// stays readable from `job.job_type` and the block presence rather than from a
/// discriminator this crate invented.
#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum OutputBlock {
    /// §7.1 / §7.3 — live segments and vod completions.
    Capture(Output),
    /// §7.4 — packaging completions.
    Packaging(PackagingOutput),
}

/// §7.1 `data.output` — five groups, each a list so a consumer iterates uniformly.
#[derive(Debug, Default, Serialize)]
pub struct Output {
    pub master_video: Vec<MasterVideoEntry>,
    pub streaming_video: Vec<StreamingVideoEntry>,
    pub proxy_video: Vec<ProxyVideoEntry>,
    pub thumbnail: Vec<ThumbnailEntry>,
    /// Always empty: §12 lists keyframes as an open question and this worker does
    /// not produce them. The key is present so the group set is uniform.
    pub keyframes: Vec<JsonValue>,
}

/// §7.4 `data.output` — the packaging group set.
///
/// IT IS A PROJECTION OF [`Output`], NOT A SECOND MAPPING (see
/// [`Output::into_packaging`]). Every entry it carries is built by the one
/// `Outputs::to_output` that the clippers use, so a change to how a thumbnail or a
/// manifest is described cannot reach one job type and miss the other. What differs
/// between the two shapes is only WHICH GROUPS ARE DRAWN, which is the only thing
/// this type states.
///
/// `source_convert` IS MISSING and that is a known gap, not an oversight: §7.4
/// defines it as the container conversion of the master source, and the packager
/// does not produce that deliverable yet. It is added the day the packager emits
/// one — an empty list here would assert the run produced no conversions, which is
/// the same false statement the empty `master_video` was making.
///
/// `keyframes` stays: §7.4 defines the group and, unlike the two that were dropped,
/// it is a deliverable of a packaging run (§12 still lists it as an open question,
/// so it renders empty until the packager fills it).
#[derive(Debug, Serialize)]
pub struct PackagingOutput {
    pub streaming_video: Vec<StreamingVideoEntry>,
    pub thumbnail: Vec<ThumbnailEntry>,
    pub keyframes: Vec<JsonValue>,
}

impl Output {
    /// Narrows §7.1's group set to §7.4's, moving the three groups both shapes share.
    ///
    /// The two it drops are always EMPTY here, which is what makes the narrowing
    /// lossless rather than a discard: `master_video` is filled from
    /// `Outputs::maxed_mp4` and `proxy_video` from `Outputs::mp4_1fps`, and a
    /// packaging run sets neither (`vod-packager`'s `outputs_of` fills `streaming`
    /// alone). Should it ever produce a mezzanine, it will need a §7.4 group to
    /// report it in — the notification cannot invent one — so this is the place that
    /// would have to change, not a silent loss to find later.
    fn into_packaging(self) -> PackagingOutput {
        PackagingOutput {
            streaming_video: self.streaming_video,
            thumbnail: self.thumbnail,
            keyframes: self.keyframes,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct MasterVideoEntry {
    #[serde(rename = "type")]
    pub entry_type: &'static str,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<String>,
    /// §7.1 names this `size` on the wire; it is the object's byte length as
    /// read back from storage. The Rust field keeps `file_size` because that
    /// is what it is — a rename attribute is cheaper than a name that has to
    /// be disambiguated at every use.
    #[serde(rename = "size", skip_serializing_if = "Option::is_none")]
    pub file_size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    pub encoder: &'static str,
    pub media_profile: Vec<MediaProfile>,
}

#[derive(Debug, Serialize)]
pub struct StreamingVideoEntry {
    #[serde(rename = "type")]
    pub entry_type: &'static str,
    pub manifest_url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    pub format: &'static str,
    pub encryption: &'static str,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub audio_languages: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_bitrate: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_duration: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub segment_count: Option<u64>,
    pub encoder: &'static str,
}

#[derive(Debug, Serialize)]
pub struct ProxyVideoEntry {
    #[serde(rename = "type")]
    pub entry_type: &'static str,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    /// §7.1 `resolution`, reported from what the scale filter actually produced
    /// (see `derivatives::proxy_resolution_label`).
    ///
    /// An Option, and no longer the literal `"480p"`, for the same reason
    /// `MasterVideoEntry::resolution` is one: the height is a property of the
    /// file, and a source that declared none leaves it absent rather than
    /// asserted.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<String>,
    /// §7.1 names this `size` on the wire; it is the object's byte length as
    /// read back from storage. The Rust field keeps `file_size` because that
    /// is what it is — a rename attribute is cheaper than a name that has to
    /// be disambiguated at every use.
    #[serde(rename = "size", skip_serializing_if = "Option::is_none")]
    pub file_size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    pub encoder: &'static str,
    /// DELIBERATELY EMPTY. The 1fps proxy is re-encoded (`-vf fps=1`, libx264), so
    /// the source variant's declaration does not describe it: its frame rate is 1
    /// and its bitrate is whatever x264 produced. Forwarding the source profile
    /// would attribute the source's frame rate and bitrate to a file with neither.
    pub media_profile: Vec<MediaProfile>,
}

#[derive(Debug, Serialize)]
pub struct ThumbnailEntry {
    #[serde(rename = "type")]
    pub entry_type: &'static str,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cdn_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u64>,
    /// §7.1 `aspect_ratio`, REDUCED FROM the width and height beside it (see
    /// [`aspect_ratio_of`]).
    ///
    /// It was the literal `"16:9"` regardless of those dimensions, so a portrait
    /// or 4:3 source published one object that contradicted itself — real
    /// `1080x1920` next to `"16:9"` — and a consumer had no way to tell which
    /// half to believe. Absent when the manifest declared no RESOLUTION, since a
    /// ratio computed from nothing is a guess dressed as a measurement.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub aspect_ratio: Option<String>,
    pub format: &'static str,
    /// §7.1 names this `size` on the wire; it is the object's byte length as
    /// read back from storage. The Rust field keeps `file_size` because that
    /// is what it is — a rename attribute is cheaper than a name that has to
    /// be disambiguated at every use.
    #[serde(rename = "size", skip_serializing_if = "Option::is_none")]
    pub file_size: Option<u64>,
    /// §7.1 defines the thumbnail as the segment's FIRST frame, so 0 is asserted
    /// rather than defaulted.
    pub timestamp: f64,
}

impl Event {
    /// Build the §7 envelope this event is published as.
    ///
    /// The §4 `job` block is echoed from [`Identity::job`] and is not a parameter:
    /// there is nothing for a caller to choose, and passing it would be one more
    /// place it could be substituted for something re-rendered.
    ///
    /// THE SCHEDULE IS NO LONGER SHAPED HERE. §7 draws one schedule form, inside the
    /// echoed `job`, so the previous two-shape `ScheduleBlock` — wrapped for a clip,
    /// flat for lifecycle, following a spec inconsistency the worker could not
    /// resolve — is gone along with the inconsistency.
    pub fn envelope(&self) -> Envelope {
        // §6 has ONE state per message now. The old two-status rule — a closed
        // segment reporting `completed` for the payload while the job reported
        // `in_progress` — came from `data.status` and `data.job.status` being two
        // separate fields. §7 collapses them into `data.status.state`, and the
        // "payload is complete" fact is carried structurally instead: a segment
        // block with an `output` beside it IS the completed payload.
        let job_status = self.status.unwrap_or(STATUS_IN_PROGRESS);
        // `segment` vs `ad`, decided by which event produced this message. Only read
        // when a segment block is emitted at all.
        let segment_type = match self.event_type {
            TYPE_AD_BREAK_START | TYPE_AD_BREAK_END => SEGMENT_TYPE_AD,
            _ => SEGMENT_TYPE_SEGMENT,
        };
        // The SEGMENT's own state, which is not the job's. An ad break that has
        // only opened is still running, so it reports `in_progress`; a closed
        // run and a closed break are both `completed`. A failed message reports
        // the failure here too, because whatever ended the job ended this
        // segment with it.
        // The two timeline markers report `in_progress` for the same reason an
        // opened ad break does: the segment is still running. This is what keeps
        // them OFF the ingest path — a consumer routes on
        // `segment.status.state`, so a marker rendered `completed` here would be
        // taken for an assembled segment and would complete a clip that has no
        // media yet.
        let segment_state = if self.error.is_some() {
            STATUS_FAILED
        } else if matches!(
            self.event_type,
            TYPE_AD_BREAK_START | TYPE_SEGMENT_STARTED | TYPE_SEGMENT_ENDED
        ) {
            STATUS_IN_PROGRESS
        } else {
            STATUS_COMPLETED
        };
        // WHICH §7 SHAPE THIS DOCUMENT IS. §7 defines a different field set per job
        // type — `status.occurrence_index` on the live shapes only (§6's table marks
        // it `(live)`), and a different `output` group set for packaging (§7.4) than
        // for the capture shapes (§7.1 / §7.3) — so a field the spec does not define
        // for this job type must not be rendered for it.
        //
        // BOTH READ THE SAME ANSWER, from the job type the dispatch declared. Asking
        // the EVENT KIND instead would answer only two thirds of it: a packaging run
        // that fails reports through `status_event`, the same constructor a live
        // lifecycle document uses, so the constructor cannot tell the two apart.
        //
        // An unknown type (a standalone run, which echoes no job at all) renders the
        // capture shape: it is what every job type but packaging draws, and a run
        // with no dispatch has no occurrence to report.
        let is_live = self.job_type == JOB_TYPE_LIVE;
        let is_packaging = self.job_type == JOB_TYPE_PACKAGING;

        Envelope {
            header: Header {
                header_type: "notification",
                signature: self.signature.clone().unwrap_or_default(),
                posted_date: self.at.clone(),
            },
            data: Data {
                // The echo. Null only for a standalone run, which has no job.
                job: self.job.clone(),
                status: Status {
                    state: job_status,
                    // LIVE ONLY — §6 marks the field `(live)` and the §7.3 / §7.4
                    // status blocks are `{state, progress, error, updated_at}`. See
                    // `Status::occurrence_index` for why the gate is the job type
                    // rather than whether an index was supplied.
                    occurrence_index: is_live.then(|| self.occurrence_index.unwrap_or(0)),
                    progress: self.progress,
                    error: self.error.as_ref().map(EventError::clone_detail),
                    updated_at: self.at.clone(),
                },
                // Present exactly when this message is about a clip or an ad
                // boundary — which is what makes the kind readable from the blocks
                // alone. A lifecycle message has no segment_id, so it has no block.
                segment: self.segment_id.clone().map(|segment_id| Segment {
                    segment_id,
                    segment_type,
                    sequence: self.sequence.unwrap_or(0),
                    close_reason: self.close_reason,
                    start_epoch: self.start_pdt.as_deref().and_then(epoch_of),
                    end_epoch: self.end_pdt.as_deref().and_then(epoch_of),
                    start_time: self.start_pdt.clone(),
                    end_time: self.end_pdt.clone(),
                    // §7.2 gives an ad marker a `duration`, but the recorder only
                    // computes one for a published content run — an ad break is a
                    // boundary pair, not a file, so nothing measures it. The
                    // notifier used to close that gap with
                    // `default(duration_sec, end_epoch - start_epoch)`; moving the
                    // envelope here silently dropped it, and the missing field
                    // reached a real notification before this was caught.
                    //
                    // Derived from the boundaries only when both are known, so an
                    // open-ended marker still reports no duration rather than one
                    // computed against a missing edge.
                    duration: self.duration_sec.or_else(|| {
                        let start = self.start_pdt.as_deref().and_then(epoch_of)?;
                        let end = self.end_pdt.as_deref().and_then(epoch_of)?;
                        Some((end - start) as f64)
                    }),
                    status: SegmentStatus {
                        // The segment's own error, not the job's — but they are
                        // the same value here, because a message reporting a
                        // segment reports the failure that ended it.
                        error: self.error.as_ref().map(EventError::clone_detail),
                        // THIS SEGMENT'S PLACE IN THE RUNNING ORDER, not the
                        // job's occurrence — so it is `sequence`, not
                        // `data.status.occurrence_index`. Content runs and ad
                        // markers share one numbering space (see Sequencer), so
                        // a first content run is 1 and the ad that follows it is
                        // 2, which is exactly the order this reports.
                        occurrence_index: self.sequence.unwrap_or(0),
                        state: segment_state,
                    },
                }),
                // ONE MAPPING, TWO GROUP SETS: the entries are built the same way for
                // every job type, and only §7.4's narrower set of groups is drawn for
                // a packaging run. See `OutputBlock`.
                output: self.outputs.as_ref().map(|o| {
                    let out = o.to_output(self.duration_sec);
                    if is_packaging {
                        OutputBlock::Packaging(out.into_packaging())
                    } else {
                        OutputBlock::Capture(out)
                    }
                }),
                capture_position: self.capture_position.clone(),
                created_at: self.at.clone(),
            },
        }
    }
}

impl EventError {
    fn clone_detail(&self) -> EventError {
        EventError {
            code: self.code.clone(),
            details: self.details.clone(),
            stage: self.stage.clone(),
        }
    }
}

/// RFC 3339 -> Unix seconds, for §7's `start_epoch` / `end_epoch`. An unparseable
/// stamp yields None rather than 0: epoch 0 is a real instant (1970), and a
/// consumer must be able to tell "not reported" from "the Unix epoch".
fn epoch_of(rfc3339: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(rfc3339)
        .ok()
        .map(|dt| dt.timestamp())
}

/// §7.1's `aspect_ratio`, reduced from the dimensions actually reported:
/// `1920 x 1080` -> `"16:9"`.
///
/// Reduced by GCD so the label is the ratio's canonical form rather than the raw
/// dimensions restated. `None` when either dimension is missing or zero — a
/// ratio needs both sides, and zero is not a side.
fn aspect_ratio_of(width: Option<u64>, height: Option<u64>) -> Option<String> {
    let (w, h) = (width?, height?);
    if w == 0 || h == 0 {
        return None;
    }
    fn gcd(a: u64, b: u64) -> u64 {
        if b == 0 {
            a
        } else {
            gcd(b, a % b)
        }
    }
    let d = gcd(w, h);
    Some(format!("{}:{}", w / d, h / d))
}

impl Outputs {
    /// Map the deliverable set onto §7.1's five output groups.
    ///
    /// Each group is a list with at most one entry here — this worker produces one
    /// of each per segment — but they stay lists because that is the shape §7.1
    /// defines and the shape a multi-rendition worker would fill.
    fn to_output(&self, duration: Option<f64>) -> Output {
        let mut out = Output::default();
        if let Some(f) = &self.maxed_mp4 {
            out.master_video.push(MasterVideoEntry {
                entry_type: "source_mp4",
                url: f.uri.clone(),
                cdn_url: f.cdn_url.clone(),
                resolution: f.resolution.clone(),
                file_size: f.file_size,
                duration,
                encoder: "ffmpeg",
                media_profile: f.media_profile.clone(),
            });
        }
        if let Some(h) = &self.hls_preview {
            out.streaming_video.push(StreamingVideoEntry {
                // §5.2/§7.1 name this type; `maxed_hls` was this repo's own name
                // and matched neither the spec nor the previously-working worker.
                entry_type: "clear_hls_source",
                manifest_url: h.uri.clone(),
                cdn_url: h.cdn_url.clone(),
                format: "hls",
                // --generate-preview is stream-copy only and this worker handles
                // clear HLS, so there is nothing to declare but "none".
                encryption: "none",
                audio_languages: h.audio_languages.clone(),
                video_bitrate: h.video_bitrate,
                duration,
                segment_duration: h.segment_duration,
                segment_count: h.segment_count,
                encoder: "ffmpeg",
            });
        }
        // The packager's presentations, AFTER the clippers' preview so the order of
        // the group is stable: a run fills one or the other, never both, and
        // appending here means the preview entry keeps its position for consumers
        // that read `streaming_video[0]`.
        for s in &self.streaming {
            out.streaming_video.push(StreamingVideoEntry {
                // Per entry, unlike the preview's fixed `clear_hls_source`: one
                // packaging run publishes the same media as several HLS versions
                // and the version is what distinguishes them.
                entry_type: s.entry_type,
                manifest_url: s.uri.clone(),
                cdn_url: s.cdn_url.clone(),
                format: "hls",
                // The packager exposes no key options today (see its README's
                // "not yet done"), so the output is clear — asserted, not assumed.
                encryption: "none",
                audio_languages: s.audio_languages.clone(),
                video_bitrate: s.video_bitrate,
                // The entry's OWN duration, not the event's: see StreamingOutput.
                duration: s.duration,
                segment_duration: s.segment_duration,
                // Deliberately unanswered — see StreamingOutput's header for why an
                // estimate would be worse than the absence.
                segment_count: None,
                encoder: "ffmpeg",
            });
        }
        // THE MASTER STANDS IN WHEN NO PROXY WAS GENERATED. The scheduler decides
        // whether to ask for one (PROXY_1FPS); the clipper always produces a
        // master. When the proxy was not asked for — or its generation failed,
        // which is best-effort and leaves the slot empty the same way — this
        // group would otherwise go out empty, and a consumer that resolves the
        // segment's analysis source through it has nothing to resolve.
        //
        // NOT a cosmetic default. `genai_video_segment_analyse` looks its source
        // up as the media_asset whose media_type is exactly
        // "preview-one-frame-video", which N8N writes from THIS group's entry
        // (its `type` carrying "1frame" is the whole discriminator). With the
        // group empty the lookup returns nothing and segment enrichment fails on
        // every segment — observed, not predicted.
        //
        // The entry keeps `PROXY_ENTRY_TYPE` deliberately: that string is what
        // the consumer matches on, so changing it here would reintroduce the very
        // breakage this closes. Every measured field — resolution, file_size —
        // comes from the master's own OutputFile, so what is reported about the
        // file is true even though the slot it sits in is named for the proxy.
        let proxy_or_master = self.mp4_1fps.as_ref().or(self.maxed_mp4.as_ref());
        if let Some(f) = proxy_or_master {
            out.proxy_video.push(ProxyVideoEntry {
                entry_type: crate::derivatives::PROXY_ENTRY_TYPE,
                url: f.uri.clone(),
                cdn_url: f.cdn_url.clone(),
                // Carried from the OutputFile the producer built, exactly as
                // master_video does — the caller ran the scale filter and is the
                // only place that knows what came out of it.
                resolution: f.resolution.clone(),
                file_size: f.file_size,
                duration,
                encoder: "ffmpeg",
                media_profile: Vec::new(),
            });
        }
        if let Some(t) = &self.thumbnail {
            out.thumbnail.push(ThumbnailEntry {
                entry_type: "1frame",
                url: t.uri.clone(),
                cdn_url: t.cdn_url.clone(),
                width: t.width,
                height: t.height,
                aspect_ratio: aspect_ratio_of(t.width, t.height),
                format: "jpeg",
                file_size: t.file_size,
                timestamp: 0.0,
            });
        }
        out
    }
}

/// Outcome of one bounded write. A timeout stays distinct from a storage error:
/// it means the document was abandoned mid-flight, which is worth its own log
/// line (and, unlike an error, says nothing about whether the target is healthy).
enum WriteOutcome {
    Written,
    Failed(anyhow::Error),
    TimedOut,
}

/// Best-effort writer of the status documents.
pub struct StatusWriter {
    /// Directory URI the documents are written under (`STATUS_URI`). `None` is
    /// standalone mode: documents are logged, never written.
    dir_uri: Option<String>,
    identity: Identity,
    /// How long one document write may take before it is abandoned
    /// (`--status-write-timeout-secs`).
    write_timeout: Duration,
    /// Identifies this process's run, so a re-run of the same job writes to a
    /// fresh sub-prefix instead of overwriting the previous run's documents.
    run_id: String,
    /// Monotonic per-process emit counter, ordering the documents by name.
    emitted: AtomicU32,
    /// Pipeline stage reported as `error.stage` if the run fails.
    stage: StdMutex<&'static str>,
    /// How far capture has got in media time, attached to every status document.
    ///
    /// Held here rather than passed in at each emission point because the
    /// terminal `failed` document is written by `main` from OUTSIDE the capture
    /// loop — the position would otherwise be out of scope exactly where it
    /// matters most, on the run that did not finish.
    position: StdMutex<Option<CapturePosition>>,
}

impl StatusWriter {
    /// Builds the writer from the job configuration. Reporting is enabled only
    /// when both a `status_uri` AND a `task` are given; otherwise the run is
    /// standalone and every document is logged instead of written.
    /// `task` is the decoded `TASK_JSON`, or `None` for a standalone run.
    ///
    /// THE SECOND CONDITION IS THE TASK, NOT A JOB ID. A document with no
    /// identity cannot be attributed to a job, and the notifier keys everything
    /// it does on `job_id` — which comes FROM the task, falling back to
    /// `fallback_job_id` only to label the documents of a misconfigured dispatch.
    /// So the task is what has to be present; there is no `JOB_ID` argument to
    /// set.
    ///
    /// It is passed in already decoded rather than parsed here because a malformed
    /// task must fail the process at startup (see [`Task::parse`]), and this
    /// constructor cannot fail — every other reporting failure is deliberately
    /// swallowed, so a parse error raised here would be swallowed with them.
    /// The four values are passed one by one rather than as the live clipper's
    /// `Args`: `vod-hls2mp4` builds the same writer from its own argument struct,
    /// and a constructor typed on one binary's CLI cannot be shared. They are
    /// exactly the fields `from_args` ever read.
    pub fn from_task(
        status_uri: Option<&str>,
        fallback_job_id: &str,
        occurrence_index: u32,
        status_write_timeout_secs: u64,
        task: Option<&Task>,
    ) -> Self {
        let identity = match task {
            Some(t) => Identity {
                // A task whose job_id is empty still labels its documents with the
                // event id, so a misconfigured dispatch produces attributable
                // output instead of documents keyed on "".
                job_id: non_empty(&t.data.job.job_id)
                    .unwrap_or_else(|| fallback_job_id.to_string()),
                signature: non_empty(&t.header.signature),
                // Which §7 shape this run's documents take — the caller's own
                // `job.job_type`, read from the block that is echoed back, so the
                // shape rendered and the type published cannot disagree.
                job_type: t.data.job.job_type.clone(),
                // Read from the same block, for the same reason: both steer what
                // this run emits, so they are taken from the task rather than
                // re-derived anywhere else.
                media_id: t.data.job.media_id.clone(),
                manual: t.data.job.is_manual(),
                // From the ORCHESTRATOR, not the task — see `Identity::occurrence_index`.
                occurrence_index: Some(occurrence_index),
                // THE ECHO, TAKEN RAW. Deliberately `job_raw` and not the typed
                // `job` view beside it: re-serializing the typed view would hand the
                // caller back only the fields this worker happens to model, which is
                // exactly what §7's "never mutated by the worker" forbids.
                job: t.data.job_raw.clone(),
            },
            // Standalone runs have no orchestration job; the event id labels the
            // logged documents so they are still attributable.
            None => Identity {
                job_id: fallback_job_id.to_string(),
                ..Default::default()
            },
        };
        // Reporting needs BOTH a destination and a task: a document with no identity
        // cannot be attributed to a job, and the notifier keys everything it does on
        // job_id. Either one missing means the run reports to the log instead.
        let dir_uri = match (status_uri, task) {
            (Some(uri), Some(_)) => Some(uri),
            _ => None,
        };
        Self::new(
            dir_uri,
            identity,
            Duration::from_secs(status_write_timeout_secs),
        )
    }

    /// Builds the writer for `dir_uri` (`None` disables writing), bounding every
    /// write by `write_timeout`. The target URI is parsed once here so an
    /// unusable one disables reporting loudly rather than failing on every event.
    pub fn new(dir_uri: Option<&str>, identity: Identity, write_timeout: Duration) -> Self {
        let dir_uri = match dir_uri {
            Some(uri) => match storage::parse_store(uri) {
                Ok(_) => Some(uri.trim_end_matches('/').to_string()),
                Err(e) => {
                    eprintln!(
                        "-> WARNING: STATUS_URI '{uri}' is unusable ({e:#}); status reporting is off."
                    );
                    None
                }
            },
            None => None,
        };
        let writer = Self {
            dir_uri,
            identity,
            write_timeout,
            run_id: run_id(Utc::now()),
            emitted: AtomicU32::new(0),
            stage: StdMutex::new(STAGE_SETUP),
            position: StdMutex::new(None),
        };
        // Log the run's own prefix, so an operator can find its documents.
        match (writer.is_enabled(), &writer.dir_uri) {
            (true, Some(uri)) => println!(
                "-> Writing status documents to {uri}/{}/{}/",
                writer.identity.job_id,
                writer.run_id()
            ),
            _ => println!(
                "-> Status reporting is off (no STATUS_URI / JOB_ID); documents are logged only."
            ),
        }
        writer
    }

    /// Whether this run is a manual clip. Read from the task at construction, so
    /// the recorder branches on the same value the notifier renders ids from and
    /// the two cannot disagree.
    pub fn is_manual(&self) -> bool {
        self.identity.manual
    }

    /// Whether documents are written (`false` = standalone, log-only).
    pub fn is_enabled(&self) -> bool {
        self.dir_uri.is_some()
    }

    /// This process's run token, the middle path element of every object name.
    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// Records the stage the pipeline is in, so a later failure reports where it
    /// happened (`error.stage`).
    pub fn set_stage(&self, stage: &'static str) {
        if let Ok(mut current) = self.stage.lock() {
            *current = stage;
        }
    }

    fn stage(&self) -> String {
        self.stage
            .lock()
            .map(|s| (*s).to_string())
            .unwrap_or_else(|_| STAGE_CAPTURE.to_string())
    }

    /// Records the media position reached by the chunk just ingested, so every
    /// status document from here on says where capture actually is. Called once
    /// per ingested chunk from the capture loop — a poisoned lock loses the
    /// update (and only the update), like every other reporting failure here.
    pub fn set_capture_position(&self, position: CapturePosition) {
        if let Ok(mut current) = self.position.lock() {
            *current = Some(position);
        }
    }

    /// The last recorded position, or `None` if nothing has been ingested yet.
    ///
    /// Also the capture loop's own answer to "where did the media stop?" when it
    /// has to close an ad break the schedule expired inside — one tracked value,
    /// so the `end_pdt` published for that break and the `last_pdt` reported on
    /// the terminal document cannot disagree.
    pub fn capture_position(&self) -> Option<CapturePosition> {
        self.position.lock().ok().and_then(|p| p.clone())
    }

    /// `type=status` — a lifecycle transition, carrying the media position
    /// reached so far.
    pub async fn status(&self, status: &'static str, progress: u8, error: Option<EventError>) {
        self.write(self.identity.status_event(
            Utc::now(),
            status,
            progress,
            error,
            self.capture_position(),
        ))
        .await;
    }

    /// `status=in_progress` — the capture window is open and recording.
    pub async fn recording(&self) {
        self.status(STATUS_IN_PROGRESS, PROGRESS_STARTED, None)
            .await;
    }

    /// `status=completed` — the event window was recorded and published.
    pub async fn completed(&self) {
        self.status(STATUS_COMPLETED, PROGRESS_DONE, None).await;
    }

    /// `status=failed` — a terminal error, tagged with the current stage.
    pub async fn failed(&self, code: &str, details: &str) {
        let error = EventError {
            code: code.to_string(),
            details: details.to_string(),
            stage: self.stage(),
        };
        self.status(STATUS_FAILED, PROGRESS_STARTED, Some(error))
            .await;
    }

    /// `type=segment_started` — a content run opened; the timeline can draw it
    /// now rather than waiting for assembly.
    pub async fn segment_started(&self, sequence: u32, start: DateTime<Utc>) {
        self.write(
            self.identity
                .segment_started_event(Utc::now(), sequence, start),
        )
        .await;
    }

    /// `type=segment_ended` — a content run's boundary is known. Emitted at the
    /// boundary, ahead of the deliverables, so the pair start/end describes the
    /// whole segment while `segment_closed` is still minutes away.
    pub async fn segment_ended(
        &self,
        sequence: u32,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
        close_reason: &'static str,
    ) {
        self.write(self.identity.segment_ended_event(
            Utc::now(),
            sequence,
            start,
            end,
            close_reason,
        ))
        .await;
    }

    /// `type=segment_closed` — one content run finished and its deliverables are
    /// in storage.
    pub async fn segment_closed(
        &self,
        sequence: u32,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
        close_reason: &'static str,
        outputs: Outputs,
    ) {
        self.write(self.identity.segment_closed_event(
            Utc::now(),
            sequence,
            start,
            end,
            close_reason,
            outputs,
        ))
        .await;
    }

    /// §7.3 — the VOD capture finished and its one deliverable set is in storage.
    /// Terminal: nothing follows it, so it reports `completed` itself rather than
    /// leaving a separate lifecycle document to say so.
    pub async fn vod_completed(&self, duration_sec: f64, outputs: Outputs) {
        self.write(
            self.identity
                .vod_completed_event(Utc::now(), duration_sec, outputs),
        )
        .await;
    }

    /// §7.4 — the packaging run finished and its manifests are in storage.
    ///
    /// TERMINAL AND SOLITARY: it is the only document the run writes, so nothing
    /// precedes it and the caller must not follow it with [`Self::completed`] (which
    /// would publish a second, output-less completion for the same job). See
    /// [`Identity::packaging_completed_event`] for why there is no duration argument.
    pub async fn packaging_completed(&self, outputs: Outputs) {
        self.write(self.identity.packaging_completed_event(Utc::now(), outputs))
            .await;
    }

    /// One span of a split VOD capture — see [`Identity::vod_segment_event`].
    /// NOT terminal: the run reports `completed` once, after its last span.
    pub async fn vod_segment(&self, span: &VodSpan, outputs: Outputs) {
        self.write(self.identity.vod_segment_event(Utc::now(), span, outputs))
            .await;
    }

    /// `type=ad_break_start` — an ad break opened at `start_pdt`.
    pub async fn ad_break_start(&self, sequence: u32, start_pdt: DateTime<Utc>) {
        self.write(self.identity.ad_break_event(
            Utc::now(),
            TYPE_AD_BREAK_START,
            sequence,
            Some(start_pdt),
            None,
        ))
        .await;
    }

    /// `type=ad_break_end` — the break cued back in at `end_pdt`. `start_pdt` is
    /// absent if the break was already under way when capture began.
    pub async fn ad_break_end(
        &self,
        sequence: u32,
        start_pdt: Option<DateTime<Utc>>,
        end_pdt: DateTime<Utc>,
    ) {
        self.write(self.identity.ad_break_event(
            Utc::now(),
            TYPE_AD_BREAK_END,
            sequence,
            start_pdt,
            Some(end_pdt),
        ))
        .await;
    }

    /// `type=ad_break_end` with `close_reason: schedule_end` — LLD §4.1: the
    /// capture window ran out while the break was still open, so no cue-in will
    /// ever arrive for it.
    ///
    /// Without this the marker stays `open` with no `end_pdt` for ever, and
    /// because the notifier publishes the §7.2 ad envelope only on an
    /// `ad_break_end`, the break is never notified AT ALL — not merely notified
    /// late. `end_pdt` is where the media stopped, so it must be the ingested
    /// PDT and never `Utc::now()` (which is minutes later, after assembly).
    ///
    /// It reuses the `sequence` the break's start allocated, so it carries the
    /// same `segment_id` and the `(schedule_id, sequence)` upsert downstream
    /// UPDATES that marker instead of minting a second one.
    pub async fn ad_break_ended_by_schedule(
        &self,
        sequence: u32,
        start_pdt: Option<DateTime<Utc>>,
        end_pdt: DateTime<Utc>,
    ) {
        self.write(self.identity.ad_break_ended_by_schedule_event(
            Utc::now(),
            sequence,
            start_pdt,
            end_pdt,
        ))
        .await;
    }

    /// Object name of one status document:
    /// `<job-id>/<run-id>/<emit-counter>-<type>.json`.
    ///
    /// - the leading `job_id` groups every document of a job under one prefix;
    /// - `run_id` (process start stamp + pid) keeps a re-run of the same job from
    ///   overwriting the previous run's documents;
    /// - the zero-padded emit counter sorts the documents in emission order (the
    ///   segment `sequence` cannot: content segments and ad markers share one
    ///   sequence space, and a marker's number appears on two events);
    /// - the `type` suffix makes a bucket listing readable.
    fn object_name(&self, event_type: &str, emitted: u32) -> String {
        format!(
            "{}/{}/{:05}-{}.json",
            self.identity.job_id, self.run_id, emitted, event_type
        )
    }

    /// Writes one status document, best-effort: serialization failures, storage
    /// failures and a write that outlives the timeout are all logged and
    /// swallowed so reporting can never break a recording. The object is closed
    /// before returning — a storage trigger only fires on a finalized object.
    async fn write(&self, event: Event) {
        // THE ENVELOPE IS WHAT LANDS IN STORAGE. Everything downstream — the
        // notifier's publish and ms-api's decode — reads this exact block, so the
        // §7 contract is decided here and nowhere else.
        let envelope = event.envelope();
        let body = match serde_json::to_vec(&envelope) {
            Ok(body) => body,
            Err(e) => {
                eprintln!(
                    "-> WARNING: serializing the '{}' status document failed: {e}",
                    event.event_type
                );
                return;
            }
        };
        // 1-based, and allocated even when disabled so the logged names match
        // what a configured run would have written.
        let emitted = self.emitted.fetch_add(1, Ordering::Relaxed) + 1;
        let name = self.object_name(event.event_type, emitted);

        let Some(dir_uri) = &self.dir_uri else {
            println!(
                "-> Status document (not written, standalone) {name}: {}",
                String::from_utf8_lossy(&body)
            );
            return;
        };
        let uri = format!("{dir_uri}/{name}");
        match self.bounded(self.write_object(&uri, &body)).await {
            WriteOutcome::Written => println!(
                "-> Status document written to {uri}: {}",
                String::from_utf8_lossy(&body)
            ),
            WriteOutcome::Failed(e) => eprintln!(
                "-> WARNING: writing the '{}' status document to {uri} failed ({e:#}); continuing.",
                event.event_type
            ),
            // Losing the stream is worse than losing one document, so the
            // recording carries on with a gap in the reported timeline.
            WriteOutcome::TimedOut => eprintln!(
                "-> WARNING: writing the '{}' status document to {uri} (job {}, sequence {}) \
                 timed out after {}s and was abandoned; continuing the recording.",
                event.event_type,
                self.identity.job_id,
                event
                    .sequence
                    .map_or_else(|| "-".to_string(), |s| s.to_string()),
                self.write_timeout.as_secs()
            ),
        }
    }

    /// Runs one write under the configured timeout. `object_store` imposes no
    /// deadline of its own, and every emission point is on the capture loop's
    /// thread of control, so an upload that hangs would stall the recording
    /// itself. Dropping the future on expiry cancels the in-flight upload: the
    /// object simply never finalizes, and no storage event fires for it.
    ///
    /// A zero timeout means "no deadline", not "expire at once": an operator who
    /// sets 0 is disabling the bound, and treating it literally would abandon
    /// every document instead.
    ///
    /// Kept separate from [`Self::write`] so the policy is testable without a
    /// storage backend.
    async fn bounded(&self, write: impl Future<Output = Result<()>>) -> WriteOutcome {
        if self.write_timeout.is_zero() {
            return match write.await {
                Ok(()) => WriteOutcome::Written,
                Err(e) => WriteOutcome::Failed(e),
            };
        }
        match tokio::time::timeout(self.write_timeout, write).await {
            Ok(Ok(())) => WriteOutcome::Written,
            Ok(Err(e)) => WriteOutcome::Failed(e),
            Err(_) => WriteOutcome::TimedOut,
        }
    }

    /// Streams `body` to a single object and finalizes it.
    async fn write_object(&self, uri: &str, body: &[u8]) -> Result<()> {
        let mut writer = storage::single_object_writer(uri)?;
        writer.write_all(body).await?;
        // Without the shutdown the multipart upload is never completed, so the
        // object never finalizes and no storage event fires. It is inside the
        // timeout for the same reason: finalization is the part that matters.
        writer.shutdown().await?;
        Ok(())
    }
}

/// Token identifying one process run: the start stamp (millisecond precision, so
/// two runs of the same job never collide) plus the pid.
/// e.g. `20260805T101530123Z-1`.
fn run_id(started: DateTime<Utc>) -> String {
    format!(
        "{}-{}",
        started.format("%Y%m%dT%H%M%S%3fZ"),
        std::process::id()
    )
}

/// `2026-08-04T19:12:00Z` — RFC 3339 UTC, second precision, `Z` suffix.
fn rfc3339(dt: DateTime<Utc>) -> String {
    dt.to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// Serializes a media timestamp in the same RFC 3339 form as every other stamp
/// on the wire — the envelope's `start_pdt` / `end_pdt` are formatted by
/// [`rfc3339`] before they are stored, so this keeps `last_pdt` identical
/// without making the field a string in the type.
fn serialize_rfc3339<S: Serializer>(dt: &DateTime<Utc>, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&rfc3339(*dt))
}

/// What the source variant declared about itself, carried from the master
/// playlist to the outputs that are byte-faithful to it.
///
/// One value threaded through the run instead of re-derived per output: the
/// declaration is a property of the captured variant, so every output that is a
/// stream-copy of it reports the SAME profile, and any divergence would mean two
/// places disagreeing about one fact.
#[derive(Debug, Clone, Copy, Default)]
pub struct SourceProfile<'a> {
    pub width: Option<u64>,
    pub height: Option<u64>,
    pub frame_rate: Option<f64>,
    /// Frames per second MEASURED from the recorded MPEG-TS, when it could be
    /// measured (see [`crate::tsprobe`]). Takes precedence over
    /// `frame_rate` in [`Self::video_profile`].
    ///
    /// It exists because `FRAME-RATE` is OPTIONAL in RFC 8216 and plenty of
    /// origins omit it, which used to mean the §7.1 `media_profile` carried no
    /// frame rate at all — nothing in the manifest can supply one honestly
    /// (`CODECS` bounds macroblocks per second, not frames; `BANDWIDTH` says
    /// nothing; and §4.3.4.3 forbids the attribute on the I-frame playlist).
    ///
    /// It is a separate field rather than a fallback written into `frame_rate`
    /// so the two can never be confused: one is what the origin ADVERTISED, the
    /// other is what the bytes actually did.
    pub measured_frame_rate: Option<f64>,
    /// The variant's `CODECS` attribute, verbatim (RFC 6381, e.g.
    /// `avc1.4d401f,mp4a.40.2`).
    pub codecs: Option<&'a str>,
    /// `AVERAGE-BANDWIDTH`, reported as the video track's `bitRate` by product
    /// ruling (average, not the `BANDWIDTH` peak). Falls back to `BANDWIDTH` when
    /// the manifest omits the average.
    ///
    /// KNOWN IMPRECISION, recorded so nobody rediscovers it as a bug: the manifest
    /// declares this per VARIANT — video and audio multiplexed — while §7.1 places
    /// `bitRate` inside a per-track entry. The video track is therefore credited
    /// with the audio's share too (~128 kbps on this source). An exact per-track
    /// rate would need the media opened, which this worker deliberately does not do.
    pub average_bandwidth: Option<u64>,
    /// What the EXT-X-MEDIA alternatives declared (see hls::Renditions). These are
    /// what let the `audio` and `cc` media_profile groups exist at all — without
    /// them the profile could only ever describe the video track.
    pub audio_languages: &'a [String],
    pub audio_group: Option<&'a str>,
    pub audio_channels: Option<&'a str>,
    pub cc_language: Option<&'a str>,
    pub cc_instream_id: Option<&'a str>,
}

impl<'a> SourceProfile<'a> {
    /// The profile a parsed variant declares, straight from its attributes.
    ///
    /// ONE MAPPING, TWO CLIPPERS. Both describe the source from the same ten
    /// manifest attributes, and two hand-written copies of that would drift the
    /// first time an attribute was added to one of them.
    ///
    /// `measured_frame_rate` is left `None` here on purpose: nothing in a manifest
    /// can supply it, and the caller folds in what its own bytes measured (the
    /// recorder for live, the probed span for VOD).
    pub fn of(r: &'a crate::hls::Renditions) -> Self {
        Self {
            width: r.resolution.map(|(w, _)| w),
            height: r.resolution.map(|(_, h)| h),
            frame_rate: r.frame_rate,
            measured_frame_rate: None,
            codecs: r.codecs.as_deref(),
            average_bandwidth: r.average_bandwidth,
            audio_languages: &r.audio_languages,
            audio_group: r.audio_group.as_deref(),
            audio_channels: r.audio_channels.as_deref(),
            cc_language: r.cc_language.as_deref(),
            cc_instream_id: r.cc_instream_id.as_deref(),
        }
    }

    /// The audio codec the variant DECLARES, mapped from its RFC 6381 tag — the
    /// same list `video_codec` reads, taking the audio family instead.
    pub fn audio_codec(&self) -> Option<String> {
        self.codecs?.split(',').find_map(|tag| {
            match tag.trim().split('.').next()?.to_ascii_lowercase().as_str() {
                "mp4a" => Some("aac".to_string()),
                "ac-3" => Some("ac3".to_string()),
                "ec-3" => Some("eac3".to_string()),
                "opus" => Some("opus".to_string()),
                _ => None,
            }
        })
    }

    /// The caption format implied by `INSTREAM-ID`.
    ///
    /// READ, NOT ASSUMED. §7.1's example shows `WebVTT`, but a `CC1`-`CC4` id is
    /// CEA-608 and a `SERVICEn` id is CEA-708 — both embedded in the video rather
    /// than delivered as a WebVTT sidecar. Reporting `WebVTT` for an INSTREAM-ID
    /// source would tell a consumer to look for a file that does not exist.
    fn cc_format(&self) -> Option<&'static str> {
        let id = self.cc_instream_id?.to_ascii_uppercase();
        if id.contains("SERVICE") {
            Some("CEA-708")
        } else if id.contains("CC") {
            Some("CEA-608")
        } else {
            None
        }
    }

    /// `channel_layouts` for a declared channel COUNT. HLS states how many
    /// channels there are, never which is which, so only the unambiguous mono and
    /// stereo cases are named; any other count yields nothing rather than an
    /// invented mapping.
    fn channel_layouts(&self) -> Vec<ChannelLayout> {
        let count: u32 = match self.audio_channels.and_then(|c| c.split('/').next()) {
            Some(first) => match first.trim().parse() {
                Ok(n) => n,
                Err(_) => return Vec::new(),
            },
            None => return Vec::new(),
        };
        let names: &[&'static str] = match count {
            1 => &["M"],
            2 => &["L", "R"],
            _ => return Vec::new(),
        };
        names
            .iter()
            .enumerate()
            .map(|(i, ch)| ChannelLayout {
                channel_locator: (i + 1).to_string(),
                audio_channel: ch,
            })
            .collect()
    }
}

impl SourceProfile<'_> {
    /// The video codec name the variant DECLARES, mapped from its RFC 6381 tag.
    ///
    /// Read rather than assumed. Hardcoding `h264` would be right for most live
    /// sources and silently wrong for an HEVC one — and a media_profile naming the
    /// wrong codec is worse than one naming none, because a consumer has no way to
    /// tell it is being misinformed.
    ///
    /// `CODECS` lists EVERY track of the variant, so this takes the first entry
    /// whose prefix is a known video codec and ignores the audio tags beside it.
    /// An unrecognised tag yields `None`: a codec family this does not know is not
    /// a codec it should name.
    pub fn video_codec(&self) -> Option<String> {
        self.codecs?.split(',').find_map(|tag| {
            let tag = tag.trim();
            // RFC 6381 tags are `<fourcc>.<profile-specific suffix>`; the fourcc
            // prefix is what identifies the family.
            match tag.split('.').next()?.to_ascii_lowercase().as_str() {
                "avc1" | "avc3" => Some("h264".to_string()),
                "hvc1" | "hev1" => Some("hevc".to_string()),
                "av01" => Some("av1".to_string()),
                "vp09" | "vp9" => Some("vp9".to_string()),
                _ => None,
            }
        })
    }

    /// The §7.1 `resolution` label — the declared height, as `<h>p`.
    ///
    /// Height, not width, because that is how the industry names a rendition and
    /// how §7.1's example ("1080p") and the previously-working worker ("720p")
    /// both label it. `None` without a declared height: a label invented from
    /// the bitrate would be a guess dressed as a measurement.
    pub fn resolution_label(&self) -> Option<String> {
        self.height.map(|h| format!("{h}p"))
    }

    /// The §7.1 video `media_profile` for an output that is byte-faithful to the
    /// source (the stream-copied master).
    ///
    /// `codec` is passed in rather than taken from CODECS because that attribute
    /// is an RFC 6381 list covering every track of the variant
    /// (`avc1.4d401f,mp4a.40.2`); splitting it into per-track codec names is a
    /// parse this does not do, so the caller states the codec it knows.
    ///
    /// Returns an EMPTY vec when the manifest declared nothing — the wire then
    /// omits `media_profile` entirely instead of carrying a hollow entry.
    pub fn video_profile(&self, codec: Option<String>) -> Vec<MediaProfile> {
        let frame_size = match (self.width, self.height) {
            (Some(width), Some(height)) => Some(FrameSize { width, height }),
            _ => None,
        };
        // MEASUREMENT FIRST, declaration second. `measured_frame_rate` comes
        // from the recorded stream's own presentation stamps and describes the
        // bytes being published; `frame_rate` is what the origin advertised
        // about a variant. Where both exist the measurement wins, and where the
        // manifest omitted FRAME-RATE entirely — which RFC 8216 permits, and
        // which used to leave this field empty — the measurement is the only
        // answer there is.
        //
        // Either way it is a decimal, while §7.1 wants a rational. Scaling by
        // 1000 keeps 29.97 as 29970/1000 rather than rounding it to 30 — a wrong
        // frame rate silently shifts every frame-accurate edit downstream.
        let frame_rate = self
            .measured_frame_rate
            .or(self.frame_rate)
            .map(|fps| FrameRate {
                numerator: (fps * 1000.0).round() as u32,
                denominator: 1000,
            });
        // The VIDEO group is emitted only when the variant declared something about
        // the video track. This guard is per-group, not per-profile: a source that
        // declares an audio rendition or captions but no video attributes must
        // still yield those groups rather than nothing at all.
        let mut groups = Vec::new();
        if frame_size.is_some()
            || frame_rate.is_some()
            || codec.is_some()
            || self.average_bandwidth.is_some()
        {
            groups.push(MediaProfile::video(TrackInfo {
                codec,
                // AVERAGE-BANDWIDTH per product ruling; see the field's note on
                // SourceProfile for the imprecision it carries.
                bit_rate: self.average_bandwidth,
                frame_rate,
                frame_size,
                sample_rate: None,
                offset_sec: 0,
            }));
        }

        // The AUDIO group, when the variant references one. Every value is
        // declared: codec from CODECS, language and group from EXT-X-MEDIA,
        // channel layout from CHANNELS. NO bitRate and NO sampleRate — HLS
        // declares neither, and the numbers that appear inside packager-chosen
        // names are a convention, not an assertion (see hls::Renditions).
        let audio_codec = self.audio_codec();
        let language = self.audio_languages.first().cloned();
        if audio_codec.is_some() || language.is_some() || self.audio_group.is_some() {
            groups.push(MediaProfile {
                group_type: "audio",
                audio_kind: Some("PRM"),
                audio_group: self.audio_group.map(str::to_string),
                language,
                format: None,
                offset_sec: None,
                track_info: Some(TrackInfo {
                    codec: audio_codec,
                    bit_rate: None,
                    frame_rate: None,
                    frame_size: None,
                    sample_rate: None,
                    offset_sec: 0,
                }),
                channel_layouts: self.channel_layouts(),
            });
        }

        // The CC group. It carries offset_sec at the TOP level and has no
        // track_info at all — §7.1 draws it that way because captions are not a
        // media track with a codec and a bitrate.
        if let Some(format) = self.cc_format() {
            groups.push(MediaProfile {
                group_type: "cc",
                audio_kind: None,
                audio_group: None,
                language: self.cc_language.map(str::to_string),
                format: Some(format),
                offset_sec: Some(0),
                track_info: None,
                channel_layouts: Vec::new(),
            });
        }
        groups
    }
}

/// Maps a `gs://bucket/object` URI onto `base + "/" + object`, but ONLY when the
/// object lives in `served_bucket` — the one bucket the CDN fronts. Every other
/// input yields `None`, so the field is omitted rather than carrying a URL the
/// player cannot fetch.
///
/// THE BUCKET CHECK IS THE POINT, not a formality. Our two destinations share a
/// key scheme — both nest `<job_id>/<event>-<start>-<end>.<ext>` — so dropping
/// the bucket and grafting the key onto the CDN host would produce a
/// well-formed URL that 404s, or worse resolves to a same-keyed object that
/// happens to exist on the other side. It also keeps the policy where it
/// belongs: which deliverables are served is decided by which bucket the clipper
/// wrote them to, not by a rule here that has to be kept in step with the
/// layout. The recorded mezzanine lives in the source bucket and therefore never
/// gets a `cdn_url` — that is correct, not an omission.
///
/// `base` is expected without a trailing slash (see [`cdn_base`]); an empty base
/// disables the field entirely, which is what an unset CDN_BASE_URL means.
pub fn cdn_url(gs_uri: &str, base: &str, served_bucket: &str) -> Option<String> {
    if base.is_empty() || served_bucket.is_empty() {
        return None;
    }
    let rest = gs_uri.strip_prefix("gs://")?;
    // A URI naming no object is not an object URI: "gs://bucket" and
    // "gs://bucket/" both yield None rather than a bare-host URL.
    let (bucket, object) = rest.split_once('/')?;
    if bucket.is_empty() || object.is_empty() || bucket != served_bucket {
        return None;
    }
    Some(format!("{base}/{object}"))
}

/// Normalises the configured CDN base: trimmed, and without the trailing slash
/// that would double against the `/` [`cdn_url`] inserts. Empty when unset, which
/// omits every `cdn_url`.
pub fn cdn_base(configured: Option<&str>) -> String {
    configured
        .unwrap_or("")
        .trim()
        .trim_end_matches('/')
        .to_string()
}

/// The bucket of a `gs://bucket/...` URI, or `""` when the input is not one.
/// Used to name the served bucket from the preview destination the clipper was
/// given, so there is no second copy of that fact to drift.
pub fn bucket_of(gs_uri: &str) -> &str {
    match gs_uri.strip_prefix("gs://") {
        Some(rest) => rest.split('/').next().unwrap_or(""),
        None => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The codec is READ from the variant declaration, never assumed. CODECS lists
    /// every track, so the video tag has to be picked out from beside the audio
    /// ones — and an unknown family must yield nothing rather than a guess.
    #[test]
    fn the_video_codec_comes_from_the_declared_codecs_tag() {
        let p = |c| SourceProfile {
            width: None,
            height: None,
            frame_rate: None,
            measured_frame_rate: None,
            codecs: Some(c),
            average_bandwidth: None,
            ..Default::default()
        };
        assert_eq!(
            p("avc1.4d401f,mp4a.40.2").video_codec().as_deref(),
            Some("h264")
        );
        // Audio first: position must not decide the answer.
        assert_eq!(
            p("mp4a.40.2,avc1.64001f").video_codec().as_deref(),
            Some("h264")
        );
        assert_eq!(p("hvc1.2.4.L120.90").video_codec().as_deref(), Some("hevc"));
        assert_eq!(p("av01.0.08M.08").video_codec().as_deref(), Some("av1"));
        // Audio only, and an unknown family: no codec rather than a wrong one.
        assert_eq!(p("mp4a.40.2").video_codec(), None);
        assert_eq!(p("xyz9.1").video_codec(), None);
        // Nothing declared at all.
        assert_eq!(
            SourceProfile {
                width: None,
                height: None,
                frame_rate: None,
                measured_frame_rate: None,
                codecs: None,
                average_bandwidth: None,
                ..Default::default()
            }
            .video_codec(),
            None
        );
    }

    /// 29.97 must survive as a rational, not round to 30 — a wrong frame rate
    /// shifts every frame-accurate edit downstream.
    #[test]
    fn a_fractional_frame_rate_is_not_rounded_away() {
        let prof = SourceProfile {
            width: Some(1280),
            height: Some(720),
            frame_rate: Some(29.97),
            measured_frame_rate: None,
            codecs: Some("avc1.4d401f"),
            average_bandwidth: Some(3_316_000),
            ..Default::default()
        };
        assert_eq!(prof.resolution_label().as_deref(), Some("720p"));
        let entries = prof.video_profile(prof.video_codec());
        assert_eq!(entries.len(), 1);
        let t = entries[0].track_info.as_ref().expect("track_info");
        let fr = t.frame_rate.as_ref().expect("frame_rate");
        assert_eq!((fr.numerator, fr.denominator), (29970, 1000));
        let fs = t.frame_size.expect("frame_size");
        assert_eq!((fs.width, fs.height), (1280, 720));
        assert_eq!(t.codec.as_deref(), Some("h264"));
        // AVERAGE-BANDWIDTH, not the BANDWIDTH peak.
        assert_eq!(t.bit_rate, Some(3_316_000));
    }

    /// A measured rate beats a declared one, and supplies the field when the
    /// manifest declared nothing at all.
    ///
    /// `FRAME-RATE` is OPTIONAL in RFC 8216, so the second case is not a corner:
    /// an origin that omits it used to leave `media_profile` with no frame rate
    /// whatsoever, which is what `tsprobe` exists to fix. The first case matters
    /// because the two can legitimately disagree — the manifest describes the
    /// variant the origin advertises, the probe describes the bytes actually
    /// recorded, and only the latter is a statement about what was published.
    #[test]
    fn a_measured_frame_rate_outranks_the_declared_one() {
        let declared_only = SourceProfile {
            height: Some(720),
            frame_rate: Some(25.0),
            measured_frame_rate: None,
            ..Default::default()
        };
        let measured_too = SourceProfile {
            measured_frame_rate: Some(29.97),
            ..declared_only
        };
        let measured_only = SourceProfile {
            height: Some(720),
            frame_rate: None,
            measured_frame_rate: Some(29.97),
            ..Default::default()
        };

        let rate_of = |p: &SourceProfile| {
            p.video_profile(None)
                .first()
                .and_then(|e| e.track_info.as_ref())
                .and_then(|t| t.frame_rate.as_ref())
                .map(|f| (f.numerator, f.denominator))
        };
        assert_eq!(rate_of(&declared_only), Some((25000, 1000)));
        assert_eq!(rate_of(&measured_too), Some((29970, 1000)));
        assert_eq!(
            rate_of(&measured_only),
            Some((29970, 1000)),
            "a manifest with no FRAME-RATE must still report the measurement"
        );
    }

    /// The audio and cc groups come from what the manifest DECLARES, using the
    /// exact attributes the live source advertises:
    ///
    ///   #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-aacl-128",LANGUAGE="en",CHANNELS="2"
    ///   #EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="textstream",LANGUAGE="en",INSTREAM-ID="CC1"
    ///   #EXT-X-STREAM-INF:...,CODECS="mp4a.40.2,avc1.4D401F",AVERAGE-BANDWIDTH=3316000
    #[test]
    fn the_audio_and_cc_groups_are_read_from_the_manifest() {
        let langs = vec!["en".to_string()];
        let prof = SourceProfile {
            width: Some(1280),
            height: Some(720),
            codecs: Some("mp4a.40.2,avc1.4D401F"),
            average_bandwidth: Some(3_316_000),
            audio_languages: &langs,
            audio_group: Some("audio-aacl-128"),
            audio_channels: Some("2"),
            cc_language: Some("en"),
            cc_instream_id: Some("CC1"),
            ..Default::default()
        };
        let groups = prof.video_profile(prof.video_codec());
        assert_eq!(groups.len(), 3, "want video + audio + cc: {groups:?}");

        let audio = &groups[1];
        assert_eq!(audio.group_type, "audio");
        assert_eq!(audio.language.as_deref(), Some("en"));
        assert_eq!(audio.audio_group.as_deref(), Some("audio-aacl-128"));
        assert_eq!(audio.audio_kind, Some("PRM"));
        let at = audio.track_info.as_ref().expect("audio track_info");
        assert_eq!(at.codec.as_deref(), Some("aac"));
        // HLS declares NEITHER, so both must stay absent rather than be invented
        // from the "128" inside GROUP-ID — that digit is a packager naming
        // convention, not a value the manifest asserts.
        assert_eq!(at.bit_rate, None, "audio bitrate must not be guessed");
        assert_eq!(at.sample_rate, None, "sample rate needs a probe");
        // CHANNELS="2" is a COUNT; L/R is the only unambiguous reading of 2.
        let ch: Vec<_> = audio
            .channel_layouts
            .iter()
            .map(|c| (c.channel_locator.as_str(), c.audio_channel))
            .collect();
        assert_eq!(ch, vec![("1", "L"), ("2", "R")]);

        let cc = &groups[2];
        assert_eq!(cc.group_type, "cc");
        assert_eq!(cc.language.as_deref(), Some("en"));
        // INSTREAM-ID=CC1 is CEA-608 embedded in the video — NOT the WebVTT the
        // §7.1 example happens to show. Claiming WebVTT would send a consumer
        // looking for a sidecar file that does not exist.
        assert_eq!(cc.format, Some("CEA-608"));
        assert_eq!(cc.offset_sec, Some(0));
        assert!(cc.track_info.is_none(), "a cc entry has no track_info");
    }

    /// A SERVICEn instream id is CEA-708; an unrecognised one yields no cc group
    /// at all rather than a guessed format.
    #[test]
    fn the_caption_format_follows_the_instream_id() {
        let none: Vec<String> = Vec::new();
        let p = |id| SourceProfile {
            codecs: Some("avc1.4D401F"),
            audio_languages: &none,
            cc_instream_id: Some(id),
            ..Default::default()
        };
        let fmt = |id| {
            p(id)
                .video_profile(None)
                .into_iter()
                .find(|g| g.group_type == "cc")
                .and_then(|g| g.format)
        };
        assert_eq!(fmt("CC1"), Some("CEA-608"));
        assert_eq!(fmt("SERVICE1"), Some("CEA-708"));
        assert_eq!(fmt("WEIRD"), None);
    }

    /// An unusual channel count is left UNSTATED. HLS declares how many channels
    /// there are, never which is which, so 6 must not become a guessed 5.1 map.
    #[test]
    fn an_unmappable_channel_count_yields_no_layout() {
        let langs = vec!["en".to_string()];
        let p = |c| SourceProfile {
            codecs: Some("mp4a.40.2"),
            audio_languages: &langs,
            audio_channels: Some(c),
            ..Default::default()
        };
        let layouts = |c| {
            p(c).video_profile(None)
                .into_iter()
                .find(|g| g.group_type == "audio")
                .map(|g| g.channel_layouts.len())
        };
        assert_eq!(layouts("1"), Some(1));
        assert_eq!(layouts("2"), Some(2));
        assert_eq!(layouts("6"), Some(0), "5.1 layout must not be invented");
        assert_eq!(layouts("banana"), Some(0));
    }

    /// A source with no audio group and no captions still yields the video group
    /// alone — the new groups are additive, never mandatory.
    #[test]
    fn a_video_only_source_still_yields_just_the_video_group() {
        let none: Vec<String> = Vec::new();
        let prof = SourceProfile {
            width: Some(1280),
            height: Some(720),
            codecs: Some("avc1.4D401F"),
            average_bandwidth: Some(3_316_000),
            audio_languages: &none,
            ..Default::default()
        };
        let groups = prof.video_profile(prof.video_codec());
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].group_type, "video");
    }

    /// The two §7.1 streaming_video facts only the producer can answer reach the
    /// wire, and stay absent on deliverables that are not playlists.
    #[test]
    fn the_playlist_facts_are_reported_only_for_the_playlist() {
        let hls = serde_json::to_string(&OutputRef {
            uri: "gs://b/clip.m3u8".into(),
            segment_count: Some(4),
            segment_duration: Some(9),
            ..Default::default()
        })
        .unwrap();
        assert!(hls.contains(r#""segment_count":4"#), "{hls}");
        assert!(hls.contains(r#""segment_duration":9"#), "{hls}");

        let jpg = serde_json::to_string(&OutputRef {
            uri: "gs://b/clip.jpg".into(),
            width: Some(1280),
            height: Some(720),
            ..Default::default()
        })
        .unwrap();
        assert!(!jpg.contains("segment_count"), "{jpg}");
        assert!(!jpg.contains("segment_duration"), "{jpg}");
    }

    /// §7.2 gives an ad marker a `duration`, and the recorder computes none for
    /// one — an ad break is a boundary pair, not a published file. The old
    /// notifier derived it from the epochs; when the envelope moved into Rust that
    /// fallback was dropped, and a real notification went out with the field
    /// missing before anyone noticed. This pins the derivation.
    #[test]
    fn an_ad_marker_reports_a_duration_derived_from_its_boundaries() {
        let id = identity();
        let start: DateTime<Utc> = "2026-08-08T02:15:22Z".parse().unwrap();
        let end: DateTime<Utc> = "2026-08-08T02:18:23Z".parse().unwrap();
        let ad = id.ad_break_event(at(), TYPE_AD_BREAK_END, 2, Some(start), Some(end));
        assert_eq!(ad.duration_sec, None, "the recorder measures none");

        let env = ad.envelope();
        let seg = env
            .data
            .segment
            .as_ref()
            .expect("an ad marker carries a segment block");
        assert_eq!(seg.segment_type, SEGMENT_TYPE_AD);
        // 02:15:22 -> 02:18:23 is 181 s.
        assert_eq!(seg.duration, Some(181.0));
        assert_eq!(seg.start_epoch, Some(start.timestamp()));
        assert_eq!(seg.end_epoch, Some(end.timestamp()));
        // §7.2: a marker carries no output — that absence is what distinguishes it
        // from a content run, now that there is no `data.type` to say so.
        assert!(env.data.output.is_none(), "an ad marker publishes no media");
    }

    /// An ad break still OPEN has no end, so no duration may be invented for it —
    /// deriving against a missing edge would report a boundary as a measurement.
    #[test]
    fn an_open_ad_marker_reports_no_duration() {
        let id = identity();
        let start: DateTime<Utc> = "2026-08-08T02:15:22Z".parse().unwrap();
        let open = id.ad_break_event(at(), TYPE_AD_BREAK_START, 2, Some(start), None);
        let env = open.envelope();
        let seg = env.data.segment.as_ref().expect("carries a segment block");
        assert_eq!(seg.duration, None);
        // The OPEN/CLOSED distinction survives the removal of `event_type`: it is
        // `close_reason` on the ad block, which is the pair consumers route on.
        assert_eq!(seg.close_reason, Some(TYPE_AD_BREAK_START));
    }

    /// A content run's own measured duration always wins over the derivation: it
    /// is the media's length, not the wall-clock span of its boundaries.
    #[test]
    fn a_measured_duration_is_never_replaced_by_the_derivation() {
        let id = identity();
        let env = id
            .segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                CLOSE_SCHEDULE_END,
                Outputs::default(),
            )
            .envelope();
        let seg = env.data.segment.as_ref().expect("carries a segment block");
        // start_pdt()..at() spans 720 s; the measured value is what it reports.
        assert_eq!(seg.duration, Some(720.0));
        assert_eq!(seg.segment_type, SEGMENT_TYPE_SEGMENT);
    }

    /// A source that declared nothing yields NO profile, so the wire omits the
    /// field rather than carrying a hollow entry.
    #[test]
    fn an_undeclared_source_yields_no_profile_at_all() {
        let bare = SourceProfile {
            width: None,
            height: None,
            frame_rate: None,
            measured_frame_rate: None,
            codecs: None,
            average_bandwidth: None,
            ..Default::default()
        };
        assert!(bare.video_profile(None).is_empty());
        assert_eq!(bare.resolution_label(), None);
    }

    /// The published ratio is REDUCED FROM the published dimensions, so the two
    /// can never disagree.
    ///
    /// The portrait case is the one that mattered: `aspect_ratio` was the literal
    /// `"16:9"` beside a real `1080x1920`, so a vertical source published an
    /// object that contradicted itself and gave a consumer no way to tell which
    /// half was true.
    #[test]
    fn the_aspect_ratio_is_reduced_from_the_reported_dimensions() {
        assert_eq!(
            aspect_ratio_of(Some(1920), Some(1080)).as_deref(),
            Some("16:9")
        );
        assert_eq!(
            aspect_ratio_of(Some(1080), Some(1920)).as_deref(),
            Some("9:16")
        );
        assert_eq!(
            aspect_ratio_of(Some(640), Some(480)).as_deref(),
            Some("4:3")
        );
        assert_eq!(
            aspect_ratio_of(Some(854), Some(480)).as_deref(),
            Some("427:240")
        );
    }

    /// No dimensions, no ratio. A source whose manifest declared no RESOLUTION
    /// leaves the field off the wire rather than asserting a default, for the
    /// same reason `resolution_label` returns None. Zero is not a side of a
    /// ratio, so it is treated as absent rather than divided by.
    #[test]
    fn the_aspect_ratio_is_omitted_rather_than_invented() {
        assert_eq!(aspect_ratio_of(None, Some(1080)), None);
        assert_eq!(aspect_ratio_of(Some(1920), None), None);
        assert_eq!(aspect_ratio_of(None, None), None);
        assert_eq!(aspect_ratio_of(Some(1920), Some(0)), None);
        assert_eq!(aspect_ratio_of(Some(0), Some(1080)), None);
    }

    const PREVIEW: &str = "gs://media-preview-firestore";
    const SOURCE: &str = "gs://media-upload-firestore";
    const BASE: &str = "https://preview.example.com";

    #[test]
    fn a_preview_object_gets_a_cdn_url() {
        let served = bucket_of(PREVIEW);
        assert_eq!(served, "media-preview-firestore");
        assert_eq!(
            cdn_url(&format!("{PREVIEW}/job-1/clip.jpg"), BASE, served),
            Some("https://preview.example.com/job-1/clip.jpg".to_string())
        );
    }

    #[test]
    fn the_source_mezzanine_never_gets_one() {
        // Both buckets nest the same <job_id>/<name> key, so dropping the bucket
        // and grafting the key onto the CDN host would yield a URL that 404s —
        // or resolves to a same-keyed object on the other side. The bucket check
        // is what prevents it.
        let served = bucket_of(PREVIEW);
        assert_eq!(
            cdn_url(&format!("{SOURCE}/job-1/clip.ts"), BASE, served),
            None
        );
    }

    #[test]
    fn an_unset_base_or_no_preview_destination_omits_the_field() {
        assert_eq!(cdn_base(None), "");
        assert_eq!(
            cdn_url(
                &format!("{PREVIEW}/job-1/clip.jpg"),
                "",
                "media-preview-firestore"
            ),
            None
        );
        // No --ais-preview-uri: bucket_of("") is "", so nothing is served.
        assert_eq!(bucket_of(""), "");
        assert_eq!(
            cdn_url(&format!("{PREVIEW}/job-1/clip.jpg"), BASE, ""),
            None
        );
    }

    #[test]
    fn the_base_is_normalised_so_the_separator_never_doubles() {
        assert_eq!(
            cdn_base(Some("  https://cdn.example.com/  ")),
            "https://cdn.example.com"
        );
        assert_eq!(
            cdn_base(Some("https://cdn.example.com///")),
            "https://cdn.example.com"
        );
        let served = bucket_of(PREVIEW);
        assert_eq!(
            cdn_url(
                &format!("{PREVIEW}/a/b.m3u8"),
                &cdn_base(Some("https://cdn.example.com/")),
                served
            ),
            Some("https://cdn.example.com/a/b.m3u8".to_string())
        );
    }

    #[test]
    fn a_uri_naming_no_object_is_not_mapped() {
        let served = bucket_of(PREVIEW);
        assert_eq!(cdn_url("gs://media-preview-firestore", BASE, served), None);
        assert_eq!(cdn_url("gs://media-preview-firestore/", BASE, served), None);
        assert_eq!(
            cdn_url("https://media-preview-firestore/x", BASE, served),
            None
        );
    }

    #[test]
    fn cdn_url_is_omitted_from_the_wire_when_absent() {
        let with = serde_json::to_string(&OutputRef {
            uri: "gs://b/k.jpg".into(),
            cdn_url: Some("https://c/k.jpg".into()),
            file_size: Some(6839),
            width: None,
            height: None,
            ..Default::default()
        })
        .unwrap();
        assert!(with.contains(r#""cdn_url":"https://c/k.jpg""#));
        assert!(with.contains(r#""file_size":6839"#));
        let without = serde_json::to_string(&OutputRef {
            uri: "gs://b/k.jpg".into(),
            cdn_url: None,
            file_size: None,
            width: None,
            height: None,
            ..Default::default()
        })
        .unwrap();
        // Same rule as cdn_url: an unreadable size is ABSENT, not 0, so a
        // consumer can tell "unknown" from "empty file".
        assert!(
            !without.contains("file_size"),
            "absent size must be absent, not 0: {without}"
        );
        assert!(
            !without.contains("cdn_url"),
            "absent must mean absent, not null: {without}"
        );
    }

    fn identity() -> Identity {
        Identity {
            job_id: "job-777".to_string(),
            ..Default::default()
        }
    }

    /// A manual clip's identity: the Video Editor named the row, so `media_id` is
    /// the segment id rather than a seed for one.
    fn manual_identity(media_id: &str) -> Identity {
        Identity {
            job_id: "job-777".to_string(),
            media_id: media_id.to_string(),
            manual: true,
            ..Default::default()
        }
    }

    /// CL-05 — a manual clip reports the id the task gave it, on every message
    /// about the segment. Deriving one would name a media_asset row that the
    /// Video Editor never created and nothing downstream could resolve.
    #[test]
    fn a_manual_clip_reports_the_inbound_media_id_as_its_segment_id() {
        let id = manual_identity("9f1d0d3e-2f6a-4a11-8f0e-7c2b5a3d1e44");
        let started = id.segment_started_event(at(), 1, start_pdt());
        let ended = id.segment_ended_event(at(), 1, start_pdt(), at(), "schedule_end");
        let closed = id.segment_closed_event(
            at(),
            1,
            start_pdt(),
            at(),
            "schedule_end",
            Outputs::default(),
        );
        for ev in [&started, &ended, &closed] {
            assert_eq!(
                ev.segment_id.as_deref(),
                Some("9f1d0d3e-2f6a-4a11-8f0e-7c2b5a3d1e44")
            );
        }
        // And the sequence does not change it — one manual job is one segment.
        let other_sequence = id.segment_started_event(at(), 7, start_pdt());
        assert_eq!(other_sequence.segment_id, started.segment_id);
    }

    /// The scheduled path is untouched: no `media_id`, no `manual`, so the id is
    /// still derived exactly as before.
    #[test]
    fn a_scheduled_clip_still_derives_its_segment_id() {
        let derived = identity().segment_started_event(at(), 1, start_pdt());
        assert_eq!(
            derived.segment_id.as_deref(),
            Some("7b4adaf5-e8f1-5001-bb8f-858952bd94b7")
        );
    }

    /// A manual task that carried no `media_id` has nothing to report, so it falls
    /// back to the derived id rather than naming the empty string.
    #[test]
    fn a_manual_clip_without_a_media_id_falls_back_to_the_derived_id() {
        let id = manual_identity("");
        let ev = id.segment_started_event(at(), 1, start_pdt());
        assert_eq!(
            ev.segment_id.as_deref(),
            Some("7b4adaf5-e8f1-5001-bb8f-858952bd94b7")
        );
    }

    /// A writer with a timeout far longer than any local write needs, so the
    /// bound never interferes with what a test is actually asserting.
    fn writer(dir_uri: Option<&str>) -> StatusWriter {
        StatusWriter::new(dir_uri, identity(), Duration::from_secs(30))
    }

    fn at() -> DateTime<Utc> {
        "2026-08-04T19:12:00Z".parse().expect("valid RFC 3339")
    }

    fn start_pdt() -> DateTime<Utc> {
        "2026-08-04T19:00:00Z".parse().expect("valid RFC 3339")
    }

    fn json(event: &Event) -> String {
        serde_json::to_string(event).expect("event serializes")
    }

    /// The media time capture stopped at, distinct from `at()` (the wall clock
    /// the document is written at) so a test can tell the two apart.
    fn stopped_at() -> DateTime<Utc> {
        "2026-08-04T19:10:12Z".parse().expect("valid RFC 3339")
    }

    /// A 5 Mbps MPEG-TS variant, the shape an orchestrated run reports.
    fn profile() -> StreamProfile {
        StreamProfile {
            variant_bandwidth: Some(5_000_000),
            container: CONTAINER_TS,
        }
    }

    /// `--event-id` of the standalone command line these tests used to parse.
    const STANDALONE_EVENT_ID: &str = "ev-42";

    /// The four values [`StatusWriter::from_task`] reads, which these tests used
    /// to get by running the live clipper's `clap` parser over a command line.
    ///
    /// THE PARSER STAYED WITH THE BINARY. `notify` moved into this crate so both
    /// clippers could share one §7 definition, and a shared module cannot depend
    /// on one binary's CLI. The defaults below are exactly what that command line
    /// produced (`STATUS_WRITE_TIMEOUT_SECS` default 30, occurrence 0, no task and
    /// no status uri), so every assertion still pins the same behaviour; what the
    /// clap layer itself does is now asserted in `live-hls2mp4`, where it lives.
    struct Args {
        event_id: &'static str,
        occurrence_index: u32,
        status_uri: Option<&'static str>,
        status_write_timeout_secs: u64,
        task_json: Option<String>,
    }

    /// The minimum command line: a standalone run, with none of the §7
    /// orchestration arguments supplied.
    fn standalone_args() -> Args {
        Args {
            event_id: STANDALONE_EVENT_ID,
            occurrence_index: 0,
            status_uri: None,
            status_write_timeout_secs: 30,
            task_json: None,
        }
    }

    impl Args {
        /// The orchestrator's `--occurrence-index`, the one value a dispatch
        /// supplies per firing.
        fn with_occurrence(mut self, index: u32) -> Self {
            self.occurrence_index = index;
            self
        }
    }

    /// `StatusWriter::from_args` as was, over the shim above.
    fn from_args(args: &Args, task: Option<&Task>) -> StatusWriter {
        StatusWriter::from_task(
            args.status_uri,
            args.event_id,
            args.occurrence_index,
            args.status_write_timeout_secs,
            task,
        )
    }

    #[test]
    fn status_in_progress_shape() {
        assert_eq!(
            json(&identity().status_event(at(), STATUS_IN_PROGRESS, 0, None, None)),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"in_progress","progress":0}"#
        );
    }

    #[test]
    fn status_failed_carries_error() {
        let error = EventError {
            code: ERR_CAPTURE_FAILED.to_string(),
            details: "playlist fetch failed".to_string(),
            stage: STAGE_CAPTURE.to_string(),
        };
        assert_eq!(
            json(&identity().status_event(at(), STATUS_FAILED, 0, Some(error), None)),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"failed","progress":0,"error":{"code":"capture_failed","details":"playlist fetch failed","stage":"capture"}}"#
        );
    }

    #[test]
    fn segment_closed_shape() {
        let outputs = Outputs {
            maxed_mp4: Some(OutputFile {
                uri: "gs://b/p/ev-a-b.mp4".to_string(),
                file_size: Some(123),
                cdn_url: None,
                resolution: None,
                media_profile: Vec::new(),
            }),
            mp4_1fps: Some(OutputFile {
                uri: "gs://b/p/ev-a-b_proxy_1fps.mp4".to_string(),
                file_size: Some(45),
                cdn_url: None,
                resolution: None,
                media_profile: Vec::new(),
            }),
            thumbnail: Some(OutputRef {
                uri: "gs://b/p/ev-a-b.jpg".to_string(),
                cdn_url: None,
                file_size: None,
                width: None,
                height: None,
                ..Default::default()
            }),
            hls_preview: Some(OutputRef {
                uri: "gs://b/p/ev-a-b.m3u8".to_string(),
                cdn_url: None,
                file_size: None,
                width: None,
                height: None,
                ..Default::default()
            }),
            // A clipper never packages, so the group stays empty here — and being
            // empty is exactly why this test's expected JSON is unchanged by its
            // addition (see `Outputs::streaming`).
            ..Default::default()
        };
        assert_eq!(
            json(&identity().segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                "ad_break_start",
                outputs
            )),
            r#"{"job_id":"job-777","type":"segment_closed","at":"2026-08-04T19:12:00Z","segment_id":"7b4adaf5-e8f1-5001-bb8f-858952bd94b7","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","duration_sec":720.0,"close_reason":"ad_break_start","outputs":{"maxed_mp4":{"uri":"gs://b/p/ev-a-b.mp4","file_size":123},"mp4_1fps":{"uri":"gs://b/p/ev-a-b_proxy_1fps.mp4","file_size":45},"thumbnail":{"uri":"gs://b/p/ev-a-b.jpg"},"hls_preview":{"uri":"gs://b/p/ev-a-b.m3u8"}}}"#
        );
    }

    #[test]
    fn segment_closed_omits_absent_derivatives_and_sizes() {
        let outputs = Outputs {
            maxed_mp4: Some(OutputFile {
                uri: "gs://b/p/ev-a-b.mp4".to_string(),
                file_size: None,
                cdn_url: None,
                resolution: None,
                media_profile: Vec::new(),
            }),
            ..Default::default()
        };
        assert_eq!(
            json(&identity().segment_closed_event(
                at(),
                2,
                start_pdt(),
                at(),
                "schedule_end",
                outputs
            )),
            r#"{"job_id":"job-777","type":"segment_closed","at":"2026-08-04T19:12:00Z","segment_id":"cc762fac-2419-52f7-b9e0-c7937a390766","sequence":2,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","duration_sec":720.0,"close_reason":"schedule_end","outputs":{"maxed_mp4":{"uri":"gs://b/p/ev-a-b.mp4"}}}"#
        );
    }

    #[test]
    fn ad_break_start_shape() {
        assert_eq!(
            json(&identity().ad_break_event(at(), TYPE_AD_BREAK_START, 1, Some(start_pdt()), None)),
            r#"{"job_id":"job-777","type":"ad_break_start","at":"2026-08-04T19:12:00Z","segment_id":"174af672-54b8-5446-9d65-3781293a713a","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","close_reason":"ad_break_start"}"#
        );
    }

    #[test]
    fn ad_break_end_shape() {
        assert_eq!(
            json(&identity().ad_break_event(
                at(),
                TYPE_AD_BREAK_END,
                1,
                Some(start_pdt()),
                Some(at())
            )),
            r#"{"job_id":"job-777","type":"ad_break_end","at":"2026-08-04T19:12:00Z","segment_id":"174af672-54b8-5446-9d65-3781293a713a","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","close_reason":"ad_break_end"}"#
        );
    }

    /// The start marker names the segment and its start, and NOTHING else: no
    /// end, no close reason, no output. The absent end is the discriminator.
    #[test]
    fn segment_started_shape() {
        assert_eq!(
            json(&identity().segment_started_event(at(), 1, start_pdt())),
            r#"{"job_id":"job-777","type":"segment_started","at":"2026-08-04T19:12:00Z","segment_id":"7b4adaf5-e8f1-5001-bb8f-858952bd94b7","sequence":1,"start_pdt":"2026-08-04T19:00:00Z"}"#
        );
    }

    /// The end marker carries the full boundary set — and still no output, which
    /// is what separates it from `segment_closed`.
    #[test]
    fn segment_ended_shape() {
        assert_eq!(
            json(&identity().segment_ended_event(at(), 1, start_pdt(), at(), "ad_break_start")),
            r#"{"job_id":"job-777","type":"segment_ended","at":"2026-08-04T19:12:00Z","segment_id":"7b4adaf5-e8f1-5001-bb8f-858952bd94b7","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","duration_sec":720.0,"close_reason":"ad_break_start"}"#
        );
    }

    /// THE ROUTING BIT. A consumer separates a marker from an assembled segment
    /// on `segment.status.state`, so both markers must render `in_progress`. If
    /// either rendered `completed` it would land on the ingest path and complete
    /// a clip whose media does not exist yet.
    #[test]
    fn both_timeline_markers_report_the_segment_in_progress() {
        let id = identity();
        let start: DateTime<Utc> = "2026-08-08T02:15:22Z".parse().unwrap();
        let end: DateTime<Utc> = "2026-08-08T02:18:23Z".parse().unwrap();

        let opened = id.segment_started_event(at(), 3, start).envelope();
        let seg = opened
            .data
            .segment
            .as_ref()
            .expect("carries a segment block");
        assert_eq!(seg.status.state, STATUS_IN_PROGRESS);
        assert_eq!(seg.segment_type, SEGMENT_TYPE_SEGMENT);
        assert_eq!(
            opened.data.status.state, STATUS_IN_PROGRESS,
            "and the job's"
        );
        assert_eq!(seg.start_epoch, Some(start.timestamp()));
        assert_eq!(seg.end_epoch, None, "an open segment has no end");
        assert_eq!(seg.end_time, None);
        assert_eq!(seg.duration, None, "no duration against a missing edge");
        assert!(opened.data.output.is_none(), "a marker publishes no media");

        let ended = id
            .segment_ended_event(at(), 3, start, end, "ad_break_start")
            .envelope();
        let seg = ended
            .data
            .segment
            .as_ref()
            .expect("carries a segment block");
        assert_eq!(
            seg.status.state, STATUS_IN_PROGRESS,
            "assembly still to come"
        );
        assert_eq!(ended.data.status.state, STATUS_IN_PROGRESS);
        assert_eq!(seg.end_epoch, Some(end.timestamp()));
        assert_eq!(seg.duration, Some(181.0));
        assert!(ended.data.output.is_none(), "still no media");
    }

    /// One segment, three messages, ONE id — the whole point of holding the
    /// number from open to close. A consumer keys on `segment_id`, so a start
    /// marker naming a different segment than its own close would split one clip
    /// into two rows downstream.
    #[test]
    fn the_start_end_and_close_of_one_run_share_a_segment_id() {
        let id = identity();
        let mut seq = Sequencer::default();
        let opened = seq.content_opened();
        let closed = seq.content_closed();
        assert_eq!(opened, closed, "the close reuses the number the open took");

        let a = id.segment_started_event(at(), opened, start_pdt());
        let b = id.segment_ended_event(at(), closed, start_pdt(), at(), "ad_break_start");
        let c = id.segment_closed_event(
            at(),
            closed,
            start_pdt(),
            at(),
            "ad_break_start",
            Outputs::default(),
        );
        assert_eq!(a.segment_id, b.segment_id);
        assert_eq!(b.segment_id, c.segment_id);
    }

    /// A run nobody announced still gets a number at its close — the CONCAT mode
    /// keeps one file across breaks and never opens a per-run marker, and it must
    /// keep numbering exactly as it did before the markers existed.
    #[test]
    fn a_run_closed_without_a_start_marker_still_takes_a_fresh_number() {
        let mut seq = Sequencer::default();
        assert_eq!(seq.content_closed(), 1);
        assert_eq!(seq.content_closed(), 2);
    }

    /// ALLOCATING AT OPEN MUST NOT RENUMBER ANYTHING. A run is closed BY the break
    /// that follows it, so it always opens first and still takes the lower number
    /// — the ordering the shared sequence space had when the number was spent at
    /// the close.
    #[test]
    fn a_content_run_still_precedes_the_break_that_closes_it() {
        let mut seq = Sequencer::default();
        let run_1 = seq.content_opened();
        let ad_1 = seq.ad_break_opened();
        assert_eq!(seq.content_closed(), run_1, "closed after the ad opened");
        assert_eq!(seq.ad_break_closed(), ad_1);
        let run_2 = seq.content_opened();
        assert_eq!((run_1, ad_1, run_2), (1, 2, 3));
    }

    #[test]
    fn ad_break_end_omits_unseen_start() {
        assert_eq!(
            json(&identity().ad_break_event(at(), TYPE_AD_BREAK_END, 1, None, Some(at()))),
            r#"{"job_id":"job-777","type":"ad_break_end","at":"2026-08-04T19:12:00Z","segment_id":"174af672-54b8-5446-9d65-3781293a713a","sequence":1,"end_pdt":"2026-08-04T19:12:00Z","close_reason":"ad_break_end"}"#
        );
    }

    /// §4.1 — a schedule that ends inside a break closes it with
    /// `close_reason: schedule_end`, at the MEDIA time capture stopped (19:10:12)
    /// and not at the wall clock the document is written at (19:12:00). Nothing
    /// cues back in after the window, so this is the only close it will ever get.
    #[test]
    fn ad_break_closed_by_the_schedule_carries_the_reason_and_media_time() {
        assert_eq!(
            json(&identity().ad_break_ended_by_schedule_event(
                at(),
                1,
                Some(start_pdt()),
                stopped_at()
            )),
            r#"{"job_id":"job-777","type":"ad_break_end","at":"2026-08-04T19:12:00Z","segment_id":"174af672-54b8-5446-9d65-3781293a713a","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:10:12Z","close_reason":"schedule_end"}"#
        );
    }

    /// The close has to UPDATE the marker its start opened, which downstream
    /// matches on `(schedule_id, sequence)` — so the number the break's start
    /// allocated is the number this document carries, and the ids agree.
    /// Otherwise §4.1 would trade a missing close for a duplicate marker.
    #[test]
    fn a_schedule_end_close_updates_the_marker_the_break_opened() {
        let id = identity();
        let mut sequencer = Sequencer::default();
        let start = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_START,
            sequencer.ad_break_opened(),
            Some(start_pdt()),
            None,
        );
        let end = id.ad_break_ended_by_schedule_event(
            at(),
            sequencer.ad_break_closed(),
            None,
            stopped_at(),
        );
        assert_eq!(start.sequence, end.sequence);
        assert_eq!(start.segment_id, end.segment_id);
        // BOTH boundaries now carry a reason: §7.2 draws `close_reason` on an ad
        // marker, and with `event_type` gone it is what tells an opened break from a
        // closed one. The open marker names itself; the close names what closed it.
        assert_eq!(start.close_reason, Some(TYPE_AD_BREAK_START));
        assert_eq!(end.close_reason, Some(CLOSE_SCHEDULE_END));
        // `start_pdt` may be omitted (the consumer carries the open marker's
        // value forward); the end boundary never may.
        assert_eq!(end.start_pdt, None);
        assert_eq!(end.end_pdt, Some("2026-08-04T19:10:12Z".to_string()));
    }

    /// The vehicle for "where did the media actually stop?": all four fields, the
    /// PDT in the same RFC 3339 form as every other stamp, and `at` left as the
    /// document-write wall clock beside it (19:12:00 vs 19:10:12).
    #[test]
    fn terminal_status_reports_the_capture_position() {
        assert_eq!(
            json(&identity().status_event(
                at(),
                STATUS_COMPLETED,
                100,
                None,
                Some(profile().at(12_345, stopped_at()))
            )),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"completed","progress":100,"capture_position":{"last_media_sequence":12345,"last_pdt":"2026-08-04T19:10:12Z","variant_bandwidth":5000000,"container":"ts"}}"#
        );
    }

    /// A failed run is the one that most needs the position: it is the only
    /// record of how far the capture got before it died.
    #[test]
    fn a_failed_status_reports_the_position_too() {
        let error = EventError {
            code: ERR_CAPTURE_FAILED.to_string(),
            details: "playlist fetch failed".to_string(),
            stage: STAGE_CAPTURE.to_string(),
        };
        assert_eq!(
            json(
                &identity().status_event(
                    at(),
                    STATUS_FAILED,
                    0,
                    Some(error),
                    Some(
                        StreamProfile {
                            variant_bandwidth: Some(3_648_000),
                            container: CONTAINER_CMAF,
                        }
                        .at(9, stopped_at())
                    )
                )
            ),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"failed","progress":0,"error":{"code":"capture_failed","details":"playlist fetch failed","stage":"capture"},"capture_position":{"last_media_sequence":9,"last_pdt":"2026-08-04T19:10:12Z","variant_bandwidth":3648000,"container":"cmaf"}}"#
        );
    }

    /// Unknown values are omitted, never faked: a source that is already a media
    /// playlist declares no `BANDWIDTH`, and a zero there would read as a real
    /// (absurd) bitrate. The media position — the part nothing downstream can
    /// derive — still ships.
    #[test]
    fn an_unknowable_variant_bandwidth_is_omitted_not_zeroed() {
        let position = StreamProfile {
            variant_bandwidth: None,
            container: CONTAINER_TS,
        }
        .at(7, stopped_at());
        let document =
            json(&identity().status_event(at(), STATUS_COMPLETED, 100, None, Some(position)));
        assert!(document.contains(
            r#""capture_position":{"last_media_sequence":7,"last_pdt":"2026-08-04T19:10:12Z","container":"ts"}"#
        ));
        assert!(!document.contains("variant_bandwidth"));
    }

    /// A run that ingested nothing (it died in setup) has no position to report,
    /// and reports none rather than a zeroed one — sequence 0 and the epoch are
    /// both real values a consumer would have to believe.
    #[test]
    fn a_status_document_omits_an_unknown_position() {
        let document = json(&identity().status_event(at(), STATUS_FAILED, 0, None, None));
        assert!(!document.contains("capture_position"));
    }

    /// The position is a property of the JOB's progress, so it rides the
    /// lifecycle document only — a clip document describes a fixed span that is
    /// already reported by its own `start_pdt`/`end_pdt`.
    #[test]
    fn only_status_documents_carry_the_position() {
        let id = identity();
        for document in [
            id.segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                "schedule_end",
                Outputs::default(),
            ),
            id.ad_break_event(at(), TYPE_AD_BREAK_START, 2, Some(start_pdt()), None),
            id.ad_break_ended_by_schedule_event(at(), 2, Some(start_pdt()), stopped_at()),
        ] {
            assert!(document.capture_position.is_none());
            assert!(!json(&document).contains("capture_position"));
        }
    }

    #[test]
    fn identity_ids_are_echoed_when_configured() {
        let identity = Identity {
            job_id: "job-777".to_string(),
            signature: Some("sig".to_string()),
            job_type: JOB_TYPE_LIVE.to_string(),
            media_id: String::new(),
            manual: false,
            occurrence_index: Some(3),
            job: serde_json::json!({
                "job_id": "job-777",
                "brand_id": "brand-1",
                "agent_id": "agent-1",
                "ext_job_id": "ext-job-1",
            }),
        };
        // The interim event log carries the raw block as ONE field, not the ids
        // spread across it — the same collapse the §7 envelope makes.
        assert_eq!(
            json(&identity.status_event(at(), STATUS_COMPLETED, 100, None, None)),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"completed","progress":100,"signature":"sig","occurrence_index":3,"job":{"agent_id":"agent-1","brand_id":"brand-1","ext_job_id":"ext-job-1","job_id":"job-777"}}"#
        );
    }

    /// A recurring schedule arms one run per occurrence, so the number the
    /// orchestrator armed this run with has to reach the notification — and the
    /// notifier reads it off whichever document woke it, so every document type
    /// carries it, not just the lifecycle one.
    #[test]
    fn occurrence_index_is_echoed_on_every_document_type() {
        let id = Identity {
            job_id: "job-777".to_string(),
            occurrence_index: Some(3),
            ..Default::default()
        };
        assert_eq!(
            json(&id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None)),
            r#"{"job_id":"job-777","type":"status","at":"2026-08-04T19:12:00Z","status":"in_progress","progress":0,"occurrence_index":3}"#
        );
        assert_eq!(
            json(&id.segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                "schedule_end",
                Outputs::default()
            )),
            r#"{"job_id":"job-777","type":"segment_closed","at":"2026-08-04T19:12:00Z","segment_id":"7b4adaf5-e8f1-5001-bb8f-858952bd94b7","sequence":1,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","duration_sec":720.0,"close_reason":"schedule_end","outputs":{},"occurrence_index":3}"#
        );
        assert_eq!(
            json(&id.ad_break_event(at(), TYPE_AD_BREAK_START, 2, Some(start_pdt()), None)),
            r#"{"job_id":"job-777","type":"ad_break_start","at":"2026-08-04T19:12:00Z","segment_id":"eec87e45-dcd2-56fe-81e2-850fcb1f18d7","sequence":2,"start_pdt":"2026-08-04T19:00:00Z","close_reason":"ad_break_start","occurrence_index":3}"#
        );
        assert_eq!(
            json(&id.ad_break_event(at(), TYPE_AD_BREAK_END, 2, Some(start_pdt()), Some(at()))),
            r#"{"job_id":"job-777","type":"ad_break_end","at":"2026-08-04T19:12:00Z","segment_id":"eec87e45-dcd2-56fe-81e2-850fcb1f18d7","sequence":2,"start_pdt":"2026-08-04T19:00:00Z","end_pdt":"2026-08-04T19:12:00Z","close_reason":"ad_break_end","occurrence_index":3}"#
        );
    }

    /// `0` is a real answer — the first occurrence — so it goes on the wire,
    /// while a run that was never given one omits the field. The notifier floors
    /// a missing value at 0, so faking absence as zero here would make "the
    /// first occurrence" and "nobody said" the same document.
    #[test]
    fn occurrence_zero_is_reported_and_an_unarmed_run_omits_it() {
        let armed = Identity {
            job_id: "job-777".to_string(),
            occurrence_index: Some(0),
            ..Default::default()
        };
        assert!(
            json(&armed.status_event(at(), STATUS_IN_PROGRESS, 0, None, None))
                .contains(r#""occurrence_index":0"#)
        );

        let standalone = identity();
        assert_eq!(standalone.occurrence_index, None);
        for document in [
            standalone.status_event(at(), STATUS_COMPLETED, 100, None, None),
            standalone.segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                "schedule_end",
                Outputs::default(),
            ),
            standalone.ad_break_event(at(), TYPE_AD_BREAK_START, 2, Some(start_pdt()), None),
            standalone.ad_break_event(at(), TYPE_AD_BREAK_END, 2, Some(start_pdt()), Some(at())),
        ] {
            assert!(!json(&document).contains("occurrence_index"));
        }
    }

    /// THE OCCURRENCE COMES FROM THE ORCHESTRATOR, NOT THE TASK — the reverse of the
    /// previous design, and deliberately so.
    ///
    /// §4 carries no `occurrence_index`: recurrence is expanded upstream into one task
    /// per window, so the task describes the event while only the dispatcher knows
    /// which occurrence it just fired. It arrives as `OCCURRENCE_INDEX` and is
    /// reported under §7 `data.status`, never stamped into the echoed `job`.
    ///
    /// A run with no task at all stays `None` — never 0, which is a real occurrence.
    #[test]
    fn the_occurrence_comes_from_the_argument_not_the_task() {
        let args = standalone_args();
        assert_eq!(args.task_json, None);
        assert_eq!(
            args.occurrence_index, 0,
            "the argument defaults to the first"
        );
        assert_eq!(
            from_args(&args, None).identity.occurrence_index,
            None,
            "no task at all reports no occurrence, not occurrence 0"
        );

        // The ARGUMENT decides, even when the task carries a different number: a task
        // that still sends one is a caller describing the event, not the dispatcher
        // naming the occurrence, so it must not win.
        let armed_args = standalone_args().with_occurrence(3);
        let stale = task_with(r#""occurrence_index": 99,"#);
        assert_eq!(
            from_args(&armed_args, Some(&stale))
                .identity
                .occurrence_index,
            Some(3)
        );
        // ...and it is NOT copied into the echoed block either way.
        let echoed = from_args(&armed_args, Some(&stale)).identity.job;
        assert_eq!(
            echoed.get("occurrence_index").and_then(JsonValue::as_u64),
            Some(99),
            "the caller's own key is echoed untouched, not overwritten with 3"
        );

        // With no argument supplied, a task present means occurrence 0 is reported.
        let first = task_with("");
        assert_eq!(
            from_args(&args, Some(&first)).identity.occurrence_index,
            Some(0)
        );
    }

    /// The identity is the job id, the signature, the orchestrator's occurrence and
    /// THE RAW JOB BLOCK — nothing else. Everything the recorder used to copy field
    /// by field now rides in that one value, which is the property that makes a
    /// future §4 field a no-op here.
    #[test]
    fn the_task_supplies_the_raw_job_block_as_the_whole_echo() {
        let args = standalone_args();
        let t = Task::parse(FULL_TASK).expect("task decodes");
        let id = from_args(&args, Some(&t)).identity;

        assert_eq!(id.job_id, "11111111-1111-4111-8111-111111111111");
        assert_eq!(id.signature.as_deref(), Some("sig-1"));

        // The echo is the block itself, field for field — including the fields the
        // typed view does not model.
        let job = &id.job;
        assert_eq!(
            job.get("brand_id").and_then(JsonValue::as_str),
            Some("brand-uuid")
        );
        assert_eq!(
            job.get("agent_id").and_then(JsonValue::as_str),
            Some("agent-uuid")
        );
        assert_eq!(
            job.get("ext_job_id").and_then(JsonValue::as_str),
            Some("ext-job-9")
        );
        assert_eq!(
            job.pointer("/schedule/ext_event_id")
                .and_then(JsonValue::as_str),
            Some("lnl-2026-08-07")
        );
        assert_eq!(
            job.pointer("/schedule/start/date_time")
                .and_then(JsonValue::as_str),
            Some("2026-08-07T19:00:00Z")
        );
        assert_eq!(
            job.pointer("/schedule/end/date_time")
                .and_then(JsonValue::as_str),
            Some("2026-08-07T19:04:00Z")
        );
        assert_eq!(
            job.pointer("/ai_flags/0").and_then(JsonValue::as_str),
            Some("GENERATE_METADATA")
        );
        assert_eq!(
            job.get("media_id").and_then(JsonValue::as_str),
            Some("media-uuid")
        );
        assert!(job
            .get("metadata")
            .and_then(JsonValue::as_array)
            .is_some_and(|m| m.len() == 1));

        // occurrence_index comes from the ORCHESTRATOR, never the task, and is never
        // stamped into the echoed block.
        assert_eq!(id.occurrence_index, Some(args.occurrence_index));
        assert!(
            job.get("occurrence_index").is_none(),
            "the worker must not add it to job"
        );
    }

    /// The echoed §4 values reach the wire, so the notifier no longer has to fetch
    /// any of them from the job record. `metadata` in particular had NO path to the
    /// notification before: nothing wrote the persisted field it was read from.
    #[test]
    fn the_echoed_task_values_reach_the_wire() {
        let args = standalone_args();
        let t = Task::parse(FULL_TASK).expect("task decodes");
        let id = from_args(&args, Some(&t)).identity;
        let wire = json(&id.segment_closed_event(
            at(),
            1,
            start_pdt(),
            at(),
            CLOSE_SCHEDULE_END,
            Outputs::default(),
        ));

        // Every one of these reaches the wire inside the echoed `job`, with no field
        // on this struct dedicated to carrying it.
        assert!(
            wire.contains(r#""ai_flags":["GENERATE_METADATA"]"#),
            "{wire}"
        );
        assert!(wire.contains(r#""media_id":"media-uuid""#), "{wire}");
        assert!(
            wire.contains(r#""title":"Late Night Live""#),
            "the caller's titles must reach the wire: {wire}"
        );
        // The bounds are handed back WRAPPED, in the shape §4 sent them — because
        // they are not re-rendered at all, they are the same bytes.
        assert!(
            wire.contains(r#""start":{"date_time":"2026-08-07T19:00:00Z"}"#),
            "{wire}"
        );
        // §4 fields this worker does not model reach the wire too.
        assert!(wire.contains(r#""priority":500"#), "{wire}");
        assert!(wire.contains(r#""email":"producer@example.com""#), "{wire}");
        assert!(wire.contains(r#""sub-category":"none""#), "{wire}");
        // And `pass_through` is GONE from §4 — nothing invents one.
        assert!(!wire.contains("pass_through"), "{wire}");
    }

    /// A run with no task echoes nothing, and the three new fields stay OFF the wire
    /// rather than appearing as empty ones — an empty `ai_flags` on a standalone
    /// document would read as "the caller asked for no processing".
    #[test]
    fn a_taskless_run_adds_nothing_to_the_wire() {
        let wire = json(&identity().status_event(at(), STATUS_IN_PROGRESS, 0, None, None));
        for absent in ["ai_flags", "pass_through", "schedule", "metadata"] {
            assert!(
                !wire.contains(absent),
                "{absent} must be absent without a task: {wire}"
            );
        }
    }

    /// AN EMPTY VALUE INSIDE `job` IS ECHOED AS THE CALLER SENT IT — and this is a
    /// deliberate change from the previous behaviour, which trimmed empty ids away.
    ///
    /// The worker no longer BUILDS the block, so it has no business normalising it:
    /// §7 requires the caller's block back unmutated, and silently dropping a key
    /// they sent is a mutation. What the worker still normalises is what it owns —
    /// the header `signature`, and the `job_id` it labels its own documents with.
    #[test]
    fn an_empty_value_inside_job_is_echoed_verbatim() {
        let args = standalone_args();
        let t = Task::parse(
            r#"{"header":{"signature":""},"data":{"job":{"job_id":"j-1","brand_id":""}}}"#,
        )
        .expect("task decodes");
        let id = from_args(&args, Some(&t)).identity;

        // Worker-owned: an empty signature stays absent on the wire.
        assert_eq!(id.signature, None);
        // Caller-owned: the empty brand_id is handed straight back.
        assert_eq!(id.job.get("brand_id").and_then(JsonValue::as_str), Some(""));

        let wire = json(&id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None));
        assert!(wire.contains(r#""brand_id":"""#), "{wire}");
        assert!(!wire.contains("signature"), "{wire}");
    }

    /// Prints the five real §7 envelopes, so the CONSUMERS can be tested against
    /// actual producer output rather than a hand-written approximation of it.
    /// `cargo test golden_envelopes -- --nocapture`
    ///
    /// EACH SAMPLE IS BUILT FROM THE TASK THAT WOULD REALLY PRODUCE IT. The four
    /// live shapes come from `FULL_TASK`; the packaging completion comes from
    /// `PACKAGING_TASK`, and it has to, because `data.job` is an ECHO. Built from
    /// the live task, the §7.4 fixture published `job_type: "live"` and a
    /// `schedule` block — a packaging notification that could never occur — so the
    /// one field consumers route on (n8n switches on `data.job.job_type`) was
    /// pinned to the wrong value in the file that exists to pin exactly that.
    #[test]
    fn golden_envelopes_for_cross_component_checks() {
        let args = standalone_args().with_occurrence(3);
        let t = Task::parse(FULL_TASK).expect("task decodes");
        let id = from_args(&args, Some(&t)).identity;
        let pkg_task = Task::parse(PACKAGING_TASK).expect("packaging task decodes");
        let pkg_id = from_args(&args, Some(&pkg_task)).identity;

        let mut seq = Sequencer::default();
        let n = seq.content_closed();
        let a = seq.ad_break_opened();
        let samples = [
            (
                "LIFECYCLE",
                id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None),
            ),
            (
                "SEGMENT",
                id.segment_closed_event(
                    at(),
                    n,
                    start_pdt(),
                    at(),
                    CLOSE_SCHEDULE_END,
                    Outputs::default(),
                ),
            ),
            (
                "AD_OPEN",
                id.ad_break_event(at(), TYPE_AD_BREAK_START, a, Some(start_pdt()), None),
            ),
            (
                "AD_CLOSED",
                id.ad_break_event(at(), TYPE_AD_BREAK_END, a, Some(start_pdt()), Some(at())),
            ),
            // §7.4 — the packaging completion. It is in this fixture for the same
            // reason the other four are: both consumers must be tested against real
            // producer output, and this is the FIFTH shape they have to classify.
            // Structurally it is a lifecycle document (no segment block) that
            // carries an `output`, which is exactly the combination that used to
            // reach ms-api and be discarded — so pinning the bytes is what stops
            // that regressing silently.
            //
            // TWO ENTRIES, NOT ONE, deliberately: the clippers publish at most one
            // streaming deliverable, so a single-entry sample would not exercise
            // the per-entry `type` that a packaging run exists to emit.
            (
                "PACKAGING",
                pkg_id.packaging_completed_event(
                    at(),
                    Outputs {
                        streaming: vec![
                            StreamingOutput {
                                entry_type: "clear_hlsv4",
                                uri: "gs://packaged/job-1/index-v4.m3u8".to_string(),
                                cdn_url: Some(
                                    "https://cdn.example.com/job-1/index-v4.m3u8".to_string(),
                                ),
                                duration: Some(505.6),
                                segment_duration: Some(4),
                                audio_languages: Vec::new(),
                                video_bitrate: None,
                            },
                            StreamingOutput {
                                entry_type: "clear_hlsv5",
                                uri: "gs://packaged/job-1/index-v5.m3u8".to_string(),
                                cdn_url: None,
                                duration: Some(505.6),
                                segment_duration: Some(4),
                                audio_languages: Vec::new(),
                                video_bitrate: None,
                            },
                        ],
                        ..Default::default()
                    },
                ),
            ),
        ];
        // THE FIXTURE MUST STILL MATCH WHAT THIS PRODUCER EMITS.
        //
        // `testdata/section7-golden.txt` is read by BOTH consumers' test suites
        // (ms-api's ParseStatusReport and notification-handler's parseEnvelope), so
        // it is the one place the contract is stated as bytes. If this assertion
        // fails, the envelope changed: regenerate the fixture with
        //     cargo test golden_envelopes -- --nocapture
        // and let the consumers' suites tell you which of them has not kept up.
        // Silently letting it drift would leave two consumers testing against a
        // shape nothing produces any more.
        let fixture = std::fs::read_to_string("../../testdata/section7-golden.txt")
            .expect("the shared golden fixture must exist");
        for (label, ev) in samples {
            let body = serde_json::to_string(&ev.envelope()).expect("serializes");
            println!("GOLDEN {label} {body}");
            let expected = fixture
                .lines()
                .find(|l| l.starts_with(&format!("{label} ")))
                .map(|l| l[label.len() + 1..].to_string())
                .unwrap_or_else(|| panic!("{label} missing from the golden fixture"));
            assert_eq!(
                body, expected,
                "the {label} envelope no longer matches testdata/section7-golden.txt — \
                 regenerate it and re-run both consumers' suites"
            );
        }
    }

    // ── §7 ENVELOPE MATRIX ────────────────────────────────────────────────────
    //
    // The four notification kinds §7 defines, asserted through the ONE property a
    // consumer is allowed to route on: which blocks are present. Plus the negative
    // cases — every retired discriminator, and every way the echo could be mutated.

    /// A helper: the parsed §7 envelope for an event, so these read as JSON.
    fn env_of(e: Event) -> serde_json::Value {
        serde_json::to_value(e.envelope()).expect("envelope serializes")
    }

    /// POSITIVE: all four §7 kinds are distinguishable from blocks alone.
    #[test]
    fn the_four_notification_kinds_are_distinguishable_from_blocks() {
        let id = identity();
        let mut seq = Sequencer::default();

        // 1. Lifecycle: no segment, no output.
        let life = env_of(id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None));
        assert!(life["data"]["segment"].is_null());
        assert!(life["data"]["output"].is_null());

        // 2. Content run: segment.type == segment, WITH output.
        let n = seq.content_closed();
        let run = env_of(id.segment_closed_event(
            at(),
            n,
            start_pdt(),
            at(),
            CLOSE_SCHEDULE_END,
            Outputs::default(),
        ));
        assert_eq!(run["data"]["segment"]["type"], "segment");
        assert!(run["data"]["output"].is_object());

        // 3. Ad opened: segment.type == ad, close_reason == ad_break_start, NO output.
        let a = seq.ad_break_opened();
        let open = env_of(id.ad_break_event(at(), TYPE_AD_BREAK_START, a, Some(start_pdt()), None));
        assert_eq!(open["data"]["segment"]["type"], "ad");
        assert_eq!(open["data"]["segment"]["close_reason"], "ad_break_start");
        assert!(open["data"]["output"].is_null());

        // 4. Ad closed: segment.type == ad, close_reason != ad_break_start, NO output.
        let closed = env_of(id.ad_break_event(
            at(),
            TYPE_AD_BREAK_END,
            seq.ad_break_closed(),
            Some(start_pdt()),
            Some(at()),
        ));
        assert_eq!(closed["data"]["segment"]["type"], "ad");
        assert_eq!(closed["data"]["segment"]["close_reason"], "ad_break_end");
        assert!(closed["data"]["output"].is_null());
    }

    /// §7.3, THE VOD COMPLETION: `job` + `status` + `output`, and NO segment.
    ///
    /// The absence is the assertion. §7.3 draws no segment block, and the whole
    /// discriminator downstream is structural — clipping-notifier files a
    /// segment-less document as `lifecycle` and ms-api's `docType` as
    /// `DocTypeStatus` — so a stray segment_id here would file the VOD capture as
    /// a live content run and number it into a sequence space it has no place in.
    ///
    /// BUILT FROM THE §4.4 TASK, not from the bare test identity, because half of
    /// what §7.3 defines is now decided by `job.job_type` — and the dispatch supplies
    /// an occurrence index (3 here) that the shape must suppress anyway.
    #[test]
    fn a_vod_capture_reports_the_section_7_3_shape() {
        let t = Task::parse(VOD_TASK).expect("the §4.4 vod task decodes");
        let id = from_args(&standalone_args().with_occurrence(3), Some(&t)).identity;
        assert_eq!(id.occurrence_index, Some(3), "the dispatch supplied one");
        let e = env_of(id.vod_completed_event(at(), 120.5, Outputs::default()));
        assert!(
            e["data"]["segment"].is_null(),
            "§7.3 carries no segment block"
        );
        assert!(e["data"]["output"].is_object(), "§7.3 carries the output");
        assert_eq!(e["data"]["status"]["state"], "completed");
        assert_eq!(e["data"]["status"]["progress"], 100);
        // Terminal and healthy: `error` is drawn as an explicit null on every
        // healthy §7 message so a consumer can read the key unconditionally.
        assert!(e["data"]["status"]["error"].is_null());
        // ...and the status block is EXACTLY the four keys §7.3 draws. It used to
        // carry a fifth, `occurrence_index: 0`, which §6 marks `(live)` and §7.3 does
        // not draw at all — a vod job is dispatched with an index like every other
        // job type, so the value existing was never the same question as the field
        // being defined for this shape.
        let status = e["data"]["status"].as_object().expect("a status block");
        let mut keys: Vec<&str> = status.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            ["error", "progress", "state", "updated_at"],
            "§7.3 draws {{state, progress, error, updated_at}} and nothing else: {e}"
        );
    }

    /// A SPLIT VOD span reports as a content run, not as §7.3 — one job yields N
    /// files there, and only a segment block can order and address them.
    ///
    /// The close reasons are the live clipper's own two: every span but the last
    /// ended because the following ad break started, the last because the window
    /// did. A consumer therefore needs no VOD-specific vocabulary.
    #[test]
    fn a_split_vod_span_reports_a_numbered_segment() {
        let id = identity();
        let mid = env_of(id.vod_segment_event(
            at(),
            &VodSpan {
                sequence: 1,
                start: Some(start_pdt()),
                end: Some(at()),
                duration_sec: 720.0,
                close_reason: CLOSE_AD_BREAK_START,
            },
            Outputs::default(),
        ));
        assert_eq!(mid["data"]["segment"]["type"], "segment");
        assert_eq!(mid["data"]["segment"]["sequence"], 1);
        assert_eq!(mid["data"]["segment"]["close_reason"], "ad_break_start");
        assert!(mid["data"]["output"].is_object());
        // The SEGMENT is complete; the JOB is not — the run reports `completed`
        // once, after its last span. Same two-level reading a live content run has.
        assert_eq!(mid["data"]["segment"]["status"]["state"], "completed");
        assert_eq!(mid["data"]["status"]["state"], "in_progress");

        let last = env_of(id.vod_segment_event(
            at(),
            &VodSpan {
                sequence: 2,
                start: Some(start_pdt()),
                end: Some(at()),
                duration_sec: 60.0,
                close_reason: CLOSE_SCHEDULE_END,
            },
            Outputs::default(),
        ));
        assert_eq!(last["data"]["segment"]["close_reason"], "schedule_end");
        // Distinct spans get distinct ids, from the same (job, kind, sequence)
        // derivation the live runs use — so the two never collide in the store.
        assert_ne!(
            mid["data"]["segment"]["segment_id"],
            last["data"]["segment"]["segment_id"]
        );
    }

    /// A source with NO `EXT-X-PROGRAM-DATE-TIME` — an ordinary published asset —
    /// reports its MEASURED duration and no wall-clock bounds.
    ///
    /// The bounds are omitted rather than filled from the download's own clock:
    /// that would report when the FETCH happened as if it were when the content
    /// aired, which is a wrong answer rather than a missing one. `duration` is
    /// therefore passed in and not derived from the bounds, which is what keeps it
    /// present when they are absent.
    #[test]
    fn a_vod_span_without_pdt_reports_a_duration_and_no_bounds() {
        let e = env_of(identity().vod_segment_event(
            at(),
            &VodSpan {
                sequence: 1,
                start: None,
                end: None,
                duration_sec: 42.5,
                close_reason: CLOSE_SCHEDULE_END,
            },
            Outputs::default(),
        ));
        assert!(e["data"]["segment"]["start_time"].is_null());
        assert!(e["data"]["segment"]["end_time"].is_null());
        assert!(e["data"]["segment"]["start_epoch"].is_null());
        assert!(e["data"]["segment"]["end_epoch"].is_null());
        assert_eq!(e["data"]["segment"]["duration"], 42.5);
    }

    /// A VOD job's echoed `job` block is the §4.4 task VERBATIM — `job_type`,
    /// `media_assets`, `metadata` and all — and it carries NO schedule, because
    /// §4.4 sends none. Nothing is synthesised to fill the gap.
    #[test]
    fn the_vod_job_block_is_the_task_echoed_whole() {
        let t = Task::parse(VOD_TASK).expect("the §4.4 vod task decodes");
        let id = from_args(&standalone_args(), Some(&t)).identity;
        let e = env_of(id.vod_completed_event(at(), 120.5, Outputs::default()));
        let job = &e["data"]["job"];
        assert_eq!(job["job_type"], "vod");
        assert_eq!(job["media_assets"]["ext_asset_id"], "master-video-006");
        assert_eq!(job["metadata"][0]["title"], "7/11 Documentary");
        assert_eq!(job["ai_flags"][0], "DETECT_SCENES");
        assert!(
            job["schedule"].is_null(),
            "§4.4 sends no schedule and none may be invented"
        );
        // The identity is taken from the task, not from the standalone fallback.
        assert_eq!(job["job_id"], "7c2a91d4-55e0-4b8f-9a3c-d81f0e6b2a17");
    }

    /// A packaging completion echoes the §4.5 `job` block — the packaging identity,
    /// and nothing lifted from the sibling `data.media` block.
    #[test]
    fn the_packaging_job_block_is_the_task_echoed_whole() {
        let t = Task::parse(PACKAGING_TASK).expect("the §4.5 packaging task decodes");
        let id = from_args(&standalone_args(), Some(&t)).identity;
        let e = env_of(id.packaging_completed_event(at(), Outputs::default()));
        let job = &e["data"]["job"];
        assert_eq!(job["job_type"], "packaging");
        assert_eq!(job["job_source"], "remote_renderer");
        assert_eq!(job["project_id"], "project-uuid");
        assert_eq!(job["ext_project_id"], "vod-capture-001");
        assert!(
            job["schedule"].is_null(),
            "§4.5 carries no schedule and none may be invented"
        );
        assert!(
            job["media_assets"].is_null(),
            "§4.5 names its source in data.media, not in data.job"
        );
        assert_eq!(job["job_id"], "7c2a91d4-55e0-4b8f-9a3c-d81f0e6b2a17");
    }

    // ── §7 FIELDS ARE PER JOB TYPE ────────────────────────────────────────────
    //
    // §7 does not define one document with optional parts: it defines a field set
    // PER JOB TYPE, and a field it does not define for a type must not be emitted
    // for that type. Two of them differ — `status.occurrence_index`, which §6 marks
    // `(live)`, and the `output` group set, which §7.4 draws differently from
    // §7.1/§7.3. The three tests below pin both, from both sides.

    /// §7.4, THE PACKAGING COMPLETION: the status block and the output group set are
    /// the ones §7.4 draws, and neither is §7.1's.
    ///
    /// FROM THE §4.5 TASK AND AN ARMED OCCURRENCE, because "is a value set" is the
    /// wrong question and this is what proves it: the dispatch supplies index 3, the
    /// identity holds it, and the document must still not report it — §7.4's status
    /// block is `{state, progress, error, updated_at}`.
    ///
    /// THE TWO MISSING GROUPS ARE THE ASSERTION. §7.4 defines `streaming_video`,
    /// `source_convert`, `thumbnail` and `keyframes`; `master_video` and
    /// `proxy_video` are §7.1/§7.3's, and a packaging run produces neither, so
    /// publishing them as empty lists reported that it had made none of a deliverable
    /// it cannot make. (`source_convert` is absent for the opposite reason: the
    /// packager does not produce it yet, and an empty list would make the same false
    /// statement in the other direction.)
    /// THE MASTER FILLS THE PROXY GROUP WHEN NO PROXY WAS MADE.
    ///
    /// The scheduler decides whether a proxy is asked for at all; the master is
    /// produced either way. A consumer resolves the segment's analysis source
    /// through this group — N8N writes a `preview-one-frame-video` media_asset
    /// from it, and `genai_video_segment_analyse` looks that row up by exact
    /// media_type — so an empty group is not a smaller payload, it is a broken
    /// lookup. That failure was observed on every segment of a real job, not
    /// theorised.
    ///
    /// Asserted three ways, because each covers a case the others let through:
    /// the proxy still WINS when present (this is a fallback, not a takeover),
    /// the master stands in when it is absent, and a run that produced neither
    /// still reports the group empty rather than inventing an entry.
    #[test]
    fn the_master_stands_in_for_a_proxy_that_was_never_generated() {
        let master = || OutputFile {
            uri: "gs://bucket/prefix/ev-a-b.ts".to_string(),
            file_size: Some(105_154_416),
            cdn_url: Some("https://cdn/ev-a-b.ts".to_string()),
            resolution: Some("720p".to_string()),
            media_profile: Vec::new(),
        };
        let proxy = || OutputFile {
            uri: "gs://bucket/prefix/ev-a-b_proxy_1fps.mp4".to_string(),
            file_size: Some(21_372_535),
            cdn_url: Some("https://cdn/ev-a-b_proxy_1fps.mp4".to_string()),
            resolution: Some("480p".to_string()),
            media_profile: Vec::new(),
        };

        // Proxy present: it wins, untouched.
        let with_proxy = Outputs {
            maxed_mp4: Some(master()),
            mp4_1fps: Some(proxy()),
            ..Default::default()
        }
        .to_output(Some(300.8));
        assert_eq!(with_proxy.proxy_video.len(), 1);
        assert_eq!(
            with_proxy.proxy_video[0].url, "gs://bucket/prefix/ev-a-b_proxy_1fps.mp4",
            "a generated proxy must not be displaced by the master"
        );
        assert_eq!(
            with_proxy.proxy_video[0].resolution.as_deref(),
            Some("480p")
        );

        // No proxy: the master stands in, and every measured field is the
        // master's own — only the slot is named for the proxy.
        let without_proxy = Outputs {
            maxed_mp4: Some(master()),
            mp4_1fps: None,
            ..Default::default()
        }
        .to_output(Some(300.8));
        assert_eq!(without_proxy.proxy_video.len(), 1, "the group is not empty");
        assert_eq!(
            without_proxy.proxy_video[0].url, "gs://bucket/prefix/ev-a-b.ts",
            "the master's own uri"
        );
        assert_eq!(
            without_proxy.proxy_video[0].cdn_url.as_deref(),
            Some("https://cdn/ev-a-b.ts")
        );
        assert_eq!(
            without_proxy.proxy_video[0].resolution.as_deref(),
            Some("720p"),
            "the master's real height, not the proxy target it never went through"
        );
        assert_eq!(
            without_proxy.proxy_video[0].entry_type,
            crate::derivatives::PROXY_ENTRY_TYPE,
            "the type is the consumer's discriminator and must not move"
        );
        // master_video still reports the same file under its own group.
        assert_eq!(without_proxy.master_video.len(), 1);
        assert_eq!(
            without_proxy.master_video[0].url,
            without_proxy.proxy_video[0].url
        );

        // Neither: nothing is invented.
        let neither = Outputs::default().to_output(None);
        assert!(neither.proxy_video.is_empty());
        assert!(neither.master_video.is_empty());
    }

    #[test]
    fn a_packaging_completion_reports_the_section_7_4_shape() {
        let t = Task::parse(PACKAGING_TASK).expect("the §4.5 packaging task decodes");
        let id = from_args(&standalone_args().with_occurrence(3), Some(&t)).identity;
        assert_eq!(id.occurrence_index, Some(3), "the dispatch supplied one");

        let e = env_of(id.packaging_completed_event(at(), Outputs::default()));
        let mut status: Vec<&str> = e["data"]["status"]
            .as_object()
            .expect("a status block")
            .keys()
            .map(String::as_str)
            .collect();
        status.sort_unstable();
        assert_eq!(
            status,
            ["error", "progress", "state", "updated_at"],
            "§7.4 draws {{state, progress, error, updated_at}} and nothing else: {e}"
        );

        let mut groups: Vec<&str> = e["data"]["output"]
            .as_object()
            .expect("an output block")
            .keys()
            .map(String::as_str)
            .collect();
        groups.sort_unstable();
        assert_eq!(
            groups,
            ["keyframes", "streaming_video", "thumbnail"],
            "§7.4's group set, minus the source_convert the packager cannot fill yet: {e}"
        );
    }

    /// The mirror: the live shapes keep every field the packaging one drops. The gate
    /// must narrow §7.4 alone — a live document that lost its occurrence would send
    /// ms-api's `scheduleForDoc` to the wrong occurrence of a recurring schedule.
    #[test]
    fn the_live_shapes_keep_the_occurrence_and_the_five_output_groups() {
        let t = Task::parse(FULL_TASK).expect("task decodes");
        let id = from_args(&standalone_args().with_occurrence(3), Some(&t)).identity;
        let mut seq = Sequencer::default();
        let n = seq.content_closed();
        let a = seq.ad_break_opened();
        for (label, ev) in [
            // §7.5 lifecycle, §7.1 segment, §7.2 ad — the three live shapes, all of
            // which draw `status.occurrence_index`.
            (
                "lifecycle",
                id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None),
            ),
            (
                "segment",
                id.segment_closed_event(
                    at(),
                    n,
                    start_pdt(),
                    at(),
                    CLOSE_SCHEDULE_END,
                    Outputs::default(),
                ),
            ),
            (
                "ad",
                id.ad_break_event(at(), TYPE_AD_BREAK_END, a, Some(start_pdt()), Some(at())),
            ),
        ] {
            let e = env_of(ev);
            assert_eq!(
                e["data"]["status"]["occurrence_index"], 3,
                "{label} lost its occurrence: {e}"
            );
        }

        let run = env_of(id.segment_closed_event(
            at(),
            n,
            start_pdt(),
            at(),
            CLOSE_SCHEDULE_END,
            Outputs::default(),
        ));
        let mut groups: Vec<&str> = run["data"]["output"]
            .as_object()
            .expect("an output block")
            .keys()
            .map(String::as_str)
            .collect();
        groups.sort_unstable();
        assert_eq!(
            groups,
            [
                "keyframes",
                "master_video",
                "proxy_video",
                "streaming_video",
                "thumbnail"
            ],
            "§7.1 draws five groups and the packaging narrowing must not reach it: {run}"
        );
    }

    /// THE GATE IS THE JOB TYPE, NOT THE EVENT KIND, and this is the case that tells
    /// them apart: a packaging run that FAILS reports through `status_event` — the
    /// same constructor a live lifecycle document uses — so a gate written on the
    /// constructor would put a live-only field on a §7.4 failure. §7.4: "on failure,
    /// the same shape is sent with `status.state: failed`".
    #[test]
    fn a_packaging_failure_reports_the_same_narrowed_status_block() {
        let t = Task::parse(PACKAGING_TASK).expect("the §4.5 packaging task decodes");
        let id = from_args(&standalone_args().with_occurrence(3), Some(&t)).identity;
        let e = env_of(id.status_event(
            at(),
            STATUS_FAILED,
            0,
            Some(EventError {
                code: ERR_PACKAGE_FAILED.to_string(),
                details: "ffmpeg exited 1".to_string(),
                stage: STAGE_PACKAGE.to_string(),
            }),
            None,
        ));
        assert_eq!(e["data"]["status"]["state"], "failed");
        assert_eq!(e["data"]["status"]["error"]["stage"], "package");
        assert!(
            e["data"]["status"]["occurrence_index"].is_null(),
            "a §7.4 failure carries no live-only field either: {e}"
        );
        // ...and no output at all, so there is no group set to narrow.
        assert!(e["data"]["output"].is_null(), "{e}");
    }

    /// The §4.4 VOD capture task, verbatim from the contract.
    const VOD_TASK: &str = r#"{
      "header": {"signature": "XXX", "type": "task", "postedDate": "03-08-2026T18:45:00z"},
      "data": {
        "job": {
          "action": "create",
          "job_id": "7c2a91d4-55e0-4b8f-9a3c-d81f0e6b2a17",
          "brand_id": "32505da7-af43-4bf7-81e8-ead65785513a",
          "agent_id": "9a41ce1e-6a0f-4a1b-8c26-31f6e9c2d7b4",
          "ext_job_id": "master-video-006",
          "project_id": "c4f0a9d2-8e17-4b6a-b3d5-92e61f7c0a44",
          "ext_project_id": "vod-capture-001",
          "job_type": "vod",
          "priority": 500,
          "email": "producer@example.com",
          "ai_flags": ["DETECT_SCENES", "GENERATE_METADATA"],
          "media_id": "b0759d38-1409-46e1-bf6a-e7ef9eda69d0",
          "media_assets": {
            "ext_asset_id": "master-video-006",
            "uri": "https://example.com/vod/006/master.m3u8",
            "duration": 120.5,
            "size": 7516192768
          },
          "metadata": [
            {
              "ext_asset_id": "master-video-006",
              "language": "en-US",
              "title": "7/11 Documentary",
              "category": "news",
              "sub-category": "documentary"
            }
          ]
        }
      }
    }"#;

    /// POSITIVE: a break the WINDOW ran out inside is still a CLOSE — its reason is
    /// `schedule_end`, not `ad_break_end`, which is exactly why a consumer must read
    /// "opened" as `ad_break_start` and treat every other reason as closed.
    #[test]
    fn a_schedule_end_ad_close_is_not_ad_break_end_but_is_still_a_close() {
        let id = identity();
        let e = env_of(id.ad_break_ended_by_schedule_event(at(), 2, Some(start_pdt()), at()));
        assert_eq!(e["data"]["segment"]["type"], "ad");
        assert_eq!(e["data"]["segment"]["close_reason"], "schedule_end");
        assert_ne!(e["data"]["segment"]["close_reason"], "ad_break_start");
        assert!(e["data"]["output"].is_null());
    }

    /// POSITIVE: §6's four states all render under `data.status.state`.
    #[test]
    fn every_status_state_renders_under_data_status() {
        let id = identity();
        for state in [STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_FAILED] {
            let e = env_of(id.status_event(at(), state, 0, None, None));
            assert_eq!(e["data"]["status"]["state"], state, "state {state}");
            // ...and never at the retired top-level path.
            assert!(e["data"]["status"].is_object());
        }
    }

    /// POSITIVE: a failure carries `{code, details, stage}` under `data.status.error`,
    /// not under `data.job` — the job block is the caller's and holds no worker state.
    #[test]
    fn a_failure_reports_its_error_under_data_status() {
        let id = identity();
        let e = env_of(id.status_event(
            at(),
            STATUS_FAILED,
            0,
            Some(EventError {
                code: ERR_CAPTURE_FAILED.to_string(),
                details: "origin unreachable".to_string(),
                stage: STAGE_CAPTURE.to_string(),
            }),
            None,
        ));
        assert_eq!(e["data"]["status"]["state"], "failed");
        assert_eq!(e["data"]["status"]["error"]["code"], ERR_CAPTURE_FAILED);
        assert_eq!(e["data"]["status"]["error"]["stage"], STAGE_CAPTURE);
        assert_eq!(
            e["data"]["status"]["error"]["details"],
            "origin unreachable"
        );
        // A healthy message renders error as an explicit null, not an omission, so a
        // consumer can read the key unconditionally.
        let ok = env_of(id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None));
        assert!(ok["data"]["status"]["error"].is_null());
        assert!(ok["data"]["status"]
            .as_object()
            .expect("obj")
            .contains_key("error"));
    }

    /// NEGATIVE: every retired discriminator is gone from every kind. This is the
    /// assertion that fails if someone reintroduces one "just for the notifier".
    #[test]
    fn no_retired_discriminator_survives_on_any_kind() {
        let id = identity();
        let kinds = [
            env_of(id.status_event(at(), STATUS_COMPLETED, 100, None, None)),
            env_of(id.segment_closed_event(
                at(),
                1,
                start_pdt(),
                at(),
                CLOSE_SCHEDULE_END,
                Outputs::default(),
            )),
            env_of(id.ad_break_event(at(), TYPE_AD_BREAK_START, 2, Some(start_pdt()), None)),
            env_of(id.ad_break_event(at(), TYPE_AD_BREAK_END, 2, Some(start_pdt()), Some(at()))),
        ];
        for (i, e) in kinds.iter().enumerate() {
            let d = &e["data"];
            for gone in [
                "id",
                "event_type",
                "type",
                "segment_id",
                "sequence",
                "close_reason",
                "start_epoch",
                "end_epoch",
                "start_time",
                "end_time",
                "duration",
                "ai_flags",
                "pass_through",
            ] {
                assert!(
                    d[gone].is_null(),
                    "kind {i}: `data.{gone}` must be gone: {d}"
                );
            }
            // `status` must be an OBJECT, never the old string.
            assert!(
                d["status"].is_object(),
                "kind {i}: status must be an object"
            );
            assert!(!d["status"].is_string(), "kind {i}");
        }
    }

    /// NEGATIVE: the worker never writes into the echoed block. Not its own state,
    /// not the occurrence, not a normalisation of the caller's values.
    #[test]
    fn the_worker_never_mutates_the_echoed_job_block() {
        let args = standalone_args().with_occurrence(5);
        let t = Task::parse(FULL_TASK).expect("task decodes");
        let id = from_args(&args, Some(&t)).identity;

        let before = t.data.job_raw.clone();
        let e = env_of(id.segment_closed_event(
            at(),
            1,
            start_pdt(),
            at(),
            CLOSE_SCHEDULE_END,
            Outputs::default(),
        ));
        // Byte-identical: the block on the wire IS the block that arrived.
        assert_eq!(e["data"]["job"], before);
        // Worker state did not leak into it.
        for leaked in [
            "status",
            "state",
            "progress",
            "error",
            "updated_at",
            "occurrence_index",
        ] {
            assert!(
                e["data"]["job"][leaked].is_null(),
                "`job.{leaked}` must not be written by the worker: {}",
                e["data"]["job"]
            );
        }
        // The occurrence went where it belongs instead.
        assert_eq!(e["data"]["status"]["occurrence_index"], 5);
    }

    /// NEGATIVE: a standalone run (no task) emits `job: null` rather than inventing a
    /// block — and still produces a well-formed envelope the rest of the way.
    #[test]
    fn a_standalone_run_emits_a_null_job_and_a_valid_envelope() {
        let args = standalone_args();
        let id = from_args(&args, None).identity;
        let e = env_of(id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None));
        assert!(e["data"]["job"].is_null(), "{e}");
        assert_eq!(e["data"]["status"]["state"], "in_progress");
        // NO OCCURRENCE. It is a live-only field (§6's table marks it `(live)`), and a
        // standalone run was dispatched as no job type at all — it echoes no `job`, so
        // there is nothing on this document that says it is live. It reported 0 before
        // the job-type gate went in, which claimed a first occurrence for a run that
        // has none. (Standalone runs write nothing: the documents are logged only.)
        assert!(e["data"]["status"]["occurrence_index"].is_null(), "{e}");
        assert_eq!(e["header"]["type"], "notification");
    }

    /// NEGATIVE: `capture_position` is additive and lifecycle-only — it must never
    /// appear on a clip or an ad marker, where it would read as a property of the clip.
    #[test]
    fn capture_position_never_rides_on_a_segment_or_ad() {
        let id = identity();
        let run = env_of(id.segment_closed_event(
            at(),
            1,
            start_pdt(),
            at(),
            CLOSE_SCHEDULE_END,
            Outputs::default(),
        ));
        assert!(run["data"]["capture_position"].is_null());
        let ad =
            env_of(id.ad_break_event(at(), TYPE_AD_BREAK_END, 2, Some(start_pdt()), Some(at())));
        assert!(ad["data"]["capture_position"].is_null());
    }

    /// A §4 live-create task carrying the whole `job` block, used by the tests above.
    ///
    /// `metadata` sits at the top of `job` (not inside `schedule`) and the schedule is
    /// purely temporal, as §4 defines. Note there is NO `occurrence_index`: §4 does
    /// not carry one.
    /// A §4.5 packaging task — the one that really produces a §7.4 completion.
    ///
    /// IT IS NOT `FULL_TASK` WITH THE job_type SWAPPED. §4.5 differs structurally:
    /// there is no `schedule` (packaging repackages an asset that already exists,
    /// so there is no window), no `job.media_assets` (the source lives in the
    /// sibling `data.media`, which the worker never reads and therefore never
    /// echoes), and there are three fields the capture shapes do not carry —
    /// `job_source`, `project_id`, `ext_project_id`. Since `data.job` is echoed
    /// verbatim, every one of those differences is visible on the wire, and a
    /// fixture that flattened them would pin a notification no caller can send.
    const PACKAGING_TASK: &str = r#"{
      "header": {"signature": "sig-1", "type": "task"},
      "data": {
        "job": {
          "action": "create",
          "job_id": "7c2a91d4-55e0-4b8f-9a3c-d81f0e6b2a17",
          "brand_id": "brand-uuid",
          "agent_id": "agent-uuid",
          "ext_job_id": "master-video-006",
          "project_id": "project-uuid",
          "ext_project_id": "vod-capture-001",
          "job_type": "packaging",
          "priority": 500,
          "email": "producer@example.com",
          "media_id": "media-uuid",
          "job_source": "remote_renderer"
        },
        "media": {"master": {
          "ext_id": "master-uuid",
          "media_assets": [{"ext_asset_id": "asset-1",
                            "uri": "gs://packaging-source/master.mp4",
                            "duration": 1588670, "size": 123142341}]
        }},
        "job_profiles": {
          "streaming_video": ["clear_hlsv3", "clear_hlsv5"],
          "source_convert": ["ts"], "thumbnail": ["1frame"], "keyframes": ["default"]
        }
      }
    }"#;

    const FULL_TASK: &str = r#"{
      "header": {"signature": "sig-1", "type": "task"},
      "data": {
        "job": {
          "action": "create",
          "job_id": "11111111-1111-4111-8111-111111111111",
          "brand_id": "brand-uuid",
          "agent_id": "agent-uuid",
          "ext_job_id": "ext-job-9",
          "job_type": "live",
          "priority": 500,
          "email": "producer@example.com",
          "media_id": "media-uuid",
          "media_assets": {"ext_asset_id": "asset-1", "uri": "https://o/master.m3u8"},
          "metadata": [{"ext_asset_id": "asset-1", "language": "en-US", "title": "Late Night Live",
                        "category": "news", "sub-category": "none"}],
          "schedule": {
            "ext_event_id": "lnl-2026-08-07",
            "start": {"date_time": "2026-08-07T19:00:00Z"},
            "end": {"date_time": "2026-08-07T19:04:00Z"}
          },
          "ai_flags": ["GENERATE_METADATA"]
        }
      }
    }"#;

    /// A minimal task with `fields` spliced into `data.job`, for asserting one field
    /// at a time.
    fn task_with(fields: &str) -> Task {
        Task::parse(&format!(
            r#"{{"data":{{"job":{{{fields}"job_id":"j-1"}}}}}}"#
        ))
        .expect("task decodes")
    }

    /// Content segments and ad markers land in one `clip_segments` collection
    /// that is unique on `(schedule_id, sequence)`, so the numbers on the wire
    /// must come from one space: content run 1 → 1, the ad break → 2 (start and
    /// end alike), content run 2 → 3.
    #[test]
    fn interleaved_content_and_ad_events_share_one_sequence_space() {
        let id = identity();
        let mut sequencer = Sequencer::default();
        let no_outputs = || Outputs::default();

        // Content run 1 closes as the break opens, then the break opens.
        let first_run = id.segment_closed_event(
            at(),
            sequencer.content_closed(),
            start_pdt(),
            at(),
            "ad_break_start",
            no_outputs(),
        );
        let break_open = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_START,
            sequencer.ad_break_opened(),
            Some(at()),
            None,
        );
        // The break closes, then content run 2 closes at the schedule end.
        let break_close = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_END,
            sequencer.ad_break_closed(),
            Some(at()),
            Some(at()),
        );
        let second_run = id.segment_closed_event(
            at(),
            sequencer.content_closed(),
            at(),
            at(),
            "schedule_end",
            no_outputs(),
        );

        assert_eq!(first_run.sequence, Some(1));
        assert_eq!(break_open.sequence, Some(2));
        assert_eq!(break_close.sequence, Some(2));
        assert_eq!(second_run.sequence, Some(3));
        // No content segment ever reuses an ad marker's number, and vice versa.
        assert_ne!(first_run.sequence, break_open.sequence);
        assert_ne!(second_run.sequence, break_open.sequence);
        // The numbers survive to the wire.
        assert!(json(&first_run).contains(r#""sequence":1,"#));
        assert!(json(&break_open).contains(r#""sequence":2,"#));
        assert!(json(&break_close).contains(r#""sequence":2,"#));
        assert!(json(&second_run).contains(r#""sequence":3,"#));
    }

    /// One break is one ad marker: its `open → completed` lifecycle is matched by
    /// sequence, so start and end must carry the same number.
    #[test]
    fn ad_break_start_and_end_carry_the_same_sequence() {
        let id = identity();
        let mut sequencer = Sequencer::default();
        let opened = sequencer.ad_break_opened();
        let start = id.ad_break_event(at(), TYPE_AD_BREAK_START, opened, Some(start_pdt()), None);
        let end = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_END,
            sequencer.ad_break_closed(),
            Some(start_pdt()),
            Some(at()),
        );
        assert_eq!(start.sequence, end.sequence);
        assert_eq!(start.sequence, Some(1));
        // One marker, one number, therefore one id: the end event updates the
        // record the start event opened.
        assert_eq!(start.segment_id, end.segment_id);
        assert_eq!(
            start.segment_id,
            Some("174af672-54b8-5446-9d65-3781293a713a".to_string())
        );
        // The next break takes the next number, not the same one again.
        assert_eq!(sequencer.ad_break_opened(), 2);
    }

    /// The ids the clipper mints must be exactly what clipping-notifier derived
    /// from `(job_id, sequence)` before — `<job>-seg-<n>` for a content run
    /// (`build_segment`) and `<job>-ad-<n>` for an ad marker (`build_ad`) — so
    /// minting them here is invisible on the wire. Both kinds draw on the one
    /// shared sequence space, so the kind infix is what keeps them apart.
    /// The id must be a real UUID (AI Studio stores it in uuid columns) AND a
    /// pure function of the run, so a redelivered document or a restarted clipper
    /// reports the same clip rather than a new one. Both properties are asserted
    /// together because satisfying either alone is what a v4 or a plain string
    /// would do.
    #[test]
    fn segment_ids_are_deterministic_uuids() {
        let a = identity().segment_id("seg", 1);
        assert_eq!(a.len(), 36, "must be a hyphenated UUID, got {a:?}");
        assert!(
            Uuid::parse_str(&a).is_ok(),
            "must parse as a UUID, got {a:?}"
        );
        // Same run, same id — the property replay and retry depend on.
        assert_eq!(a, identity().segment_id("seg", 1));
        // The kind infix and the sequence each have to matter, or an ad marker
        // and the content run sharing its number would collide.
        assert_ne!(a, identity().segment_id("ad", 1));
        assert_ne!(a, identity().segment_id("seg", 2));
    }

    #[test]
    fn segment_ids_are_minted_for_content_runs_and_ad_markers() {
        let id = identity();
        let mut sequencer = Sequencer::default();

        let first_run = id.segment_closed_event(
            at(),
            sequencer.content_closed(),
            start_pdt(),
            at(),
            "ad_break_start",
            Outputs::default(),
        );
        let break_open = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_START,
            sequencer.ad_break_opened(),
            Some(at()),
            None,
        );
        let break_close = id.ad_break_event(
            at(),
            TYPE_AD_BREAK_END,
            sequencer.ad_break_closed(),
            Some(at()),
            Some(at()),
        );
        let second_run = id.segment_closed_event(
            at(),
            sequencer.content_closed(),
            at(),
            at(),
            "schedule_end",
            Outputs::default(),
        );

        assert_eq!(
            first_run.segment_id,
            Some("7b4adaf5-e8f1-5001-bb8f-858952bd94b7".to_string())
        );
        assert_eq!(
            break_open.segment_id,
            Some("eec87e45-dcd2-56fe-81e2-850fcb1f18d7".to_string())
        );
        assert_eq!(
            break_close.segment_id,
            Some("eec87e45-dcd2-56fe-81e2-850fcb1f18d7".to_string())
        );
        assert_eq!(
            second_run.segment_id,
            Some("9d00a2c8-7e02-58fb-a717-e432037d1db4".to_string())
        );
        // The ids survive to the wire, next to the sequence they are built from.
        assert!(json(&first_run)
            .contains(r#""segment_id":"7b4adaf5-e8f1-5001-bb8f-858952bd94b7","sequence":1,"#));
        assert!(json(&break_close)
            .contains(r#""segment_id":"eec87e45-dcd2-56fe-81e2-850fcb1f18d7","sequence":2,"#));
        // A lifecycle event is about the job, not a clip: no id at all.
        let lifecycle = id.status_event(at(), STATUS_IN_PROGRESS, 0, None, None);
        assert_eq!(lifecycle.segment_id, None);
        assert!(!json(&lifecycle).contains("segment_id"));
    }

    /// A standalone run has no orchestration job, so the event id stands in for it
    /// as the name the id is derived from — the id stays attributable to a run
    /// rather than being derived from an empty job id, which would make every
    /// standalone run's segment 1 share one id.
    ///
    /// The expectations are the v5 hashes of `ev-42:seg:1` / `ev-42:ad:2`, written
    /// out rather than recomputed in the test: pinning the literal is what would
    /// catch an accidental change to the namespace or the name format, which a
    /// self-referential assertion would silently follow.
    #[test]
    fn segment_id_falls_back_to_the_event_id_without_a_job_id() {
        let args = standalone_args();
        assert!(args.task_json.is_none());

        let writer = from_args(&args, None);
        assert!(!writer.is_enabled());
        assert_eq!(
            writer.identity.segment_id(SEGMENT_KIND_CONTENT, 1),
            "0fce825f-ecf2-5602-a07c-bf32c1a483a8"
        );
        assert_eq!(
            writer.identity.segment_id(SEGMENT_KIND_AD, 2),
            "fecbbf29-48d0-5734-8084-ff927cda6ca6"
        );
        // Unset on the command line and in the environment: the documented default.
        assert_eq!(writer.write_timeout, Duration::from_secs(30));
    }

    /// A hung upload must not become a hung recording: the write is abandoned at
    /// the configured bound, and the bound is whatever the job was given.
    #[tokio::test]
    async fn a_write_that_outlives_the_timeout_is_abandoned() {
        let writer = StatusWriter::new(None, identity(), Duration::from_millis(20));
        assert_eq!(writer.write_timeout, Duration::from_millis(20));
        // Stands in for an upload that never finalizes.
        let outcome = writer.bounded(std::future::pending::<Result<()>>()).await;
        assert!(matches!(outcome, WriteOutcome::TimedOut));
    }

    /// A zero bound disables the timeout rather than expiring immediately: an
    /// operator who passes 0 wants to wait for the write, not to drop every
    /// document. A literal `timeout(0)` would abandon even an instant write.
    #[tokio::test]
    async fn a_zero_timeout_means_no_deadline() {
        let writer = StatusWriter::new(None, identity(), Duration::ZERO);
        assert_eq!(writer.write_timeout, Duration::ZERO);
        assert!(matches!(
            writer.bounded(async { Ok(()) }).await,
            WriteOutcome::Written
        ));
        // The write's own failure still surfaces; only the deadline is removed.
        assert!(matches!(
            writer
                .bounded(async { Err(anyhow::anyhow!("storage refused the object")) })
                .await,
            WriteOutcome::Failed(_)
        ));
    }

    /// Inside the bound the outcome is the write's own: success or the storage
    /// error, which are logged differently from an abandoned document.
    #[tokio::test]
    async fn a_write_inside_the_timeout_keeps_its_own_outcome() {
        let writer = writer(None);
        assert!(matches!(
            writer.bounded(async { Ok(()) }).await,
            WriteOutcome::Written
        ));
        assert!(matches!(
            writer
                .bounded(async { Err(anyhow::anyhow!("storage refused the object")) })
                .await,
            WriteOutcome::Failed(_)
        ));
    }

    /// One object per event, grouped under the job, sorting in emission order.
    #[test]
    fn object_names_are_unique_sorted_and_grouped_by_job() {
        let writer = writer(None);
        let names: Vec<String> = [
            (TYPE_STATUS, 1),
            (TYPE_AD_BREAK_START, 2),
            (TYPE_AD_BREAK_END, 3),
            (TYPE_SEGMENT_CLOSED, 4),
            (TYPE_SEGMENT_CLOSED, 5),
            (TYPE_STATUS, 6),
        ]
        .iter()
        .map(|(event_type, emitted)| writer.object_name(event_type, *emitted))
        .collect();

        // Grouped per job, and every document lands in this run's sub-prefix.
        let prefix = format!("job-777/{}/", writer.run_id());
        assert!(names.iter().all(|n| n.starts_with(&prefix)));
        // One object per event: no two events share a name.
        let unique: std::collections::BTreeSet<&String> = names.iter().collect();
        assert_eq!(unique.len(), names.len());
        // Lexicographic order is emission order.
        let mut sorted = names.clone();
        sorted.sort();
        assert_eq!(sorted, names);
        assert_eq!(names[0], format!("{prefix}00001-status.json"));
        assert_eq!(names[3], format!("{prefix}00004-segment_closed.json"));
    }

    /// A re-run of the same job must not overwrite the first run's documents.
    #[test]
    fn a_rerun_of_the_same_job_writes_under_a_fresh_prefix() {
        let first = run_id("2026-08-05T10:15:30.123Z".parse().expect("valid RFC 3339"));
        let second = run_id("2026-08-05T10:15:30.124Z".parse().expect("valid RFC 3339"));
        assert_ne!(first, second);
        // Sortable by run start, and a plain path element (no delimiters).
        assert!(first < second);
        assert!(!first.contains('/'));
        assert!(first.starts_with("20260805T101530123Z-"));
    }

    #[tokio::test]
    async fn disabled_writer_logs_and_never_writes() {
        let dir = std::env::temp_dir().join(format!("lc-status-off-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("temp dir");
        // No STATUS_URI: nothing may be written anywhere.
        let writer = writer(None);
        assert!(!writer.is_enabled());
        writer.recording().await;
        writer.completed().await;
        assert_eq!(
            std::fs::read_dir(&dir).expect("temp dir readable").count(),
            0
        );
        // The counter still advanced, so the logged names match a live run's.
        assert_eq!(writer.emitted.load(Ordering::Relaxed), 2);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn writes_one_finalized_object_per_event() {
        let dir = std::env::temp_dir().join(format!("lc-status-on-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(&dir).expect("temp dir");
        let writer = writer(Some(&format!("file://{}", dir.display())));
        assert!(writer.is_enabled());

        writer.recording().await;
        writer
            .segment_closed(
                1,
                start_pdt(),
                at(),
                "ad_break_start",
                Outputs {
                    maxed_mp4: Some(OutputFile {
                        uri: "gs://bucket/prefix/ev-a-b.mp4".to_string(),
                        file_size: Some(123),
                        cdn_url: None,
                        resolution: None,
                        media_profile: Vec::new(),
                    }),
                    ..Default::default()
                },
            )
            .await;

        let run_dir = dir.join("job-777").join(writer.run_id());
        let mut written: Vec<String> = std::fs::read_dir(&run_dir)
            .expect("run prefix exists")
            .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
            .collect();
        written.sort();
        assert_eq!(
            written,
            vec!["00001-status.json", "00002-segment_closed.json"]
        );

        // Each object is finalized (readable) and holds exactly its own event —
        // AS THE §7 ENVELOPE, which is what the notifier publishes verbatim and
        // ms-api decodes. This is the contract's only end-to-end assertion, so it
        // checks the envelope's shape rather than a field or two.
        let first = std::fs::read_to_string(run_dir.join("00001-status.json")).expect("readable");
        let e1: serde_json::Value = serde_json::from_str(&first).expect("envelope parses");
        assert_eq!(e1["header"]["type"], "notification");
        assert_eq!(e1["data"]["status"]["state"], "in_progress");
        // A LIFECYCLE MESSAGE IS ONE WITH NO `segment` AND NO `output`. That absence
        // is the whole classification now — there is no `event_type` and no
        // `data.type` to say it a second way.
        assert!(e1["data"]["segment"].is_null(), "{first}");
        assert!(e1["data"]["output"].is_null(), "{first}");
        // The retired discriminators must not reappear.
        assert!(e1["data"]["event_type"].is_null(), "{first}");
        assert!(e1["data"]["type"].is_null(), "{first}");
        assert!(e1["data"]["id"].is_null(), "{first}");

        let second =
            std::fs::read_to_string(run_dir.join("00002-segment_closed.json")).expect("readable");
        let e2: serde_json::Value = serde_json::from_str(&second).expect("envelope parses");
        // A CONTENT RUN IS `segment.type == "segment"` WITH AN `output` BESIDE IT.
        assert_eq!(e2["data"]["segment"]["type"], "segment");
        assert_eq!(e2["data"]["segment"]["close_reason"], "ad_break_start");
        assert!(e2["data"]["output"].is_object(), "{second}");
        // ONE state per message: §7 collapsed the old two-status rule (payload
        // `completed` beside job `in_progress`) into `data.status.state`, and the
        // "payload is complete" fact is now carried structurally by the output block.
        assert_eq!(e2["data"]["status"]["state"], "in_progress");
        assert!(e2["data"]["event_type"].is_null(), "{second}");
        assert!(e2["data"]["type"].is_null(), "{second}");
        // The clip's identity is INSIDE the segment block now, not flat on `data`.
        assert_eq!(
            e2["data"]["segment"]["segment_id"],
            "7b4adaf5-e8f1-5001-bb8f-858952bd94b7"
        );
        assert_eq!(e2["data"]["segment"]["sequence"], 1);
        // ...and nothing is left behind at the old flat paths.
        assert!(e2["data"]["segment_id"].is_null(), "{second}");
        assert!(e2["data"]["sequence"].is_null(), "{second}");
        assert!(e2["data"]["close_reason"].is_null(), "{second}");
        assert!(e2["data"]["ai_flags"].is_null(), "{second}");
        assert!(e2["data"]["pass_through"].is_null(), "{second}");
        // The deliverable reached §7.1's output group, not a flat `outputs` map.
        let mv = &e2["data"]["output"]["master_video"][0];
        assert_eq!(mv["type"], "source_mp4");
        assert_eq!(mv["url"], "gs://bucket/prefix/ev-a-b.mp4");
        // §7.1 names the byte length `size` on the wire, not `file_size`.
        assert_eq!(mv["size"], 123);
        assert!(
            mv["file_size"].is_null(),
            "the retired name must not come back: {e2}"
        );
        assert_eq!(mv["encoder"], "ffmpeg");
        // Every group key is present even when empty, so a consumer can iterate
        // them uniformly.
        for group in [
            "master_video",
            "streaming_video",
            "proxy_video",
            "thumbnail",
            "keyframes",
        ] {
            assert!(
                e2["data"]["output"][group].is_array(),
                "{group} missing from output: {second}"
            );
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The position the capture loop last recorded is what the TERMINAL document
    /// carries — the writer holds it precisely because the terminal document is
    /// emitted from outside the loop (and, for a failure, outside `run` entirely).
    /// The first document predates any ingest, so it correctly has none.
    #[tokio::test]
    async fn the_last_recorded_position_reaches_the_terminal_document() {
        let dir = std::env::temp_dir().join(format!("lc-status-pos-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(&dir).expect("temp dir");
        let writer = writer(Some(&format!("file://{}", dir.display())));

        // Capture starts before anything has been ingested.
        assert!(writer.capture_position().is_none());
        writer.recording().await;
        // Two chunks ingested; the LAST one is the position that counts.
        writer.set_capture_position(profile().at(12_344, start_pdt()));
        writer.set_capture_position(profile().at(12_345, stopped_at()));
        writer.completed().await;

        let run_dir = dir.join("job-777").join(writer.run_id());
        let first = std::fs::read_to_string(run_dir.join("00001-status.json")).expect("readable");
        assert!(!first.contains("capture_position"));
        let terminal =
            std::fs::read_to_string(run_dir.join("00002-status.json")).expect("readable");
        assert!(terminal.contains(
            r#""capture_position":{"last_media_sequence":12345,"last_pdt":"2026-08-04T19:10:12Z","variant_bandwidth":5000000,"container":"ts"}"#
        ));
        // `at` stays the write time: the media time is ADDED, not substituted.
        assert!(terminal.contains(r#""state":"completed""#));
        assert!(!terminal.contains(r#""at":"2026-08-04T19:10:12Z""#));

        // The same tracked value is what the schedule-end ad close uses, so the
        // marker's end_pdt and the terminal document's last_pdt cannot disagree.
        let position = writer.capture_position().expect("a chunk was ingested");
        assert_eq!(position.last_pdt, stopped_at());
        assert_eq!(position.last_media_sequence, 12_345);
        writer
            .ad_break_ended_by_schedule(2, Some(start_pdt()), position.last_pdt)
            .await;
        let closed =
            std::fs::read_to_string(run_dir.join("00003-ad_break_end.json")).expect("readable");
        assert!(closed.contains(r#""close_reason":"schedule_end""#));
        assert!(closed.contains(r#""end_time":"2026-08-04T19:10:12Z""#));
        assert!(closed.contains(r#""segment_id":"eec87e45-dcd2-56fe-81e2-850fcb1f18d7""#));
        std::fs::remove_dir_all(&dir).ok();
    }
}
