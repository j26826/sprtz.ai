//! Retrying HTTP fetch shared by the HLS tools.

use std::time::Duration;

use anyhow::{anyhow, Result};
use reqwest::Client;

/// Fetches a URL body, retrying transient failures a few times with a short
/// backoff. Non-2xx responses are treated as errors (via `error_for_status`)
/// and retried, so a transient 5xx or a truncated body neither aborts the whole
/// download nor silently writes an error page into the output.
/// Fetches a URL body, returning `Ok(None)` when the server says the resource
/// is not there rather than that it could not answer.
///
/// A 404 or 410 is permanent: retrying it spends the attempts for nothing and
/// then aborts whatever was reading. A live playlist with a DVR window lists
/// segments that expire from the edge while a long download is still walking
/// it, so a single missing segment out of thousands is ordinary — and losing
/// the whole recording to it is not.
pub async fn fetch_bytes_optional(
    client: &Client,
    url: &str,
    attempts: usize,
) -> Result<Option<Vec<u8>>> {
    match fetch_bytes(client, url, attempts).await {
        Ok(bytes) => Ok(Some(bytes)),
        Err(e) => {
            if is_gone(&e) {
                Ok(None)
            } else {
                Err(e)
            }
        }
    }
}

/// Whether an error from [`fetch_bytes`] is the server saying "not here".
fn is_gone(err: &anyhow::Error) -> bool {
    err.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .and_then(|e| e.status())
            .is_some_and(|s| s == reqwest::StatusCode::NOT_FOUND || s == reqwest::StatusCode::GONE)
    })
}

pub async fn fetch_bytes(client: &Client, url: &str, attempts: usize) -> Result<Vec<u8>> {
    let mut last_err = None;
    for attempt in 0..attempts {
        match client.get(url).send().await {
            Ok(resp) => match resp.error_for_status() {
                Ok(ok) => match ok.bytes().await {
                    Ok(b) => return Ok(b.to_vec()),
                    Err(e) => last_err = Some(anyhow::Error::from(e)),
                },
                Err(e) => last_err = Some(anyhow::Error::from(e)),
            },
            Err(e) => last_err = Some(anyhow::Error::from(e)),
        }
        if attempt + 1 < attempts {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
    Err(last_err.unwrap_or_else(|| anyhow!("request to {url} failed")))
}
