//! HLS manifest resolution and media-playlist fetching, shared by both
//! clippers. The SCTE-35 ad-break state machine lives in [`crate::scte35`] and
//! is re-exported here.

use anyhow::{anyhow, Result};
use m3u8_rs::{AlternativeMediaType, MediaPlaylist, Playlist};
use reqwest::Client;

pub use crate::scte35::AdState;

/// The media-playlist URLs selected from a master: the highest-bandwidth video
/// variant plus, for CMAF streams that carry audio as a separate rendition, the
/// matching audio group's playlist (used to mux audio into the output).
pub struct Renditions {
    pub video: String,
    pub audio: Option<String>,
    /// `BANDWIDTH` of the selected video variant, reported as the status
    /// documents' `capture_position.variant_bandwidth` so downstream knows WHICH
    /// rendition was captured. `None` for a source that is already a media
    /// playlist: there is no variant declaration to read it from.
    pub bandwidth: Option<u64>,
    /// `RESOLUTION`, `CODECS` and `FRAME-RATE` of the selected video variant, as
    /// the master playlist DECLARES them.
    ///
    /// Free: the variant is already parsed to pick it, so reading three more of
    /// its attributes costs no request and no decode — which is the only reason
    /// they are here rather than probed. They are what let §7.1's
    /// `media_profile` / `resolution` and the thumbnail's pixel dimensions be
    /// reported at all.
    ///
    /// DECLARED, NOT MEASURED, and every consumer of these must hold that
    /// distinction. They describe the variant the origin advertised; nothing here
    /// opens the media to confirm it. They are therefore trustworthy for the
    /// stream-copied master and for the unscaled thumbnail, and NOT for anything
    /// re-encoded. All three are `None` for a source that is already a media
    /// playlist — there is no variant declaration to read — and each is
    /// independently optional in the manifest, so absence is normal and must
    /// never be filled with a default.
    pub resolution: Option<(u64, u64)>,
    pub codecs: Option<String>,
    pub frame_rate: Option<f64>,
    /// `AVERAGE-BANDWIDTH` of the selected variant, reported as the §7.1
    /// `media_profile` bitRate (product ruling: average, not the `BANDWIDTH`
    /// peak). Falls back to `BANDWIDTH` only when the manifest omits the average,
    /// since a peak is closer to the truth than nothing.
    pub average_bandwidth: Option<u64>,
    /// What the `EXT-X-MEDIA` alternatives DECLARE about the audio and closed
    /// captions the selected variant references.
    ///
    /// Free for the same reason the variant attributes above are: the alternatives
    /// list is already walked to find the audio rendition's URI, so reading the
    /// attributes while there costs no request and no decode. They are what let
    /// §7.1's `audio` and `cc` media_profile groups and `streaming_video`'s
    /// `audio_languages` be reported at all.
    ///
    /// DECLARED, NOT MEASURED — the same caveat as the variant attributes. Note
    /// what is deliberately NOT here: an audio BITRATE. HLS declares none, and the
    /// only place a number appears is inside packager-chosen names
    /// (`GROUP-ID="audio-aacl-128"`, `…audio_eng=128000…`). Parsing digits out of
    /// an identifier is a naming convention of one packager, not a value the
    /// manifest asserts, so it is left absent rather than reported as if measured.
    pub audio_languages: Vec<String>,
    pub audio_group: Option<String>,
    /// `CHANNELS` of the selected audio rendition (HLS declares it as a count,
    /// e.g. "2").
    pub audio_channels: Option<String>,
    pub cc_language: Option<String>,
    /// `INSTREAM-ID` of the closed-captions rendition (e.g. `CC1`). It decides the
    /// caption FORMAT: a `CC1`-`CC4` / `SERVICEn` id is CEA-608/708 embedded in
    /// the video, which is NOT the `WebVTT` §7.1's example happens to show.
    pub cc_instream_id: Option<String>,
}

/// The result of fetching a playlist URL.
pub enum Fetched {
    Media(Box<MediaPlaylist>),
    /// A master playlist was returned (e.g. a CDN redirect) — the caller should
    /// re-resolve to a media playlist.
    Master,
}

/// Resolves an HLS URL to a concrete media-playlist URL. For a master playlist,
/// selects the highest-bandwidth variant (single pass). For a media playlist,
/// returns the URL unchanged.
pub async fn resolve_media_playlist_url(client: &Client, initial_url: &str) -> Result<String> {
    let body = client
        .get(initial_url)
        .send()
        .await?
        .error_for_status()?
        .bytes()
        .await?;
    match m3u8_rs::parse_playlist_res(&body) {
        Ok(Playlist::MasterPlaylist(master)) => {
            let best = master
                .variants
                .into_iter()
                .max_by_key(|v| v.bandwidth)
                .ok_or_else(|| anyhow!("Master playlist contains zero variants."))?;
            println!(
                "-> Selected highest-bitrate variant: {} ({} bps)",
                best.uri, best.bandwidth
            );
            resolve_url(initial_url, &best.uri)
        }
        Ok(Playlist::MediaPlaylist(_)) => Ok(initial_url.to_string()),
        _ => Err(anyhow!("Invalid HLS stream playlist.")),
    }
}

