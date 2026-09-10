//! The inbound §4 task envelope, as handed to the recorder verbatim.
//!
//! WHY THE RAW `job` BLOCK IS THE SOURCE OF TRUTH. §4 makes `data.job` a
//! **verbatim passthrough** of the API request plus backend-enriched identity, and
//! §7 requires the worker to echo that block on every notification, "never mutated
//! by the worker". So this module keeps the block as raw JSON
//! ([`TaskData::job_raw`]) and derives a typed view from it ([`TaskData::job`]) for
//! the handful of values the recorder ACTS on.
//!
//! WHAT "NEVER MUTATED" GUARANTEES, PRECISELY. No field is added, removed, renamed
//! or rewritten anywhere between here and the outbound `/status` call — that is the
//! invariant, and it is what downstream depends on. It is NOT textual identity: the
//! block is held as a `serde_json::Value`, and this crate does not enable
//! `serde_json/preserve_order`, so re-serialization canonicalizes object key order
//! and normalizes numeric spelling (`180.0` re-emits as `180`). The notifier
//! decodes and re-encodes the envelope in transit as well. Values, names and
//! structure survive; the original bytes do not. Do not write an assertion that
//! depends on byte equality across those hops — use field-level comparison.
//!
//! That asymmetry is the whole design. A previous version modeled one struct field
//! per echoed value, which meant a field added to §4 could not reach N8N until this
//! file grew to match it — and any field this component never modeled was silently
//! dropped from the caller's own request. Echoing the raw value inverts that: new
//! §4 fields travel through untouched, and the typed view carries only what steers
//! behaviour. The typed view must therefore NEVER be re-serialized in the raw
//! block's place; doing so would hand the caller back a re-rendered approximation
//! of their request instead of their request.
//!
//! WHAT IS NOT IN THE TASK, ON PURPOSE:
//!
//! * `occurrence_index` — which occurrence of a recurring schedule this run covers.
//!   §4 does not carry it and the recorder cannot derive it, because one dispatch
//!   covers exactly one occurrence and only the orchestrator knows which. It
//!   arrives as orchestrator config (`OCCURRENCE_INDEX`) alongside the window and
//!   the destinations, and is reported under §7's `data.status` — which is worker-
//!   owned and purely derived, never part of the echoed `job`.
//! * `output_path` — destinations are orchestrator configuration
//!   (`AIS_SOURCE_URI` / `AIS_PREVIEW_URI` / `STATUS_URI`, each nested under
//!   `<job_id>/`). A task that still carries the field decodes into `job_raw`, is
//!   echoed back, and steers nothing. Ignoring it is what lets an older caller keep
//!   working without letting it redirect where media lands.
//!
//! EVERY FIELD TOLERATES ABSENCE. A recorder run standalone from a shell has no
//! task at all, and a `#[serde(default)]` on each field means a partial or future
//! envelope still decodes rather than failing the run — the recording matters more
//! than the completeness of the identity stamped on its reports.

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;

/// §4 `job.job_type` values — one per media job the orchestrator dispatches to.
///
/// A BINARY OWNS ONE OF THEM and warns about the others rather than rejecting them
/// (see each binary's `main`): the orchestrator chose which image to launch, so a
/// mismatched `job_type` is far more likely a routing bug worth surfacing in the job
/// log than a reason to discard work that would otherwise succeed. Refusing would
/// also make a worker the arbiter of a vocabulary the API owns.
///
/// * `live` — `live-hls2mp4`, recording a scheduled window off a live edge.
/// * `vod` — `vod-hls2mp4`, downloading a published asset.
/// * `packaging` — `vod-packager`, transcoding an ABR ladder and packaging HLS.
///   It reports ONE terminal §7 document per run and no intermediate lifecycle
///   notifications (§6), which is why it is a job type of its own rather than a
///   flavour of `vod`: the two report differently, so a consumer has to be able to
///   tell them apart from the task alone.
pub const JOB_TYPE_LIVE: &str = "live";
pub const JOB_TYPE_VOD: &str = "vod";
pub const JOB_TYPE_PACKAGING: &str = "packaging";

