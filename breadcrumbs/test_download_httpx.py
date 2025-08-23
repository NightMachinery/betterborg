#!/usr/bin/env python3
"""
Quick diagnostic to compare httpx streaming downloads against wget/curl behavior.

Usage:
  python scripts/test_download_httpx.py [URL]

It will try multiple header/transport variants and report status, headers,
and whether streaming bytes succeeded. It only downloads the first ~1 MB to
avoid large transfers.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

import httpx


TEST_URL_DEFAULT = (
    "https://api.substack.com/feed/podcast/171551669/"
    "39d6b393a94286a74bcae9af3829a01d.mp3"
)


async def try_variant(
    url: str,
    *,
    label: str,
    headers: Optional[Dict[str, str]] = None,
    http2: Optional[bool] = None,
) -> None:
    timeout = httpx.Timeout(60.0)
    client_kwargs = {"timeout": timeout, "follow_redirects": True}
    if http2 is not None:
        client_kwargs["http2"] = http2

    print(f"\n=== Variant: {label} ===")
    print(f"http2={client_kwargs.get('http2', 'default')}")
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url, headers=headers) as response:
                final_url = str(response.url)
                status = response.status_code
                content_type = response.headers.get("content-type", "").split(";")[0]
                server = response.headers.get("server", "")
                accept_ranges = response.headers.get("accept-ranges", "")
                content_length = response.headers.get("content-length", "")
                print(f"Status: {status}")
                print(f"Final URL: {final_url}")
                print(f"Content-Type: {content_type}")
                print(f"Server: {server}")
                print(f"Accept-Ranges: {accept_ranges}")
                print(f"Content-Length: {content_length}")
                if status not in (200, 206):
                    text = await response.aread()
                    print(f"Non-OK response body (first 512 bytes): {text[:512]!r}")
                    return

                # Stream only the first ~1MB to validate transfer
                tmpdir = Path(tempfile.gettempdir())
                out = tmpdir / f"test_httpx_partial_{label.replace(' ', '_')}.bin"
                total = 0
                limit = 1 * 1024 * 1024
                with open(out, "wb") as f:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        total += len(chunk)
                        if total >= limit:
                            break
                print(f"Wrote ~{total} bytes to {out}")
    except Exception as e:
        print(f"Exception during variant '{label}': {type(e).__name__}: {e}")


async def main(url: str) -> None:
    print(f"Testing URL: {url}")
    # Baseline
    await try_variant(url, label="baseline (no headers)")

    # Emulate wget
    await try_variant(
        url,
        label="wget UA",
        headers={
            "User-Agent": "Wget/1.21.3 (linux-gnu)",
            "Accept": "*/*",
            "Connection": "Keep-Alive",
        },
    )

    # Emulate common browser
    await try_variant(
        url,
        label="browser UA",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )

    # Force Range request
    await try_variant(
        url,
        label="range + browser UA",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Range": "bytes=0-",
        },
    )

    # Try forcing HTTP/1.1
    await try_variant(
        url,
        label="http1.1 forced",
        headers={"User-Agent": "Wget/1.21.3 (linux-gnu)", "Accept": "*/*"},
        http2=False,
    )

    # Try forcing HTTP/2
    await try_variant(
        url,
        label="http2 forced",
        headers={"User-Agent": "Wget/1.21.3 (linux-gnu)", "Accept": "*/*"},
        http2=True,
    )

    # Try HEAD preflight to populate cookies, then GET
    print("\n=== Variant: HEAD preflight then GET (same client) ===")
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r_head = await client.head(
                url,
                headers={
                    "User-Agent": "curl/8.4.0",
                    "Accept": "*/*",
                },
            )
            print(
                "HEAD ->",
                r_head.status_code,
                r_head.headers.get("location", ""),
                "cookies:",
                str(client.cookies),
            )
            async with client.stream(
                "GET",
                url,
                headers={
                    "User-Agent": "curl/8.4.0",
                    "Accept": "*/*",
                },
            ) as resp:
                print(
                    "GET after HEAD ->",
                    resp.status_code,
                    resp.headers.get("content-type"),
                )
                if resp.status_code in (200, 206):
                    total = 0
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > 128 * 1024:
                            break
                    print(f"Streamed ~{total} bytes after HEAD preflight")
                else:
                    body = await resp.aread()
                    print(f"Body (first 256): {body[:256]!r}")
    except Exception as e:
        print("Exception during HEAD+GET:", e)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else TEST_URL_DEFAULT
    asyncio.run(main(url))
