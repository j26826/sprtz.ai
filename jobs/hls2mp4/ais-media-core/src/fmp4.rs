//! Minimal fMP4/CMAF box surgery, all stream-copy (never re-encodes samples):
//!
//! 1. **Zero-basing** — rewrite each `tfdt`'s `baseMediaDecodeTime` so an output
//!    file's timeline starts at zero. Byte-concat otherwise preserves the
//!    *source* stream's absolute decode times (a live edge is billions of ticks
//!    in), leaving a huge `start_time` and a nonsensical container duration.
//! 2. **Muxing** — CMAF keeps audio and video in *separate* renditions, so the
//!    recorder combines them into one file: [`build_combined_init`] merges the
//!    two `moov`s (giving the audio track a distinct `track_ID`), and each audio
//!    fragment's `tfhd` `track_ID` is remapped to match.
//!
//! Only fixed-size fields are touched (`tfdt`, `tfhd` `track_ID`, `mfhd`
//! sequence, `sidx` `earliest_presentation_time`) — box sizes never change in
//! place, so every `trun` `data_offset` into `mdat` stays valid. Any `sidx` in
//! a pass-through segment is rebased alongside its track's `tfdt` (a stale
//! index would break seeking); the merge path drops `sidx` instead, since a
//! per-rendition index cannot describe the merged layout. Decode-time bases
//! are tracked **per track** (muxed audio and video have independent
//! timescales) and persist for the life of one output file.

use std::collections::HashMap;

use anyhow::{Context, Result};

/// Rewrites one CMAF media segment for output: zero-bases each `tfdt` (relative
/// to the first fragment seen per track) and renumbers each `moof`'s `mfhd`
/// `sequence_number` from `frag_seq` (advanced per moof) so numbers stay
/// globally monotonic across interleaved video+audio moofs — some players drop
/// a moof whose sequence duplicates an earlier one. Video path (no `track_ID`
/// change).
pub fn zero_base_segment(
    seg: &[u8],
    bases: &mut HashMap<u32, u64>,
    frag_seq: &mut u32,
) -> Vec<u8> {
    process(seg, bases, None, frag_seq)
}

/// Like [`zero_base_segment`], but also forces every fragment's `tfhd`
/// `track_ID` to `track_id` — used for the audio rendition so its fragments
/// reference the audio track in the combined init.
pub fn zero_base_segment_as(
    seg: &[u8],
    bases: &mut HashMap<u32, u64>,
    track_id: u32,
    frag_seq: &mut u32,
) -> Vec<u8> {
    process(seg, bases, Some(track_id), frag_seq)
}

/// The `track_ID` of the first `trak` in an init segment (`moov/trak/tkhd`).
pub fn track_id(init: &[u8]) -> Option<u32> {
    let (_, moov) = child(init, b"moov")?;
    let (_, trak) = child(moov, b"trak")?;
    let (_, tkhd) = child(trak, b"tkhd")?;
    read_tkhd_track_id(tkhd)
}

/// Builds one init segment carrying **both** the video track (unchanged) and the
/// audio track (its `track_ID` remapped to `audio_tid`), so a single file can
/// interleave both renditions' fragments. Output: `ftyp` + a merged `moov`
/// (`mvhd` + video `trak` + audio `trak` + `mvex` with both `trex`).
pub fn build_combined_init(video_init: &[u8], audio_init: &[u8], audio_tid: u32) -> Result<Vec<u8>> {
    let (ftyp_full, _) = child(video_init, b"ftyp").context("video init: no ftyp")?;
    let (_, v_moov) = child(video_init, b"moov").context("video init: no moov")?;
    let (_, a_moov) = child(audio_init, b"moov").context("audio init: no moov")?;

    let (mvhd_full, _) = child(v_moov, b"mvhd").context("video moov: no mvhd")?;
    let (v_trak_full, _) = child(v_moov, b"trak").context("video moov: no trak")?;
    let (_, v_mvex) = child(v_moov, b"mvex").context("video moov: no mvex")?;
    let (a_trak_full, _) = child(a_moov, b"trak").context("audio moov: no trak")?;
    let (_, a_mvex) = child(a_moov, b"mvex").context("audio moov: no mvex")?;
    let (a_trex_full, _) = child(a_mvex, b"trex").context("audio mvex: no trex")?;

    let a_trak = remap_trak_track_id(a_trak_full, audio_tid)?;
    let a_trex = remap_trex_track_id(a_trex_full, audio_tid);
    let mvhd = set_next_track_id(mvhd_full, audio_tid + 1);

    // mvex = existing video children (mehd?/trex) + the audio trex.
    let mut mvex_payload = v_mvex.to_vec();
    mvex_payload.extend_from_slice(&a_trex);
    let mvex = mp4_box(b"mvex", &mvex_payload);

    let mut moov_payload = mvhd;
    moov_payload.extend_from_slice(v_trak_full);
    moov_payload.extend_from_slice(&a_trak);
    moov_payload.extend_from_slice(&mvex);
    let moov = mp4_box(b"moov", &moov_payload);

    let mut out = ftyp_full.to_vec();
    out.extend_from_slice(&moov);
    Ok(out)
}