/// The §4 envelope: `{header, data}`.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Task {
    #[serde(default)]
    pub header: TaskHeader,
    #[serde(default)]
    pub data: TaskData,
}

/// §4 `header`. Only `signature` is read: it is echoed onto the §7 envelope the
/// recorder writes, so the notification carries the signature the caller sent.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct TaskHeader {
    #[serde(default)]
    pub signature: String,
}

/// §4 `data` — a single `job` block, kept both raw and typed.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct TaskData {
    /// The `job` block as it arrived, structurally intact — no field added, removed,
    /// renamed or rewritten. This is what §7 echoes. (Not textually identical: see
    /// the module header on key order and numeric spelling.)
    ///
    /// Deserialized from `job`; the typed [`TaskData::job`] beside it is derived
    /// from this value in [`Task::parse`], never the other way round.
    #[serde(rename = "job", default)]
    pub job_raw: Value,
    /// The typed view of the same block — only the fields the recorder acts on.
    ///
    /// `skip`ped by serde and populated in [`Task::parse`] so there is exactly one
    /// wire representation of `job` and no way for the two to disagree.
    #[serde(skip)]
    pub job: TaskJob,
}

/// §4 `data.job` — ONLY the fields the recorder actually reads.
///
/// DELIBERATELY TINY, and it must stay that way. Everything else in the block —
/// `brand_id`, `agent_id`, `ext_job_id`, `priority`, `email`, `ai_flags`,
/// `parent_job_id`, `metadata[]`, and the vod-only `project_id` /
/// `ext_project_id` — rides in [`TaskData::job_raw`] and reaches the notification
/// untouched. Adding a field here buys nothing unless the recorder branches on it,
/// and costs the thing this design exists for: a §4 addition that needs no change
/// to this file.
///
/// `media_id` IS modeled, and is the exception that shows the rule: it was
/// echo-only until a manual clip started naming its segment with it, at which
/// point the recorder began branching on it and it had to be read. `parent_job_id`
/// arrived in the same change and is NOT here, because nothing branches on it.
///
/// NOT MODELED, though §4 carries them:
///
/// * `media_assets.uri` — the source the recorder reads is `EXT_SOURCE_URI`, an
///   orchestrator decision. Taking it from the task instead would let a caller
///   redirect what gets recorded.
/// * `schedule.start` / `.end` — the window is `--start` / `--end`, likewise the
///   orchestrator's. `schedule` is purely temporal in §4 (`metadata` moved out to
///   `job.metadata[]`), and none of it steers this process.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct TaskJob {
    /// `create` | `update` | `delete`, logged at startup so an operator reading the
    /// job log can see which dispatch this was. Never branched on: `update` and
    /// `delete` are resolved upstream by rescheduling, and an occurrence already
    /// capturing runs to completion regardless.
    #[serde(default)]
    pub action: String,
    /// `live` | `vod`. Checked at startup so a live recorder handed a vod task SAYS
    /// SO, instead of recording a window that means nothing for that job type. A
    /// warning and not a refusal: the orchestrator chose this binary, and a
    /// mislabelled task is more likely a routing bug worth surfacing than a reason
    /// to throw away a capture that would otherwise succeed.
    #[serde(default)]
    pub job_type: String,
    /// `scheduled` | `manual`, live-only. MODELED BECAUSE THE RECORDER BRANCHES ON
    /// IT — the rule this struct is kept small by. A manual clip is one window
    /// clipped whole: ad breaks do not split it, no ad markers are emitted, the
    /// deliverables stop at the master, and `segment_id` comes from the task
    /// instead of being minted. Absent or `scheduled` is the behaviour that
    /// existed before, so a producer that never sends the field is unaffected.
    #[serde(default)]
    pub job_sub_type: String,
    /// The CMS media this job belongs to. Modeled for the same reason: on a manual
    /// clip it IS the segment id (the Video Editor pre-created the row and named
    /// it), so the recorder reads it rather than deriving one that would address
    /// nothing.
    #[serde(default)]
    pub media_id: String,
    #[serde(default)]
    pub job_id: String,
}

/// `data.job.job_sub_type` values. `parent_job_id` is deliberately NOT modeled:
/// nothing branches on it, so it rides `job_raw` onto every notification echo
/// untouched, which is all the contract asks of this worker.
pub const SUB_TYPE_MANUAL: &str = "manual";

