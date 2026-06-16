"""Redis-driven DataDome cookie refill worker."""
from __future__ import annotations

import logging

from config import settings
from services.refill_worker import run_refill_loop


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.info(
        "worker redis=%s:%s key=%s target_pool=%d browser_pool=%d headless=%s proxy=%s",
        settings.rds_host,
        settings.rds_port,
        settings.redis_key,
        settings.target_pool_size,
        settings.browser_pool_size,
        settings.headless,
        "on" if settings.proxy_url else "off",
    )
    run_refill_loop()
