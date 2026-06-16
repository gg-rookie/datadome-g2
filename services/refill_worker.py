"""Redis-driven cookie refill loop."""
from __future__ import annotations

import logging
import time

from config import settings
from services.browser_pool import browser_pool
from services.cookie_store import (
    append_ck_to_pool,
    pool_has_cookie,
    pool_size,
    redis_status,
    save_ck,
)
from services.cookie_validator import validate_cookie

logger = logging.getLogger("datadome.refill")


def _fetch_cookie(use_profile_cache: bool) -> dict:
    try:
        return browser_pool.fetch(
            url=settings.target_url,
            use_profile_cache=use_profile_cache,
        )
    except Exception as e:
        logger.exception("browser pool fetch crashed: %s", e)
        browser_pool.stop()
        browser_pool.start()
        return {
            "ok": False,
            "state": "pool_crashed",
            "error": f"{type(e).__name__}: {e}",
        }


def _validate_and_store(result: dict) -> bool:
    if not result.get("ok"):
        logger.warning(
            "cookie fetch failed worker=%s state=%s error=%s",
            result.get("worker_id"),
            result.get("state"),
            result.get("error"),
        )
        return False

    validation = validate_cookie(result, url=settings.target_url)
    result["validation"] = validation
    if not validation.get("ok"):
        logger.warning(
            "cookie validation failed worker=%s http=%s bytes=%s error=%s",
            result.get("worker_id"),
            validation.get("status"),
            validation.get("bytes"),
            validation.get("error"),
        )
        return False

    if pool_has_cookie(result.get("cookie", "")):
        logger.info("cookie skipped duplicate worker=%s", result.get("worker_id"))
        return False

    save_ck(result)
    append_ck_to_pool(result)
    logger.info(
        "cookie stored worker=%s pool_size=%d http=%s bytes=%s",
        result.get("worker_id"),
        pool_size(),
        validation.get("status"),
        validation.get("bytes"),
    )
    return True


def refill_once() -> bool:
    """Try to produce one cookie when the Redis pool is below target."""
    current = pool_size()
    if current >= settings.target_pool_size:
        logger.info("redis pool full size=%d target=%d", current, settings.target_pool_size)
        return False

    logger.info("redis pool needs refill size=%d target=%d", current, settings.target_pool_size)
    return _validate_and_store(_fetch_cookie(use_profile_cache=False))


def run_refill_loop() -> None:
    logger.info(
        "refill worker starting redis=%s:%s key=%s pool_key=%s target=%d poll=%ss",
        settings.rds_host,
        settings.rds_port,
        settings.redis_key,
        f"{settings.redis_key}:pool",
        settings.target_pool_size,
        settings.redis_poll_interval,
    )
    browser_pool.start()
    try:
        while True:
            status = redis_status()
            if not status.get("ok"):
                logger.warning("redis unavailable: %s", status.get("error"))
                time.sleep(settings.redis_poll_interval)
                continue

            if status["pool_size"] < settings.target_pool_size:
                produced = refill_once()
                time.sleep(
                    settings.refill_success_interval
                    if produced
                    else settings.refill_failure_interval
                )
                continue

            time.sleep(settings.redis_poll_interval)
    finally:
        browser_pool.stop()
