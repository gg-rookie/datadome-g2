"""Validate acquired G2 cookie headers before putting them in Redis.

Primary validation now happens *inside the browser* (see
``_browser_validate`` in ``browser.py``).  The browser reloads the page in
the same context that acquired the cookie, guaranteeing TLS-fingerprint
consistency.

This module provides a fallback curl_cffi validation for offline checks
(e.g. ``test_pool_validity.py``).  Based on empirical data (2026-06-16),
``impersonate=firefox`` with the original browser User-Agent yields a ~50%
success rate — better than any other strategy.
"""
from __future__ import annotations

import logging
import time

from curl_cffi import requests as cffi_requests

from config import settings

logger = logging.getLogger("datadome.validator")


def validate_cookie(result: dict, url: str | None = None) -> dict:
    """Replay a cookie against the target URL via curl_cffi.

    NOTE: this is unreliable (~50% success) because DataDome binds cookies
    to TLS fingerprints.  Prefer the in-browser validation flow in
    ``refill_worker.py`` for production use.
    """
    target = url or settings.target_url
    cookie = result.get("cookie", "")
    user_agent = result.get("user_agent", "")

    # Strategy from data: impersonate=firefox + original UA works best.
    headers: dict[str, str] = {
        "Cookie": cookie,
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    t0 = time.monotonic()
    try:
        r = cffi_requests.get(
            target,
            headers=headers,
            timeout=30,
            impersonate="firefox",
        )
        body_len = len(r.text or "")
        ok_ = r.status_code == 200 and body_len > 20_000
        return {
            "ok": ok_,
            "status": r.status_code,
            "bytes": body_len,
            "elapsed": round(time.monotonic() - t0, 2),
            "url": str(r.url),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "bytes": 0,
            "elapsed": round(time.monotonic() - t0, 2),
            "error": f"{type(e).__name__}: {e}",
            "url": target,
        }
