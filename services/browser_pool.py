"""Fixed-size Firefox process pool for acquiring DataDome cookies."""
from __future__ import annotations

import logging
import multiprocessing
import queue
import sys
import threading
from pathlib import Path

from config import settings
from services.browser import (
    BrowserLaunchConfig,
    G2PageError,
    fetch_g2_session,
    proxy_url_for_worker,
)

logger = logging.getLogger("datadome.pool")
ROOT = Path(__file__).resolve().parent.parent


def _worker_fetch(args: tuple) -> dict:
    (
        worker_id,
        url,
        timeout,
        base_port,
        profiles_root,
        firefox_path,
        headless,
        proxy_url,
        use_profile_cache,
    ) = args
    sys.path.insert(0, str(ROOT))

    cfg = BrowserLaunchConfig(
        target_url=url,
        firefox_path=firefox_path,
        headless=headless,
        proxy_url=proxy_url,
        cookie_timeout=timeout,
        profiles_root=Path(profiles_root),
    )
    user_dir = Path(profiles_root) / f"worker_{worker_id}"
    port = base_port + worker_id
    proxy = proxy_url_for_worker(proxy_url, worker_id)

    try:
        session = fetch_g2_session(
            cfg,
            url=url,
            timeout=timeout,
            user_dir=user_dir,
            proxy_url=proxy or None,
            port=port,
            use_profile_cache=use_profile_cache,
        )
        session["worker_id"] = worker_id
        session["ok"] = True
        return session
    except G2PageError as e:
        return {
            "ok": False,
            "worker_id": worker_id,
            "state": e.state,
            "url": e.url,
            "error": str(e),
        }
    except Exception as e:
        return {
            "ok": False,
            "worker_id": worker_id,
            "error": f"{type(e).__name__}: {e}",
        }


def _worker_entry(args: tuple, result_queue: multiprocessing.Queue) -> None:
    result_queue.put(_worker_fetch(args))


class BrowserPool:
    def __init__(self) -> None:
        self.pool_size = settings.browser_pool_size
        self._started = False
        self._slot = 0
        self._slot_lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        logger.info(
            "browser pool started size=%d headless=%s",
            self.pool_size,
            settings.headless,
        )

    def stop(self) -> None:
        self._started = False

    def fetch(self, url: str | None = None, *, use_profile_cache: bool = True) -> dict:
        if not self._started:
            raise RuntimeError("browser pool not started")
        slot = self._next_slot()
        target = url or settings.target_url
        args = (
            slot,
            target,
            settings.cookie_timeout,
            settings.batch_base_port,
            str(settings.profiles_dir),
            settings.firefox_path,
            settings.headless,
            settings.proxy_url,
            use_profile_cache,
        )
        timeout = settings.cookie_timeout + 60
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue(maxsize=1)
        proc = ctx.Process(target=_worker_entry, args=(args, result_queue))
        proc.start()
        proc.join(timeout=timeout)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
            return {
                "ok": False,
                "worker_id": slot,
                "state": "timeout",
                "error": f"browser task timed out (>{settings.cookie_timeout}s)",
            }
        try:
            return result_queue.get_nowait()
        except queue.Empty:
            return {
                "ok": False,
                "worker_id": slot,
                "state": "worker_exited",
                "error": f"browser worker exited with code {proc.exitcode}",
            }

    def _next_slot(self) -> int:
        with self._slot_lock:
            slot = self._slot % self.pool_size
            self._slot += 1
            return slot


browser_pool = BrowserPool()
