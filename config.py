"""环境变量配置。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_env = _parse_env(ENV_FILE)


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


@dataclass(frozen=True)
class Settings:
    host: str = _env.get("HOST", "0.0.0.0")
    port: int = int(_env.get("PORT", "51051"))
    debug: bool = _bool(_env.get("DEBUG", ""), default=False)
    api_key: str = _env.get("API_KEY", "")

    target_url: str = _env.get(
        "TARGET_URL", "https://www.g2.com/products/slack/reviews"
    )
    firefox_path: str = _env.get("FIREFOX_PATH", "")
    profiles_dir: Path = _resolve_path(_env.get("PROFILES_DIR", "profiles"))
    headless: bool = _bool(_env.get("HEADLESS", ""), default=True)
    proxy_url: str = _env.get("PROXY_URL", "")
    cookie_timeout: int = int(_env.get("COOKIE_TIMEOUT", "120"))
    browser_pool_size: int = int(
        _env.get("BROWSER_POOL_SIZE") or _env.get("BATCH_WORKERS_DEFAULT", "5")
    )
    batch_base_port: int = int(_env.get("BATCH_BASE_PORT", "9222"))

    rds_host: str = _env.get("RDS_HOST", "127.0.0.1")
    rds_port: int = int(_env.get("RDS_PORT", "6379"))
    rds_password: str = _env.get("RDS_PASSWORD", "")
    redis_key: str = _env.get("REDIS_KEY", "datadome:g2:ck")
    redis_ttl: int = int(_env.get("REDIS_TTL", "3600"))


settings = Settings()
