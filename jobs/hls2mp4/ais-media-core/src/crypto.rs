//! CENC-CBCS decryption and CPIX/KMS key retrieval.
//!
//! The canonical home for the crypto path shared by `vod-hls2mp4` and
//! `live-hls2mp4`: init-map `tenc` parsing, CPIX key exchange, and in-place
//! `cbcs` (1:9) pattern decryption.

use aes::cipher::{BlockDecryptMut, KeyIvInit};
use anyhow::{anyhow, Result};
use reqwest::Client;
use serde::Deserialize;

type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;

// CPIX XML schema
#[derive(Debug, Deserialize)]
struct CpixResponse {
    #[serde(rename = "ContentKeyList")]
    content_key_list: ContentKeyList,
}

#[derive(Debug, Deserialize)]
struct ContentKeyList {
    #[serde(rename = "ContentKey")]
    content_keys: Vec<ContentKey>,
}

#[derive(Debug, Deserialize)]
struct ContentKey {
    #[serde(rename = "@kid")]
    #[allow(dead_code)]
    kid: String,
    #[serde(rename = "Data")]
    data: KeyData,
}

#[derive(Debug, Deserialize)]
struct KeyData {
    #[serde(rename = "Secret")]
    secret: SecretData,
}

#[derive(Debug, Deserialize)]
struct SecretData {
    #[serde(rename = "PlainValue")]
    plain_value: String,
}

/// Encryption parameters parsed from the fMP4 init map's `tenc`
/// (TrackEncryptionBox), per ISO/IEC 23001-7.
#[derive(Debug, Clone)]
pub struct TencInfo {
    /// The default key ID (16 bytes) used for the CPIX key lookup.
    pub kid: Vec<u8>,
    /// `default_Per_Sample_IV_Size`. `0` means the track carries a single
    /// `default_constant_IV` (the `cbcs` common case). A non-zero value means
    /// per-sample IVs are stored in each fragment's `senc` box, which this tool
    /// does not parse — see [`constant_iv`](Self::constant_iv).
    pub per_sample_iv_size: u8,
    /// The `cbcs` `default_constant_IV` (16 bytes) when the track is protected
    /// with a constant IV (`per_sample_iv_size == 0`); `None` when the track
    /// uses per-sample IVs.
    pub constant_iv: Option<[u8; 16]>,
}

/// Scans the fMP4 init map for a `tenc` box and returns its encryption
/// parameters, or `None` for a clear track (no `tenc`, or `default_isProtected
/// == 0`).
pub fn parse_tenc(init_bytes: &[u8]) -> Result<Option<TencInfo>> {
    let Some(pos) = init_bytes.windows(4).position(|w| w == b"tenc") else {
        return Ok(None);
    };
    // Layout after the "tenc" fourcc: version(1) + flags(3), then the
    // TrackEncryptionBox payload: reserved(1), the crypt/skip pattern byte(1),
    // default_isProtected(1), default_Per_Sample_IV_Size(1), default_KID(16).
    // When protected with a constant IV, default_KID is followed by
    // default_constant_IV_size(1) and that many IV bytes.
    let body = pos + 4 + 4;
    let is_protected_pos = body + 2;
    let per_sample_iv_size_pos = body + 3;
    let kid_start = body + 4;
    let kid_end = kid_start + 16;

    // Not enough bytes for a KID: treat as not-a-valid-tenc (clear), matching
    // the original lenient behaviour rather than aborting.
    if kid_end > init_bytes.len() || init_bytes[is_protected_pos] != 1 {
        return Ok(None);
    }

    let per_sample_iv_size = init_bytes[per_sample_iv_size_pos];
    let kid = init_bytes[kid_start..kid_end].to_vec();

    // A protected track with per_sample_iv_size == 0 MUST carry a
    // default_constant_IV; if it is missing/malformed the stream is broken and
    // we must not fall back to a bogus IV.
    let constant_iv = if per_sample_iv_size == 0 {
        let size = *init_bytes
            .get(kid_end)
            .ok_or_else(|| anyhow!("truncated tenc: missing default_constant_IV size"))?
            as usize;
        if size != 16 {
            return Err(anyhow!(
                "unsupported cbcs constant IV size {size} (expected 16)"
            ));
        }
        let iv = init_bytes
            .get(kid_end + 1..kid_end + 1 + 16)
            .ok_or_else(|| anyhow!("truncated tenc: default_constant_IV runs past the init map"))?;
        let mut buf = [0u8; 16];
        buf.copy_from_slice(iv);
        Some(buf)
    } else {
        None
    };

    Ok(Some(TencInfo {
        kid,
        per_sample_iv_size,
        constant_iv,
    }))
}

