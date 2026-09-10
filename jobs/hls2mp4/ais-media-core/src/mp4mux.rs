//! Streaming CMAF → **progressive MP4** remux (stream-copy, never re-encodes).
//!
//! Fragmented MP4 is a delivery format; players seek it poorly (VLC in
//! particular guesses byte offsets and lands mid-GOP — pixelation and A/V
//! drift after a seek). A finished VOD asset should be a *progressive* MP4
//! with real sample tables, which every player seeks frame-accurately.
//!
//! [`ProgressiveMp4`] consumes CMAF segments (video, plus an optional paired
//! audio rendition) and emits, in file order and without ever seeking back:
//!
//! 1. `ftyp` — up front,
//! 2. one `mdat` per pushed segment (size known per segment, so no
//!    placeholder patching is needed), containing the video samples then the
//!    audio samples as contiguous chunks,
//! 3. a final `moov` with full sample tables (`stts`, `ctts`, `stsc`, `stsz`,
//!    `co64`, `stss`) built from the fragments' `trun`s — written by
//!    [`ProgressiveMp4::finish`].
//!
//! Sample *data* is copied verbatim; only container metadata is rebuilt, so
//! the zero-transcode policy holds. Because sample tables store durations
//! rather than absolute timestamps, segments dropped upstream (`--remove-ads`)
//! splice together gaplessly.

use anyhow::{anyhow, Context, Result};

// ---------------------------------------------------------------------------
// box primitives (kept private and independent from fmp4.rs internals)
// ---------------------------------------------------------------------------

fn mp4_box(btype: &[u8; 4], payload: &[u8]) -> Vec<u8> {
    let mut b = ((8 + payload.len()) as u32).to_be_bytes().to_vec();
    b.extend_from_slice(btype);
    b.extend_from_slice(payload);
    b
}

/// `(header_len, total_len, type)` of the box at the start of `buf`.
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

/// First direct child of `typ`: `(full_box, payload)`.
fn child<'a>(container: &'a [u8], typ: &[u8; 4]) -> Option<(&'a [u8], &'a [u8])> {
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&container[pos..]) {
        if bl < hl || pos + bl > container.len() {
            return None;
        }
        if &t == typ {
            return Some((&container[pos..pos + bl], &container[pos + hl..pos + bl]));
        }
        pos += bl;
    }
    None
}

fn be_u32(b: &[u8]) -> u32 {
    u32::from_be_bytes(b[..4].try_into().unwrap())
}

// ---------------------------------------------------------------------------
// init-segment context
// ---------------------------------------------------------------------------

/// What the builder needs from one rendition's init segment.
struct TrackInit {
    /// `tkhd` payload (fullbox payload, duration patched at finish).
    tkhd: Vec<u8>,
    /// `mdhd` payload (duration patched at finish).
    mdhd: Vec<u8>,
    /// `hdlr` full box, copied verbatim.
    hdlr: Vec<u8>,
    /// Media header (`vmhd`/`smhd`) + `dinf`, copied verbatim in order.
    minf_headers: Vec<u8>,
    /// `stsd` full box, copied verbatim (codec configuration).
    stsd: Vec<u8>,
    /// Media timescale from `mdhd`.
    timescale: u32,
    /// `trex` defaults: (sample_duration, sample_size, sample_flags).
    trex: (u32, u32, u32),
}

fn parse_init(init: &[u8]) -> Result<TrackInit> {
    let (_, moov) = child(init, b"moov").context("init: no moov")?;
    let (_, trak) = child(moov, b"trak").context("init: no trak")?;
    let (_, tkhd) = child(trak, b"tkhd").context("init: no tkhd")?;
    let (_, mdia) = child(trak, b"mdia").context("init: no mdia")?;
    let (_, mdhd) = child(mdia, b"mdhd").context("init: no mdhd")?;
    let (hdlr_full, _) = child(mdia, b"hdlr").context("init: no hdlr")?;
    let (_, minf) = child(mdia, b"minf").context("init: no minf")?;
    let (_, stbl) = child(minf, b"stbl").context("init: no stbl")?;
    let (stsd_full, _) = child(stbl, b"stsd").context("init: no stsd")?;

    // vmhd/smhd + dinf, in original order (everything in minf except stbl).
    let mut minf_headers = Vec::new();
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&minf[pos..]) {
        if bl < hl || pos + bl > minf.len() {
            break;
        }
        let _ = hl;
        if &t != b"stbl" {
            minf_headers.extend_from_slice(&minf[pos..pos + bl]);
        }
        pos += bl;
    }

    let timescale = match mdhd.first() {
        Some(1) if mdhd.len() >= 24 => be_u32(&mdhd[20..]),
        Some(0) if mdhd.len() >= 16 => be_u32(&mdhd[12..]),
        _ => return Err(anyhow!("init: bad mdhd")),
    };

    let trex = child(moov, b"mvex")
        .and_then(|(_, mvex)| child(mvex, b"trex"))
        .map(|(_, t)| {
            if t.len() >= 24 {
                (be_u32(&t[12..]), be_u32(&t[16..]), be_u32(&t[20..]))
            } else {
                (0, 0, 0)
            }
        })
        .unwrap_or((0, 0, 0));

    Ok(TrackInit {
        tkhd: tkhd.to_vec(),
        mdhd: mdhd.to_vec(),
        hdlr: hdlr_full.to_vec(),
        minf_headers,
        stsd: stsd_full.to_vec(),
        timescale,
        trex,
    })
}

// ---------------------------------------------------------------------------
// per-track accumulated sample tables
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Tables {
    sizes: Vec<u32>,
    durations: Vec<u32>,
    /// Composition offsets (signed); empty means "no ctts needed".
    ctts: Vec<i64>,
    any_ctts: bool,
    /// 1-based sync sample numbers; `all_sync` suppresses `stss`.
    sync: Vec<u32>,
    all_sync: bool,
    /// One chunk per pushed fragment:
    /// (absolute offset, sample count, 1-based sample-description index).
    chunks: Vec<(u64, u32, u32)>,
    total_duration: u64,
}

