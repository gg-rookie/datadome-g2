"""DataDome Cookie 单机服务：Flask + Firefox 同进程部署（生产机直接启动）。"""
from __future__ import annotations

import logging

from flask import Flask

from api.routes import register_routes
from config import settings
from services.browser_pool import browser_pool


def create_app() -> Flask:
    app = Flask(__name__)
    register_routes(app)
    browser_pool.start()
    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.info(
        "standalone server redis=%s:%s key=%s proxy=%s pool=%d headless=%s",
        settings.rds_host,
        settings.rds_port,
        settings.redis_key,
        "on" if settings.proxy_url else "off",
        settings.browser_pool_size,
        settings.headless,
    )
    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        threaded=True,
    )
