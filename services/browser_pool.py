"""固定大小的 Firefox 进程池，同步取 datadome cookie。"""
from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from config import settings
from services.browser import BrowserLaunchConfig, G2PageError, fetch_g2_session, proxy_url_for_worker

logger = logging.getLogger("datadome.pool")
ROOT = Path(__file__).resolve().parent.parent


def _worker_fetch(args: tuple) -> dict:
    worker_id, url, timeout, base_port, profiles_root, firefox_path, headless, proxy_url = args
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


class BrowserPool:
    def __init__(self) -> None:
        self.pool_size = settings.browser_pool_size
        self._executor: ProcessPoolExecutor | None = None
        self._slot = 0
        self._slot_lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._executor is not None

    def start(self) -> None:
        if self._executor:
            return
        self._executor = ProcessPoolExecutor(max_workers=self.pool_size)
        logger.info("browser pool started size=%d headless=%s", self.pool_size, settings.headless)

    def stop(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def fetch(self, url: str | None = None) -> dict:
        if not self._executor:
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
        )
        fut = self._executor.submit(_worker_fetch, args)
        try:
            return fut.result(timeout=settings.cookie_timeout + 60)
        except FuturesTimeoutError:
            fut.cancel()
            return {
                "ok": False,
                "worker_id": slot,
                "state": "timeout",
                "error": f"浏览器任务超时（>{settings.cookie_timeout}s）",
            }

    def _next_slot(self) -> int:
        with self._slot_lock:
            slot = self._slot % self.pool_size
            self._slot += 1
            return slot


browser_pool = BrowserPool()
