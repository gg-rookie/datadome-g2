"""Redis cookie storage helpers."""
from __future__ import annotations

import json
import time

import redis

from config import settings

_redis: redis.Redis | None = None


def redis_client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(
            host=settings.rds_host,
            port=settings.rds_port,
            password=settings.rds_password or None,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=5,
            protocol=2,
        )
    return _redis


def redis_status() -> dict:
    """Return Redis availability without raising on connection failure."""
    try:
        client = redis_client()
        client.ping()
        return {
            "ok": True,
            "pool_size": int(client.llen(pool_key()) or 0),
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False,
            "pool_size": 0,
            "error": f"{type(e).__name__}: {e}",
        }


def pool_key() -> str:
    return f"{settings.redis_key}:pool"


def save_ck(payload: dict) -> str:
    data = {**payload, "updated_at": int(time.time())}
    body = json.dumps(data, ensure_ascii=False)
    client = redis_client()
    if settings.redis_ttl > 0:
        client.setex(settings.redis_key, settings.redis_ttl, body)
    else:
        client.set(settings.redis_key, body)
    return settings.redis_key


def append_ck_to_pool(payload: dict) -> str:
    data = {**payload, "updated_at": int(time.time())}
    key = pool_key()
    client = redis_client()
    client.rpush(key, json.dumps(data, ensure_ascii=False))
    if settings.redis_ttl > 0:
        client.expire(key, settings.redis_ttl)
    return key


def load_pool() -> list[dict]:
    rows = redis_client().lrange(pool_key(), 0, -1)
    items: list[dict] = []
    for row in rows:
        try:
            items.append(json.loads(row))
        except Exception:
            continue
    return items


def pool_has_cookie(cookie: str) -> bool:
    if not cookie:
        return False
    return any(item.get("cookie") == cookie for item in load_pool())


def load_ck() -> dict | None:
    raw = redis_client().get(settings.redis_key)
    if not raw:
        return None
    return json.loads(raw)


def pool_size() -> int:
    try:
        return int(redis_client().llen(pool_key()) or 0)
    except Exception:
        return 0
