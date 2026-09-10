//! Shared foundation for the media jobs: the byte-concat HLS tools
//! (`vod-hls2mp4`, `live-hls2mp4`) and the ABR packager (`vod-packager`).
//!
//! - [`crypto`] — fMP4 `tenc` parsing, CPIX/KMS key exchange, and in-place
//!   `cbcs` (1:9) pattern decryption.
//! - [`fmp4`] — stream-copy fMP4/CMAF box surgery: timeline zero-basing
//!   (`tfdt`/`sidx`), `mfhd` renumbering, and video+audio fragment muxing.
//! - [`storage`] — `object_store`-backed output (`s3://` / `gs://` / `file://`),
//!   as a single-object writer or a directory target that renames temps.
//! - [`net`] — a small retrying HTTP GET used by both tools.

//! - [`scte35`] — the SCTE-35 ad-break state machine (DATERANGE + inline cues).
//! - [`tsprobe`] — frame rate measured from the recorded MPEG-TS, for the
//!   sources whose manifest declares none.
//! - [`hls`] — master-playlist resolution and the variant/rendition attributes
//!   the §7.1 `media_profile` is built from.
//! - [`task`] — the inbound §4 task envelope (live and vod shapes).
//! - [`notify`] — the §7 notification envelope and the storage-only status
//!   writer that emits it.
//!
//! `hls`, `task` and `notify` moved here from `live-hls2mp4` when `vod-hls2mp4`
//! grew the same reporting, and `vod-packager` is the third consumer. The §7
//! contract is frozen by golden-JSON tests, so it must have exactly ONE
//! definition: two copies of it would drift the first time a field was added to
//! only one job. `vod-packager` uses `notify` and `task` but none of the
//! capture-side modules — it reads no manifest and decrypts nothing.

pub mod crypto;
pub mod derivatives;
pub mod fmp4;
pub mod hls;
pub mod hlspreview;
pub mod mp4mux;
pub mod net;
pub mod notify;
pub mod scte35;
pub mod splice;
pub mod storage;
pub mod task;
pub mod tsprobe;