impl TaskJob {
    /// Whether this is a user-triggered clip of one past window.
    pub fn is_manual(&self) -> bool {
        self.job_sub_type.eq_ignore_ascii_case(SUB_TYPE_MANUAL)
    }
}

impl Task {
    /// Decodes the `TASK_JSON` argument, then derives the typed `job` view from the
    /// raw block.
    ///
    /// A malformed task is an ERROR, not a warning, and that asymmetry is
    /// deliberate: the recorder can run with no task (standalone), but a task that
    /// was supplied and cannot be read means the orchestrator and the recorder
    /// disagree about the contract. Continuing would produce a recording whose
    /// reports carry no identity — work that is done but cannot be attributed to
    /// the job that asked for it, which is worse than failing at startup where the
    /// operator sees why.
    ///
    /// The second decode (raw → typed) is held to the same standard: a `job` whose
    /// modeled fields have the wrong types is the same contract disagreement, so it
    /// fails here rather than defaulting silently and recording against an empty
    /// window.
    pub fn parse(raw: &str) -> Result<Self> {
        let mut task: Task =
            serde_json::from_str(raw).context("TASK_JSON is not a §4 {header, data} envelope")?;
        if !task.data.job_raw.is_null() {
            task.data.job = serde_json::from_value(task.data.job_raw.clone())
                .context("TASK_JSON data.job is not a §4 job block")?;
        }
        Ok(task)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The echoed block of a decoded task.
    fn raw_of(t: &Task) -> &Value {
        &t.data.job_raw
    }

    /// CL-01 / CL-05 — the manual sub-type and the media id it names its segment
    /// with are decoded, and `parent_job_id` is NOT modeled yet still survives in
    /// the echo, which is all §4 asks of this worker.
    #[test]
    fn a_manual_task_decodes_its_sub_type_and_media_id() {
        let t = Task::parse(
            r#"{
              "header": {"signature": "sig-1", "type": "task"},
              "data": {
                "job": {
                  "action": "create",
                  "job_id": "11111111-1111-4111-8111-111111111111",
                  "job_type": "live",
                  "job_sub_type": "manual",
                  "parent_job_id": "44444444-4444-4444-8444-444444444444",
                  "media_id": "55555555-5555-4555-8555-555555555555"
                }
              }
            }"#,
        )
        .expect("decodes");
        assert!(t.data.job.is_manual());
        assert_eq!(t.data.job.media_id, "55555555-5555-4555-8555-555555555555");
        assert_eq!(
            raw_of(&t)["parent_job_id"],
            "44444444-4444-4444-8444-444444444444",
            "unmodeled, but echoed"
        );
    }