/// Merges one video CMAF segment and its paired audio segment into a single
/// fragment — `moof{ mfhd, traf(video), traf(audio) }` + one shared `mdat` —
/// the shape general-purpose muxers produce. Interleaving whole 6-second
/// moofs per track (video moof, audio moof, …) starves one decoder for a full
/// fragment duration and stalls strict players (VLC: "buffer deadlock
/// prevented"); a two-traf moof delivers both tracks together.
///
/// Also zero-bases both `tfdt`s, remaps the audio `track_ID` to `audio_tid`,
/// stamps the next `mfhd` sequence number, and drops `styp`/`prft`/`free`
/// clutter. Any `sidx` is dropped too (not rebased): a per-segment index
/// describes one rendition's layout, which the merge invalidates, and players
/// don't need one for a plain file. Requires each segment to be a single
/// `moof`+`mdat` with
/// `default-base-is-moof` `tfhd`s and offset-bearing `trun`s (the CMAF norm) —
/// returns `None` otherwise so the caller can fall back to plain interleaving.
pub fn mux_pair(
    video_seg: &[u8],
    audio_seg: &[u8],
    audio_tid: u32,
    bases: &mut HashMap<u32, u64>,
    frag_seq: &mut u32,
) -> Option<Vec<u8>> {
    let (v_moof, v_mdat) = single_fragment(video_seg)?;
    let (a_moof, a_mdat) = single_fragment(audio_seg)?;

    let (_, v_moof_payload) = split_box(v_moof)?;
    let (_, a_moof_payload) = split_box(a_moof)?;
    let (mfhd_full, _) = child(v_moof_payload, b"mfhd")?;
    let (v_traf_full, _) = child(v_moof_payload, b"traf")?;
    let (a_traf_full, _) = child(a_moof_payload, b"traf")?;

    // Sizes are stable under our patches, so the merged moof size (and with it
    // every trun's data-offset delta) is known up front.
    let new_moof_len = 8 + mfhd_full.len() + v_traf_full.len() + a_traf_full.len();
    let delta_v = new_moof_len as i64 - v_moof.len() as i64;
    let delta_a = new_moof_len as i64 - a_moof.len() as i64 + v_mdat.len() as i64;

    let mut mfhd = mfhd_full.to_vec();
    if mfhd.len() >= 16 {
        mfhd[12..16].copy_from_slice(&frag_seq.to_be_bytes());
        *frag_seq += 1;
    }
    let v_traf = patch_traf(v_traf_full, None, bases, delta_v)?;
    let a_traf = patch_traf(a_traf_full, Some(audio_tid), bases, delta_a)?;

    let mut moof_payload = mfhd;
    moof_payload.extend_from_slice(&v_traf);
    moof_payload.extend_from_slice(&a_traf);
    let moof = mp4_box(b"moof", &moof_payload);
    debug_assert_eq!(moof.len(), new_moof_len);

    let mut mdat_payload = Vec::with_capacity(v_mdat.len() + a_mdat.len());
    mdat_payload.extend_from_slice(v_mdat);
    mdat_payload.extend_from_slice(a_mdat);

    let mut out = moof;
    out.extend_from_slice(&mp4_box(b"mdat", &mdat_payload));
    Some(out)
}

/// The single `moof` box and `mdat` payload of a CMAF segment, ignoring
/// `styp`/`prft`/`free`/`sidx` clutter. `None` unless there is exactly one of
/// each, moof first, and every `tfhd` uses `default-base-is-moof` with
/// offset-bearing `trun`s (so data offsets stay patchable).
fn single_fragment(seg: &[u8]) -> Option<(&[u8], &[u8])> {
    let mut moof: Option<&[u8]> = None;
    let mut mdat: Option<&[u8]> = None;
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&seg[pos..]) {
        if bl < hl || pos + bl > seg.len() {
            return None;
        }
        match &t {
            b"moof" => {
                if moof.is_some() {
                    return None;
                }
                moof = Some(&seg[pos..pos + bl]);
            }
            b"mdat" => {
                if mdat.is_some() || moof.is_none() {
                    return None;
                }
                mdat = Some(&seg[pos + hl..pos + bl]);
            }
            _ => {} // styp / prft / free / sidx — dropped
        }
        pos += bl;
    }
    let moof = moof?;
    let (_, payload) = split_box(moof)?;
    let (_, traf) = child(payload, b"traf")?;
    let (_, tfhd) = child(traf, b"tfhd")?;
    let flags = u32::from_be_bytes(tfhd.get(..4)?.try_into().ok()?) & 0xFF_FFFF;
    if flags & 0x1 != 0 || flags & 0x2_0000 == 0 {
        return None; // absolute base-data-offset, or not moof-relative
    }
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&traf[pos..]) {
        if bl < hl || pos + bl > traf.len() {
            return None;
        }
        if &t == b"trun" {
            let fl =
                u32::from_be_bytes(traf.get(pos + hl..pos + hl + 4)?.try_into().ok()?) & 0xFF_FFFF;
            if fl & 0x1 == 0 {
                return None; // no data_offset to patch
            }
        }
        pos += bl;
    }
    Some((moof, mdat?))
}

