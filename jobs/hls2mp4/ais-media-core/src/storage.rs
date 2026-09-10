//! Cloud-native output target backed by the Apache Arrow `object_store` engine.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Result};
use object_store::aws::AmazonS3Builder;
use object_store::buffered::BufWriter;
use object_store::gcp::GoogleCloudStorageBuilder;
use object_store::local::LocalFileSystem;
use object_store::signer::Signer;
use object_store::{path::Path as StorePath, Attribute, AttributeValue, Attributes, ObjectStore};
use reqwest::Method;
use url::Url;

/// Parses an `s3://` / `gs://` / `file://` URI into an `object_store` backend
/// plus the key path within it. Credentials resolve from the environment
/// (`*_from_env`), which covers Cloud Run's metadata server and ECS task roles.
pub fn parse_store(uri: &str) -> Result<(Arc<dyn ObjectStore>, StorePath)> {
    let url = Url::parse(uri)?;
    let path = StorePath::from(url.path().trim_start_matches('/'));

    let store: Arc<dyn ObjectStore> = match url.scheme() {
        "s3" => {
            let bucket = url
                .host_str()
                .ok_or_else(|| anyhow!("Missing bucket in S3 URL"))?;
            Arc::new(
                AmazonS3Builder::from_env()
                    .with_bucket_name(bucket)
                    .build()?,
            )
        }
        "gs" | "gcs" => {
            let bucket = url
                .host_str()
                .ok_or_else(|| anyhow!("Missing bucket in GCS URL"))?;
            Arc::new(
                GoogleCloudStorageBuilder::from_env()
                    .with_bucket_name(bucket)
                    .build()?,
            )
        }
        "file" => Arc::new(LocalFileSystem::new_with_prefix("/")?),
        other => {
            return Err(anyhow!(
                "Unsupported scheme '{other}'. Use s3://, gs://, or file://"
            ))
        }
    };
    Ok((store, path))
}

/// The `Content-Type` a published object should be served with, chosen from its
/// extension, or `None` for an extension we do not publish.
///
/// WHY THIS MATTERS AT ALL. Object storage defaults an unspecified type to
/// `application/octet-stream`, and the type is fixed at WRITE time — a reader
/// cannot correct it later. For the media we publish that is not cosmetic: an
/// HLS playlist served as `octet-stream` is refused outright by strict clients
/// (Safari / iOS native HLS keys off the MIME type), so the deliverable is in the
/// bucket, fetchable, and still unplayable. `ffprobe` and `hls.js` sniff the body
/// and so hide the problem, which is what lets it reach a device before anyone
/// notices.
///
/// Returning `None` rather than guessing a default is deliberate: an unknown
/// extension keeps the store's own behaviour instead of being labelled something
/// plausible-but-wrong, which is harder to debug than an honest `octet-stream`.
fn content_type_for(key: &str) -> Option<&'static str> {
    // rsplit_once, so a dotted directory earlier in the key cannot be mistaken
    // for the extension, and a key with no dot at all yields None.
    let ext = key.rsplit_once('.')?.1.to_ascii_lowercase();
    Some(match ext.as_str() {
        // The playlist. `application/vnd.apple.mpegurl` is the registered type;
        // the older `application/x-mpegURL` is a de-facto alias, and the
        // registered one is what current clients expect.
        "m3u8" => "application/vnd.apple.mpegurl",
        "ts" => "video/mp2t",
        "mp4" => "video/mp4",
        // fMP4/CMAF media segment, as emitted for a CMAF source.
        "m4s" => "video/iso.segment",
        "jpg" | "jpeg" => "image/jpeg",
        "png" => "image/png",
        // The §7 status documents. Not consumer media, but they are read back
        // over HTTP by the notifier, and there is no reason to label JSON as an
        // opaque blob.
        "json" => "application/json",
        _ => return None,
    })
}