    /// A task with no sub-type is the scheduled path — the behaviour that existed
    /// before the field did.
    #[test]
    fn a_task_without_a_sub_type_is_not_manual() {
        let t = Task::parse(r#"{"header": {}, "data": {"job": {"job_type": "live"}}}"#)
            .expect("decodes");
        assert!(!t.data.job.is_manual());
        assert_eq!(t.data.job.media_id, "");
    }

    /// A full §4 live-create task: the typed view reads what it acts on, and the raw
    /// block keeps everything — including the fields this module does not model.
    #[test]
    fn a_full_task_decodes_the_typed_view_and_keeps_the_raw_block() {
        let t = Task::parse(
            r#"{
              "header": {"signature": "sig-1", "type": "task"},
              "data": {
                "job": {
                  "action": "create",
                  "job_id": "11111111-1111-4111-8111-111111111111",
                  "brand_id": "22222222-2222-4222-8222-222222222222",
                  "agent_id": "33333333-3333-4333-8333-333333333333",
                  "ext_job_id": "ext-job-9",
                  "job_type": "live",
                  "priority": 500,
                  "email": "producer@example.com",
                  "ai_flags": ["GENERATE_METADATA", "DETECT_SCENES"],
                  "media_id": "44444444-4444-4444-8444-444444444444",
                  "media_assets": {"ext_asset_id": "asset-1", "uri": "https://o/master.m3u8"},
                  "metadata": [
                    {"ext_asset_id": "asset-1", "language": "en-US", "title": "Late Night Live",
                     "category": "news", "sub-category": "none"}
                  ],
                  "schedule": {
                    "ext_event_id": "lnl-2026-08-07",
                    "start": {"date_time": "2026-08-07T19:00:00Z"},
                    "end": {"date_time": "2026-08-07T19:04:00Z"}
                  }
                }
              }
            }"#,
        )
        .expect("decodes");

        // Header.
        assert_eq!(t.header.signature, "sig-1");

        // The typed view — only what the recorder acts on.
        assert_eq!(t.data.job.action, "create");
        assert_eq!(t.data.job.job_type, "live");
        assert_eq!(t.data.job.job_id, "11111111-1111-4111-8111-111111111111");
        // media_assets and schedule are NOT in the typed view — they are the
        // orchestrator's to decide — but they are still echoed, whole.
        assert_eq!(
            raw_of(&t)
                .pointer("/media_assets/uri")
                .and_then(Value::as_str),
            Some("https://o/master.m3u8")
        );
        assert_eq!(
            raw_of(&t)
                .pointer("/schedule/start/date_time")
                .and_then(Value::as_str),
            Some("2026-08-07T19:00:00Z")
        );

        // The raw block — everything, including what the typed view omits.
        let raw = &t.data.job_raw;
        assert_eq!(
            raw.get("brand_id").and_then(Value::as_str),
            Some("22222222-2222-4222-8222-222222222222")
        );
        assert_eq!(
            raw.get("agent_id").and_then(Value::as_str),
            Some("33333333-3333-4333-8333-333333333333")
        );
        assert_eq!(
            raw.get("ext_job_id").and_then(Value::as_str),
            Some("ext-job-9")
        );
        assert_eq!(raw.get("priority").and_then(Value::as_u64), Some(500));
        assert_eq!(
            raw.get("email").and_then(Value::as_str),
            Some("producer@example.com")
        );
        assert_eq!(
            raw.get("media_id").and_then(Value::as_str),
            Some("44444444-4444-4444-8444-444444444444")
        );
        assert_eq!(
            raw.get("ai_flags").and_then(Value::as_array).map(Vec::len),
            Some(2)
        );
        // metadata is NOT modeled and must still be present, whole.
        let meta = raw
            .get("metadata")
            .and_then(Value::as_array)
            .expect("metadata survives");
        assert_eq!(meta.len(), 1);
        assert_eq!(
            meta[0].get("category").and_then(Value::as_str),
            Some("news")
        );
        assert_eq!(
            meta[0].get("sub-category").and_then(Value::as_str),
            Some("none")
        );
    }