struct TrackCtx {
    init: TrackInit,
    track_id: u32,
    /// Sample-description entries (`stsd` children). Entry 1 is the source
    /// stream's; re-encoded boundary pieces register additional entries.
    descs: Vec<Vec<u8>>,
    tables: Tables,
}

/// One fragment's samples, extracted from a CMAF segment.
struct FragmentSamples<'a> {
    data: &'a [u8],
    sizes: Vec<u32>,
    durations: Vec<u32>,
    ctts: Vec<i64>,
    any_ctts: bool,
    /// Per-sample "is sync" flags.
    sync: Vec<bool>,
}

/// Parses the single `moof`+`mdat` fragment of a CMAF segment and returns the
/// contiguous sample run. Requires `default-base-is-moof` (offsets are
/// moof-relative), the CMAF norm.
fn parse_fragment<'a>(seg: &'a [u8], trex: (u32, u32, u32)) -> Result<FragmentSamples<'a>> {
    // Locate moof (with its absolute position in `seg`) and mdat.
    let mut moof_full: Option<(usize, &[u8])> = None;
    let mut pos = 0usize;
    while let Some((hl, bl, t)) = read_box_header(&seg[pos..]) {
        if bl < hl || pos + bl > seg.len() {
            return Err(anyhow!("truncated box"));
        }
        let _ = hl;
        if &t == b"moof" {
            if moof_full.is_some() {
                return Err(anyhow!("multi-moof segments not supported"));
            }
            moof_full = Some((pos, &seg[pos..pos + bl]));
        }
        pos += bl;
    }
    let (moof_pos, moof) = moof_full.context("segment: no moof")?;
    let (mhl, mbl, _) = read_box_header(moof).context("bad moof")?;
    let moof_payload = &moof[mhl..mbl];

    let (_, traf) = child(moof_payload, b"traf").context("moof: no traf")?;
    let (_, tfhd) = child(traf, b"tfhd").context("traf: no tfhd")?;
    let tf_flags = be_u32(tfhd) & 0xFF_FFFF;
    if tf_flags & 0x1 != 0 || tf_flags & 0x2_0000 == 0 {
        return Err(anyhow!("tfhd is not default-base-is-moof"));
    }
    // tfhd optional fields after track_ID, in flag order.
    let mut off = 8usize;
    if tf_flags & 0x2 != 0 {
        off += 4; // sample_description_index
    }
    let d_dur = if tf_flags & 0x8 != 0 {
        let v = be_u32(&tfhd[off..]);
        off += 4;
        v
    } else {
        trex.0
    };
    let d_size = if tf_flags & 0x10 != 0 {
        let v = be_u32(&tfhd[off..]);
        off += 4;
        v
    } else {
        trex.1
    };
    let d_flags = if tf_flags & 0x20 != 0 {
        be_u32(&tfhd[off..])
    } else {
        trex.2
    };

    let mut out = FragmentSamples {
        data: &[],
        sizes: Vec::new(),
        durations: Vec::new(),
        ctts: Vec::new(),
        any_ctts: false,
        sync: Vec::new(),
    };
    let mut data_start: Option<usize> = None;
    let mut expected_next: Option<usize> = None;

    // Iterate every trun in this traf, in order.
    let mut tpos = 0usize;
    while let Some((thl, tbl, tt)) = read_box_header(&traf[tpos..]) {
        if tbl < thl || tpos + tbl > traf.len() {
            return Err(anyhow!("truncated traf"));
        }
        if &tt == b"trun" {
            let trun = &traf[tpos + thl..tpos + tbl];
            let version = trun[0];
            let fl = be_u32(trun) & 0xFF_FFFF;
            let count = be_u32(&trun[4..]) as usize;
            let mut p = 8usize;
            if fl & 0x1 == 0 {
                return Err(anyhow!("trun without data_offset"));
            }
            let data_offset = i32::from_be_bytes(trun[p..p + 4].try_into().unwrap());
            p += 4;
            let abs = moof_pos
                .checked_add_signed(data_offset as isize)
                .context("bad trun data_offset")?;
            match expected_next {
                None => data_start = Some(abs),
                Some(want) if want == abs => {}
                Some(_) => return Err(anyhow!("non-contiguous truns")),
            }
            let first_flags = if fl & 0x4 != 0 {
                let v = be_u32(&trun[p..]);
                p += 4;
                Some(v)
            } else {
                None
            };
            let mut run_bytes = 0usize;
            for i in 0..count {
                let dur = if fl & 0x100 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else {
                    d_dur
                };
                let size = if fl & 0x200 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else {
                    d_size
                };
                let flags = if fl & 0x400 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else if i == 0 {
                    first_flags.unwrap_or(d_flags)
                } else {
                    d_flags
                };
                let ct = if fl & 0x800 != 0 {
                    let raw = &trun[p..p + 4];
                    p += 4;
                    if version == 0 {
                        i64::from(be_u32(raw))
                    } else {
                        i64::from(i32::from_be_bytes(raw.try_into().unwrap()))
                    }
                } else {
                    0
                };
                out.durations.push(dur);
                out.sizes.push(size);
                out.ctts.push(ct);
                if ct != 0 {
                    out.any_ctts = true;
                }
                // sample_is_non_sync_sample == bit 16.
                out.sync.push(flags & 0x0001_0000 == 0);
                run_bytes += size as usize;
            }
            expected_next = Some(match expected_next {
                Some(want) => want + run_bytes,
                None => abs + run_bytes,
            });
        }
        tpos += tbl;
    }

    let start = data_start.context("traf: no trun")?;
    let end = expected_next.unwrap_or(start);
    if end > seg.len() {
        return Err(anyhow!("sample run exceeds segment"));
    }
    out.data = &seg[start..end];
    Ok(out)
}