/// Whether the backend behind `uri` accepts object attributes.
///
/// NOT a stylistic choice. `object_store`'s LocalFileSystem rejects any write
/// carrying attributes with "Operation not yet implemented" — a plain filesystem
/// has nowhere to keep a Content-Type — so asking for one on a `file://`
/// destination fails the WRITE, not just the metadata.
///
/// That failure mode is why this is a hard gate rather than a best-effort
/// attempt: the status-document writer treats a failed write as a WARNING and
/// carries on (see live-hls2mp4 `StatusWriter::write`), so a `file://` run would
/// not have crashed — it would have quietly stopped producing status documents
/// while reporting success. A local run must behave like a cloud run.
fn supports_attributes(uri: &str) -> bool {
    match Url::parse(uri) {
        Ok(url) => url.scheme() != "file",
        // An unparseable URI already fails in parse_store, so this helper does not
        // get to decide that error's outcome.
        Err(_) => true,
    }
}

/// Wraps a writer with the `Content-Type` for its key, when we know one and the
/// backend can store it.
///
/// Kept as one helper used by BOTH constructors below so the two cannot drift:
/// a type set on the per-segment path but not the single-object path would mean
/// the same file is served differently depending on which clipper wrote it.
fn typed_writer(store: Arc<dyn ObjectStore>, path: StorePath, typed: bool) -> BufWriter {
    let writer = BufWriter::new(store, path.clone());
    match content_type_for(path.as_ref()).filter(|_| typed) {
        Some(ct) => writer.with_attributes(Attributes::from_iter([(
            Attribute::ContentType,
            AttributeValue::from(ct),
        )])),
        None => writer,
    }
}

/// Opens a streaming multipart writer to a single output object at `uri`. Used
/// by `vod-hls2mp4`, which writes exactly one file at a known key.
pub fn single_object_writer(uri: &str) -> Result<BufWriter> {
    let (store, path) = parse_store(uri)?;
    Ok(typed_writer(store, path, supports_attributes(uri)))
}

/// A resolved output directory (`s3://`, `gs://`, or `file://`) that mints one
/// streaming writer per segment file and renames temp objects into place.
pub struct OutputTarget {
    store: Arc<dyn ObjectStore>,
    prefix: StorePath,
    /// Resolved once from the destination URI: `writer()` only sees a file name,
    /// so the scheme has to be remembered here rather than re-derived per call.
    typed: bool,
}

impl OutputTarget {
    /// Builds the target from an output *directory* URI. The path component is
    /// treated as a key prefix; individual segment file names are appended.
    pub fn from_uri(output_uri: &str) -> Result<Self> {
        let (store, prefix) = parse_store(output_uri)?;
        Ok(Self {
            store,
            prefix,
            typed: supports_attributes(output_uri),
        })
    }

    /// Opens a streaming multipart writer for a file within the target prefix.
    pub fn writer(&self, file_name: &str) -> BufWriter {
        let path = self.prefix.child(file_name);
        typed_writer(self.store.clone(), path, self.typed)
    }

    /// Renames a completed object within the target prefix. Used to promote a
    /// temp file to its final `<event>-<start>-<end>.mp4` name once the content
    /// window is known.
    pub async fn rename(&self, from_name: &str, to_name: &str) -> Result<()> {
        let from = self.prefix.child(from_name);
        let to = self.prefix.child(to_name);
        self.store.rename(&from, &to).await?;
        Ok(())
    }
}

/// FFmpeg-openable references to objects in storage, resolved by
/// [`ffmpeg_input_urls`]. `header` (e.g. `Authorization: Bearer …\r\n`), when
/// present, must be passed to FFmpeg via `-headers` for the URLs to work.
///
/// `sign_error` carries WHY signing was abandoned whenever `header` is set. It
/// exists because a consumer that cannot use the bearer-header fallback (the
/// concat demuxer — see `live-hls2mp4::restamp`) must be able to report the real
/// cause rather than the symptom. Without it the signing error is only visible
/// on the sub-case where the bearer token is *also* missing, which never happens
/// on Cloud Run, so the actual reason was silently discarded.
pub struct FfmpegInputs {
    pub urls: Vec<String>,
    pub header: Option<String>,
    pub sign_error: Option<String>,
}