/// Exchanges a KID for a plain CEK over the DASH-IF CPIX XML protocol.
pub async fn fetch_key_from_cpix(client: &Client, endpoint: &str, kid: &str) -> Result<Vec<u8>> {
    let request_body = format!(
        r#"<?xml version="1.0"?><cpix:CPIX xmlns:cpix="urn:dashif:org:cpix"><cpix:ContentKeyList><cpix:ContentKey kid="{}"/></cpix:ContentKeyList></cpix:CPIX>"#,
        kid
    );
    let resp = client
        .post(endpoint)
        .header("Content-Type", "application/xml")
        .body(request_body)
        .send()
        .await?
        .text()
        .await?;
    let cpix: CpixResponse = quick_xml::de::from_str(&resp)?;
    let value = cpix
        .content_key_list
        .content_keys
        .first()
        .ok_or_else(|| anyhow!("Empty keys matrix"))?
        .data
        .secret
        .plain_value
        .trim();
    if let Ok(hex_decoded) = hex::decode(value) {
        Ok(hex_decoded)
    } else {
        use base64::Engine;
        Ok(base64::engine::general_purpose::STANDARD.decode(value)?)
    }
}

/// In-place `cbcs` pattern decryption (1 encrypted block, 9 skipped) over the
/// payload inside the `mdat` box. Leaves NAL units otherwise untouched.
///
/// `iv` is the `cbcs` constant IV from the `tenc` box (see [`parse_tenc`]); the
/// CBC chain is seeded from it, so passing the wrong IV corrupts the first
/// decrypted block. NOTE: this treats the `mdat` payload as a single protected
/// region and does not parse the `senc` box for per-subsample boundaries or
/// per-sample IVs — it is correct for constant-IV `cbcs` content encrypted from
/// the start of the payload, which is what this pipeline targets.
pub fn decrypt_cmaf_cbcs_inplace(
    seg_bytes: &mut [u8],
    key_bytes: &[u8],
    iv: &[u8; 16],
) -> Result<()> {
    if key_bytes.len() != 16 {
        return Err(anyhow!(
            "cbcs content key must be 16 bytes, got {}",
            key_bytes.len()
        ));
    }
    let key = aes::cipher::Key::<aes::Aes128>::from_slice(key_bytes);
    if let Some(pos) = seg_bytes.windows(4).position(|w| w == b"mdat") {
        let mdat_payload = &mut seg_bytes[pos + 4..];
        let mut decryptor =
            Aes128CbcDec::new(key, aes::cipher::Iv::<Aes128CbcDec>::from_slice(iv));
        let mut index = 0;
        while index + 16 <= mdat_payload.len() {
            decryptor.decrypt_block_mut(aes::cipher::Block::<aes::Aes128>::from_mut_slice(
                &mut mdat_payload[index..index + 16],
            ));
            index += 16 + (9 * 16);
        }
    }
    Ok(())
}