// ---------------------------------------------------------------------------
// the builder
// ---------------------------------------------------------------------------

/// Streaming progressive-MP4 builder. See the module docs for the layout.
pub struct ProgressiveMp4 {
    tracks: Vec<TrackCtx>,
    /// Absolute file offset of the next byte to be written.
    offset: u64,
}

impl ProgressiveMp4 {
    /// Creates the builder from the video init segment and, when muxing a
    /// separate audio rendition, its init segment. Returns the builder plus
    /// the leading bytes to write (`ftyp`).
    pub fn new(video_init: &[u8], audio_init: Option<&[u8]>) -> Result<(Self, Vec<u8>)> {
        let (ftyp_full, _) = child(video_init, b"ftyp").context("video init: no ftyp")?;
        let mut tracks = Vec::new();
        for (init_bytes, track_id) in std::iter::once((video_init, 1))
            .chain(audio_init.map(|a| (a, 2)))
        {
            let init = parse_init(init_bytes)
                .with_context(|| format!("parsing init of track {track_id}"))?;
            let desc = first_stsd_entry(&init.stsd)
                .with_context(|| format!("track {track_id}: stsd has no sample entry"))?;
            tracks.push(TrackCtx {
                init,
                track_id,
                descs: vec![desc],
                tables: Tables {
                    all_sync: true,
                    ..Default::default()
                },
            });
        }
        let ftyp = ftyp_full.to_vec();
        let offset = ftyp.len() as u64;
        Ok((Self { tracks, offset }, ftyp))
    }

    /// Consumes one video segment (and its paired audio segment when muxing)
    /// and returns the `mdat` box to append to the file.
    pub fn push(&mut self, video_seg: &[u8], audio_seg: Option<&[u8]>) -> Result<Vec<u8>> {
        let segs: Vec<&[u8]> = match (audio_seg, self.tracks.len()) {
            (Some(a), 2) => vec![video_seg, a],
            (None, 1) => vec![video_seg],
            _ => return Err(anyhow!("audio segment presence must match the track layout")),
        };

        let mut payload_len = 0usize;
        let mut parsed = Vec::with_capacity(segs.len());
        for (track, seg) in self.tracks.iter().zip(&segs) {
            let f = parse_fragment(seg, track.init.trex)
                .with_context(|| format!("track {}", track.track_id))?;
            payload_len += f.data.len();
            parsed.push(f);
        }

        // Record chunk offsets (absolute, into this mdat's payload) and tables.
        let mut cursor = self.offset + 8; // after the mdat header
        let mut mdat_payload = Vec::with_capacity(payload_len);
        for (track, f) in self.tracks.iter_mut().zip(parsed) {
            let t = &mut track.tables;
            t.chunks.push((cursor, f.sizes.len() as u32, 1));
            for (i, ((size, dur), sync)) in
                f.sizes.iter().zip(&f.durations).zip(&f.sync).enumerate()
            {
                let _ = i;
                t.sizes.push(*size);
                t.durations.push(*dur);
                t.total_duration += u64::from(*dur);
                let n = t.sizes.len() as u32;
                if *sync {
                    t.sync.push(n);
                } else {
                    t.all_sync = false;
                }
            }
            t.any_ctts |= f.any_ctts;
            t.ctts.extend(f.ctts);
            cursor += f.data.len() as u64;
            mdat_payload.extend_from_slice(f.data);
        }

        let mdat = mp4_box(b"mdat", &mdat_payload);
        self.offset += mdat.len() as u64;
        Ok(mdat)
    }

    /// The video track's media timescale (needed to prepare re-encoded
    /// boundary pieces whose timescale must match).
    pub fn video_timescale(&self) -> u32 {
        self.tracks[0].init.timescale
    }

    /// Frames per second MEASURED from the video samples ingested so far.
    ///
    /// Free: the per-sample durations and the `mdhd` timescale are already held
    /// because `stts` is built from them, so this is arithmetic over numbers
    /// that exist rather than a second parse. It is the CMAF counterpart of
    /// `crate::tsprobe`, which has to walk transport-stream packets to reach the
    /// same fact.
    ///
    /// The MEAN sample duration, not the modal one: for a rate whose period is
    /// not a whole number of ticks no single duration is correct, and only the
    /// average recovers it. That makes this exact for a constant-rate source
    /// and an average for a variable-rate one — the caller decides whether that
    /// is worth publishing (see `Recorder`, which takes the first answer of the
    /// event and keeps it).
    ///
    /// `None` before any sample is ingested, and when nothing ever stated a
    /// duration: `trun` may omit them and the `trex` default may be zero, which
    /// is an absent measurement rather than an infinite frame rate.
    pub fn video_frame_rate(&self) -> Option<f64> {
        let video = self.tracks.first()?;
        let samples = video.tables.durations.len() as u64;
        let ticks = video.tables.total_duration;
        if samples == 0 || ticks == 0 || video.init.timescale == 0 {
            return None;
        }
        Some(f64::from(video.init.timescale) * samples as f64 / ticks as f64)
    }

