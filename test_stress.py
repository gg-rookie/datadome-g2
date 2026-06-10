"""压测：并发 acquire + G2 下游成功率 + 滑块统计。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
DEFAULT_G2_URL = "https://www.g2.com/search/products?query=slack&order=popular"


def _load_defaults() -> tuple[str, str, int]:
    base = "http://127.0.0.1:51051"
    key = ""
    timeout = 180
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "PORT":
                base = f"http://127.0.0.1:{v}"
            elif k == "API_KEY":
                key = v
            elif k == "COOKIE_TIMEOUT":
                timeout = int(v) + 90
    return base, key, timeout


def _require_health(base: str) -> dict:
    url = f"{base}/health"
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"health 请求失败: {url}", flush=True)
        print(f"  {type(e).__name__}: {e}", flush=True)
        print("请先启动服务: .venv\\Scripts\\python.exe app.py", flush=True)
        sys.exit(2)
    try:
        body = r.json()
    except ValueError:
        print(f"health 返回非 JSON (http={r.status_code}):", flush=True)
        print(r.text[:500], flush=True)
        sys.exit(2)
    if "data" not in body:
        print(f"health 响应无 data 字段 (http={r.status_code}): {body}", flush=True)
        print("常见原因: 1) datadome-service 未启动  2) PORT 不对  3) 该端口上是别的程序", flush=True)
        sys.exit(2)
    if body.get("code") != 0:
        print(f"health 失败: {body}", flush=True)
        sys.exit(2)
    return body["data"]


def probe_g2(ck: dict, url: str) -> dict:
    headers = {"Cookie": ck["cookie"], "User-Agent": ck["user_agent"]}
    t0 = time.monotonic()
    try:
        r = cffi_requests.get(url, headers=headers, timeout=30, impersonate="chrome")
        ok = r.status_code == 200 and len(r.text) > 20_000
        return {
            "g2_ok": ok,
            "g2_status": r.status_code,
            "g2_bytes": len(r.text),
            "g2_elapsed": round(time.monotonic() - t0, 2),
        }
    except Exception as e:
        return {
            "g2_ok": False,
            "g2_status": 0,
            "g2_bytes": 0,
            "g2_elapsed": round(time.monotonic() - t0, 2),
            "g2_error": f"{type(e).__name__}: {e}",
        }


def worker_task(
    base: str, key: str, idx: int, timeout: int, g2_url: str,
) -> dict:
    row: dict = {"thread": idx, "acquire_ok": False}
    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{base}/api/datadome/v1/cookie/acquire",
            params={"key": key},
            timeout=timeout,
        )
        row["acquire_http"] = r.status_code
        body = r.json()
        row["acquire_code"] = body.get("code")
        data = body.get("data") or {}
        row["acquire_ok"] = r.status_code == 200 and body.get("code") == 0
        row["worker_id"] = data.get("worker_id")
        row["slider_attempts"] = data.get("slider_attempts", 0)
        row["acquire_elapsed"] = round(time.monotonic() - t0, 2)

        if not row["acquire_ok"]:
            row["acquire_error"] = body.get("message")
            row["g2_ok"] = False
            return row

        row["cookie_prefix"] = str(data.get("cookie", ""))[:32]
        g2 = probe_g2(data, g2_url)
        row.update(g2)
        row["total_elapsed"] = round(time.monotonic() - t0, 2)
        return row
    except Exception as e:
        row["acquire_error"] = f"{type(e).__name__}: {e}"
        row["acquire_elapsed"] = round(time.monotonic() - t0, 2)
        row["g2_ok"] = False
        return row


def _slider_summary(results: list[dict]) -> dict:
    c = Counter(r.get("slider_attempts", -1) for r in results if r.get("acquire_ok"))
    fail_msgs = [r.get("acquire_error", "") for r in results if not r.get("acquire_ok")]
    slider_fail = sum(1 for m in fail_msgs if m and "滑块" in m)
    return {
        "no_slider": c.get(0, 0),
        "slider_1": c.get(1, 0),
        "slider_2": c.get(2, 0),
        "slider_other": sum(v for k, v in c.items() if k not in (0, 1, 2)),
        "acquire_slider_fail": slider_fail,
    }


def main() -> None:
    default_base, default_key, default_timeout = _load_defaults()
    parser = argparse.ArgumentParser(description="acquire 压测 + G2 下游 + 滑块统计")
    parser.add_argument("--base", default=default_base)
    parser.add_argument("--key", default=default_key)
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=1, help="压测轮数（每轮 threads 个并发）")
    parser.add_argument("--timeout", type=int, default=default_timeout)
    parser.add_argument("--g2-url", default=DEFAULT_G2_URL)
    parser.add_argument("--json-out", default="test_stress_results.json")
    args = parser.parse_args()

    total_tasks = args.threads * args.rounds
    print(
        f"=== 压测 acquire+G2  threads={args.threads} rounds={args.rounds} "
        f"total={total_tasks} ===",
        flush=True,
    )
    health = _require_health(args.base)
    redis = health.get("redis", {})
    print(
        f"mode={health.get('mode')} pool={health.get('browser_pool_size')} "
        f"headless={health.get('headless')} "
        f"redis={redis.get('host')}:{redis.get('port')} pool_size={redis.get('pool_size')}",
        flush=True,
    )

    all_results: list[dict] = []
    wall_t0 = time.monotonic()
    for rnd in range(1, args.rounds + 1):
        if args.rounds > 1:
            print(f"\n--- round {rnd}/{args.rounds} ---", flush=True)
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futs = [
                pool.submit(
                    worker_task, args.base, args.key,
                    (rnd - 1) * args.threads + i + 1,
                    args.timeout, args.g2_url,
                )
                for i in range(args.threads)
            ]
            for fut in as_completed(futs):
                row = fut.result()
                all_results.append(row)
                a_tag = "OK" if row.get("acquire_ok") else "FAIL"
                g_tag = "OK" if row.get("g2_ok") else "FAIL" if row.get("acquire_ok") else "-"
                print(
                    f"  [acquire={a_tag} g2={g_tag}] thread={row['thread']} "
                    f"worker={row.get('worker_id')} slider={row.get('slider_attempts', '-')} "
                    f"acquire={row.get('acquire_elapsed')}s "
                    f"g2={row.get('g2_status', '-')} "
                    f"{row.get('acquire_error') or row.get('g2_error') or ''}",
                    flush=True,
                )

    wall = round(time.monotonic() - wall_t0, 2)
    all_results.sort(key=lambda x: x["thread"])

    acquire_ok = sum(1 for r in all_results if r.get("acquire_ok"))
    g2_ok = sum(1 for r in all_results if r.get("g2_ok"))
    acquire_times = [r["acquire_elapsed"] for r in all_results if r.get("acquire_ok")]
    slider = _slider_summary(all_results)

    summary = {
        "threads": args.threads,
        "rounds": args.rounds,
        "total": total_tasks,
        "acquire_ok": acquire_ok,
        "acquire_fail": total_tasks - acquire_ok,
        "acquire_success_rate_pct": round(acquire_ok / total_tasks * 100, 1) if total_tasks else 0,
        "g2_ok": g2_ok,
        "g2_fail": acquire_ok - g2_ok,
        "g2_success_rate_pct": round(g2_ok / acquire_ok * 100, 1) if acquire_ok else 0,
        "end_to_end_success_rate_pct": round(g2_ok / total_tasks * 100, 1) if total_tasks else 0,
        "wall_sec": wall,
        "acquire_avg_sec": round(sum(acquire_times) / len(acquire_times), 2) if acquire_times else None,
        "acquire_max_sec": max(acquire_times) if acquire_times else None,
        "acquire_min_sec": min(acquire_times) if acquire_times else None,
        "slider": slider,
        "results": all_results,
    }

    out = ROOT / args.json_out
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 汇总 ===", flush=True)
    print(
        f"acquire: {acquire_ok}/{total_tasks} ({summary['acquire_success_rate_pct']}%)  "
        f"wall={wall}s avg={summary['acquire_avg_sec']}s",
        flush=True,
    )
    print(
        f"G2下游:  {g2_ok}/{acquire_ok} ({summary['g2_success_rate_pct']}%)  "
        f"端到端 {g2_ok}/{total_tasks} ({summary['end_to_end_success_rate_pct']}%)",
        flush=True,
    )
    print(
        f"滑块: 无={slider['no_slider']}  1次={slider['slider_1']}  "
        f"2次={slider['slider_2']}  acquire滑块失败={slider['acquire_slider_fail']}",
        flush=True,
    )
    print(f"详情: {out}", flush=True)
    sys.exit(0 if g2_ok == total_tasks else 1)


if __name__ == "__main__":
    main()
