"""验证 Redis datadome:g2:ck:pool 里每条 cookie 对 G2 是否有效。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services.cookie_store import pool_key, redis_client

G2_URL = "https://www.g2.com/search/products?query=slack&order=popular"


def probe(item: dict, index: int) -> dict:
    wid = item.get("worker_id", "?")
    prefix = (item.get("cookie") or "")[:36]
    updated = item.get("updated_at", "?")
    headers = {
        "Cookie": item["cookie"],
        "User-Agent": item.get("user_agent", ""),
    }
    t0 = time.monotonic()
    try:
        r = cffi_requests.get(G2_URL, headers=headers, timeout=30, impersonate="chrome")
        ok = r.status_code == 200 and len(r.text) > 20_000
        return {
            "index": index,
            "worker_id": wid,
            "updated_at": updated,
            "ok": ok,
            "status": r.status_code,
            "bytes": len(r.text),
            "elapsed": round(time.monotonic() - t0, 2),
            "cookie_prefix": prefix,
        }
    except Exception as e:
        return {
            "index": index,
            "worker_id": wid,
            "updated_at": updated,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "cookie_prefix": prefix,
        }


def main() -> None:
    raw = redis_client().lrange(pool_key(), 0, -1)
    pool = [json.loads(x) for x in raw]
    print(f"pool key={pool_key()} count={len(pool)}", flush=True)
    if not pool:
        print("pool 为空", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, item in enumerate(pool, 1):
        row = probe(item, i)
        results.append(row)
        tag = "OK" if row.get("ok") else "FAIL"
        print(
            f"  [{tag}] #{i:2d} worker={row['worker_id']} "
            f"http={row.get('status', '-')} bytes={row.get('bytes', 0)} "
            f"updated={row['updated_at']} {row['cookie_prefix']}...",
            flush=True,
        )

    ok_n = sum(1 for r in results if r.get("ok"))
    rate = round(ok_n / len(pool) * 100, 1)
    summary = {
        "pool_key": pool_key(),
        "total": len(pool),
        "ok": ok_n,
        "fail": len(pool) - ok_n,
        "success_rate_pct": rate,
        "results": results,
    }
    out = ROOT / "test_pool_validity_results.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== {ok_n}/{len(pool)} 有效 ({rate}%) ===", flush=True)
    print(f"详情: {out}", flush=True)
    sys.exit(0 if ok_n == len(pool) else 1)


if __name__ == "__main__":
    main()
