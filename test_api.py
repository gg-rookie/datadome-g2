"""datadome-g2 API 测试。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def _load_env_defaults() -> tuple[str, str]:
    base = "http://127.0.0.1:51051"
    key = ""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "PORT":
                base = f"http://127.0.0.1:{v}"
            elif k == "API_KEY":
                key = v
    return base, key


def run_case(session: requests.Session, base: str, key: str, name: str, method: str, path: str, **kwargs) -> dict:
    print(f"\n--- {name} ---", flush=True)
    params = kwargs.pop("params", {})
    if key:
        params.setdefault("key", key)
    t0 = time.monotonic()
    try:
        r = session.request(method, f"{base}{path}", params=params, **kwargs)
        elapsed = round(time.monotonic() - t0, 2)
        body = r.json()
        ok = r.status_code == 200 and body.get("code") == 0
        print(f"[{'PASS' if ok else 'FAIL'}] http={r.status_code} code={body.get('code')} ({elapsed}s)", flush=True)
        return {"name": name, "status": "PASS" if ok else "FAIL", "elapsed": elapsed, "body": body}
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return {"name": name, "status": "ERROR", "detail": str(e)}


def main():
    default_base, default_key = _load_env_defaults()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=default_base)
    parser.add_argument("--key", default=default_key)
    parser.add_argument("--acquire", action="store_true", help="测 POST /cookie/acquire（开浏览器）")
    parser.add_argument("--json-out", default="test_api_results.json")
    args = parser.parse_args()

    session = requests.Session()
    results = [
        run_case(session, args.base, args.key, "health", "GET", "/health"),
        run_case(session, args.base, args.key, "config", "GET", "/api/datadome/v1/config"),
        run_case(session, args.base, args.key, "cookie_latest", "GET", "/api/datadome/v1/cookie"),
    ]

    if args.acquire:
        timeout = 200
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("COOKIE_TIMEOUT="):
                    timeout = int(line.split("=", 1)[1].strip()) + 60
        results.append(run_case(
            session, args.base, args.key, "acquire",
            "POST", "/api/datadome/v1/cookie/acquire",
            timeout=timeout,
        ))

    passed = sum(1 for r in results if r.get("status") == "PASS")
    slim = [{"name": r["name"], "status": r["status"], "elapsed": r.get("elapsed")} for r in results]
    (ROOT / args.json_out).write_text(
        json.dumps({"passed": passed, "total": len(results), "results": slim}, indent=2),
        encoding="utf-8",
    )
    print(f"\n=== DONE {passed}/{len(results)} PASS ===", flush=True)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
