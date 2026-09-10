//! SCTE-35 ad-break detection over HLS media segments, shared by the live
//! recorder (split/skip ads while polling) and the VOD downloader
//! (`--remove-ads`).

use chrono::{DateTime, Duration, Utc};
use m3u8_rs::{DateRange, MediaSegment};

/// An explicit SCTE-35 cue edge signalled on a media segment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdEdge {
    /// Ad break begins (or continues): `#EXT-X-CUE-OUT` / `X-SCTE35-OUT`.
    Out,
    /// Content resumes: `#EXT-X-CUE-IN` / `X-SCTE35-IN`.
    In,
}

/// Detects an explicit SCTE-35 cue edge in a segment's tags. The parser strips
/// the leading `#EXT-`, so tags arrive as `X-...`. Recognises `X-CUE-OUT`,
/// `X-CUE-OUT-CONT`, `X-CUE-IN`, and `X-SCTE35-OUT/IN`. `In` wins when a segment
/// carries both edges (content resumes here).
pub fn cue_marker(segment: &MediaSegment) -> Option<AdEdge> {
    for tag in &segment.unknown_tags {
        let name = tag.tag.as_str();
        if name == "X-CUE-IN" || name.starts_with("X-SCTE35-IN") {
            return Some(AdEdge::In);
        }
        if name == "X-CUE-OUT" || name == "X-CUE-OUT-CONT" || name.starts_with("X-SCTE35-OUT") {
            return Some(AdEdge::Out);
        }
    }
    None
}

/// SCTE-35 edges carried by an `#EXT-X-DATERANGE` tag: `(out, in)`.
///
/// `SCTE35-OUT` and `SCTE35-IN` are explicit edges. `SCTE35-CMD` is a *generic*
/// splice command (often a `time_signal` marking chapter/program points, e.g.
/// USP emits one at t=0), so it only counts as an ad-out when the daterange
/// carries a **positive** duration — i.e. someone authored it as a timed
/// break. The `CLASS`-only fallback (no explicit attribute at all) gets the
/// same treatment.
fn daterange_scte_edges(dr: &DateRange) -> (bool, bool) {
    let mut out = false;
    let mut cmd = false;
    let mut is_in = false;
    if let Some(attrs) = &dr.other_attributes {
        for key in attrs.keys() {
            let k = key.to_uppercase();
            if k.starts_with("SCTE35-OUT") {
                out = true;
            }
            if k.starts_with("SCTE35-CMD") {
                cmd = true;
            }
            if k.starts_with("SCTE35-IN") {
                is_in = true;
            }
        }
    }
    if !out && !cmd && !is_in {
        if let Some(class) = &dr.class {
            if class.to_uppercase().contains("SCTE35") {
                cmd = true;
            }
        }
    }
    let timed_break = dr.planned_duration.or(dr.duration).unwrap_or(0.0) > 0.0;
    (out || (cmd && timed_break), is_in)
}

/// The auto-return end of a DATERANGE break: `START-DATE + PLANNED-DURATION`
/// (falling back to `DURATION`). `None` when the daterange carries no duration.
fn scte35_window_end(dr: &DateRange) -> Option<DateTime<Utc>> {
    let secs = dr.planned_duration.or(dr.duration)?;
    Some(dr.start_date.with_timezone(&Utc) + Duration::milliseconds((secs * 1000.0) as i64))
}

/// Persistent SCTE-35 ad-break state machine.
///
/// Unifies the three signal shapes seen in the wild and the edge cases between
/// them:
///
/// - **DATERANGE `SCTE35-OUT`** — enters the break. If it carries
///   `PLANNED-DURATION`/`DURATION`, an auto-return deadline is armed so the
///   break ends on time even if no explicit IN ever arrives. If it carries no
///   duration, the break stays open until an explicit IN.
/// - **DATERANGE `SCTE35-IN`** — ends the break immediately, even mid
///   auto-return window (an early return).
/// - **Inline cues** — `CUE-OUT` … `CUE-OUT-CONT` (mid-roll continuation) …
///   `CUE-IN`, plus `X-SCTE35-OUT/IN`.
///
/// Within a single segment, IN is applied after OUT so a segment carrying both
/// resumes content. A `CUE-OUT-CONT` (out with no new window) never clears an
/// already-armed auto-return deadline.
#[derive(Debug, Default)]
pub struct AdState {
    in_ad: bool,
    ad_until: Option<DateTime<Utc>>,
    /// A pre-announced break (DATERANGE whose `START-DATE` lies beyond the
    /// tagged segment): `(starts_at, auto_return)`. Armed until the segment
    /// containing `starts_at` arrives, or an IN cancels it.
    pending: Option<(DateTime<Utc>, Option<DateTime<Utc>>)>,
}