/// Resolves each `file_name` under the output directory `output_dir_uri` into a
/// reference FFmpeg can open **without staging the media locally**:
///
/// - `s3://` — a time-limited **signed HTTPS GET URL**.
/// - `gs://` — a signed URL when the credential can sign; otherwise falls back
///   to the plain object URL plus a **bearer-token header**, and records why in
///   `sign_error`. Signing is NOT a given on Cloud Run: a metadata-server
///   credential has no private key, so `object_store` computes the V4 signature
///   by calling the IAM Credentials `projects/-/serviceAccounts/<sa>:signBlob`
///   API as itself, which needs `iam.serviceAccounts.signBlob` on its own
///   service account (`roles/iam.serviceAccountTokenCreator`; `roles/editor`
///   does NOT include it). Local `authorized_user` gcloud credentials cannot
///   sign either.
/// - `file://` — the plain local path (FFmpeg opens it directly; no signing).
///
/// Used by the live restamp step and the derivative (proxy/thumbnail)
/// generation to feed published objects back into FFmpeg from the destination
/// bucket rather than from container disk.
pub async fn ffmpeg_input_urls(
    output_dir_uri: &str,
    file_names: &[String],
    expires_in: Duration,
) -> Result<FfmpegInputs> {
    let url = Url::parse(output_dir_uri)?;
    let prefix = StorePath::from(url.path().trim_start_matches('/'));

    match url.scheme() {
        "s3" => {
            let bucket = url
                .host_str()
                .ok_or_else(|| anyhow!("Missing bucket in S3 URL"))?;
            let store = AmazonS3Builder::from_env()
                .with_bucket_name(bucket)
                .build()?;
            Ok(FfmpegInputs {
                urls: sign_get_urls(&store, &prefix, file_names, expires_in).await?,
                header: None,
                sign_error: None,
            })
        }
        "gs" | "gcs" => {
            let bucket = url
                .host_str()
                .ok_or_else(|| anyhow!("Missing bucket in GCS URL"))?;
            let store = GoogleCloudStorageBuilder::from_env()
                .with_bucket_name(bucket)
                .build()?;
            match sign_get_urls(&store, &prefix, file_names, expires_in).await {
                Ok(urls) => Ok(FfmpegInputs {
                    urls,
                    header: None,
                    sign_error: None,
                }),
                Err(sign_err) => {
                    // The credential cannot sign. Reads still work with the same
                    // bearer token uploads use — hand FFmpeg the plain object
                    // URLs plus an Authorization header.
                    //
                    // Log the signing error HERE, unconditionally. This is the
                    // path actually taken in production; the `map_err` below only
                    // fires when the bearer token is missing too, which on Cloud
                    // Run it never is, so anything reported only there is
                    // invisible. Downgrading a signing failure to bearer auth is
                    // recoverable for a single `-i` input but NOT for the concat
                    // demuxer, so the reason has to survive to the operator.
                    let sign_error = format!("{sign_err:#}");
                    eprintln!(
                        "-> WARNING: GCS URL signing failed for gs://{bucket}/{prefix} \
                         ({sign_error}); falling back to plain object URLs with a bearer \
                         token. This works for a direct FFmpeg input but NOT for the \
                         concat demuxer."
                    );
                    let cred = store.credentials().get_credential().await.map_err(|e| {
                        anyhow!("GCS URL signing failed ({sign_error}) and no bearer token is available: {e}")
                    })?;
                    let urls = file_names
                        .iter()
                        .map(|f| {
                            format!(
                                "https://storage.googleapis.com/{bucket}/{}",
                                prefix.child(f.as_str())
                            )
                        })
                        .collect();
                    Ok(FfmpegInputs {
                        urls,
                        header: Some(format!("Authorization: Bearer {}\r\n", cred.bearer)),
                        sign_error: Some(sign_error),
                    })
                }
            }
        }
        "file" => {
            // Explicit `file:` scheme so the concat demuxer treats each entry as
            // an absolute URL. Without a scheme, entries in a script read from a
            // pipe get resolved relative to `pipe:` (-> `pipe:/path`, which fails).
            let base = url.path().trim_end_matches('/');
            Ok(FfmpegInputs {
                urls: file_names
                    .iter()
                    .map(|f| format!("file:{base}/{f}"))
                    .collect(),
                header: None,
                sign_error: None,
            })
        }
        other => Err(anyhow!(
            "Unsupported scheme '{other}' for FFmpeg input; use s3://, gs://, or file://"
        )),
    }
}

