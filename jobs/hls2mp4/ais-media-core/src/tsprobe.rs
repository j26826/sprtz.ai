//! Frame rate MEASURED from the recorded MPEG-TS bytes.
//!
//! HLS declares `FRAME-RATE` on `EXT-X-STREAM-INF`, but RFC 8216 makes it
//! OPTIONAL and plenty of origins omit it — and when they do, the §7.1
//! `media_profile` goes out with no frame rate at all. A frame-accurate editor
//! downstream then has nothing to cut against.
//!
//! Nothing in the manifest can fill that gap honestly. `CODECS` carries an
//! RFC 6381 level, which bounds macroblocks per second but does not determine
//! frames per second (level 3.1 permits 720p60 and 720p30 alike); `BANDWIDTH`
//! says nothing about frame timing; and RFC 8216 §4.3.4.3 explicitly FORBIDS
//! `FRAME-RATE` on `EXT-X-I-FRAME-STREAM-INF`, so the I-frame playlist is not a
//! fallback either. The only honest source is the media.
//!
//! So this reads it from the media — the same bytes the recorder is already
//! holding in memory on their way to the object store. No probe subprocess, no
//! second fetch, no new dependency: one linear pass over packets that are
//! already resident.
//!
//! **It measures or it says nothing.** Every guard here fails to `None` rather
//! than to a plausible default, which is the same standard `SourceProfile`'s
//! `video_codec` and `resolution_label` already hold themselves to: a frame rate
//! invented for a variable-rate source would be worse than an absent one,
//! because a consumer cannot tell it is being misinformed.

/// MPEG-TS packet length. The 192-byte M2TS variant (a 4-byte arrival timecode
/// ahead of each packet) is not handled: HLS carries plain 188-byte packets, and
/// a misread would be silently wrong rather than loudly absent.
const PACKET_LEN: usize = 188;

/// Every TS packet opens with this.
const SYNC_BYTE: u8 = 0x47;

/// PTS ticks per second (RFC 8216 / ISO 13818-1 system clock, 90 kHz).
const PTS_HZ: f64 = 90_000.0;

/// PES `stream_id` range that denotes a video elementary stream (ISO 13818-1
/// Table 2-18). Audio is `0xC0..=0xDF`, which is why the PID can be recognised
/// from the PES header alone and neither PAT nor PMT has to be parsed.
const VIDEO_STREAM_IDS: std::ops::RangeInclusive<u8> = 0xE0..=0xEF;

/// How many presentation stamps the leading sample holds.
///
/// BOUNDED ON PURPOSE. A constant frame rate is established in seconds, so a
/// two-hour window has nothing to add that its first few minutes did not
/// already settle — and an unbounded `Vec` would grow with the recording for no
/// gain. 8192 stamps is roughly four and a half minutes of 30 fps video and
/// costs 64 KiB.
const MAX_SAMPLES: usize = 8192;

/// How many of the MOST RECENT stamps are kept alongside the leading sample.
///
/// The leading sample alone would answer for the whole event from its first few
/// minutes, and a stream that changed rate after that would be reported at a
/// rate it no longer runs at — invisibly, because nothing would be looking. The
/// trailing ring is what makes that detectable: it is compared against the lead
/// in [`FrameRateProbe::frame_rate`], and a disagreement retracts the answer
/// rather than picking a half. ~1 minute of 30 fps video, 16 KiB.
const TAIL_SAMPLES: usize = 2048;

/// How far the trailing sample may drift from the leading one, in percent,
/// before the probe concludes the rate CHANGED and reports nothing.
///
/// Tight because two samples of one constant-rate stream agree to within
/// rounding; anything looser would start tolerating a real rate change.
const TAIL_TOLERANCE_PCT: f64 = 1.0;

/// Fewest stamps that can support an answer, ~2 seconds of 30 fps video.
///
/// A handful of frames can agree by coincidence; this is the floor below which
/// the probe reports nothing rather than a number it cannot stand behind.
const MIN_SAMPLES: usize = 60;

/// Share of intervals that must agree before the result is published, in
/// percent. Below this the source is variable-rate, or spliced badly enough
/// that an average would describe no part of it.
const REQUIRED_AGREEMENT_PCT: usize = 90;