/// Copies a `traf`, optionally forcing its `tfhd` `track_ID`, zero-basing its
/// `tfdt`, and shifting every `trun` `data_offset` by `delta`.
fn patch_traf(
    traf_full: &[u8],
    force_tid: Option<u32>,
    bases: &mut HashMap<u32, u64>,
    delta: i64,
) -> Option<Vec<u8>> {
    let mut out = traf_full.to_vec();
    let (hl, _, _) = read_box_header(&out)?;
    let mut pos = hl;
    let mut track_id = 0u32;
    while let Some((bhl, bbl, t)) = read_box_header(&out[pos..]) {
        if bbl < bhl || pos + bbl > out.len() {
            return None;
        }
        let payload = &mut out[pos + bhl..pos + bbl];
        match &t {
            b"tfhd" if payload.len() >= 8 => {
                if let Some(tid) = force_tid {
                    payload[4..8].copy_from_slice(&tid.to_be_bytes());
                }
                track_id = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
            }
            b"tfdt" => rebase_tfdt(payload, track_id, bases),
            // version(1) flags(3) sample_count(4) data_offset(4, signed).
            b"trun" if payload.len() >= 12 => {
                let old = i32::from_be_bytes(payload[8..12].try_into().unwrap());
                let new = i32::try_from(old as i64 + delta).ok()?;
                payload[8..12].copy_from_slice(&new.to_be_bytes());
            }
            _ => {}
        }
        pos += bbl;
    }
    Some(out)
}

/// Splits a box into `(header_len, payload)`.
fn split_box(full: &[u8]) -> Option<(usize, &[u8])> {
    let (hl, bl, _) = read_box_header(full)?;
    full.get(hl..bl).map(|p| (hl, p))
}

/// A random-access point for the `mfra` trailer: the (zero-based) decode time
/// of a fragment's first video sample and the absolute file offset of its
/// `moof`.
#[derive(Debug, Clone, Copy)]
pub struct FragmentAccess {
    pub time: u64,
    pub moof_offset: u64,
}

/// Locates the first `moof` in an already-processed output payload and returns
/// `(offset_within_payload, first_tfdt_decode_time)`. Used to record a
/// [`FragmentAccess`] entry as each fragment is written.
pub fn first_moof(payload: &[u8]) -> Option<(usize, u64)> {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&payload[pos..]) {
        if bl < hl || pos + bl > payload.len() {
            return None;
        }
        if &t == b"moof" {
            let moof = &payload[pos + hl..pos + bl];
            let (_, traf) = child(moof, b"traf")?;
            let (_, tfdt) = child(traf, b"tfdt")?;
            let time = match tfdt.first() {
                Some(1) if tfdt.len() >= 12 => {
                    u64::from_be_bytes(tfdt[4..12].try_into().unwrap())
                }
                Some(0) if tfdt.len() >= 8 => {
                    u64::from(u32::from_be_bytes(tfdt[4..8].try_into().unwrap()))
                }
                _ => return None,
            };
            return Some((pos, time));
        }
        pos += bl;
    }
    None
}

/// Builds an `mfra` trailer (one `tfra` for `track_id` + the closing `mfro`)
/// from the fragments' access points. Appended at the end of a finished file,
/// it lets players seek straight to a fragment boundary — an IDR frame for
/// HLS/CMAF content — instead of guessing a byte offset and decoding mid-GOP
/// (visible as pixelation until the next keyframe).
pub fn build_mfra(track_id: u32, entries: &[FragmentAccess]) -> Vec<u8> {
    // tfra, version 1: 64-bit times/offsets; traf/trun/sample numbers stored
    // in 1 byte each (length fields 0) and always 1 — the RAP is the first
    // sample of the first trun of the first traf.
    let mut tfra = vec![1u8, 0, 0, 0]; // version 1, flags 0
    tfra.extend_from_slice(&track_id.to_be_bytes());
    tfra.extend_from_slice(&0u32.to_be_bytes()); // length sizes: 1 byte each
    tfra.extend_from_slice(&(entries.len() as u32).to_be_bytes());
    for e in entries {
        tfra.extend_from_slice(&e.time.to_be_bytes());
        tfra.extend_from_slice(&e.moof_offset.to_be_bytes());
        tfra.extend_from_slice(&[1u8, 1, 1]); // traf 1, trun 1, sample 1
    }
    let tfra = mp4_box(b"tfra", &tfra);

    // mfra = tfra + mfro; mfro carries the total mfra box size so a reader can
    // find the trailer by reading the last 16 bytes of the file.
    let mfro_payload_len = 4 + 4; // version/flags + size
    let mfra_total = 8 + tfra.len() + 8 + mfro_payload_len;
    let mut mfro = vec![0u8, 0, 0, 0];
    mfro.extend_from_slice(&(mfra_total as u32).to_be_bytes());
    let mfro = mp4_box(b"mfro", &mfro);

    let mut payload = tfra;
    payload.extend_from_slice(&mfro);
    mp4_box(b"mfra", &payload)
}

// --- fragment rewriting ----------------------------------------------------

fn process(
    seg: &[u8],
    bases: &mut HashMap<u32, u64>,
    force_tid: Option<u32>,
    frag_seq: &mut u32,
) -> Vec<u8> {
    let mut buf = seg.to_vec();
    // A `sidx` precedes its `moof`, so the per-track bases must be known before
    // the rewrite pass reaches it: seed them from the `tfdt`s first.
    seed_bases(&buf, bases, force_tid);
    walk_top(&mut buf, bases, force_tid, frag_seq);
    buf
}