/// Signs a GET URL per object under `prefix`.
async fn sign_get_urls<S: Signer>(
    store: &S,
    prefix: &StorePath,
    file_names: &[String],
    expires_in: Duration,
) -> Result<Vec<String>> {
    let mut urls = Vec::with_capacity(file_names.len());
    for name in file_names {
        let path = prefix.child(name.as_str());
        let signed = store.signed_url(Method::GET, &path, expires_in).await?;
        urls.push(signed.to_string());
    }
    Ok(urls)
}

/// Key used by [`probe_signing`]. Never written or read — only signed — so it
/// does not have to exist, and the leading dot keeps it out of the way if it
/// ever shows up in a signed URL in a log.
const SIGNING_PROBE_KEY: &str = ".signing-probe";

/// Signed-URL TTL for [`probe_signing`]. The URL is discarded immediately; the
/// value only has to be inside GCS's 7-day ceiling.
const SIGNING_PROBE_TTL: Duration = Duration::from_secs(60);

/// Whether a destination's credential can mint signed GET URLs, as established
/// by [`probe_signing`].
#[derive(Debug)]
pub enum SigningCapability {
    /// The scheme has no notion of signing (`file://`) — nothing to check.
    NotRequired,
    /// The credential signed a URL successfully.
    Available,
    /// Signing failed. The payload is the underlying error, verbatim.
    Unavailable(String),
}

/// Probes whether the credential behind `dir_uri` can mint signed GET URLs, so a
/// run that will NEED signing can find out during setup rather than after the
/// capture window has been spent (see the `signing_gate` in `live-hls2mp4`).
///
/// The probe has no side effects and does not depend on the destination being
/// populated — which matters, because at setup time it is empty:
///
/// * nothing is written, and no object is read. `object_store`'s signers build
///   the signature from the URL, the credential and the clock alone (0.10.2
///   `gcp::GoogleCloudStorage::signed_url` -> `GCSAuthorizer::sign`; the only
///   network call on that path is the IAM `signBlob` request for credentials
///   with no private key). No HEAD or GET is issued, so [`SIGNING_PROBE_KEY`]
///   does not have to exist.
/// * cost is at most that one `signBlob` round trip — and zero when the
///   credential holds a private key, or for S3, where SigV4 is computed locally.
///
/// A failure is reported, never propagated: the caller decides whether missing
/// signing is fatal for the run it is about to do.
pub async fn probe_signing(dir_uri: &str) -> SigningCapability {
    match Url::parse(dir_uri).map(|u| u.scheme() == "file") {
        Ok(true) => return SigningCapability::NotRequired,
        Ok(false) => {}
        Err(e) => return SigningCapability::Unavailable(format!("unusable URI {dir_uri}: {e}")),
    }
    // Deliberately routed through `ffmpeg_input_urls` rather than calling
    // `sign_get_urls` directly: the probe must answer the exact question the
    // consumers ask ("did this resolve to a SIGNED url?"), including the
    // fallback-to-bearer behaviour, and must not drift from it.
    let probe = [SIGNING_PROBE_KEY.to_string()];
    match ffmpeg_input_urls(dir_uri, &probe, SIGNING_PROBE_TTL).await {
        Ok(inputs) => match inputs.sign_error {
            Some(cause) => SigningCapability::Unavailable(cause),
            None => SigningCapability::Available,
        },
        Err(e) => SigningCapability::Unavailable(format!("{e:#}")),
    }
}

/// Size in bytes of `file_name` under the output directory `output_dir_uri`, via
/// a single metadata lookup (HEAD) — the object is never read back. Used to
/// report published file sizes.
pub async fn object_size(output_dir_uri: &str, file_name: &str) -> Result<u64> {
    let (store, prefix) = parse_store(output_dir_uri)?;
    let meta = store.head(&prefix.child(file_name)).await?;
    Ok(meta.size as u64)
}