/// Accumulates video presentation stamps from the MPEG-TS being recorded, and
/// reports the frame rate they imply.
///
/// Fed the same slices the recorder writes, so it observes exactly what was
/// published rather than re-reading the origin's advertisement.
#[derive(Debug, Default)]
pub struct FrameRateProbe {
    /// The PID the first video PES appeared on. Fixed once seen, so a stream
    /// carrying two video programs contributes only the one being recorded
    /// rather than interleaving both into one nonsense interval set.
    video_pid: Option<u16>,
    /// The event's LEADING stamps, in the order they were parsed — DECODE
    /// order, which is why [`Self::frame_rate`] sorts before differencing.
    lead: Vec<u64>,
    /// The event's MOST RECENT stamps, oldest evicted. Kept so a rate that
    /// changes after `lead` filled is still visible.
    tail: std::collections::VecDeque<u64>,
    /// Set once `lead` is full — i.e. the event outran the leading sample, so
    /// the trailing one covers a genuinely later part of it and is worth
    /// comparing against.
    saturated: bool,
}

impl FrameRateProbe {
    pub fn new() -> Self {
        Self::default()
    }

    /// Reads any video presentation stamps out of one segment's worth of TS.
    ///
    /// Cheap enough to sit on the capture path: one pass over bytes already
    /// resident, both buffers bounded, no allocation once they are full. It
    /// keeps reading for the WHOLE event rather than stopping when the leading
    /// sample fills — that is the only way a rate change later on can be seen
    /// at all, and the cost is a fixed ~100 microseconds per segment.
    pub fn feed(&mut self, ts: &[u8]) {
        let Some(start) = alignment(ts) else {
            return;
        };
        for packet in ts[start..].chunks_exact(PACKET_LEN) {
            // Alignment is established once, at the top. A packet that does not
            // start with the sync byte means the stream desynchronised, and
            // guessing a new offset risks reading a payload as a header — so the
            // pass ends here and reports what it already has.
            if packet[0] != SYNC_BYTE {
                return;
            }
            if let Some(pts) = self.stamp_in(packet) {
                if self.lead.len() < MAX_SAMPLES {
                    self.lead.push(pts);
                } else {
                    self.saturated = true;
                }
                if self.tail.len() == TAIL_SAMPLES {
                    self.tail.pop_front();
                }
                self.tail.push_back(pts);
            }
        }
    }

    /// The presentation stamp a packet starts, when it starts a video PES that
    /// carries one.
    fn stamp_in(&mut self, packet: &[u8]) -> Option<u64> {
        // Only a payload-unit-start packet can open a PES header; the rest are
        // continuation payload with no timing of their own.
        if packet[1] & 0x40 == 0 {
            return None;
        }
        let pid = (u16::from(packet[1] & 0x1F) << 8) | u16::from(packet[2]);

        // adaptation_field_control: 0b01 payload only, 0b11 adaptation then
        // payload. 0b00 and 0b10 carry no payload at all.
        let payload_start = match (packet[3] >> 4) & 0x03 {
            0b01 => 4,
            0b11 => 5 + usize::from(packet[4]),
            _ => return None,
        };
        let payload = packet.get(payload_start..)?;

        // PES: 3-byte start code prefix, stream_id, 2-byte length, then the
        // optional header whose first two bits are `10`.
        if payload.len() < 14 || payload[0..3] != [0x00, 0x00, 0x01] {
            return None;
        }
        if !VIDEO_STREAM_IDS.contains(&payload[3]) {
            return None;
        }
        match self.video_pid {
            None => self.video_pid = Some(pid),
            Some(seen) if seen != pid => return None,
            Some(_) => {}
        }
        if payload[6] & 0xC0 != 0x80 {
            return None;
        }
        // PTS_DTS_flags occupies the top two bits; bit 1 says a PTS is present.
        if payload[7] & 0x80 == 0 {
            return None;
        }
        Some(decode_pts(&payload[9..14]))
    }

    /// Frames per second, or `None` when the stamps cannot support an answer.
    ///
    /// The stamps are sorted before differencing because PES packets arrive in
    /// DECODE order: a stream with B-frames emits them out of presentation
    /// order, so consecutive-as-parsed intervals would be negative or doubled
    /// for reasons that say nothing about the frame rate.
    ///
    /// The reported value is the mean of the intervals that AGREE, not of all of
    /// them. A run spanning a splice or a 33-bit PTS wrap contains one enormous
    /// interval that is an artefact of the discontinuity rather than a frame
    /// duration, and averaging it in would drag the answer arbitrarily far from
    /// every real frame. Trimming to a window around the median removes it
    /// without needing to detect the discontinuity itself.
    /// A rate that CHANGED mid-event is retracted rather than averaged. The
    /// leading and trailing samples are measured independently and must agree:
    /// no single number describes a stream that ran at two rates, so the honest
    /// answer is none at all. The comparison only applies once the event
    /// outran the leading sample — before that the two overlap and agree
    /// trivially.
    pub fn frame_rate(&self) -> Option<f64> {
        let lead = rate_of(&self.lead)?;
        if self.saturated {
            let tail: Vec<u64> = self.tail.iter().copied().collect();
            // A trailing sample that cannot support an answer of its own is not
            // evidence of a change; only a confident DISAGREEMENT retracts.
            if let Some(tail) = rate_of(&tail) {
                if (tail - lead).abs() / lead * 100.0 > TAIL_TOLERANCE_PCT {
                    return None;
                }
            }
        }
        Some(lead)
    }
}