    /// Ingests a **re-encoded boundary piece** — a self-contained fragmented
    /// MP4 (`ftyp`+`moov` init and `moof`/`mdat` fragments) as produced by
    /// [`crate::splice::reencode_trim_cmaf`] — appending its samples to this
    /// file under an additional sample description (its codec parameters
    /// differ from the source stream's). Returns the `mdat` box to write.
    ///
    /// The piece's video track must share this file's video timescale (the
    /// splice runner forces it); other-timescale tracks (audio) are rescaled.
    pub fn push_encoded(&mut self, fmp4: &[u8]) -> Result<Vec<u8>> {
        let (_, moov) = child(fmp4, b"moov").context("encoded piece: no moov")?;

        let mut theirs: Vec<(u32, PieceTrack)> = Vec::new();
        let mut pos = 0usize;
        while let Some((hl, bl, t)) = read_box_header(&moov[pos..]) {
            if bl < hl || pos + bl > moov.len() {
                break;
            }
            if &t == b"trak" {
                let trak = &moov[pos + hl..pos + bl];
                let (_, tkhd) = child(trak, b"tkhd").context("piece: no tkhd")?;
                let their_id = be_u32(&tkhd[12..]);
                let (_, mdia) = child(trak, b"mdia").context("piece: no mdia")?;
                let (_, hdlr) = child(mdia, b"hdlr").context("piece: no hdlr")?;
                let (_, mdhd) = child(mdia, b"mdhd").context("piece: no mdhd")?;
                let timescale = match mdhd.first() {
                    Some(1) if mdhd.len() >= 24 => be_u32(&mdhd[20..]),
                    _ => be_u32(&mdhd[12..]),
                };
                let ours = match &hdlr[8..12] {
                    b"vide" => 0usize,
                    b"soun" => 1usize,
                    _ => {
                        pos += bl;
                        continue; // e.g. a data track — not ours to keep
                    }
                };
                if ours >= self.tracks.len() {
                    return Err(anyhow!("encoded piece has audio but this file has no audio track"));
                }
                let (_, minf) = child(mdia, b"minf").context("piece: no minf")?;
                let (_, stbl) = child(minf, b"stbl").context("piece: no stbl")?;
                let (stsd_full, _) = child(stbl, b"stsd").context("piece: no stsd")?;
                let entry = first_stsd_entry(stsd_full).context("piece: empty stsd")?;
                let trex = {
                    let mut found = (0, 0, 0);
                    if let Some((_, mvex)) = child(moov, b"mvex") {
                        let mut p = 0usize;
                        while let Some((thl, tbl, tt)) = read_box_header(&mvex[p..]) {
                            if &tt == b"trex" {
                                let tx = &mvex[p + thl..p + tbl];
                                if tx.len() >= 24 && be_u32(&tx[4..]) == their_id {
                                    found = (be_u32(&tx[12..]), be_u32(&tx[16..]), be_u32(&tx[20..]));
                                }
                            }
                            p += tbl;
                        }
                    }
                    found
                };
                // Register (or reuse) the sample description on our track.
                let track = &mut self.tracks[ours];
                let desc_idx = match track.descs.iter().position(|d| *d == entry) {
                    Some(i) => i as u32 + 1,
                    None => {
                        track.descs.push(entry);
                        track.descs.len() as u32
                    }
                };
                if ours == 0 && timescale != track.init.timescale {
                    return Err(anyhow!(
                        "encoded piece video timescale {timescale} != track timescale {}",
                        track.init.timescale
                    ));
                }
                theirs.push((their_id, PieceTrack { ours, desc_idx, timescale, trex }));
            }
            pos += bl;
        }
        if theirs.is_empty() {
            return Err(anyhow!("encoded piece has no usable tracks"));
        }

        // Walk every moof/mdat, collecting samples + bytes per target track.
        let our_timescales: Vec<u32> = self.tracks.iter().map(|t| t.init.timescale).collect();
        let mut acc: Vec<PieceAcc> = (0..self.tracks.len()).map(|_| PieceAcc::default()).collect();

        let mut pos = 0usize;
        while let Some((hl, bl, t)) = read_box_header(&fmp4[pos..]) {
            if bl < hl || pos + bl > fmp4.len() {
                return Err(anyhow!("piece: truncated box"));
            }
            if &t == b"moof" {
                let moof_pos = pos;
                let moof = &fmp4[pos + hl..pos + bl];
                let mut mp = 0usize;
                while let Some((thl, tbl, tt)) = read_box_header(&moof[mp..]) {
                    if tbl < thl || mp + tbl > moof.len() {
                        return Err(anyhow!("piece: truncated moof"));
                    }
                    if &tt == b"traf" {
                        let traf = &moof[mp + thl..mp + tbl];
                        collect_piece_traf(fmp4, moof_pos, traf, &theirs, &our_timescales, &mut acc)?;
                    }
                    mp += tbl;
                }
            }
            pos += bl;
        }

        // One chunk per track, appended to the shared mdat.
        let mut cursor = self.offset + 8;
        let mut mdat_payload = Vec::new();
        for (i, a) in acc.iter().enumerate() {
            if a.sizes.is_empty() {
                continue;
            }
            let desc_idx = theirs
                .iter()
                .find(|(_, th)| th.ours == i)
                .map(|(_, th)| th.desc_idx)
                .unwrap_or(1);
            let t = &mut self.tracks[i].tables;
            t.chunks.push((cursor, a.sizes.len() as u32, desc_idx));
            for (j, (size, dur)) in a.sizes.iter().zip(&a.durations).enumerate() {
                t.sizes.push(*size);
                t.durations.push(*dur);
                t.total_duration += u64::from(*dur);
                let n = t.sizes.len() as u32;
                if a.sync[j] {
                    t.sync.push(n);
                } else {
                    t.all_sync = false;
                }
            }
            t.any_ctts |= a.any_ctts;
            t.ctts.extend(&a.ctts);
            cursor += a.data.len() as u64;
            mdat_payload.extend_from_slice(&a.data);
        }
        if mdat_payload.is_empty() {
            return Err(anyhow!("encoded piece contained no samples"));
        }

        let mdat = mp4_box(b"mdat", &mdat_payload);
        self.offset += mdat.len() as u64;
        Ok(mdat)
    }