/// Deletes objects `file_names` under the output directory `output_dir_uri`.
/// Used to clean up the intermediate content-run objects after a restamp merge.
pub async fn delete_objects(output_dir_uri: &str, file_names: &[String]) -> Result<()> {
    let (store, prefix) = parse_store(output_dir_uri)?;
    for name in file_names {
        let path = prefix.child(name.as_str());
        store.delete(&path).await?;
    }
    Ok(())
}

/// Resolves the per-event folder under a destination base: `<base>/<event_id>`,
/// unless the base ALREADY ends in that event id, in which case it is returned
/// unchanged (minus any trailing slash).
///
/// WHY IDEMPOTENT RATHER THAN AN UNCONDITIONAL APPEND. Two kinds of caller now
/// exist and both must land in exactly one folder per event:
///
///   * a caller that passes a bare bucket base and relies on this nesting —
///     the vod-hls2mp4 contract and every hand-run invocation;
///   * the clipping-scheduler, which passes `<bucket>/<job_id>` explicitly, so
///     the orchestrator owns the layout and a later change to the nesting rule
///     here cannot silently move where a job's artifacts appear.
///
/// An unconditional append would give the second caller `<bucket>/<job_id>/<job_id>/`.
/// Matching on the trailing component is exact, not a substring test, so a job id
/// that merely appears earlier in the path is still appended correctly.
pub fn event_folder(base: &str, event_id: &str) -> String {
    let base = base.trim_end_matches('/');
    // Only the object PATH can name the event folder, never the bucket/host, so
    // the `<scheme>://<authority>` prefix is dropped before the test: a base of
    // `gs://<event_id>` is a *bucket* that still needs the folder appended.
    let path = base.split_once("://").map_or(base, |(_, rest)| rest);
    if path
        .rsplit_once('/')
        .is_some_and(|(_, last)| last == event_id)
    {
        return base.to_string();
    }
    format!("{base}/{event_id}")
}

#[cfg(test)]
mod event_folder_tests {
    use super::event_folder;

    #[test]
    fn a_bare_base_gets_the_event_folder_appended() {
        assert_eq!(
            event_folder("gs://bucket/prefix", "job-1"),
            "gs://bucket/prefix/job-1"
        );
        // A trailing slash on the base must not double the separator.
        assert_eq!(
            event_folder("gs://bucket/prefix/", "job-1"),
            "gs://bucket/prefix/job-1"
        );
        assert_eq!(event_folder("gs://bucket", "job-1"), "gs://bucket/job-1");
    }

    #[test]
    fn a_base_that_already_names_the_event_is_left_alone() {
        // What clipping-scheduler sends: the orchestrator owns the folder, so
        // appending again would bury the run one level deeper.
        assert_eq!(
            event_folder("gs://bucket/job-1", "job-1"),
            "gs://bucket/job-1"
        );
        assert_eq!(
            event_folder("gs://bucket/job-1/", "job-1"),
            "gs://bucket/job-1"
        );
    }

    #[test]
    fn only_the_trailing_component_counts_as_the_event_folder() {
        // The id appears in the path but is not the last component: still appended.
        assert_eq!(
            event_folder("gs://bucket/job-1/runs", "job-1"),
            "gs://bucket/job-1/runs/job-1"
        );
        // A component that merely starts with the id is not a match.
        assert_eq!(
            event_folder("gs://bucket/job-12", "job-1"),
            "gs://bucket/job-12/job-1"
        );
        // The bucket is not a path component: an id that happens to equal the
        // bucket name must still get its own folder, not publish at the root.
        assert_eq!(event_folder("gs://job-1", "job-1"), "gs://job-1/job-1");
        // A local base behaves the same with and without the file: scheme.
        assert_eq!(
            event_folder("file:///tmp/out/job-1", "job-1"),
            "file:///tmp/out/job-1"
        );
        assert_eq!(event_folder("/tmp/out", "job-1"), "/tmp/out/job-1");
    }