/// Frames per second implied by one set of presentation stamps.
fn rate_of(stamps: &[u64]) -> Option<f64> {
    if stamps.len() < MIN_SAMPLES {
        return None;
    }
    let mut ordered = stamps.to_vec();
    ordered.sort_unstable();
    // Duplicates are the same frame stamped twice (a repeated PES header), not
    // a zero-length frame; a zero interval would drag the mean down.
    ordered.dedup();
    if ordered.len() < MIN_SAMPLES {
        return None;
    }

    let intervals: Vec<u64> = ordered.windows(2).map(|w| w[1] - w[0]).collect();
    let mut ranked = intervals.clone();
    ranked.sort_unstable();
    let median = ranked[ranked.len() / 2];
    if median == 0 {
        return None;
    }
    // +/-10% of the median. Generous next to the jitter a constant rate
    // actually shows — integer stamps make 90000/29.97 alternate 3003 and 3004,
    // 0.03% — and tight enough that two DIFFERENT rates cannot both sit inside
    // it. At +/-25% they could: 30 fps and 25 fps are 3000 and 3600 ticks apart,
    // both within 25% of 3600, so a stream carrying each for half its length
    // passed the agreement gate below and reported a mean describing neither.
    let (low, high) = (median * 90 / 100, median * 110 / 100);
    let agreeing: Vec<u64> = intervals
        .iter()
        .copied()
        .filter(|d| (low..=high).contains(d))
        .collect();
    if agreeing.len() * 100 < intervals.len() * REQUIRED_AGREEMENT_PCT {
        return None;
    }
    let total: u64 = agreeing.iter().sum();
    if total == 0 {
        return None;
    }
    // Mean over the agreeing set rather than the modal interval: for a rate
    // whose period is not a whole number of ticks, no single interval is
    // correct and only their average recovers it.
    Some(PTS_HZ * agreeing.len() as f64 / total as f64)
}

/// Offset of the first packet boundary, found by requiring the sync byte to
/// recur at the packet stride.
///
/// A single `0x47` proves nothing — it is a perfectly ordinary payload byte — so
/// a candidate offset is accepted only when the byte repeats one packet later.
fn alignment(ts: &[u8]) -> Option<usize> {
    (0..PACKET_LEN.min(ts.len())).find(|&i| {
        ts.get(i) == Some(&SYNC_BYTE)
            && ts
                .get(i + PACKET_LEN)
                .is_none_or(|next| *next == SYNC_BYTE)
    })
}