/// Resolves an HLS URL to its media playlists: the highest-bandwidth video
/// variant and, if that variant references an `EXT-X-MEDIA TYPE=AUDIO` group
/// with its own URI, the audio rendition playlist. For a direct media playlist
/// (no master), returns it as `video` with no audio.
pub async fn resolve_renditions(client: &Client, initial_url: &str) -> Result<Renditions> {
    let body = client
        .get(initial_url)
        .send()
        .await?
        .error_for_status()?
        .bytes()
        .await?;
    match m3u8_rs::parse_playlist_res(&body) {
        Ok(Playlist::MasterPlaylist(master)) => {
            let best = master
                .variants
                .iter()
                .filter(|v| !v.is_i_frame)
                .max_by_key(|v| v.bandwidth)
                .ok_or_else(|| anyhow!("Master playlist contains zero variants."))?;
            println!(
                "-> Selected highest-bitrate variant: {} ({} bps)",
                best.uri, best.bandwidth
            );

            // A separate audio rendition referenced by the chosen variant's
            // AUDIO group (CMAF keeps audio out of the video segments).
            let audio_uri = best.audio.as_ref().and_then(|group| {
                master
                    .alternatives
                    .iter()
                    .find(|a| {
                        a.media_type == AlternativeMediaType::Audio
                            && &a.group_id == group
                            && a.uri.is_some()
                    })
                    .and_then(|a| a.uri.clone())
            });
            if let Some(uri) = &audio_uri {
                println!("-> Audio rendition: {uri}");
            }

            // The audio alternative the chosen variant references, and the
            // closed-captions one. Both are read from the SAME alternatives list
            // the audio URI lookup above already walks.
            let audio_alt = best.audio.as_ref().and_then(|group| {
                master
                    .alternatives
                    .iter()
                    .find(|a| a.media_type == AlternativeMediaType::Audio && &a.group_id == group)
            });
            // Every distinct language advertised in the variant's audio GROUP —
            // one entry per rendition, deduped, in declaration order.
            let mut audio_languages: Vec<String> = Vec::new();
            if let Some(group) = best.audio.as_ref() {
                for a in master.alternatives.iter().filter(|a| {
                    a.media_type == AlternativeMediaType::Audio && &a.group_id == group
                }) {
                    if let Some(lang) = a.language.as_ref() {
                        if !lang.is_empty() && !audio_languages.iter().any(|l| l == lang) {
                            audio_languages.push(lang.clone());
                        }
                    }
                }
            }
            let cc_alt = master.alternatives.iter().find(|a| {
                a.media_type == AlternativeMediaType::ClosedCaptions
                    && best
                        .closed_captions
                        .as_ref()
                        // ClosedCaptionGroupId is an enum (None | GroupId(String)),
                        // so the id is matched through its Debug form rather than a
                        // Display impl the crate does not provide.
                        .map(|g| format!("{g:?}").contains(a.group_id.as_str()))
                        .unwrap_or(false)
            });

            let video = resolve_url(initial_url, &best.uri)?;
            let audio = match audio_uri {
                Some(uri) => Some(resolve_url(initial_url, &uri)?),
                None => None,
            };
            Ok(Renditions {
                video,
                audio,
                bandwidth: Some(best.bandwidth),
                resolution: best.resolution.map(|r| (r.width, r.height)),
                codecs: best.codecs.clone(),
                frame_rate: best.frame_rate,
                average_bandwidth: best.average_bandwidth.or(Some(best.bandwidth)),
                audio_languages,
                audio_group: best.audio.clone(),
                audio_channels: audio_alt.and_then(|a| a.channels.clone()),
                cc_language: cc_alt.and_then(|a| a.language.clone()),
                cc_instream_id: cc_alt
                    .and_then(|a| a.instream_id.as_ref())
                    .map(|i| format!("{i:?}")),
            })
        }
        // A direct media playlist declares no variant, so there is nothing to
        // read. None here is what stops §7.1's media_profile being filled with
        // invented values for a source that never advertised any.
        Ok(Playlist::MediaPlaylist(_)) => Ok(Renditions {
            video: initial_url.to_string(),
            audio: None,
            bandwidth: None,
            resolution: None,
            codecs: None,
            frame_rate: None,
            average_bandwidth: None,
            // No master means no EXT-X-MEDIA either: empty and None throughout,
            // never a substituted default.
            audio_languages: Vec::new(),
            audio_group: None,
            audio_channels: None,
            cc_language: None,
            cc_instream_id: None,
        }),
        _ => Err(anyhow!("Invalid HLS stream playlist.")),
    }
}

/// Fetches and parses a playlist, distinguishing media from master.
pub async fn fetch_playlist(client: &Client, url: &str) -> Result<Fetched> {
    let body = client
        .get(url)
        .send()
        .await?
        .error_for_status()?
        .bytes()
        .await?;
    match m3u8_rs::parse_playlist_res(&body) {
        Ok(Playlist::MediaPlaylist(media)) => Ok(Fetched::Media(Box::new(media))),
        Ok(Playlist::MasterPlaylist(_)) => Ok(Fetched::Master),
        _ => Err(anyhow!("Failed to parse HLS playlist.")),
    }
}

/// Resolves a possibly-relative segment/map URI against a base playlist URL.
pub fn resolve_url(base: &str, rel: &str) -> Result<String> {
    if rel.starts_with("http") {
        Ok(rel.to_string())
    } else {
        Ok(reqwest::Url::parse(base)?.join(rel)?.to_string())
    }
}