/// Read-only pre-pass: records each track's first `tfdt` decode time into
/// `bases` (keyed by the *output* track ID, i.e. `force_tid` when remapping),
/// without modifying the segment.
fn seed_bases(buf: &[u8], bases: &mut HashMap<u32, u64>, force_tid: Option<u32>) {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&buf[pos..]) {
        if bl < hl || pos + bl > buf.len() {
            break;
        }
        if &t == b"moof" {
            let moof = &buf[pos + hl..pos + bl];
            let mut mpos = 0usize;
            while let Some((thl, tbl, tt)) = read_box_header(&moof[mpos..]) {
                if tbl < thl || mpos + tbl > moof.len() {
                    break;
                }
                if &tt == b"traf" {
                    let traf = &moof[mpos + thl..mpos + tbl];
                    let tid = force_tid.or_else(|| {
                        child(traf, b"tfhd").and_then(|(_, p)| {
                            p.get(4..8).map(|b| u32::from_be_bytes(b.try_into().unwrap()))
                        })
                    });
                    if let (Some(tid), Some((_, tfdt))) = (tid, child(traf, b"tfdt")) {
                        let raw = match tfdt.first() {
                            Some(1) if tfdt.len() >= 12 => {
                                Some(u64::from_be_bytes(tfdt[4..12].try_into().unwrap()))
                            }
                            Some(0) if tfdt.len() >= 8 => Some(u64::from(u32::from_be_bytes(
                                tfdt[4..8].try_into().unwrap(),
                            ))),
                            _ => None,
                        };
                        if let Some(raw) = raw {
                            bases.entry(tid).or_insert(raw);
                        }
                    }
                }
                mpos += tbl;
            }
        }
        pos += bl;
    }
}

/// Top-level boxes: descend only into `moof` (never `mdat`, so its payload is
/// never misread as box headers). Each `moof` gets the next `frag_seq`; each
/// `sidx` has its `earliest_presentation_time` rebased (and its `reference_ID`
/// remapped) to stay consistent with the zero-based `tfdt`s.
fn walk_top(buf: &mut [u8], bases: &mut HashMap<u32, u64>, force_tid: Option<u32>, frag_seq: &mut u32) {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&buf[pos..]) {
        if bl < hl || pos + bl > buf.len() {
            break;
        }
        if &t == b"moof" {
            let moof = &mut buf[pos + hl..pos + bl];
            set_mfhd_sequence(moof, *frag_seq);
            *frag_seq += 1;
            walk_moof(moof, bases, force_tid);
        } else if &t == b"sidx" {
            rebase_sidx(&mut buf[pos + hl..pos + bl], bases, force_tid);
        }
        pos += bl;
    }
}

/// Rebases a `sidx` payload in place: remaps `reference_ID` when `force_tid` is
/// set and subtracts the referenced track's base from
/// `earliest_presentation_time`, mirroring the `tfdt` rewrite. The box's
/// `timescale` field matches the media timescale in CMAF, so the same tick
/// base applies. Layout: version(1) flags(3) reference_ID(4) timescale(4),
/// then EPT + first_offset as u32 (v0) or u64 (v1).
fn rebase_sidx(payload: &mut [u8], bases: &HashMap<u32, u64>, force_tid: Option<u32>) {
    if payload.len() < 16 {
        return;
    }
    if let Some(tid) = force_tid {
        payload[4..8].copy_from_slice(&tid.to_be_bytes());
    }
    let ref_id = u32::from_be_bytes(payload[4..8].try_into().unwrap());
    let Some(&base) = bases.get(&ref_id) else {
        return; // no fragment seen for this track — nothing to rebase against
    };
    match payload[0] {
        0 => {
            let ept = u64::from(u32::from_be_bytes(payload[12..16].try_into().unwrap()));
            let new = ept.saturating_sub(base) as u32;
            payload[12..16].copy_from_slice(&new.to_be_bytes());
        }
        1 if payload.len() >= 20 => {
            let ept = u64::from_be_bytes(payload[12..20].try_into().unwrap());
            payload[12..20].copy_from_slice(&ept.saturating_sub(base).to_be_bytes());
        }
        _ => {}
    }
}

/// Sets a `moof`'s `mfhd` `sequence_number` (payload: version/flags(4), then
/// `sequence_number(4)`).
fn set_mfhd_sequence(moof: &mut [u8], seq: u32) {
    if let Some((s, hl, _)) = child_range(moof, b"mfhd") {
        let off = s + hl + 4;
        if moof.len() >= off + 4 {
            moof[off..off + 4].copy_from_slice(&seq.to_be_bytes());
        }
    }
}

fn walk_moof(moof: &mut [u8], bases: &mut HashMap<u32, u64>, force_tid: Option<u32>) {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&moof[pos..]) {
        if bl < hl || pos + bl > moof.len() {
            break;
        }
        if &t == b"traf" {
            walk_traf(&mut moof[pos + hl..pos + bl], bases, force_tid);
        }
        pos += bl;
    }
}

/// `traf` children in order: resolve the track (`tfhd`, optionally remapped),
/// then rebase its `tfdt`.
fn walk_traf(traf: &mut [u8], bases: &mut HashMap<u32, u64>, force_tid: Option<u32>) {
    let mut pos = 0usize;
    let mut track_id = 0u32;
    while let Some((hl, bl, t)) = read_box_header(&traf[pos..]) {
        if bl < hl || pos + bl > traf.len() {
            break;
        }
        let payload = &mut traf[pos + hl..pos + bl];
        if &t == b"tfhd" {
            // version(1) flags(3) track_ID(4) ...
            if payload.len() >= 8 {
                if let Some(tid) = force_tid {
                    payload[4..8].copy_from_slice(&tid.to_be_bytes());
                }
                track_id = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
            }
        } else if &t == b"tfdt" {
            rebase_tfdt(payload, track_id, bases);
        }
        pos += bl;
    }
}