/// The 33-bit presentation stamp packed across 5 bytes with marker bits
/// interleaved (ISO 13818-1 §2.4.3.7).
fn decode_pts(p: &[u8]) -> u64 {
    (u64::from(p[0] & 0x0E) >> 1) << 30
        | u64::from(p[1]) << 22
        | (u64::from(p[2] & 0xFE) >> 1) << 15
        | u64::from(p[3]) << 7
        | (u64::from(p[4] & 0xFE) >> 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One TS packet opening a video PES that carries `pts`.
    fn video_pes_packet(pid: u16, pts: u64) -> Vec<u8> {
        let mut p = vec![0xFF; PACKET_LEN];
        p[0] = SYNC_BYTE;
        p[1] = 0x40 | ((pid >> 8) as u8 & 0x1F); // payload_unit_start_indicator
        p[2] = (pid & 0xFF) as u8;
        p[3] = 0x10; // payload only
        p[4..7].copy_from_slice(&[0x00, 0x00, 0x01]);
        p[7] = 0xE0; // stream_id: video
        p[8..10].copy_from_slice(&[0x00, 0x00]); // PES_packet_length (unbounded)
        p[10] = 0x80; // '10' marker, no scrambling
        p[11] = 0x80; // PTS_DTS_flags = '10' (PTS only)
        p[12] = 0x05; // PES_header_data_length
        p[13] = 0x21 | (((pts >> 30) as u8 & 0x07) << 1);
        p[14] = ((pts >> 22) & 0xFF) as u8;
        p[15] = 0x01 | (((pts >> 15) as u8 & 0x7F) << 1);
        p[16] = ((pts >> 7) & 0xFF) as u8;
        p[17] = 0x01 | (((pts) as u8 & 0x7F) << 1);
        p
    }

    /// `count` packets whose stamps advance by `step`, optionally shuffled into
    /// a decode order that is not presentation order.
    fn stream(pid: u16, count: u64, step: u64, reorder: bool) -> Vec<u8> {
        let mut stamps: Vec<u64> = (0..count).map(|i| 900_000 + i * step).collect();
        if reorder {
            // A crude B-frame pattern: every pair swapped, so as-parsed
            // differencing would see -step then +2*step.
            for pair in stamps.chunks_mut(2) {
                pair.reverse();
            }
        }
        stamps
            .into_iter()
            .flat_map(|pts| video_pes_packet(pid, pts))
            .collect()
    }

    /// A stamp survives the marker-bit packing intact.
    #[test]
    fn a_presentation_stamp_round_trips_through_its_packing() {
        for pts in [0_u64, 1, 3003, 900_000, (1 << 33) - 1] {
            let packet = video_pes_packet(0x100, pts);
            assert_eq!(decode_pts(&packet[13..18]), pts, "pts {pts}");
        }
    }

    #[test]
    fn a_constant_rate_stream_reports_its_rate() {
        let mut probe = FrameRateProbe::new();
        probe.feed(&stream(0x100, 300, 3000, false));
        let fps = probe.frame_rate().expect("300 stamps is plenty");
        assert!((fps - 30.0).abs() < 0.01, "got {fps}");
    }

    /// 90000/29.97 is 3003.003, so integer stamps cannot all be one interval
    /// apart. The mean over the agreeing set is what recovers the true rate — a
    /// modal interval would report 29.970030 as exactly 90000/3003.
    #[test]
    fn a_non_integer_rate_is_recovered_from_jittered_intervals() {
        let mut probe = FrameRateProbe::new();
        let stamps: Vec<u64> = (0..300)
            .map(|i| 900_000 + (i as f64 * 3003.003).round() as u64)
            .collect();
        let bytes: Vec<u8> = stamps
            .into_iter()
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&bytes);
        let fps = probe.frame_rate().expect("300 stamps is plenty");
        assert!((fps - 29.97).abs() < 0.01, "got {fps}");
    }

    /// PES packets arrive in decode order, so the stamps must be sorted before
    /// they are differenced. Without that, a reordered stream yields intervals
    /// that describe the reordering rather than the frame rate.
    #[test]
    fn decode_order_does_not_change_the_answer() {
        let mut ordered = FrameRateProbe::new();
        ordered.feed(&stream(0x100, 300, 3000, false));
        let mut reordered = FrameRateProbe::new();
        reordered.feed(&stream(0x100, 300, 3000, true));
        assert_eq!(ordered.frame_rate(), reordered.frame_rate());
    }

    /// A splice or a 33-bit counter wrap puts one enormous interval in the set.
    /// It is an artefact of the discontinuity, not a frame duration, and the
    /// answer must not move because of it.
    #[test]
    fn one_discontinuity_does_not_move_the_answer() {
        let mut probe = FrameRateProbe::new();
        probe.feed(&stream(0x100, 200, 3000, false));
        // Resume far away, as a wrap or a re-based splice would.
        let jumped: Vec<u8> = (0..200)
            .map(|i| 7_000_000_000 + i * 3000)
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&jumped);
        let fps = probe.frame_rate().expect("400 stamps is plenty");
        assert!((fps - 30.0).abs() < 0.01, "got {fps}");
    }

    /// Variable-rate sources get no answer. The intervals genuinely disagree, so
    /// any single number would describe none of the stream.
    #[test]
    fn a_variable_rate_stream_reports_nothing() {
        let mut probe = FrameRateProbe::new();
        let stamps: Vec<u64> = (0..300)
            .map(|i: u64| 900_000 + i * 3000 + (i % 7) * 1500)
            .collect();
        let bytes: Vec<u8> = stamps
            .into_iter()
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&bytes);
        assert_eq!(probe.frame_rate(), None);
    }

    /// TWO RATES IN ONE SAMPLE GET NO ANSWER, even when they are close.
    ///
    /// 30 fps and 25 fps are 3000 and 3600 ticks apart — 20%, which the old
    /// +/-25% trim admitted BOTH of. Every interval then counted as agreeing,
    /// the agreement gate never fired, and the reported mean sat between the two
    /// describing neither half of the stream.
    #[test]
    fn a_rate_change_inside_the_sample_reports_nothing() {
        let mut stamps = Vec::new();
        let mut t = 900_000_u64;
        for _ in 0..200 {
            stamps.push(t);
            t += 3000; // 30 fps
        }
        for _ in 0..200 {
            stamps.push(t);
            t += 3600; // 25 fps
        }
        let bytes: Vec<u8> = stamps
            .into_iter()
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        let mut probe = FrameRateProbe::new();
        probe.feed(&bytes);
        assert_eq!(probe.frame_rate(), None);
    }

    /// A rate that changes AFTER the leading sample filled is still caught.
    ///
    /// The lead stops accepting stamps at `MAX_SAMPLES`, so on its own it would
    /// answer for the whole event from its first few minutes and never notice.
    /// The trailing ring is what sees the change, and the disagreement retracts
    /// the answer instead of reporting a rate the stream no longer runs at.
    #[test]
    fn a_rate_change_after_the_lead_filled_reports_nothing() {
        let mut probe = FrameRateProbe::new();
        let mut t = 900_000_u64;
        // Fill the lead and then some, all at 30 fps.
        let head: Vec<u8> = (0..MAX_SAMPLES + 500)
            .map(|_| {
                let pts = t;
                t += 3000;
                pts
            })
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&head);
        let before = probe.frame_rate().expect("a full lead answers");
        assert!((before - 30.0).abs() < 0.01, "got {before}");

        // The stream switches to 25 fps for long enough to fill the tail.
        let after: Vec<u8> = (0..TAIL_SAMPLES + 100)
            .map(|_| {
                let pts = t;
                t += 3600;
                pts
            })
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&after);
        assert_eq!(
            probe.frame_rate(),
            None,
            "the trailing sample must retract an answer the lead can no longer support"
        );
    }

    /// A long CONSTANT stream still answers. The lead/tail comparison must not
    /// punish an event merely for outlasting the leading sample.
    #[test]
    fn a_long_constant_stream_still_answers() {
        let mut probe = FrameRateProbe::new();
        let mut t = 900_000_u64;
        let bytes: Vec<u8> = (0..MAX_SAMPLES + TAIL_SAMPLES + 1000)
            .map(|_| {
                let pts = t;
                t += 3003;
                pts
            })
            .flat_map(|pts| video_pes_packet(0x100, pts))
            .collect();
        probe.feed(&bytes);
        let fps = probe.frame_rate().expect("a constant stream answers at any length");
        assert!((fps - 29.97).abs() < 0.01, "got {fps}");
    }

    /// Too few frames to stand behind an answer.
    #[test]
    fn a_short_sample_reports_nothing() {
        let mut probe = FrameRateProbe::new();
        probe.feed(&stream(0x100, 10, 3000, false));
        assert_eq!(probe.frame_rate(), None);
    }

    /// Audio PES carries its own stamps at its own cadence. Counting them would
    /// report the audio frame rate as the video one.
    #[test]
    fn audio_stamps_are_not_counted() {
        let mut probe = FrameRateProbe::new();
        let mut bytes = stream(0x100, 300, 3000, false);
        let audio: Vec<u8> = (0..300)
            .map(|i| 900_000 + i * 4000)
            .flat_map(|pts| {
                let mut p = video_pes_packet(0x101, pts);
                p[7] = 0xC0; // stream_id: audio
                p
            })
            .collect();
        bytes.extend(audio);
        let fps = probe.frame_rate_after(&bytes);
        assert!((fps.expect("video stamps stand alone") - 30.0).abs() < 0.01);
    }

    /// A second video program on another PID must not interleave into the
    /// answer: two unrelated cadences merged would describe neither.
    #[test]
    fn a_second_video_pid_is_ignored() {
        let mut probe = FrameRateProbe::new();
        let mut bytes = stream(0x100, 300, 3000, false);
        bytes.extend(stream(0x200, 300, 1800, false));
        probe.feed(&bytes);
        let fps = probe.frame_rate().expect("the first video PID wins");
        assert!((fps - 30.0).abs() < 0.01, "got {fps}");
    }

    /// Nothing to read is not an error, and not a zero.
    #[test]
    fn non_ts_input_reports_nothing() {
        let mut probe = FrameRateProbe::new();
        probe.feed(b"not a transport stream at all");
        probe.feed(&[]);
        assert_eq!(probe.frame_rate(), None);
    }

    impl FrameRateProbe {
        /// Feed-then-read, for tests that build their bytes in one go.
        fn frame_rate_after(&mut self, ts: &[u8]) -> Option<f64> {
            self.feed(ts);
            self.frame_rate()
        }
    }
}