    /// Builds the closing `moov` with the accumulated sample tables.
    pub fn finish(&self) -> Result<Vec<u8>> {
        // mvhd: timescale 1000; duration = longest track, in ms.
        let dur_ms = self
            .tracks
            .iter()
            .map(|t| t.tables.total_duration * 1000 / u64::from(t.init.timescale.max(1)))
            .max()
            .unwrap_or(0);
        let mut mvhd = vec![0u8; 100];
        mvhd[12..16].copy_from_slice(&1000u32.to_be_bytes()); // timescale
        mvhd[16..20].copy_from_slice(&(dur_ms.min(u32::MAX as u64) as u32).to_be_bytes());
        mvhd[20..24].copy_from_slice(&0x0001_0000u32.to_be_bytes()); // rate 1.0
        mvhd[24..26].copy_from_slice(&0x0100u16.to_be_bytes()); // volume 1.0
        // identity matrix
        for (i, v) in [(36usize, 0x0001_0000u32), (52, 0x0001_0000), (68, 0x4000_0000)] {
            mvhd[i..i + 4].copy_from_slice(&v.to_be_bytes());
        }
        let next_id = self.tracks.len() as u32 + 1;
        mvhd[96..100].copy_from_slice(&next_id.to_be_bytes());
        let mut moov_payload = mp4_box(b"mvhd", &mvhd);

        for track in &self.tracks {
            moov_payload.extend_from_slice(&build_trak(track, dur_ms)?);
        }
        Ok(mp4_box(b"moov", &moov_payload))
    }
}

/// A re-encoded piece's track, mapped onto one of ours.
struct PieceTrack {
    ours: usize,
    desc_idx: u32,
    timescale: u32,
    trex: (u32, u32, u32),
}

/// Samples collected from a re-encoded piece for one of our tracks.
#[derive(Default)]
struct PieceAcc {
    sizes: Vec<u32>,
    durations: Vec<u32>,
    ctts: Vec<i64>,
    any_ctts: bool,
    sync: Vec<bool>,
    data: Vec<u8>,
}

/// The first sample-entry box inside a full `stsd` box.
fn first_stsd_entry(stsd_full: &[u8]) -> Result<Vec<u8>> {
    let (hl, bl, _) = read_box_header(stsd_full).context("bad stsd")?;
    let payload = &stsd_full[hl..bl];
    // version/flags(4) entry_count(4), then the entries.
    let entries = payload.get(8..).context("stsd too short")?;
    let (ehl, ebl, _) = read_box_header(entries).context("stsd has no entries")?;
    let _ = ehl;
    Ok(entries[..ebl].to_vec())
}

/// Collects one `traf`'s samples from a re-encoded piece into `acc`, rescaling
/// durations/offsets from the piece's timescale to ours.
fn collect_piece_traf(
    buf: &[u8],
    moof_pos: usize,
    traf: &[u8],
    theirs: &[(u32, PieceTrack)],
    our_timescales: &[u32],
    acc: &mut [PieceAcc],
) -> Result<()> {
    let (_, tfhd) = child(traf, b"tfhd").context("piece traf: no tfhd")?;
    let tf_flags = be_u32(tfhd) & 0xFF_FFFF;
    if tf_flags & 0x1 != 0 || tf_flags & 0x2_0000 == 0 {
        return Err(anyhow!("piece tfhd is not default-base-is-moof"));
    }
    let their_id = be_u32(&tfhd[4..]);
    let Some((_, info)) = theirs.iter().find(|(id, _)| *id == their_id) else {
        return Ok(()); // a track we don't keep
    };
    let ours_ts = u64::from(our_timescales[info.ours]);
    let piece_ts = u64::from(info.timescale.max(1));
    let rescale = |v: u64| -> u32 { ((v * ours_ts + piece_ts / 2) / piece_ts) as u32 };

    // tfhd optional fields after track_ID, in flag order.
    let mut off = 8usize;
    if tf_flags & 0x2 != 0 {
        off += 4;
    }
    let d_dur = if tf_flags & 0x8 != 0 {
        let v = be_u32(&tfhd[off..]);
        off += 4;
        v
    } else {
        info.trex.0
    };
    let d_size = if tf_flags & 0x10 != 0 {
        let v = be_u32(&tfhd[off..]);
        off += 4;
        v
    } else {
        info.trex.1
    };
    let d_flags = if tf_flags & 0x20 != 0 {
        be_u32(&tfhd[off..])
    } else {
        info.trex.2
    };

    let a = &mut acc[info.ours];
    let mut tpos = 0usize;
    while let Some((thl, tbl, tt)) = read_box_header(&traf[tpos..]) {
        if tbl < thl || tpos + tbl > traf.len() {
            return Err(anyhow!("piece: truncated traf"));
        }
        if &tt == b"trun" {
            let trun = &traf[tpos + thl..tpos + tbl];
            let version = trun[0];
            let fl = be_u32(trun) & 0xFF_FFFF;
            let count = be_u32(&trun[4..]) as usize;
            let mut p = 8usize;
            if fl & 0x1 == 0 {
                return Err(anyhow!("piece trun without data_offset"));
            }
            let data_offset = i32::from_be_bytes(trun[p..p + 4].try_into().unwrap());
            p += 4;
            let mut src = moof_pos
                .checked_add_signed(data_offset as isize)
                .context("piece: bad trun data_offset")?;
            let first_flags = if fl & 0x4 != 0 {
                let v = be_u32(&trun[p..]);
                p += 4;
                Some(v)
            } else {
                None
            };
            for i in 0..count {
                let dur = if fl & 0x100 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else {
                    d_dur
                };
                let size = if fl & 0x200 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else {
                    d_size
                };
                let flags = if fl & 0x400 != 0 {
                    let v = be_u32(&trun[p..]);
                    p += 4;
                    v
                } else if i == 0 {
                    first_flags.unwrap_or(d_flags)
                } else {
                    d_flags
                };
                let ct = if fl & 0x800 != 0 {
                    let raw = &trun[p..p + 4];
                    p += 4;
                    if version == 0 {
                        i64::from(be_u32(raw))
                    } else {
                        i64::from(i32::from_be_bytes(raw.try_into().unwrap()))
                    }
                } else {
                    0
                };
                let end = src + size as usize;
                if end > buf.len() {
                    return Err(anyhow!("piece: sample run exceeds buffer"));
                }
                a.data.extend_from_slice(&buf[src..end]);
                src = end;
                a.sizes.push(size);
                a.durations.push(rescale(u64::from(dur)));
                let ct_scaled = if ct == 0 {
                    0
                } else {
                    (ct * ours_ts as i64 + (piece_ts as i64) / 2) / piece_ts as i64
                };
                a.ctts.push(ct_scaled);
                if ct_scaled != 0 {
                    a.any_ctts = true;
                }
                a.sync.push(flags & 0x0001_0000 == 0);
            }
        }
        tpos += tbl;
    }
    Ok(())
}