/// Subtracts the track's first decode time from a `tfdt` payload
/// (`version(1) flags(3) baseMediaDecodeTime(4|8)`), in place.
fn rebase_tfdt(payload: &mut [u8], track_id: u32, bases: &mut HashMap<u32, u64>) {
    match payload.first() {
        Some(1) if payload.len() >= 12 => {
            let cur = u64::from_be_bytes(payload[4..12].try_into().unwrap());
            let base = *bases.entry(track_id).or_insert(cur);
            payload[4..12].copy_from_slice(&cur.saturating_sub(base).to_be_bytes());
        }
        Some(0) if payload.len() >= 8 => {
            let cur = u32::from_be_bytes(payload[4..8].try_into().unwrap()) as u64;
            let base = *bases.entry(track_id).or_insert(cur);
            let new = cur.saturating_sub(base) as u32;
            payload[4..8].copy_from_slice(&new.to_be_bytes());
        }
        _ => {}
    }
}

// --- init rewriting --------------------------------------------------------

fn read_tkhd_track_id(tkhd_payload: &[u8]) -> Option<u32> {
    let ver = *tkhd_payload.first()?;
    let off = if ver == 1 { 20 } else { 12 };
    tkhd_payload
        .get(off..off + 4)
        .map(|b| u32::from_be_bytes(b.try_into().unwrap()))
}

/// Copies a `trak` box and sets its `tkhd` `track_ID` (fixed-size field, so box
/// sizes are unchanged).
fn remap_trak_track_id(trak_full: &[u8], tid: u32) -> Result<Vec<u8>> {
    let mut out = trak_full.to_vec();
    let (trak_hl, _, _) = read_box_header(&out).context("bad trak header")?;
    let (tkhd_start, tkhd_hl, _) =
        child_range(&out[trak_hl..], b"tkhd").context("trak: no tkhd")?;
    let payload_abs = trak_hl + tkhd_start + tkhd_hl;
    let ver = out[payload_abs];
    let tid_off = payload_abs + if ver == 1 { 20 } else { 12 };
    out[tid_off..tid_off + 4].copy_from_slice(&tid.to_be_bytes());
    Ok(out)
}

/// Copies a `trex` box and sets its `track_ID` (payload: version/flags(4), then
/// `track_ID(4)`).
fn remap_trex_track_id(trex_full: &[u8], tid: u32) -> Vec<u8> {
    let mut out = trex_full.to_vec();
    if let Some((hl, _, _)) = read_box_header(&out) {
        if out.len() >= hl + 8 {
            out[hl + 4..hl + 8].copy_from_slice(&tid.to_be_bytes());
        }
    }
    out
}

/// Copies an `mvhd` box and sets its trailing `next_track_ID` (last 4 bytes).
fn set_next_track_id(mvhd_full: &[u8], next: u32) -> Vec<u8> {
    let mut out = mvhd_full.to_vec();
    let n = out.len();
    if n >= 4 {
        out[n - 4..].copy_from_slice(&next.to_be_bytes());
    }
    out
}

// --- box primitives --------------------------------------------------------

/// `[size:u32][type][payload]`.
fn mp4_box(btype: &[u8; 4], payload: &[u8]) -> Vec<u8> {
    let size = (8 + payload.len()) as u32;
    let mut b = size.to_be_bytes().to_vec();
    b.extend_from_slice(btype);
    b.extend_from_slice(payload);
    b
}

/// Parses the box header at the start of `buf`: `(header_len, total_len, type)`.
/// Handles 64-bit `largesize` (`size == 1`) and box-to-end (`size == 0`).
fn read_box_header(buf: &[u8]) -> Option<(usize, usize, [u8; 4])> {
    if buf.len() < 8 {
        return None;
    }
    let size = u32::from_be_bytes([buf[0], buf[1], buf[2], buf[3]]);
    let btype = [buf[4], buf[5], buf[6], buf[7]];
    match size {
        1 => {
            if buf.len() < 16 {
                return None;
            }
            let large = u64::from_be_bytes(buf[8..16].try_into().unwrap()) as usize;
            Some((16, large, btype))
        }
        0 => Some((8, buf.len(), btype)),
        n => Some((8, n as usize, btype)),
    }
}

/// Range `(box_start, header_len, box_end)` of the first direct child of type
/// `typ` within `container`.
fn child_range(container: &[u8], typ: &[u8; 4]) -> Option<(usize, usize, usize)> {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&container[pos..]) {
        if bl < hl || pos + bl > container.len() {
            break;
        }
        if &t == typ {
            return Some((pos, hl, pos + bl));
        }
        pos += bl;
    }
    None
}

