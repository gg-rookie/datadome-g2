"""Validate acquired G2 cookie headers before putting them in Redis."""
from __future__ import annotations

import time

from curl_cffi import requests as cffi_requests

from config import settings


def validate_cookie(result: dict, url: str | None = None) -> dict:
    target = url or settings.target_url
    headers = {
        "Cookie": result.get("cookie", ""),
        "User-Agent": result.get("user_agent", ""),
    }
    t0 = time.monotonic()
    try:
        r = cffi_requests.get(
            target,
            headers=headers,
            timeout=30,
            impersonate="chrome",
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