/// The classification of one media segment against the ad-break state,
/// including the **frame-accurate splice position** when a cue lands inside
/// the segment rather than on its boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SegmentClass {
    /// Entirely content.
    Content,
    /// Entirely ad — skip it.
    Ad,
    /// Content until `splice`, ad after it (the break starts mid-segment):
    /// keep only the samples before `splice`.
    EntersAd { splice: DateTime<Utc> },
    /// Ad until `splice`, content after it (the break ends mid-segment):
    /// keep only the samples from `splice` on.
    ExitsAd { splice: DateTime<Utc> },
}

impl AdState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether a break is open **right now** — i.e. whether the next segment
    /// continues one unless it says otherwise.
    ///
    /// Exposed because a break is signalled ONCE, on its first segment, and a
    /// consumer that begins reading the playlist later has to be able to ask the
    /// state machine what it learned from the segments it has already been shown
    /// but is not keeping (the live recorder's pre-window seeding — see
    /// `seed_pre_window` in `live-hls2mp4`). It is deliberately the ONLY window
    /// into the state: the `ad_until` deadline and the `pending`
    /// pre-announcement are the machine's own bookkeeping, and answering
    /// "am I in a break" from them outside would be a second interpretation of
    /// the same cues.
    pub fn in_ad(&self) -> bool {
        self.in_ad
    }

    /// Updates the state from `seg` (which begins at `seg_start`) and returns
    /// `true` if this segment is ad content that must be skipped. Boundary
    /// (segment-accurate) view of [`AdState::classify`].
    pub fn update(&mut self, seg: &MediaSegment, seg_start: DateTime<Utc>) -> bool {
        !matches!(
            self.classify(seg, seg_start, seg_start),
            SegmentClass::Content
        )
    }

    /// Classifies the segment spanning `[seg_start, seg_end)`, resolving the
    /// exact splice position when an SCTE-35 cue falls strictly inside the
    /// segment: the DATERANGE `START-DATE` for a break start, the
    /// `START-DATE + PLANNED-DURATION` auto-return (or an IN daterange's
    /// `START-DATE`) for a break end. Inline `CUE-OUT`/`CUE-IN` tags carry no
    /// timestamp and are treated as boundary cues.
    pub fn classify(
        &mut self,
        seg: &MediaSegment,
        seg_start: DateTime<Utc>,
        seg_end: DateTime<Utc>,
    ) -> SegmentClass {
        let was_ad = self.in_ad;
        let mut saw_out = false;
        let mut saw_in = false;
        let mut out_at: Option<DateTime<Utc>> = None;
        let mut in_at: Option<DateTime<Utc>> = None;
        let mut new_window: Option<DateTime<Utc>> = None;

        if let Some(dr) = &seg.daterange {
            let (dr_out, dr_in) = daterange_scte_edges(dr);
            if dr_out {
                saw_out = true;
                out_at = Some(dr.start_date.with_timezone(&Utc));
                new_window = scte35_window_end(dr);
            }
            if dr_in {
                saw_in = true;
                in_at = Some(dr.start_date.with_timezone(&Utc));
            }
        }

        match cue_marker(seg) {
            Some(AdEdge::Out) => saw_out = true,
            Some(AdEdge::In) => saw_in = true,
            None => {}
        }

        // A previously pre-announced break whose start time this segment has
        // now reached behaves like an OUT seen here.
        if !saw_out {
            if let Some((from, until)) = self.pending {
                if from < seg_end {
                    saw_out = true;
                    out_at = Some(from);
                    new_window = until;
                    self.pending = None;
                }
            }
        }

        // OUT first, then IN, so a segment with both resumes content.
        if saw_out {
            // A DATERANGE `START-DATE` at/after the segment end is a
            // pre-announcement — schedule it instead of entering the break now.
            if out_at.is_some_and(|at| at >= seg_end && at > seg_start) {
                self.pending = Some((out_at.unwrap(), new_window));
            } else {
                self.in_ad = true;
                // Only (re)arm the deadline when this OUT actually specifies
                // one; a CUE-OUT-CONT (no window) must not clear an armed
                // auto-return.
                if new_window.is_some() {
                    self.ad_until = new_window;
                }
            }
        }
        if saw_in {
            self.in_ad = false;
            self.ad_until = None;
            self.pending = None;
        }

        // Auto-return backstop: expire the break once its window has elapsed —
        // a mid-segment expiry is a frame-accurate exit splice.
        let mut auto_return: Option<DateTime<Utc>> = None;
        if self.in_ad {
            if let Some(until) = self.ad_until {
                if until <= seg_end {
                    self.in_ad = false;
                    self.ad_until = None;
                    if until > seg_start {
                        auto_return = Some(until);
                    }
                }
            }
        }

        // Splices within ~one frame of a segment boundary snap to the boundary:
        // packagers condition segments at cue points, and float/PDT rounding
        // (sub-millisecond) must not trigger a degenerate sliver re-encode.
        const BOUNDARY_EPS_MS: i64 = 40;
        let inside = |at: DateTime<Utc>| {
            (at - seg_start).num_milliseconds() > BOUNDARY_EPS_MS
                && (seg_end - at).num_milliseconds() > BOUNDARY_EPS_MS
        };

        match (was_ad, self.in_ad) {
            (false, false) if auto_return.is_some() => {
                // Entered *and* auto-returned within this one segment.
                match out_at {
                    Some(at) if inside(at) => SegmentClass::EntersAd { splice: at },
                    _ => match auto_return {
                        Some(at) if inside(at) => SegmentClass::ExitsAd { splice: at },
                        _ => SegmentClass::Content,
                    },
                }
            }
            (false, false) => SegmentClass::Content,
            (true, true) => SegmentClass::Ad,
            (false, true) => match out_at {
                // The break starts strictly inside this segment.
                Some(at) if inside(at) => SegmentClass::EntersAd { splice: at },
                _ => SegmentClass::Ad,
            },
            (true, false) => {
                match auto_return.or(in_at) {
                    // Ends strictly inside this segment.
                    Some(at) if inside(at) => SegmentClass::ExitsAd { splice: at },
                    // At/near the segment start (or no time at all): content.
                    Some(at) if (at - seg_start).num_milliseconds() <= BOUNDARY_EPS_MS => {
                        SegmentClass::Content
                    }
                    None => SegmentClass::Content,
                    // Near/past the segment end: this segment is still all ad.
                    Some(_) => SegmentClass::Ad,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use m3u8_rs::{ExtTag, QuotedOrUnquoted};
    use std::collections::HashMap;

    fn t(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn plain() -> MediaSegment {
        MediaSegment::default()
    }

    fn with_tags(tags: &[&str]) -> MediaSegment {
        MediaSegment {
            unknown_tags: tags
                .iter()
                .map(|tag| ExtTag {
                    tag: tag.to_string(),
                    rest: None,
                })
                .collect(),
            ..Default::default()
        }
    }

    fn with_daterange(start: &str, attrs: &[&str], planned: Option<f64>) -> MediaSegment {
        let mut map = HashMap::new();
        for a in attrs {
            map.insert(
                a.to_string(),
                QuotedOrUnquoted::Unquoted("0xFC".to_string()),
            );
        }
        MediaSegment {
            daterange: Some(DateRange {
                id: "ad".to_string(),
                class: None,
                start_date: DateTime::parse_from_rfc3339(start).unwrap(),
                end_date: None,
                duration: None,
                planned_duration: planned,
                x_prefixed: None,
                end_on_next: false,
                other_attributes: (!map.is_empty()).then_some(map),
            }),
            ..Default::default()
        }
    }

    // A DATERANGE ad-out with a duration auto-returns when the window elapses,
    // even with no explicit CUE-IN (the graylive case).
    #[test]
    fn daterange_out_auto_returns_after_window() {
        let mut ad = AdState::new();
        assert!(ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], Some(120.0)),
            t("2026-01-01T00:00:00Z")
        ));
        assert!(
            ad.update(&plain(), t("2026-01-01T00:01:00Z")),
            "still in ad mid-window"
        );
        assert!(
            !ad.update(&plain(), t("2026-01-01T00:02:00Z")),
            "auto-returned at window end"
        );
    }

    // A DATERANGE ad-out with NO duration stays in-ad until an explicit IN.
    #[test]
    fn daterange_out_no_duration_waits_for_in() {
        let mut ad = AdState::new();
        assert!(ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], None),
            t("2026-01-01T00:00:00Z")
        ));
        assert!(
            ad.update(&plain(), t("2026-01-01T00:05:00Z")),
            "no window → still in ad long after"
        );
        assert!(!ad.update(
            &with_daterange("2026-01-01T00:05:06Z", &["SCTE35-IN"], None),
            t("2026-01-01T00:05:06Z")
        ));
    }

    // An explicit SCTE35-IN ends the break early, before the auto-return window.
    #[test]
    fn daterange_in_returns_early() {
        let mut ad = AdState::new();
        assert!(ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], Some(120.0)),
            t("2026-01-01T00:00:00Z")
        ));
        assert!(!ad.update(
            &with_daterange("2026-01-01T00:00:30Z", &["SCTE35-IN"], None),
            t("2026-01-01T00:00:30Z")
        ));
    }

    // CUE-OUT … CUE-OUT-CONT … CUE-IN span; the CONT must keep the break open.
    #[test]
    fn cue_out_cont_in_span() {
        let mut ad = AdState::new();
        assert!(ad.update(&with_tags(&["X-CUE-OUT"]), t("2026-01-01T00:00:00Z")));
        assert!(ad.update(&with_tags(&["X-CUE-OUT-CONT"]), t("2026-01-01T00:00:06Z")));
        assert!(ad.update(&plain(), t("2026-01-01T00:00:12Z")));
        assert!(!ad.update(&with_tags(&["X-CUE-IN"]), t("2026-01-01T00:00:18Z")));
    }

    // A segment carrying both OUT and IN resumes content (IN wins).
    #[test]
    fn daterange_out_and_in_same_segment_resumes() {
        let mut ad = AdState::new();
        assert!(ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], None),
            t("2026-01-01T00:00:00Z")
        ));
        assert!(!ad.update(
            &with_daterange("2026-01-01T00:00:06Z", &["SCTE35-OUT", "SCTE35-IN"], None),
            t("2026-01-01T00:00:06Z")
        ));
    }

    // A bare SCTE35-CMD (generic splice command, e.g. USP's time_signal at
    // t=0) with no duration must NOT open a break — only an authored, timed
    // one may.
    #[test]
    fn cmd_without_duration_is_not_an_ad() {
        let mut ad = AdState::new();
        assert!(!ad.update(
            &with_daterange("1970-01-01T00:00:00Z", &["SCTE35-CMD"], None),
            t("1970-01-01T00:00:00Z")
        ));
        assert!(
            !ad.update(&plain(), t("1970-01-01T00:05:00Z")),
            "content must keep flowing after a bare CMD"
        );
    }

    // A SCTE35-CMD that does carry a positive duration is a timed break.
    #[test]
    fn cmd_with_duration_enters_break() {
        let mut ad = AdState::new();
        assert!(ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-CMD"], Some(60.0)),
            t("2026-01-01T00:00:00Z")
        ));
        assert!(!ad.update(&plain(), t("2026-01-01T00:01:00Z")), "auto-return");
    }

    // classify(): a DATERANGE OUT whose START-DATE falls inside the segment
    // yields a frame-accurate EntersAd splice; the ad then runs; the
    // auto-return landing inside a later segment yields ExitsAd.
    #[test]
    fn classify_resolves_mid_segment_splices() {
        let mut ad = AdState::new();
        // Segment 00:00–00:06.4; break starts at 00:03 with a 10s window.
        let seg = with_daterange("2026-01-01T00:00:03Z", &["SCTE35-OUT"], Some(10.0));
        assert_eq!(
            ad.classify(&seg, t("2026-01-01T00:00:00Z"), t("2026-01-01T00:00:06.4Z")),
            SegmentClass::EntersAd {
                splice: t("2026-01-01T00:00:03Z")
            }
        );
        // Fully inside the break.
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:06.4Z"), t("2026-01-01T00:00:12.8Z")),
            SegmentClass::Ad
        );
        // Auto-return at 00:13 lands inside this segment.
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:12.8Z"), t("2026-01-01T00:00:19.2Z")),
            SegmentClass::ExitsAd {
                splice: t("2026-01-01T00:00:13Z")
            }
        );
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:19.2Z"), t("2026-01-01T00:00:25.6Z")),
            SegmentClass::Content
        );
    }

    // classify(): a pre-announced DATERANGE (START-DATE beyond the tagged
    // segment) must not enter the break early — it is scheduled and fires on
    // the segment that actually contains the splice.
    #[test]
    fn classify_schedules_pre_announced_breaks() {
        let mut ad = AdState::new();
        // Tagged at 00:00–00:06.4, but the break starts at 00:10.
        let seg = with_daterange("2026-01-01T00:00:10Z", &["SCTE35-OUT"], Some(60.0));
        assert_eq!(
            ad.classify(&seg, t("2026-01-01T00:00:00Z"), t("2026-01-01T00:00:06.4Z")),
            SegmentClass::Content
        );
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:06.4Z"), t("2026-01-01T00:00:12.8Z")),
            SegmentClass::EntersAd {
                splice: t("2026-01-01T00:00:10Z")
            }
        );
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:12.8Z"), t("2026-01-01T00:00:19.2Z")),
            SegmentClass::Ad
        );
    }

    // A splice within a frame of a segment boundary snaps to the boundary —
    // no degenerate sliver trim (the live wand case: auto-return 0.4ms before
    // the conditioned segment's end).
    #[test]
    fn classify_snaps_near_boundary_splices() {
        let mut ad = AdState::new();
        // Break with a window ending 0.4ms before a segment boundary.
        assert_eq!(
            ad.classify(
                &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], Some(6.3996)),
                t("2026-01-01T00:00:00Z"),
                t("2026-01-01T00:00:03.2Z")
            ),
            SegmentClass::Ad
        );
        // The segment ending at 00:06.4: window end 00:06.3996 is within 40ms
        // of the boundary -> whole segment is still ad, no sliver.
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:03.2Z"), t("2026-01-01T00:00:06.4Z")),
            SegmentClass::Ad
        );
        assert_eq!(
            ad.classify(&plain(), t("2026-01-01T00:00:06.4Z"), t("2026-01-01T00:00:12.8Z")),
            SegmentClass::Content
        );
    }

    // `in_ad()` answers what a consumer joining mid-stream needs to know: is a
    // break open right now. It must follow the break through both of its ends —
    // the auto-return and an explicit IN — so a caller that seeds the machine
    // from segments it is not keeping cannot carry a finished break forward.
    #[test]
    fn in_ad_tracks_the_open_break_through_both_of_its_ends() {
        let mut ad = AdState::new();
        assert!(!ad.in_ad(), "no break has been signalled yet");

        // Auto-return: the break closes itself when the window elapses.
        ad.update(
            &with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], Some(120.0)),
            t("2026-01-01T00:00:00Z"),
        );
        assert!(ad.in_ad(), "the OUT opened it");
        ad.update(&plain(), t("2026-01-01T00:01:00Z"));
        assert!(ad.in_ad(), "still inside the window");
        ad.update(&plain(), t("2026-01-01T00:02:00Z"));
        assert!(!ad.in_ad(), "the auto-return closed it");

        // Explicit IN: the same answer by the other route.
        ad.update(&with_tags(&["X-CUE-OUT"]), t("2026-01-01T00:03:00Z"));
        assert!(ad.in_ad());
        ad.update(&with_tags(&["X-CUE-IN"]), t("2026-01-01T00:03:30Z"));
        assert!(!ad.in_ad());
    }

    // A break pre-announced by a DATERANGE whose START-DATE lies beyond the
    // tagged segment has NOT begun: `in_ad()` must not report one, or a consumer
    // seeding from this segment would drop content that is still programme.
    #[test]
    fn in_ad_is_false_while_a_break_is_only_pending() {
        let mut ad = AdState::new();
        ad.classify(
            &with_daterange("2026-01-01T00:00:10Z", &["SCTE35-OUT"], Some(60.0)),
            t("2026-01-01T00:00:00Z"),
            t("2026-01-01T00:00:06.4Z"),
        );
        assert!(!ad.in_ad(), "announced, not started");
        ad.classify(
            &plain(),
            t("2026-01-01T00:00:06.4Z"),
            t("2026-01-01T00:00:12.8Z"),
        );
        assert!(ad.in_ad(), "the announced start has now been reached");
    }

    // A zero-length placement opportunity (CUE-OUT:0 + CUE-IN on the same
    // segment, PLANNED-DURATION=0) must not mark any content as ad.
    #[test]
    fn zero_length_placement_opportunity_keeps_content() {
        let mut ad = AdState::new();
        let mut seg = with_tags(&["X-CUE-OUT", "X-CUE-IN"]);
        seg.daterange = with_daterange("2026-01-01T00:00:00Z", &["SCTE35-OUT"], Some(0.0))
            .daterange
            .take();
        assert!(!ad.update(&seg, t("2026-01-01T00:00:00Z")));
        assert!(!ad.update(&plain(), t("2026-01-01T00:00:06Z")));
    }
}
