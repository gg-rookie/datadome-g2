"""DataDome Cookie Flask API（单机：HTTP + Firefox 同机）。"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, request
from flask_restful import Api, Resource

from api.response import fail, ok
from config import settings
from services.browser_pool import browser_pool
from services.cookie_store import load_ck, redis_status, save_ck, append_ck_to_pool, pool_key

bp = Blueprint("datadome_api", __name__)
api = Api(bp)


def _require_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not settings.api_key:
            return fn(*args, **kwargs)
        key = request.args.get("key") or request.headers.get("X-API-Key")
        if key != settings.api_key:
            return fail("invalid api key", code=401, http_status=401)
        return fn(*args, **kwargs)
    return wrapper


def _cookie_payload(result: dict) -> dict:
    data = {
        "cookie": result.get("cookie"),
        "user_agent": result.get("user_agent"),
        "url": result.get("url"),
        "worker_id": result.get("worker_id"),
        "slider_attempts": result.get("slider_attempts", 0),
    }
    return {k: v for k, v in data.items() if v is not None}


def _fetch_error_response(result: dict):
    state = (result.get("state") or "").lower()
    err = result.get("error") or state or "fetch failed"
    if state == "timeout" or "timeout" in err.lower():
        return fail(err, code=504, http_status=504, data=result)
    if state in ("blocked", "proxy_auth", "proxy_invalid"):
        return fail(err, code=502, http_status=502, data=result)
    return fail(err, code=500, http_status=500, data=result)


class HealthController(Resource):
    def get(self):
        rs = redis_status()
        return ok({
            "service": "datadome-g2",
            "mode": "standalone",
            "browser_pool_size": settings.browser_pool_size,
            "headless": settings.headless,
            "pool_started": browser_pool.started,
            "redis": {
                "host": settings.rds_host,
                "port": settings.rds_port,
                "key": settings.redis_key,
                "pool_key": pool_key(),
                "pool_size": rs["pool_size"],
                "ok": rs["ok"],
                "error": rs["error"],
            },
        })


class ConfigController(Resource):
    method_decorators = [_require_key]

    def get(self):
        return ok({
            "mode": "standalone",
            "target_url": settings.target_url,
            "headless": settings.headless,
            "cookie_timeout": settings.cookie_timeout,
            "browser_pool_size": settings.browser_pool_size,
            "profiles_dir": str(settings.profiles_dir),
            "proxy_configured": bool(settings.proxy_url),
            "redis": {
                "host": settings.rds_host,
                "port": settings.rds_port,
                "key": settings.redis_key,
                "pool_key": pool_key(),
                "ttl": settings.redis_ttl,
            },
        })


class CookieAcquireController(Resource):
    """下游入口：HTTP 阻塞，本机开 Firefox 取 datadome cookie。"""
    method_decorators = [_require_key]

    def post(self):
        body = request.get_json(silent=True) or {}
        url = body.get("url") or request.args.get("url")
        try:
            result = browser_pool.fetch(url=url)
        except Exception as e:
            return fail(str(e), code=500, http_status=500)
        if result.get("ok"):
            save_ck(result)
            append_ck_to_pool(result)
            return ok(_cookie_payload(result))
        return _fetch_error_response(result)


class CookieLatestController(Resource):
    """调试用：读 Redis 里最近一次成功的 cookie。"""
    method_decorators = [_require_key]

    def get(self):
        data = load_ck()
        if not data:
            return fail("cookie not found", code=404, http_status=404)
        return ok(data)


def register_routes(app):
    app.register_blueprint(bp)
    api.add_resource(HealthController, "/health")
    api.add_resource(ConfigController, "/api/datadome/v1/config")
    api.add_resource(CookieAcquireController, "/api/datadome/v1/cookie/acquire")
    api.add_resource(CookieLatestController, "/api/datadome/v1/cookie")