/// Formats a 16-byte KID as a canonical UUID string.
pub fn format_to_uuid(b: &[u8]) -> String {
    if b.len() != 16 {
        return hex::encode(b);
    }
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use aes::cipher::BlockEncryptMut;

    type Aes128CbcEnc = cbc::Encryptor<aes::Aes128>;

    /// Builds a minimal `tenc` box body preceded by the `"tenc"` fourcc, as the
    /// parser locates it. Optionally appends a `default_constant_IV`.
    fn tenc(is_protected: u8, per_sample_iv_size: u8, kid: &[u8; 16], iv: Option<&[u8; 16]>) -> Vec<u8> {
        let mut v = Vec::new();
        v.extend_from_slice(b"tenc");
        v.push(0); // version
        v.extend_from_slice(&[0, 0, 0]); // flags
        v.push(0); // reserved
        v.push(0); // crypt/skip pattern byte
        v.push(is_protected);
        v.push(per_sample_iv_size);
        v.extend_from_slice(kid);
        if let Some(iv) = iv {
            v.push(16); // default_constant_IV_size
            v.extend_from_slice(iv);
        }
        v
    }

    #[test]
    fn parse_tenc_reads_kid_and_constant_iv() {
        let kid = [0xABu8; 16];
        let iv = [0x11u8; 16];
        let info = parse_tenc(&tenc(1, 0, &kid, Some(&iv)))
            .unwrap()
            .expect("protected track");
        assert_eq!(info.kid, kid);
        assert_eq!(info.per_sample_iv_size, 0);
        assert_eq!(info.constant_iv, Some(iv));
    }

    #[test]
    fn parse_tenc_clear_track_is_none() {
        assert!(parse_tenc(&tenc(0, 0, &[0u8; 16], Some(&[0u8; 16])))
            .unwrap()
            .is_none());
        assert!(parse_tenc(b"no boxes here").unwrap().is_none());
    }

    // A per-sample-IV track has no constant IV; the caller rejects it rather
    // than decrypting with a bogus IV.
    #[test]
    fn parse_tenc_per_sample_iv_has_no_constant_iv() {
        let info = parse_tenc(&tenc(1, 8, &[0x01u8; 16], None))
            .unwrap()
            .expect("protected track");
        assert_eq!(info.per_sample_iv_size, 8);
        assert_eq!(info.constant_iv, None);
    }

    /// Encrypts `payload` in place with the same 1:9 `cbcs` pattern and chaining
    /// the decryptor uses, so a round-trip must recover the original.
    fn encrypt_cbcs_1_9(payload: &mut [u8], key: &[u8; 16], iv: &[u8; 16]) {
        let mut enc = Aes128CbcEnc::new(key.into(), iv.into());
        let mut i = 0;
        while i + 16 <= payload.len() {
            enc.encrypt_block_mut(aes::cipher::Block::<aes::Aes128>::from_mut_slice(
                &mut payload[i..i + 16],
            ));
            i += 16 + (9 * 16);
        }
    }

    #[test]
    fn cbcs_roundtrip_honours_nonzero_iv() {
        let key = [0x42u8; 16];
        let iv = [0x37u8; 16];
        let plain: Vec<u8> = (0..400u32).map(|n| (n % 256) as u8).collect();

        // Wrap the payload in a fake `mdat` box: [size(4)]["mdat"][payload].
        let make_box = |body: &[u8]| {
            let mut b = vec![0, 0, 0, 0];
            b.extend_from_slice(b"mdat");
            b.extend_from_slice(body);
            b
        };

        let mut cipher_body = plain.clone();
        encrypt_cbcs_1_9(&mut cipher_body, &key, &iv);

        // Correct IV recovers the payload exactly.
        let mut ok = make_box(&cipher_body);
        decrypt_cmaf_cbcs_inplace(&mut ok, &key, &iv).unwrap();
        assert_eq!(&ok[8..], &plain[..], "correct IV must round-trip");

        // Wrong (zero) IV corrupts the first encrypted block only.
        let mut bad = make_box(&cipher_body);
        decrypt_cmaf_cbcs_inplace(&mut bad, &key, &[0u8; 16]).unwrap();
        assert_ne!(&bad[8..24], &plain[0..16], "zero IV must corrupt block 0");
    }

    #[test]
    fn decrypt_rejects_bad_key_length() {
        let mut buf = b"\0\0\0\0mdat................".to_vec();
        assert!(decrypt_cmaf_cbcs_inplace(&mut buf, &[0u8; 8], &[0u8; 16]).is_err());
    }
}