/// The first direct child of type `typ`: `(full_box, payload)`.
fn child<'a>(container: &'a [u8], typ: &[u8; 4]) -> Option<(&'a [u8], &'a [u8])> {
    let (s, hl, e) = child_range(container, typ)?;
    Some((&container[s..e], &container[s + hl..e]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tk_box(btype: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        mp4_box(btype, payload)
    }

    /// A `moof` + `mdat` segment: one `traf` (given track_id, tfdt v1 base).
    fn segment(track_id: u32, base_decode: u64) -> Vec<u8> {
        let mut tfdt = vec![1u8, 0, 0, 0];
        tfdt.extend_from_slice(&base_decode.to_be_bytes());
        let mut tfhd = vec![0u8, 0, 0, 0];
        tfhd.extend_from_slice(&track_id.to_be_bytes());
        let mut traf = tk_box(b"tfhd", &tfhd);
        traf.extend_from_slice(&tk_box(b"tfdt", &tfdt));
        let mut moof_payload = tk_box(b"mfhd", &[0, 0, 0, 0, 0, 0, 0, 1]);
        moof_payload.extend_from_slice(&tk_box(b"traf", &traf));
        let mut seg = tk_box(b"moof", &moof_payload);
        // 'tfdt' inside mdat proves mdat is never descended.
        seg.extend_from_slice(&tk_box(b"mdat", b"tfdt\x00\x00\x00\x00\x00\x00\x00\x99"));
        seg
    }

    fn read_seg_tfdt(seg: &[u8]) -> u64 {
        let off = 8 + 16 + 8 + 16 + 8 + 4; // moof+mfhd+traf hdr+tfhd+tfdt hdr+ver/flags
        u64::from_be_bytes(seg[off..off + 8].try_into().unwrap())
    }
    fn read_seg_tfhd_tid(seg: &[u8]) -> u32 {
        let off = 8 + 16 + 8 + 8 + 4; // moof+mfhd+traf hdr+tfhd hdr+ver/flags
        u32::from_be_bytes(seg[off..off + 4].try_into().unwrap())
    }
    fn read_seg_mfhd_seq(seg: &[u8]) -> u32 {
        let off = 8 + 8 + 4; // moof hdr + mfhd hdr + ver/flags
        u32::from_be_bytes(seg[off..off + 4].try_into().unwrap())
    }

    /// A minimal single-track init: `ftyp` + `moov{ mvhd, trak{tkhd}, mvex{trex} }`.
    fn init(handler: &[u8; 4], track_id: u32) -> Vec<u8> {
        // mvhd v0: 100 payload bytes; last 4 = next_track_ID.
        let mut mvhd = vec![0u8; 100];
        mvhd[96..100].copy_from_slice(&2u32.to_be_bytes());
        // tkhd v0: track_ID at payload offset 12.
        let mut tkhd = vec![0u8; 84];
        tkhd[12..16].copy_from_slice(&track_id.to_be_bytes());
        let mut hdlr = vec![0u8; 8];
        hdlr.extend_from_slice(handler);
        let mut trak = tk_box(b"tkhd", &tkhd);
        trak.extend_from_slice(&tk_box(b"hdlr", &hdlr));
        let mut trex = vec![0u8, 0, 0, 0];
        trex.extend_from_slice(&track_id.to_be_bytes());
        trex.extend_from_slice(&[0u8; 16]);
        let mvex = tk_box(b"trex", &trex);
        let mut moov = tk_box(b"mvhd", &mvhd);
        moov.extend_from_slice(&tk_box(b"trak", &trak));
        moov.extend_from_slice(&tk_box(b"mvex", &mvex));
        let mut out = tk_box(b"ftyp", b"isom\x00\x00\x00\x00");
        out.extend_from_slice(&tk_box(b"moov", &moov));
        out
    }

    #[test]
    fn first_fragment_becomes_zero() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        let out = zero_base_segment(&segment(1, 1000), &mut bases, &mut fs);
        assert_eq!(read_seg_tfdt(&out), 0);
        assert_eq!(bases.get(&1), Some(&1000));
    }

    #[test]
    fn later_fragments_are_relative() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        let _ = zero_base_segment(&segment(1, 1000), &mut bases, &mut fs);
        let out = zero_base_segment(&segment(1, 1500), &mut bases, &mut fs);
        assert_eq!(read_seg_tfdt(&out), 500);
    }

    #[test]
    fn tracks_rebased_independently() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        let _ = zero_base_segment(&segment(1, 1000), &mut bases, &mut fs);
        let _ = zero_base_segment(&segment(2, 90_000), &mut bases, &mut fs);
        let out = zero_base_segment(&segment(2, 93_000), &mut bases, &mut fs);
        assert_eq!(read_seg_tfdt(&out), 3_000);
    }

    #[test]
    fn audio_fragment_is_remapped_and_zero_based() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        // Source audio uses track 1; force it to 2 for the combined file.
        let out = zero_base_segment_as(&segment(1, 90_000), &mut bases, 2, &mut fs);
        assert_eq!(read_seg_tfhd_tid(&out), 2);
        assert_eq!(read_seg_tfdt(&out), 0);
        assert_eq!(bases.get(&2), Some(&90_000));
    }

    /// A realistic CMAF segment: `styp` + `prft` clutter, then
    /// `moof{ mfhd, traf{ tfhd(default-base-is-moof), tfdt, trun } }` + `mdat`,
    /// with the trun's `data_offset` correctly targeting the mdat payload.
    fn cmaf_segment(track_id: u32, base_decode: u64, payload: &[u8]) -> Vec<u8> {
        let mut tfhd = vec![0u8, 0x02, 0x00, 0x00]; // flags: default-base-is-moof
        tfhd.extend_from_slice(&track_id.to_be_bytes());
        let mut tfdt = vec![1u8, 0, 0, 0];
        tfdt.extend_from_slice(&base_decode.to_be_bytes());
        let mut trun = vec![0u8, 0x00, 0x02, 0x01]; // flags: data-offset + sample-size
        trun.extend_from_slice(&1u32.to_be_bytes()); // sample_count
        trun.extend_from_slice(&0i32.to_be_bytes()); // data_offset (patched below)
        trun.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        let mut traf = tk_box(b"tfhd", &tfhd);
        traf.extend_from_slice(&tk_box(b"tfdt", &tfdt));
        traf.extend_from_slice(&tk_box(b"trun", &trun));
        let mut moof_payload = tk_box(b"mfhd", &[0, 0, 0, 0, 0, 0, 0, 9]);
        moof_payload.extend_from_slice(&tk_box(b"traf", &traf));
        let mut moof = tk_box(b"moof", &moof_payload);
        // Point the trun's data_offset at the mdat payload (moof-relative).
        let off = (moof.len() + 8) as i32;
        let idx = find_trun_offset_pos(&moof);
        moof[idx..idx + 4].copy_from_slice(&off.to_be_bytes());
        let mut seg = tk_box(b"styp", b"cmfs\x00\x00\x00\x00");
        seg.extend_from_slice(&tk_box(b"prft", &[0u8; 16]));
        seg.extend_from_slice(&sidx(track_id, base_decode)); // dropped by the merge
        seg.extend_from_slice(&moof);
        seg.extend_from_slice(&tk_box(b"mdat", payload));
        seg
    }

    /// Byte position of the (single) trun's `data_offset` inside a moof.
    fn find_trun_offset_pos(moof: &[u8]) -> usize {
        let mut p = 0;
        while p + 8 <= moof.len() {
            if &moof[p + 4..p + 8] == b"trun" {
                return p + 8 + 8; // header + version/flags + sample_count
            }
            p += 1;
        }
        panic!("no trun");
    }

    /// A `sidx` (version 1) referencing `ref_id` with the given
    /// `earliest_presentation_time`.
    fn sidx(ref_id: u32, ept: u64) -> Vec<u8> {
        let mut p = vec![1u8, 0, 0, 0]; // version 1, flags 0
        p.extend_from_slice(&ref_id.to_be_bytes());
        p.extend_from_slice(&48_000u32.to_be_bytes()); // timescale
        p.extend_from_slice(&ept.to_be_bytes());
        p.extend_from_slice(&0u64.to_be_bytes()); // first_offset
        p.extend_from_slice(&[0u8; 4]); // reserved + reference_count(0)
        tk_box(b"sidx", &p)
    }

    fn read_sidx(seg: &[u8]) -> (u32, u64) {
        let (_, p) = child(seg, b"sidx").unwrap();
        (
            u32::from_be_bytes(p[4..8].try_into().unwrap()),
            u64::from_be_bytes(p[12..20].try_into().unwrap()),
        )
    }

    /// `sidx` + the plain test segment, mirroring a DASH-style layout where the
    /// index precedes the fragment it describes.
    fn segment_with_sidx(track_id: u32, base_decode: u64) -> Vec<u8> {
        let mut seg = sidx(track_id, base_decode);
        seg.extend_from_slice(&segment(track_id, base_decode));
        seg
    }

    #[test]
    fn sidx_ept_is_rebased_with_the_track() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        // First segment: sidx precedes the moof, yet both zero out.
        let out = zero_base_segment(&segment_with_sidx(1, 90_000), &mut bases, &mut fs);
        assert_eq!(read_sidx(&out), (1, 0));
        assert_eq!(read_seg_tfdt(&out[40..]), 0); // moof follows the 40-byte sidx
        // Later segment: EPT relative to the same base.
        let out = zero_base_segment(&segment_with_sidx(1, 96_400), &mut bases, &mut fs);
        assert_eq!(read_sidx(&out), (1, 6_400));
    }

    #[test]
    fn sidx_reference_id_is_remapped_for_audio() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        let out = zero_base_segment_as(&segment_with_sidx(1, 90_000), &mut bases, 2, &mut fs);
        assert_eq!(read_sidx(&out), (2, 0));
    }

    #[test]
    fn first_moof_skips_clutter_and_reads_time() {
        let mut bases = HashMap::new();
        let mut fs = 1;
        // Zero-based second fragment: sidx clutter precedes the moof.
        let _ = zero_base_segment(&segment_with_sidx(1, 90_000), &mut bases, &mut fs);
        let out = zero_base_segment(&segment_with_sidx(1, 96_400), &mut bases, &mut fs);
        let (off, time) = first_moof(&out).unwrap();
        assert_eq!(off, 40); // after the 40-byte sidx
        assert_eq!(time, 6_400);
        assert_eq!(&out[off + 4..off + 8], b"moof");
    }

    #[test]
    fn mfra_trailer_round_trips() {
        let entries = [
            FragmentAccess { time: 0, moof_offset: 1153 },
            FragmentAccess { time: 192_512, moof_offset: 1_270_000 },
        ];
        let mfra = build_mfra(1, &entries);
        let (_, payload) = split_box(&mfra).unwrap();
        assert_eq!(&mfra[4..8], b"mfra");

        let (tfra_full, tfra) = child(payload, b"tfra").unwrap();
        assert_eq!(tfra[0], 1, "version 1");
        assert_eq!(u32::from_be_bytes(tfra[4..8].try_into().unwrap()), 1); // track
        assert_eq!(u32::from_be_bytes(tfra[12..16].try_into().unwrap()), 2); // entries
        // First entry: time 0, offset 1153, traf/trun/sample = 1.
        assert_eq!(u64::from_be_bytes(tfra[16..24].try_into().unwrap()), 0);
        assert_eq!(u64::from_be_bytes(tfra[24..32].try_into().unwrap()), 1153);
        assert_eq!(&tfra[32..35], &[1, 1, 1]);

        // mfro carries the full trailer size (last 16 bytes of the file).
        let (mfro_full, mfro) = child(payload, b"mfro").unwrap();
        assert_eq!(mfro_full.len(), 16);
        assert_eq!(
            u32::from_be_bytes(mfro[4..8].try_into().unwrap()) as usize,
            mfra.len()
        );
        let _ = tfra_full;
    }

    #[test]
    fn mux_pair_produces_single_two_traf_fragment() {
        let v = cmaf_segment(1, 6000, b"VIDEODATA");
        let a = cmaf_segment(1, 90_000, b"AUDIO");
        let mut bases = HashMap::new();
        let mut fs = 1;
        let out = mux_pair(&v, &a, 2, &mut bases, &mut fs).expect("mergeable");

        // Top level: exactly moof + mdat (clutter dropped).
        let tops: Vec<[u8; 4]> = {
            let mut v = Vec::new();
            let mut p = 0;
            while let Some((_, bl, t)) = read_box_header(&out[p..]) {
                v.push(t);
                p += bl;
            }
            v
        };
        assert_eq!(tops, vec![*b"moof", *b"mdat"]);

        let (moof_full, _) = child(&out, b"moof").unwrap();
        let (_, moof) = split_box(moof_full).unwrap();
        // mfhd renumbered from the shared counter.
        let (_, mfhd) = child(moof, b"mfhd").unwrap();
        assert_eq!(u32::from_be_bytes(mfhd[4..8].try_into().unwrap()), 1);
        assert_eq!(fs, 2);

        // Two trafs: video tid 1 then audio tid 2, both tfdt zero-based.
        let mut trafs = Vec::new();
        let mut p = 0;
        while let Some((hl, bl, t)) = read_box_header(&moof[p..]) {
            if &t == b"traf" {
                trafs.push(&moof[p + hl..p + bl]);
            }
            p += bl;
        }
        assert_eq!(trafs.len(), 2);
        for (traf, want_tid) in trafs.iter().zip([1u32, 2u32]) {
            let (_, tfhd) = child(traf, b"tfhd").unwrap();
            assert_eq!(u32::from_be_bytes(tfhd[4..8].try_into().unwrap()), want_tid);
            let (_, tfdt) = child(traf, b"tfdt").unwrap();
            assert_eq!(u64::from_be_bytes(tfdt[4..12].try_into().unwrap()), 0);
        }

        // trun offsets land exactly on each track's slice of the shared mdat.
        let mdat_payload_start = moof_full.len() + 8;
        let (_, v_trun) = child(trafs[0], b"trun").unwrap();
        let v_off = i32::from_be_bytes(v_trun[8..12].try_into().unwrap()) as usize;
        assert_eq!(v_off, mdat_payload_start);
        assert_eq!(&out[v_off..v_off + 9], b"VIDEODATA");
        let (_, a_trun) = child(trafs[1], b"trun").unwrap();
        let a_off = i32::from_be_bytes(a_trun[8..12].try_into().unwrap()) as usize;
        assert_eq!(a_off, mdat_payload_start + 9);
        assert_eq!(&out[a_off..a_off + 5], b"AUDIO");
    }

    #[test]
    fn interleaved_moofs_get_monotonic_sequence_numbers() {
        // Mirror the recorder: one shared counter across paired video+audio
        // moofs (both source moofs carry sequence_number 1 by construction).
        let mut bases = HashMap::new();
        let mut fs = 1;
        let v0 = zero_base_segment(&segment(1, 0), &mut bases, &mut fs);
        let a0 = zero_base_segment_as(&segment(1, 0), &mut bases, 2, &mut fs);
        let v1 = zero_base_segment(&segment(1, 6000), &mut bases, &mut fs);
        let a1 = zero_base_segment_as(&segment(1, 6000), &mut bases, 2, &mut fs);
        assert_eq!(
            [
                read_seg_mfhd_seq(&v0),
                read_seg_mfhd_seq(&a0),
                read_seg_mfhd_seq(&v1),
                read_seg_mfhd_seq(&a1),
            ],
            [1, 2, 3, 4]
        );
    }

    #[test]
    fn combined_init_has_both_tracks() {
        let video = init(b"vide", 1);
        let audio = init(b"soun", 1);
        assert_eq!(track_id(&video), Some(1));
        let combined = build_combined_init(&video, &audio, 2).unwrap();

        let (_, moov) = child(&combined, b"moov").unwrap();
        // Two traks: video keeps id 1, audio becomes id 2.
        let mut pos = 0;
        let mut traks = Vec::new();
        while let Some((hl, bl, t)) = read_box_header(&moov[pos..]) {
            if &t == b"trak" {
                let (_, tkhd) = child(&moov[pos + hl..pos + bl], b"tkhd").unwrap();
                traks.push(read_tkhd_track_id(tkhd).unwrap());
            }
            pos += bl;
        }
        assert_eq!(traks, vec![1, 2]);

        // mvex now carries two trex boxes.
        let (_, mvex) = child(moov, b"mvex").unwrap();
        let trex_count = {
            let mut pos = 0;
            let mut n = 0;
            while let Some((_, bl, t)) = read_box_header(&mvex[pos..]) {
                if &t == b"trex" {
                    n += 1;
                }
                pos += bl;
            }
            n
        };
        assert_eq!(trex_count, 2);
    }
}