/// Assembles one `trak` from the init boxes + accumulated tables.
fn build_trak(track: &TrackCtx, movie_dur_ms: u64) -> Result<Vec<u8>> {
    let t = &track.tables;
    let init = &track.init;

    // tkhd: patch track_ID + duration (movie timescale).
    let mut tkhd = init.tkhd.clone();
    let dur32 = movie_dur_ms.min(u32::MAX as u64) as u32;
    match tkhd.first() {
        Some(0) if tkhd.len() >= 84 => {
            tkhd[12..16].copy_from_slice(&track.track_id.to_be_bytes());
            tkhd[20..24].copy_from_slice(&dur32.to_be_bytes());
            tkhd[3] = 0x3; // enabled | in movie
        }
        Some(1) if tkhd.len() >= 96 => {
            tkhd[20..24].copy_from_slice(&track.track_id.to_be_bytes());
            tkhd[28..36].copy_from_slice(&movie_dur_ms.to_be_bytes());
            tkhd[3] = 0x3;
        }
        _ => return Err(anyhow!("unsupported tkhd")),
    }

    // mdhd: patch duration (media timescale).
    let mut mdhd = init.mdhd.clone();
    match mdhd.first() {
        Some(0) if mdhd.len() >= 20 => {
            let d = t.total_duration.min(u32::MAX as u64) as u32;
            mdhd[16..20].copy_from_slice(&d.to_be_bytes());
        }
        Some(1) if mdhd.len() >= 32 => {
            mdhd[24..32].copy_from_slice(&t.total_duration.to_be_bytes());
        }
        _ => return Err(anyhow!("unsupported mdhd")),
    }

    // stts: run-length encoded durations.
    let mut stts_entries: Vec<(u32, u32)> = Vec::new();
    for &d in &t.durations {
        match stts_entries.last_mut() {
            Some((count, delta)) if *delta == d => *count += 1,
            _ => stts_entries.push((1, d)),
        }
    }
    let mut stts = vec![0u8; 4];
    stts.extend_from_slice(&(stts_entries.len() as u32).to_be_bytes());
    for (c, d) in &stts_entries {
        stts.extend_from_slice(&c.to_be_bytes());
        stts.extend_from_slice(&d.to_be_bytes());
    }

    // ctts (only when any composition offset is non-zero). Version 1 (signed).
    let ctts = t.any_ctts.then(|| {
        let mut entries: Vec<(u32, i32)> = Vec::new();
        for &c in &t.ctts {
            let c = c as i32;
            match entries.last_mut() {
                Some((count, v)) if *v == c => *count += 1,
                _ => entries.push((1, c)),
            }
        }
        let mut b = vec![1u8, 0, 0, 0];
        b.extend_from_slice(&(entries.len() as u32).to_be_bytes());
        for (c, v) in entries {
            b.extend_from_slice(&c.to_be_bytes());
            b.extend_from_slice(&v.to_be_bytes());
        }
        b
    });

    // stsc: (first_chunk, samples_per_chunk, sample_description_index),
    // run-length over chunks.
    let mut stsc_entries: Vec<(u32, u32, u32)> = Vec::new();
    for (i, (_, count, desc)) in t.chunks.iter().enumerate() {
        match stsc_entries.last() {
            Some((_, c, d)) if c == count && d == desc => {}
            _ => stsc_entries.push((i as u32 + 1, *count, *desc)),
        }
    }
    let mut stsc = vec![0u8; 4];
    stsc.extend_from_slice(&(stsc_entries.len() as u32).to_be_bytes());
    for (first, count, desc) in &stsc_entries {
        stsc.extend_from_slice(&first.to_be_bytes());
        stsc.extend_from_slice(&count.to_be_bytes());
        stsc.extend_from_slice(&desc.to_be_bytes());
    }

    // stsz: per-sample sizes.
    let mut stsz = vec![0u8; 8]; // version/flags + sample_size(0 = per-sample)
    stsz.extend_from_slice(&(t.sizes.len() as u32).to_be_bytes());
    for s in &t.sizes {
        stsz.extend_from_slice(&s.to_be_bytes());
    }

    // co64: absolute chunk offsets.
    let mut co64 = vec![0u8; 4];
    co64.extend_from_slice(&(t.chunks.len() as u32).to_be_bytes());
    for (off, _, _) in &t.chunks {
        co64.extend_from_slice(&off.to_be_bytes());
    }

    // stbl = stsd + stts + [ctts] + stsc + stsz + co64 + [stss]. The stsd is
    // rebuilt from the registered sample descriptions (the source stream's,
    // plus one per distinct re-encoded boundary piece).
    let mut stsd_payload = vec![0u8; 4];
    stsd_payload.extend_from_slice(&(track.descs.len() as u32).to_be_bytes());
    for d in &track.descs {
        stsd_payload.extend_from_slice(d);
    }
    let mut stbl = mp4_box(b"stsd", &stsd_payload);
    stbl.extend_from_slice(&mp4_box(b"stts", &stts));
    if let Some(c) = ctts {
        stbl.extend_from_slice(&mp4_box(b"ctts", &c));
    }
    stbl.extend_from_slice(&mp4_box(b"stsc", &stsc));
    stbl.extend_from_slice(&mp4_box(b"stsz", &stsz));
    stbl.extend_from_slice(&mp4_box(b"co64", &co64));
    if !t.all_sync {
        let mut stss = vec![0u8; 4];
        stss.extend_from_slice(&(t.sync.len() as u32).to_be_bytes());
        for s in &t.sync {
            stss.extend_from_slice(&s.to_be_bytes());
        }
        stbl.extend_from_slice(&mp4_box(b"stss", &stss));
    }

    let mut minf = init.minf_headers.clone();
    minf.extend_from_slice(&mp4_box(b"stbl", &stbl));

    let mut mdia = mp4_box(b"mdhd", &mdhd);
    mdia.extend_from_slice(&init.hdlr);
    mdia.extend_from_slice(&mp4_box(b"minf", &minf));

    let mut trak = mp4_box(b"tkhd", &tkhd);
    trak.extend_from_slice(&mp4_box(b"mdia", &mdia));
    Ok(mp4_box(b"trak", &trak))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn be_u64(b: &[u8]) -> u64 {
        u64::from_be_bytes(b[..8].try_into().unwrap())
    }

    fn full_box(t: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        mp4_box(t, payload)
    }

    /// Minimal single-track init: ftyp + moov{mvhd, trak{tkhd, mdia{mdhd,
    /// hdlr, minf{vmhd, dinf, stbl{stsd}}}}, mvex{trex}}.
    fn init(handler: &[u8; 4], timescale: u32) -> Vec<u8> {
        let mut mvhd = vec![0u8; 100];
        mvhd[96..100].copy_from_slice(&2u32.to_be_bytes());
        let mut tkhd = vec![0u8; 84];
        tkhd[12..16].copy_from_slice(&1u32.to_be_bytes());
        let mut mdhd = vec![0u8; 20];
        mdhd[12..16].copy_from_slice(&timescale.to_be_bytes());
        let mut hdlr = vec![0u8; 8];
        hdlr.extend_from_slice(handler);
        hdlr.extend_from_slice(&[0u8; 13]);
        // stsd with one dummy sample entry ("tst " box).
        let mut stsd_payload = vec![0u8; 4];
        stsd_payload.extend_from_slice(&1u32.to_be_bytes());
        stsd_payload.extend_from_slice(&full_box(b"tst ", &[0u8; 8]));
        let stsd = full_box(b"stsd", &stsd_payload);
        let mut stbl = stsd;
        // (init tables intentionally omitted — parse_init only reads stsd)
        let mut minf = full_box(b"vmhd", &[0u8; 12]);
        minf.extend_from_slice(&full_box(b"dinf", &full_box(b"dref", &[0u8; 8])));
        minf.extend_from_slice(&full_box(b"stbl", &stbl));
        stbl = Vec::new();
        let _ = stbl;
        let mut mdia = full_box(b"mdhd", &mdhd);
        mdia.extend_from_slice(&full_box(b"hdlr", &hdlr));
        mdia.extend_from_slice(&full_box(b"minf", &minf));
        let mut trak = full_box(b"tkhd", &tkhd);
        trak.extend_from_slice(&full_box(b"mdia", &mdia));
        let mut trex = vec![0u8; 4];
        trex.extend_from_slice(&1u32.to_be_bytes()); // track id
        trex.extend_from_slice(&1u32.to_be_bytes()); // default desc idx
        trex.extend_from_slice(&512u32.to_be_bytes()); // default duration
        trex.extend_from_slice(&0u32.to_be_bytes()); // default size
        trex.extend_from_slice(&0u32.to_be_bytes()); // default flags
        let mvex = full_box(b"trex", &trex);
        let mut moov = full_box(b"mvhd", &mvhd);
        moov.extend_from_slice(&full_box(b"trak", &trak));
        moov.extend_from_slice(&full_box(b"mvex", &mvex));
        let mut out = full_box(b"ftyp", b"isom\x00\x00\x00\x00");
        out.extend_from_slice(&full_box(b"moov", &moov));
        out
    }

    /// CMAF segment: moof{mfhd, traf{tfhd(default-base-is-moof), tfdt,
    /// trun(sizes+flags per sample)}} + mdat with the given sample payloads.
    fn segment(samples: &[(&[u8], bool)]) -> Vec<u8> {
        let mut tfhd = vec![0u8, 0x02, 0x00, 0x00];
        tfhd.extend_from_slice(&1u32.to_be_bytes());
        let mut tfdt = vec![1u8, 0, 0, 0];
        tfdt.extend_from_slice(&0u64.to_be_bytes());
        // trun flags: data-offset | sample-size | sample-flags
        let mut trun = vec![0u8, 0x00, 0x06, 0x01];
        trun.extend_from_slice(&(samples.len() as u32).to_be_bytes());
        trun.extend_from_slice(&0i32.to_be_bytes()); // patched below
        for (data, sync) in samples {
            trun.extend_from_slice(&(data.len() as u32).to_be_bytes());
            let flags: u32 = if *sync { 0 } else { 0x0001_0000 };
            trun.extend_from_slice(&flags.to_be_bytes());
        }
        let mut traf = full_box(b"tfhd", &tfhd);
        traf.extend_from_slice(&full_box(b"tfdt", &tfdt));
        traf.extend_from_slice(&full_box(b"trun", &trun));
        let mut moof_payload = full_box(b"mfhd", &[0, 0, 0, 0, 0, 0, 0, 1]);
        moof_payload.extend_from_slice(&full_box(b"traf", &traf));
        let mut moof = full_box(b"moof", &moof_payload);
        let off = (moof.len() + 8) as i32;
        // trun data_offset position: search for the trun and patch.
        let mut p = 0;
        while &moof[p + 4..p + 8] != b"trun" {
            p += 1;
        }
        moof[p + 16..p + 20].copy_from_slice(&off.to_be_bytes());
        let mut payload = Vec::new();
        for (d, _) in samples {
            payload.extend_from_slice(d);
        }
        let mut seg = moof;
        seg.extend_from_slice(&full_box(b"mdat", &payload));
        seg
    }

    /// The frame rate falls out of the tables `stts` is built from, so it costs
    /// nothing to report: 15360 ticks per second at 512 ticks per sample is
    /// exactly 30 fps.
    ///
    /// This is the CMAF counterpart of `crate::tsprobe` — same fact, reached
    /// without walking a single packet, because the muxer already had to count
    /// these durations to write the sample table.
    #[test]
    fn the_video_frame_rate_is_measured_from_the_sample_durations() {
        let vinit = init(b"vide", 15360);
        let (mut b, _) = ProgressiveMp4::new(&vinit, None).unwrap();
        assert_eq!(
            b.video_frame_rate(),
            None,
            "no samples ingested is an absent measurement, not a rate"
        );

        b.push(&segment(&[(b"KEYFRAME", true), (b"delta", false)]), None)
            .unwrap();
        b.push(&segment(&[(b"KEY2", true)]), None).unwrap();

        let fps = b.video_frame_rate().expect("three samples were ingested");
        assert!((fps - 30.0).abs() < 1e-9, "got {fps}");
    }

    #[test]
    fn builds_progressive_mp4_with_correct_tables() {
        let vinit = init(b"vide", 30);
        let (mut b, ftyp) = ProgressiveMp4::new(&vinit, None).unwrap();
        assert_eq!(&ftyp[4..8], b"ftyp");

        let mdat1 = b
            .push(&segment(&[(b"KEYFRAME", true), (b"delta", false)]), None)
            .unwrap();
        let mdat2 = b.push(&segment(&[(b"KEY2", true)]), None).unwrap();
        assert_eq!(&mdat1[4..8], b"mdat");
        assert_eq!(&mdat1[8..16], b"KEYFRAME");

        let moov = b.finish().unwrap();
        let (_, moov_p) = child(&moov, b"moov").unwrap();
        let (_, trak) = child(moov_p, b"trak").unwrap();
        let (_, mdia) = child(trak, b"mdia").unwrap();
        let (_, minf) = child(mdia, b"minf").unwrap();
        let (_, stbl) = child(minf, b"stbl").unwrap();

        // stsz: 3 samples with the right sizes.
        let (_, stsz) = child(stbl, b"stsz").unwrap();
        assert_eq!(be_u32(&stsz[8..]), 3);
        assert_eq!(be_u32(&stsz[12..]), 8);
        assert_eq!(be_u32(&stsz[16..]), 5);
        assert_eq!(be_u32(&stsz[20..]), 4);

        // stts: all durations from trex default (512) → one run of 3.
        let (_, stts) = child(stbl, b"stts").unwrap();
        assert_eq!(be_u32(&stts[4..]), 1);
        assert_eq!(be_u32(&stts[8..]), 3);
        assert_eq!(be_u32(&stts[12..]), 512);

        // stss: sample 2 is non-sync → entries [1, 3].
        let (_, stss) = child(stbl, b"stss").unwrap();
        assert_eq!(be_u32(&stss[4..]), 2);
        assert_eq!(be_u32(&stss[8..]), 1);
        assert_eq!(be_u32(&stss[12..]), 3);

        // co64: two chunks; first begins right after ftyp + mdat header.
        let (_, co64) = child(stbl, b"co64").unwrap();
        assert_eq!(be_u32(&co64[4..]), 2);
        assert_eq!(be_u64(&co64[8..]), (ftyp.len() + 8) as u64);
        assert_eq!(
            be_u64(&co64[16..]),
            (ftyp.len() + mdat1.len() + 8) as u64
        );
        let _ = mdat2;

        // mdhd duration = 3 * 512.
        let (_, mdhd) = child(mdia, b"mdhd").unwrap();
        assert_eq!(be_u32(&mdhd[16..]), 1536);
    }

    #[test]
    fn muxes_video_and_audio_chunks_in_one_mdat() {
        let vinit = init(b"vide", 30);
        let ainit = init(b"soun", 48_000);
        let (mut b, ftyp) = ProgressiveMp4::new(&vinit, Some(&ainit)).unwrap();
        let mdat = b
            .push(
                &segment(&[(b"VVVV", true)]),
                Some(&segment(&[(b"AA", true), (b"BB", true)])),
            )
            .unwrap();
        assert_eq!(&mdat[8..12], b"VVVV");
        assert_eq!(&mdat[12..16], b"AABB");

        let moov = b.finish().unwrap();
        let (_, moov_p) = child(&moov, b"moov").unwrap();
        // Two traks; audio track id = 2, chunk offset lands on "AA".
        let mut traks = Vec::new();
        let mut pos = 0;
        while let Some((hl, bl, t)) = read_box_header(&moov_p[pos..]) {
            if &t == b"trak" {
                traks.push(&moov_p[pos + hl..pos + bl]);
            }
            pos += bl;
        }
        assert_eq!(traks.len(), 2);
        let (_, a_tkhd) = child(traks[1], b"tkhd").unwrap();
        assert_eq!(be_u32(&a_tkhd[12..]), 2);
        let (_, a_mdia) = child(traks[1], b"mdia").unwrap();
        let (_, a_minf) = child(a_mdia, b"minf").unwrap();
        let (_, a_stbl) = child(a_minf, b"stbl").unwrap();
        let (_, a_co64) = child(a_stbl, b"co64").unwrap();
        assert_eq!(be_u64(&a_co64[8..]), (ftyp.len() + 8 + 4) as u64);
        // Audio all-sync → no stss.
        assert!(child(a_stbl, b"stss").is_none());
    }
}