    #[test]
    fn the_scheduler_vod_call_shape_does_not_double_the_job_folder() {
        // Regression: clipping-scheduler sends AIS_PREVIEW_URI as
        // `<bucket>/<job_id>` AND EVENT_ID as `<job_id>`. vod-hls2mp4 appended
        // the sub-folder unconditionally, so every derivative landed in
        // `<bucket>/<job_id>/<job_id>/`. Live was unaffected because it already
        // routed through this helper; sharing it is what closed the gap.
        let preview_uri = "gs://live-segments/job-42";
        let event_id = "job-42";
        assert_eq!(
            event_folder(preview_uri, event_id),
            "gs://live-segments/job-42",
            "the scheduler already owns the per-job folder"
        );
        // A hand-run invocation passing only the bucket base still gets nested.
        assert_eq!(
            event_folder("gs://live-segments", event_id),
            "gs://live-segments/job-42"
        );
    }
}

#[cfg(test)]
mod content_type_tests {
    use super::{content_type_for, supports_attributes};

    // The published deliverables. A wrong type here is invisible to ffprobe and
    // to hls.js, so these assertions are the only thing standing between a
    // mislabelled playlist and a device that refuses to play it.
    #[test]
    fn published_media_gets_its_registered_type() {
        for (key, want) in [
            ("ev/ev-a-b.m3u8", "application/vnd.apple.mpegurl"),
            ("ev/ev-a-b_00000.ts", "video/mp2t"),
            ("ev/ev-a-b.mp4", "video/mp4"),
            ("ev/ev-a-b_00000.m4s", "video/iso.segment"),
            ("ev/ev-a-b.jpg", "image/jpeg"),
            ("ev/ev-a-b.jpeg", "image/jpeg"),
            ("ev/ev-a-b.png", "image/png"),
        ] {
            assert_eq!(content_type_for(key), Some(want), "key {key}");
        }
    }

    #[test]
    fn the_extension_is_case_insensitive() {
        // Nothing we write upper-cases an extension today, but a caller that
        // did would otherwise silently fall through to octet-stream.
        assert_eq!(
            content_type_for("ev/A.M3U8"),
            Some("application/vnd.apple.mpegurl")
        );
        assert_eq!(content_type_for("ev/A.TS"), Some("video/mp2t"));
    }

    #[test]
    fn an_unknown_or_absent_extension_is_left_to_the_store() {
        // None means "do not set a type", which is different from setting
        // octet-stream ourselves: the store's default stays visible as a default.
        assert_eq!(content_type_for("ev/manifest"), None);
        assert_eq!(content_type_for("ev/data.bin"), None);
        assert_eq!(content_type_for(""), None);
    }

    // The recorder streams every mezzanine run to `<name>.part.<ext>` and renames
    // it into place once the window is known. Object rename is copy+delete, which
    // CARRIES THE SOURCE'S CONTENT TYPE — so the temp key is what decides the
    // final object's type, and it has to resolve correctly despite the `.part.`
    // infix. Taking the last dot is what makes that work.
    #[test]
    fn the_recorder_temp_key_still_resolves_its_real_extension() {
        assert_eq!(
            content_type_for("ev/ev-20260807T061119Z.part.ts"),
            Some("video/mp2t")
        );
        assert_eq!(
            content_type_for("ev/ev-20260807T061119Z.part.mp4"),
            Some("video/mp4")
        );
    }

    // Regression: setting attributes on a file:// destination fails the WRITE
    // ("Operation not yet implemented"), and the status-document writer downgrades
    // a failed write to a warning — so without this gate a local run silently
    // stops emitting status documents instead of failing.
    #[test]
    fn only_cloud_backends_are_asked_to_store_a_content_type() {
        assert!(supports_attributes("gs://bucket/prefix"));
        assert!(supports_attributes("s3://bucket/prefix"));
        assert!(!supports_attributes("file:///tmp/out"));
    }

    #[test]
    fn a_dotted_folder_is_not_mistaken_for_the_extension() {
        // rsplit_once takes the LAST dot, so the `.v2` folder cannot win over
        // the real extension — and a dotted folder with an extensionless file
        // must not inherit the folder's suffix.
        assert_eq!(
            content_type_for("bucket/ev.v2/clip.m3u8"),
            Some("application/vnd.apple.mpegurl")
        );
        assert_eq!(content_type_for("bucket/ev.m3u8/clip"), None);
    }
}