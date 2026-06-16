"""Validate every cookie payload in the Redis pool against TARGET_URL."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services.cookie_store import pool_key, redis_client
from services.cookie_validator import validate_cookie


def probe(item: dict, index: int) -> dict:
    result = validate_cookie(item)
    return {
        "index": index,
        "worker_id": item.get("worker_id", "?"),
        "updated_at": item.get("updated_at", "?"),
        "ok": result.get("ok", False),
        "status": result.get("status"),
        "bytes": result.get("bytes", 0),
        "elapsed": result.get("elapsed"),
        "url": result.get("url"),
        "error": result.get("error"),
        "cookie_prefix": (item.get("cookie") or "")[:36],
    }


def main() -> None:
    raw = redis_client().lrange(pool_key(), 0, -1)
    pool = [json.loads(x) for x in raw]
    print(f"pool key={pool_key()} count={len(pool)}", flush=True)
    if not pool:
        print("pool is empty", file=sys.stderr)
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
    print(f"\n=== {ok_n}/{len(pool)} valid ({rate}%) ===", flush=True)
    print(f"details: {out}", flush=True)
    sys.exit(0 if ok_n == len(pool) else 1)


if __name__ == "__main__":
    main()