    /// A key this component knows nothing about must reach the notification. This is
    /// the property that makes a future §4 field a no-op here.
    #[test]
    fn unmodeled_job_keys_survive_whole() {
        let t = Task::parse(
            r#"{"data":{"job":{
                 "job_id":"j-1",
                 "some_future_field":{"nested":[1,2,3]},
                 "metadata":[{"language":"en-US","title":"T","subtitle":"S","rank":7}]
               }}}"#,
        )
        .expect("decodes");
        let raw = &t.data.job_raw;
        assert_eq!(
            raw.pointer("/some_future_field/nested/2")
                .and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            raw.pointer("/metadata/0/subtitle").and_then(Value::as_str),
            Some("S")
        );
        assert_eq!(
            raw.pointer("/metadata/0/rank").and_then(Value::as_u64),
            Some(7)
        );
    }

    /// `output_path` is ignored, not rejected: an older caller still sending it must
    /// not fail, and must not steer where anything lands. It is echoed like any
    /// other unmodeled key.
    #[test]
    fn an_output_path_in_the_job_is_ignored_but_echoed() {
        let t = Task::parse(
            r#"{"data":{"job":{"job_id":"j-1","output_path":"gs://somewhere-else/x/"}}}"#,
        )
        .expect("an unmodeled field must not fail the decode");
        assert_eq!(t.data.job.job_id, "j-1");
        assert_eq!(
            t.data.job_raw.get("output_path").and_then(Value::as_str),
            Some("gs://somewhere-else/x/")
        );
    }

    /// A partial envelope still decodes — the recording matters more than a complete
    /// identity, and every field defaults.
    #[test]
    fn a_partial_task_decodes_to_defaults() {
        let t = Task::parse(r#"{"data":{"job":{"job_id":"j-2"}}}"#).expect("decodes");
        assert_eq!(t.data.job.job_id, "j-2");
        assert_eq!(t.header.signature, "");
        assert_eq!(t.data.job.action, "");
        assert_eq!(t.data.job.job_type, "");

        // No `job` block at all: the raw value stays null and the typed view is all
        // defaults, which is what a task carrying no job means.
        let empty = Task::parse("{}").expect("decodes");
        assert!(empty.data.job_raw.is_null());
        assert_eq!(empty.data.job.job_id, "");
    }

    /// A supplied-but-unreadable task fails loudly. See [`Task::parse`] for why this
    /// is an error while having no task at all is not.
    #[test]
    fn a_malformed_task_is_an_error() {
        assert!(Task::parse("not json").is_err());
        assert!(Task::parse("").is_err());
        // A scalar cannot be a struct, so the envelope shape itself is checked.
        assert!(Task::parse(r#"{"data": 5}"#).is_err());
        assert!(Task::parse(r#"{"header": "sig-1"}"#).is_err());
        // A modeled field with the wrong type is a contract disagreement, caught on
        // the second decode rather than defaulted away.
        assert!(Task::parse(r#"{"data":{"job":{"job_id":7}}}"#).is_err());
        assert!(Task::parse(r#"{"data":{"job":{"action":[]}}}"#).is_err());
        assert!(Task::parse(r#"{"data":{"job":{"job_type":{}}}}"#).is_err());
        // A `job` that is not an object cannot be a job block.
        assert!(Task::parse(r#"{"data":{"job":5}}"#).is_err());
        assert!(Task::parse(r#"{"data":{"job":"create"}}"#).is_err());
    }

    /// THE SEQUENCE FORM OF A STRUCT IS ACCEPTED, and that is worth pinning because
    /// it is surprising: serde can deserialize a struct from a SEQUENCE as well as a
    /// map, so with every field defaulted an empty array yields an all-default value.
    ///
    /// Recorded rather than "fixed" because the outcome is right either way — an
    /// all-default `media_assets` is what a job carrying no asset means, and the
    /// recorder falls back to its configured source URI. What would be wrong is
    /// believing this case is rejected.
    #[test]
    fn the_sequence_form_of_a_nested_struct_is_accepted_as_all_defaults() {
        // `job` itself as a sequence: all three modeled fields default.
        let t =
            Task::parse(r#"{"data":{"job":[]}}"#).expect("serde accepts the seq form of a struct");
        assert_eq!(t.data.job.job_id, "");
        assert_eq!(t.data.job.action, "");
        // Echoed exactly as it arrived, oddity included — an array, not an object.
        assert!(t.data.job_raw.is_array());

        // And an unmodeled field of any shape is simply carried.
        let u = Task::parse(r#"{"data":{"job":{"job_id":"j","media_assets":[],"schedule":[]}}}"#)
            .expect("unmodeled fields are not type-checked here");
        assert_eq!(u.data.job.job_id, "j");
        assert!(u
            .data
            .job_raw
            .get("media_assets")
            .is_some_and(Value::is_array));
    }

    /// An UNMODELED field with a surprising type must NOT fail: the recorder does not
    /// read it, and rejecting it would make this component the gatekeeper for a
    /// contract it deliberately does not interpret.
    #[test]
    fn an_unmodeled_field_of_any_type_is_accepted() {
        let t = Task::parse(r#"{"data":{"job":{"job_id":"j","priority":"high","email":[]}}}"#)
            .expect("unmodeled types are the caller's business, not the recorder's");
        assert_eq!(t.data.job.job_id, "j");
        assert_eq!(
            t.data.job_raw.get("priority").and_then(Value::as_str),
            Some("high")
        );
    }

    // ── §4 CONTRACT MATRIX ────────────────────────────────────────────────────
    //
    // Positive and negative cases for the decode boundary, kept together so the
    // contract's edges are readable in one place. The rule under test throughout:
    // a MODELED field with the wrong type is a contract disagreement and fails;
    // an UNMODELED field is the caller's business and is echoed untouched.

    /// POSITIVE: `action` — every value §4 defines decodes, and is echoed.
    #[test]
    fn every_action_value_decodes() {
        for action in ["create", "update", "delete"] {
            let t = Task::parse(&format!(r#"{{"data":{{"job":{{"action":"{action}"}}}}}}"#))
                .unwrap_or_else(|e| panic!("{action} must decode: {e}"));
            assert_eq!(t.data.job.action, action);
            assert_eq!(
                t.data.job_raw.get("action").and_then(Value::as_str),
                Some(action)
            );
        }
    }

    /// POSITIVE: `job_type` — every value the contract defines decodes. An UNKNOWN
    /// one also decodes rather than failing: the recorder logs and records; refusing
    /// would make this component the gatekeeper for a vocabulary the API owns.
    ///
    /// The three constants are spelled out rather than looped over as `[JOB_TYPE_LIVE,
    /// …]` so the literal wire strings are pinned here: a constant renamed to a new
    /// value would still pass a test written against the constant.
    #[test]
    fn job_type_decodes_including_an_unknown_value() {
        for jt in ["live", "vod", "packaging", "something_new"] {
            let t = Task::parse(&format!(r#"{{"data":{{"job":{{"job_type":"{jt}"}}}}}}"#))
                .unwrap_or_else(|e| panic!("{jt} must decode: {e}"));
            assert_eq!(t.data.job.job_type, jt);
        }
        // And the constants ARE those strings. Asserted here because every branch
        // in the workers compares against the constant, so a constant whose value
        // drifted would keep compiling and silently stop matching real tasks.
        assert_eq!(JOB_TYPE_LIVE, "live");
        assert_eq!(JOB_TYPE_VOD, "vod");
        assert_eq!(JOB_TYPE_PACKAGING, "packaging");
    }

    /// POSITIVE: a VOD task has no `schedule` at all (§4.4). It must decode, leaving
    /// the temporal fields empty rather than failing.
    #[test]
    fn a_schedule_less_vod_task_decodes() {
        let t = Task::parse(
            r#"{"data":{"job":{"action":"create","job_type":"vod","job_id":"v-1",
                 "media_assets":{"ext_asset_id":"a-1","uri":"gs://b/in.mp4"},
                 "project_id":"p-1","ext_project_id":"ext-p-1"}}}"#,
        )
        .expect("a vod task has no schedule and must still decode");
        assert_eq!(t.data.job.job_type, "vod");
        assert!(
            t.data.job_raw.get("schedule").is_none(),
            "a vod task carries none"
        );
        // The vod-only enrichment fields are unmodeled here and ride in the echo.
        assert_eq!(
            t.data.job_raw.get("project_id").and_then(Value::as_str),
            Some("p-1")
        );
        assert_eq!(
            t.data.job_raw.get("ext_project_id").and_then(Value::as_str),
            Some("ext-p-1")
        );
    }

    /// POSITIVE: the echo is BYTE-level, not value-level — key order, unicode,
    /// deep nesting and numeric precision all survive, because nothing is
    /// re-rendered.
    #[test]
    fn the_echo_preserves_awkward_values() {
        let t = Task::parse(
            r#"{"data":{"job":{
                 "job_id":"j",
                 "metadata":[{"language":"ja-JP","title":"深夜ライブ 🎬","note":"a\"quoted\" \\ backslash"}],
                 "deep":{"a":{"b":{"c":[1,{"d":null}]}}},
                 "big":12345678901234567890,
                 "float":0.30000000000000004,
                 "empty_obj":{},
                 "empty_arr":[]
               }}}"#,
        )
        .expect("decodes");
        let raw = &t.data.job_raw;
        assert_eq!(
            raw.pointer("/metadata/0/title").and_then(Value::as_str),
            Some("深夜ライブ 🎬")
        );
        assert_eq!(
            raw.pointer("/metadata/0/note").and_then(Value::as_str),
            Some(r#"a"quoted" \ backslash"#)
        );
        assert!(raw.pointer("/deep/a/b/c/1/d").is_some_and(Value::is_null));
        assert!(raw
            .get("empty_obj")
            .is_some_and(|v| v.as_object().is_some_and(|o| o.is_empty())));
        assert!(raw
            .get("empty_arr")
            .is_some_and(|v| v.as_array().is_some_and(|a| a.is_empty())));
        // Re-serializing the echo must not lose the awkward numbers.
        let round = serde_json::to_string(raw).expect("serializes");
        assert!(round.contains("12345678901234567890"), "{round}");
        assert!(round.contains("0.30000000000000004"), "{round}");
    }

    /// NEGATIVE: a modeled STRING field given a non-string fails, for each one.
    #[test]
    fn a_modeled_string_field_of_the_wrong_type_fails() {
        for body in [
            r#"{"data":{"job":{"action":7}}}"#,
            r#"{"data":{"job":{"job_type":true}}}"#,
            r#"{"data":{"job":{"job_id":[]}}}"#,
            r#"{"header":{"signature":9},"data":{"job":{}}}"#,
        ] {
            assert!(Task::parse(body).is_err(), "must reject: {body}");
        }
    }

    /// NEGATIVE: the envelope's own shape is checked — `data` and `header` must be
    /// structs, and `job` must be an object (or absent).
    #[test]
    fn a_broken_envelope_shape_fails() {
        for body in [
            // NOTE: a bare `[]` is NOT here — serde reads the sequence form of a
            // struct, so it decodes to an all-default `Task`. Pinned as accepted in
            // `the_sequence_form_of_the_struct_is_accepted_as_all_defaults`.
            "null",
            "42",
            r#""a string""#,
            r#"{"data":"job"}"#,
            r#"{"data":{"job":true}}"#,
            r#"{"data":{"job":[1,2]}}"#,
            // `{"header":[]}` is likewise ACCEPTED (the sequence form of TaskHeader),
            // so it is not asserted here either.
        ] {
            assert!(Task::parse(body).is_err(), "must reject: {body}");
        }
    }

    /// NEGATIVE: truncated / malformed JSON fails rather than half-decoding.
    #[test]
    fn malformed_json_fails() {
        for body in [
            r#"{"data":{"job":{"job_id":"j""#,
            r#"{"data":{"job":{}}"#,
            r#"{'data':{'job':{}}}"#,
            "{,}",
            "\u{feff}{}",
        ] {
            assert!(Task::parse(body).is_err(), "must reject: {body}");
        }
    }

    /// NEGATIVE / BOUNDARY: a `job` present but EMPTY decodes to all defaults and is
    /// echoed as an empty object — not as null, and not rejected. An empty job is a
    /// misconfigured dispatch, which the recorder reports against the event id
    /// rather than refusing to record.
    #[test]
    fn an_empty_job_object_decodes_and_is_echoed_as_an_object() {
        let t = Task::parse(r#"{"data":{"job":{}}}"#).expect("decodes");
        assert_eq!(t.data.job.job_id, "");
        assert!(t.data.job_raw.is_object(), "echoed as {{}} not null");
        assert!(!t.data.job_raw.is_null());
    }

    /// BOUNDARY: an explicit `"job": null` is treated as "no job" — the typed view
    /// stays default and the echo stays null, which is what a standalone run emits.
    #[test]
    fn an_explicit_null_job_is_treated_as_absent() {
        let t = Task::parse(r#"{"data":{"job":null}}"#).expect("decodes");
        assert!(t.data.job_raw.is_null());
        assert_eq!(t.data.job.job_id, "");
    }

    /// BOUNDARY: a duplicate key — last-one-wins in serde_json — must behave the
    /// same in the typed view and the echo, so they cannot disagree about which
    /// value the caller meant.
    #[test]
    fn a_duplicate_key_resolves_identically_in_both_views() {
        let t = Task::parse(r#"{"data":{"job":{"job_id":"first","job_id":"second"}}}"#)
            .expect("decodes");
        assert_eq!(t.data.job.job_id, "second");
        assert_eq!(
            t.data.job_raw.get("job_id").and_then(Value::as_str),
            Some("second")
        );
    }
}
