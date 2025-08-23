# Substack audio download: httpx returns 403 while curl/wget succeed

## Summary
- Problem: `_download_audio_from_url` (httpx) fails for `https://api.substack.com/feed/podcast/171551669/39d6b393a94286a74bcae9af3829a01d.mp3`, while curl/wget can download it.
- Root cause: Cloudflare Bot Management blocks httpx traffic (403 HTML error page), likely based on TLS/JA3 or client heuristics. curl/wget are allowed and receive the redirect to the signed CDN URL.

## Observations
- httpx GET to the URL consistently returns `HTTP 403` with `Server: cloudflare` and `Content-Type: text/html` (Substack error page).
- Tweaks attempted: `User-Agent` variations (wget/browser/curl), `Range: bytes=0-`, forcing HTTP/1.1 — still 403.
- HEAD→GET with same client and cookies (including `__cf_bm`) — still 403.
- curl succeeds: receives `307` from `api.substack.com` to a signed `substackcdn.com/...transcoded.mp3?...` URL, then `200` from CloudFront/S3 with the MP3.
- Forcing httpx HTTP/2 would require installing `h2` (`pip install httpx[http2]`); not tested here due to missing dependency.

## Reproduction
1) httpx diagnostic (added):
   - Script: `scripts/test_download_httpx.py`
   - Run: `python scripts/test_download_httpx.py`
   - Output shows multiple variants; all httpx attempts return 403.

2) curl (works):
   - `curl -sSL -D - -o /dev/null "https://api.substack.com/feed/podcast/171551669/39d6b393a94286a74bcae9af3829a01d.mp3"`
   - Shows `HTTP/2 307` with `Location: https://substackcdn.com/...transcoded.mp3?...` followed by `HTTP/2 200` from CloudFront/AmazonS3.

## Likely Cause
- Cloudflare bot rules reject httpx’s network fingerprint from this environment. Headers alone do not bypass; the decision appears to consider TLS handshake/client characteristics beyond simple headers.

## Workarounds
- Curl/wget fallback: If httpx gets `403` with `Server: cloudflare`, shell out to `curl -sSL` (or `wget -qO-`) to write the file to a temp path.
- Cloudflare-aware session: Use `cloudscraper`/`cfscrape` to acquire valid cookies and retry with that session. This adds a dependency and may be brittle as CF rules change.
- yt-dlp fallback: `yt-dlp -o` often handles CF/CDN flows reliably.
- Try http2: `pip install httpx[http2]` and retest; sometimes different ALPN/TLS fingerprints help, but it’s not guaranteed.

## Recommendation
- Implement a conservative fallback: keep current httpx logic, and on `403` with `Server: cloudflare`, retry download via `curl -sSL --fail --location` into the temp file. Only trigger the fallback for these specific conditions.

