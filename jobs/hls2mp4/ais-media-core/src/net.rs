//! Retrying HTTP fetch shared by the HLS tools.

use std::time::Duration;

use anyhow::{anyhow, Result};
use reqwest::Client;

/// Fetches a URL body, retrying transient failures a few times with a short
/// backoff. Non-2xx responses are treated as errors (via `error_for_status`)
/// and retried, so a transient 5xx or a truncated body neither aborts the whole
/// download nor silently writes an error page into the output.
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
