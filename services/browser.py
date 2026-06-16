"""Acquire G2 DataDome cookies with ruyiPage + Firefox."""
from __future__ import annotations

import logging
import random
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from ruyipage import FirefoxOptions, FirefoxPage, apply_smart_fingerprint
from ruyipage._bidi import session as bidi_session
from ruyipage._functions.settings import Settings

G2_REVIEWS_RE = re.compile(r"g2\.com/products/.+/reviews", re.I)

logger = logging.getLogger("datadome.browser")

_DEFAULT_BIDI_TIMEOUT = 90
_POLL_INTERVAL_SEC = 1.0

_SLIDER_HANDLE_SELECTORS = (
    "css:#captcha__slider__handle",
    "css:.captcha__slider__handle",
    "css:#captchasliderhandle",
    "css:#slider",
    "css:.slider",
    "css:[class*='slider'][class*='handle']",
    "css:[role='slider']",
)

_CAPTCHA_IFRAME_SELECTORS = (
    "css:iframe[src*='captcha-delivery']",
    "css:#captcha__frame",
)

_G2_COOKIE_NAMES = ("__cf_bm", "cf_clearance", "datadome")


@dataclass
class BrowserLaunchConfig:
    target_url: str
    firefox_path: str
    headless: bool
    proxy_url: str
    cookie_timeout: int
    profiles_root: Path


class G2PageError(RuntimeError):
    def __init__(self, state: str, url: str, title: str, detail: str):
        self.state = state
        self.url = url
        self.title = title
        super().__init__(detail)


def _configure_ruyipage_timeouts(bidi_timeout: int) -> None:
    """Raise BiDi timeouts inside browser worker processes."""
    bidi_timeout = max(30, bidi_timeout)
    Settings.bidi_timeout = bidi_timeout
    Settings.script_timeout = bidi_timeout
    Settings.page_load_timeout = max(60, bidi_timeout)
    Settings.element_find_timeout = min(8, max(3, bidi_timeout // 12))


def _hard_blocked_url(url_l: str) -> bool:
    return "captcha-delivery.com" in url_l


def _hard_blocked_title(title_l: str) -> bool:
    return "datadome" in title_l and "g2" not in title_l


def _is_g2_reviews_url(url: str) -> bool:
    return bool(G2_REVIEWS_RE.search(url or ""))


def _ele_visible(page, selector: str, timeout: float = 0.15) -> bool:
    el = page.ele(selector, timeout=timeout)
    if not el:
        return False
    try:
        return bool(el.run_js(
            """
            function () {
                const el = this;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0)
                    return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
            }
            """
        ))
    except Exception:
        return False


def _soft_blocked_light(page) -> bool:
    """Treat only visible DataDome containers as a challenge."""
    for sel in _CAPTCHA_IFRAME_SELECTORS:
        if _ele_visible(page, sel):
            return True
    if _ele_visible(page, "css:#ddChallengeContainer"):
        return True
    return False


def _soft_blocked(page) -> bool:
    if _soft_blocked_light(page):
        return True
    for sel in (
        "text:You have been blocked",
        "text:verify you are human",
        "text:slide right to complete",
        "text:slide to complete",
    ):
        if page.ele(sel, timeout=0.15):
            return True
    return False


def _hard_blocked_text_light(page) -> bool:
    for sel in (
        "text:Please enable JS and disable any ad blocker",
        "text:Pardon Our Interruption",
        "text:You have been blocked",
        "text:Access denied",
    ):
        if page.ele(sel, timeout=0.15):
            return True
    return False


def _extract_datadome_cookie(page) -> str:
    """Read the HttpOnly datadome cookie through BiDi storage."""
    from ruyipage._bidi import storage as bidi_storage

    driver = page._driver._browser_driver
    ctx_id = page._context_id

    def _pick_value(raw: dict) -> str:
        val = raw.get("value")
        if isinstance(val, dict):
            val = val.get("value", "")
        val = str(val or "")
        return val

    for partition in ({"context": ctx_id}, None):
        try:
            result = bidi_storage.get_cookies(
                driver,
                filter_={"name": "datadome"},
                partition=partition,
            )
        except Exception:
            continue
        for item in result.get("cookies", []):
            val = _pick_value(item)
            if val:
                return val
    return ""


def _string_value(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return str(raw or "")


def _header_value(headers, name: str) -> str:
    name_l = name.lower()
    for item in headers or []:
        if str(item.get("name") or "").lower() == name_l:
            return _string_value(item.get("value"))
    return ""


def _cookie_header_complete(cookie_header: str) -> bool:
    return all(f"{name}=" in (cookie_header or "") for name in ("cf_clearance", "datadome"))


def _install_cookie_header_capture(page) -> dict[str, str]:
    captured = {"cookie": "", "url": ""}
    driver = page._driver._browser_driver
    context_id = page._context_id

    def _on_request(params: dict) -> None:
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        if "g2.com/products/" not in url and "www.g2.com" not in url:
            return
        cookie_header = _header_value(request.get("headers"), "cookie")
        if _cookie_header_complete(cookie_header):
            captured["cookie"] = cookie_header
            captured["url"] = url

    try:
        bidi_session.subscribe(
            driver,
            ["network.beforeRequestSent"],
            contexts=[context_id],
        )
        page._driver.set_global_callback(
            "network.beforeRequestSent",
            _on_request,
            immediate=True,
        )
    except Exception as e:
        logger.debug("cookie header capture unavailable: %s", e)
    return captured


def _extract_cookie_values_bidi(page, names: tuple[str, ...]) -> dict[str, str]:
    from ruyipage._bidi import storage as bidi_storage

    driver = page._driver._browser_driver
    ctx_id = page._context_id
    values: dict[str, str] = {}

    def _pick_value(raw: dict) -> str:
        val = raw.get("value")
        if isinstance(val, dict):
            val = val.get("value", "")
        return str(val or "")

    for partition in ({"context": ctx_id}, None):
        for name in names:
            if name in values:
                continue
            try:
                result = bidi_storage.get_cookies(
                    driver,
                    filter_={"name": name},
                    partition=partition,
                )
            except Exception:
                continue
            for item in result.get("cookies", []):
                val = _pick_value(item)
                if val:
                    values[name] = val
                    break
    return values


def _extract_cookie_values_sqlite(profile_dir: Path, names: tuple[str, ...]) -> dict[str, str]:
    db = profile_dir / "cookies.sqlite"
    if not db.exists():
        return {}
    placeholders = ",".join("?" for _ in names)
    query = (
        "select name,value from moz_cookies "
        f"where name in ({placeholders}) and host like ? "
        "order by lastAccessed desc"
    )
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        rows = con.execute(query, (*names, "%g2.com%")).fetchall()
        con.close()
    except Exception:
        return {}
    values: dict[str, str] = {}
    for name, value in rows:
        if name not in values and value:
            values[str(name)] = str(value)
    return values


def _build_g2_cookie_header(page, profile_dir: Path) -> str:
    values = _extract_cookie_values_bidi(page, _G2_COOKIE_NAMES)
    if "datadome" not in values or "cf_clearance" not in values:
        values = _extract_cookie_values_sqlite(profile_dir, _G2_COOKIE_NAMES)
    if "datadome" not in values or "cf_clearance" not in values:
        return ""
    return "; ".join(
        f"{name}={values[name]}"
        for name in _G2_COOKIE_NAMES
        if values.get(name)
    )


def _profile_user_agent(profile_dir: Path) -> str:
    prefs = profile_dir / "prefs.js"
    if not prefs.exists():
        return ""
    try:
        text = prefs.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(r'general\.useragent\.override", "([^"]+)', text)
    return m.group(1) if m else ""


def _profile_cookie_session(profile_dir: Path, cur_url: str = "") -> dict[str, str] | None:
    values = _extract_cookie_values_sqlite(profile_dir, _G2_COOKIE_NAMES)
    if not values.get("datadome") or not values.get("cf_clearance"):
        return None
    cookie_header = "; ".join(
        f"{name}={values[name]}"
        for name in _G2_COOKIE_NAMES
        if values.get(name)
    )
    return {
        "cookie": cookie_header,
        "user_agent": _profile_user_agent(profile_dir),
        "url": cur_url,
    }


def _try_finish_session(
    page,
    profile_dir: Path,
    cur_url: str = "",
    browser_cookie_header: str = "",
) -> dict[str, str] | None:
    """Return the session once G2 has a complete cookie header."""
    cookie_header = (
        browser_cookie_header
        if _cookie_header_complete(browser_cookie_header)
        else _build_g2_cookie_header(page, profile_dir)
    )
    if not cookie_header:
        return None
    try:
        user_agent = page.user_agent or ""
    except Exception:
        user_agent = ""
    if not cur_url:
        cur_url = _page_url_bidi(page)
    return {
        "cookie": cookie_header,
        "user_agent": user_agent,
        "url": cur_url,
    }


def _page_url_bidi(page) -> str:
    """Read the current URL through BiDi instead of page.url."""
    try:
        result = page._driver._browser_driver.run(
            "browsingContext.getTree",
            {"root": page._context_id},
        )
        contexts = result.get("contexts") or []
        if contexts:
            return contexts[0].get("url") or ""
    except Exception:
        pass
    return ""


def _has_visible_slider(page) -> bool:
    """Return true only when a visible DataDome slider handle exists."""
    for locator in _CAPTCHA_IFRAME_SELECTORS:
        if not _ele_visible(page, locator):
            continue
        frame = page.get_frame(locator)
        root = frame if frame else page
        if _find_slider_handle(root):
            return True
    if _ele_visible(page, "css:#ddChallengeContainer"):
        if _find_slider_handle(page):
            return True
    return False


def _page_state(page) -> tuple[str, str, str]:
    cur_url = _page_url_bidi(page)
    url_l = cur_url.lower()

    if _hard_blocked_url(url_l):
        return "blocked", cur_url, ""

    cur_title = page.title or ""
    title_l = cur_title.lower()
    if _hard_blocked_title(title_l) or _hard_blocked_text_light(page):
        return "blocked", cur_url, cur_title

    if _is_g2_reviews_url(cur_url):
        if not _soft_blocked_light(page):
            return "ready", cur_url, ""

        if _extract_datadome_cookie(page) and not _has_visible_slider(page):
            return "ready", cur_url, ""
        return "challenge", cur_url, ""

    if _soft_blocked(page):
        return "challenge", cur_url, cur_title

    return "pending", cur_url, cur_title


def _fpfile_proxy_ready(
    fpfile: Path, host: str, port: int, username: str, password: str
) -> bool:
    if not fpfile.exists():
        return False
    text = fpfile.read_text(encoding="utf-8")
    rotate = f"proxy.rotate.proxy:http://{host}:{port}:{username}:{password}"
    return (
        f"httpauth.username:{username}" in text
        and f"httpauth.password:{password}" in text
        and rotate in text
    )


def _clear_fpfile_proxy(fpfile: Path) -> None:
    """Remove stale fpfile proxy credentials when PROXY_URL is empty."""
    if not fpfile.exists():
        return
    skip_prefixes = (
        "httpauth.username",
        "httpauth.password",
        "proxy.rotate.enabled",
        "proxy.rotate.exhausted",
        "proxy.rotate.proxy",
        "httpproxy.rotate.",
        "socksauth.",
    )
    lines = fpfile.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.strip().startswith(skip_prefixes)]
    while kept and not kept[-1].strip():
        kept.pop()
    if kept:
        fpfile.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        fpfile.unlink(missing_ok=True)


def _ensure_fpfile_proxy(
    fpfile: Path, host: str, port: int, username: str, password: str
) -> None:
    if _fpfile_proxy_ready(fpfile, host, port, username, password):
        return

    skip_prefixes = (
        "httpauth.username",
        "httpauth.password",
        "proxy.rotate.enabled",
        "proxy.rotate.exhausted",
        "proxy.rotate.proxy",
        "httpproxy.rotate.",
        "socksauth.",
    )
    lines = fpfile.read_text(encoding="utf-8").splitlines() if fpfile.exists() else []
    kept = [line for line in lines if not line.strip().startswith(skip_prefixes)]
    while kept and not kept[-1].strip():
        kept.pop()

    rotate = f"http://{host}:{port}:{username}:{password}"
    kept.extend([
        f"httpauth.username:{username}",
        f"httpauth.password:{password}",
        "proxy.rotate.enabled:true",
        "proxy.rotate.exhausted:wrap",
        f"proxy.rotate.proxy:{rotate}",
        "",
    ])
    fpfile.parent.mkdir(parents=True, exist_ok=True)
    fpfile.write_text("\n".join(kept), encoding="utf-8")


def _fpfile_arg(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _apply_proxy(
    opts: FirefoxOptions,
    *,
    user_dir: Path,
    proxy_url: str,
) -> None:
    fpfile = user_dir.resolve() / "fpfile.txt"
    if not proxy_url:
        _clear_fpfile_proxy(fpfile)
        opts.set_pref("network.proxy.type", 0)
        return

    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        raise G2PageError("proxy_invalid", "", "", f"invalid PROXY_URL: {proxy_url}")

    port = parsed.port or 1000
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname

    opts.set_proxy(f"http://{host}:{port}")
    opts.set_pref("signon.autologin.proxy", True)
    opts.set_pref("network.auth.subresource-http-auth-allow", 2)

    if username and password:
        _ensure_fpfile_proxy(fpfile, host, port, username, password)
        opts.set_fpfile(_fpfile_arg(fpfile))


def _apply_fingerprint(opts: FirefoxOptions, profile_dir: Path):
    try:
        ctx = apply_smart_fingerprint(
            opts,
            userdir=str(profile_dir),
            require_country=None,
            fetch_ipv6=False,
            set_proxy_on_opts=False,
            logger=logger.info,
        )
        opts.set_pref("general.useragent.override", ctx.fingerprint.useragent)
        logger.info("fingerprint ready %s", ctx.summary())
        return ctx
    except Exception as e:
        logger.warning("fingerprint setup skipped: %s", e)
        return None


def proxy_url_for_worker(proxy_url: str, worker_id: int) -> str:
    if not proxy_url:
        return ""
    if re.search(r"session[-_]", proxy_url, re.I):
        return re.sub(
            r"session[-_][^_:@/]+",
            f"session-w{worker_id}",
            proxy_url,
            count=1,
            flags=re.I,
        )
    return proxy_url


def _proxy_auth_popup(page) -> bool:
    return "authentication required" in (page.title or "").lower()


def _captcha_root(page):
    for locator in _CAPTCHA_IFRAME_SELECTORS:
        if not page.ele(locator, timeout=0.3):
            continue
        frame = page.get_frame(locator)
        if frame:
            return frame
    frames = page.get_frames()
    if frames:
        return frames[0]
    for sel in _SLIDER_HANDLE_SELECTORS:
        if page.ele(sel, timeout=0.2):
            return page
    return None


def _find_slider_handle(root):
    for sel in _SLIDER_HANDLE_SELECTORS:
        handle = root.ele(sel, timeout=0.4)
        if handle:
            return handle
    return None


def _slider_drag_target(handle):
    """Calculate a coordinate near the right edge of the slider track."""
    return handle.run_js(
        """
        function () {
            const el = this;
            let track =
                el.closest('.captcha__slider, .sliderContainer, #captcha__slider') ||
                el.parentElement;
            if (!track) return null;
            const hr = el.getBoundingClientRect();
            let tr = track.getBoundingClientRect();
            for (let node = track; node; node = node.parentElement) {
                const nr = node.getBoundingClientRect();
                if (nr.width >= Math.max(hr.width * 4, 160)) {
                    track = node;
                    tr = nr;
                    break;
                }
            }
            if (hr.width < 2 || tr.width < 10) return null;
            const rightPad = 1 + Math.random() * 2;
            const viewportRight = (window.innerWidth || document.documentElement.clientWidth || tr.right) - 2;
            const targetX = Math.min(tr.right - rightPad, viewportRight);
            return {
                x: Math.round(targetX),
                y: Math.round(hr.y + hr.height / 2 + (Math.random() - 0.5) * 2),
                track_left: Math.round(tr.left),
                track_right: Math.round(tr.right),
                track_width: Math.round(tr.width),
                handle_width: Math.round(hr.width),
            };
        }
        """
    )


def _try_solve_datadome_slider(page) -> bool:
    root = _captcha_root(page)
    if not root:
        return False
    handle = None
    for _ in range(12):
        handle = _find_slider_handle(root)
        if handle:
            break
        time.sleep(0.5)
    if not handle:
        return False
    target = _slider_drag_target(handle)
    if not target or not target.get("x"):
        return False
    duration = random.uniform(1.0, 1.6)
    handle.drag_to(target, duration=duration)
    time.sleep(random.uniform(1.0, 1.5))
    logger.info(
        "slider drag done duration=%.2fs target_x=%s track=%s-%s width=%s handle=%s",
        duration,
        target.get("x"),
        target.get("track_left"),
        target.get("track_right"),
        target.get("track_width"),
        target.get("handle_width"),
    )
    return True


def fetch_g2_session(
    cfg: BrowserLaunchConfig,
    url: str | None = None,
    timeout: int | None = None,
    *,
    user_dir: Path | None = None,
    proxy_url: str | None = None,
    port: int | None = None,
    headless: bool | None = None,
    use_profile_cache: bool = True,
) -> dict[str, str]:
    profile_dir = (user_dir or cfg.profiles_root / "default").resolve()
    if not use_profile_cache and profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cached_session = _profile_cookie_session(profile_dir, url or cfg.target_url)
    if use_profile_cache and cached_session:
        cached_session["slider_attempts"] = 0
        logger.info("cookie ok (profile cache) url=%s", cached_session["url"][:80])
        return cached_session
    effective_proxy = proxy_url if proxy_url is not None else cfg.proxy_url
    wait_timeout = timeout or cfg.cookie_timeout
    session_deadline = time.time() + wait_timeout

    def _remaining(min_sec: float = 3.0) -> float:
        return max(min_sec, session_deadline - time.time())

    bidi_timeout = max(_DEFAULT_BIDI_TIMEOUT, min(int(_remaining()), 120))

    _configure_ruyipage_timeouts(bidi_timeout)

    opts = FirefoxOptions()
    opts.headless(cfg.headless if headless is None else headless)
    if cfg.firefox_path:
        opts.set_browser_path(cfg.firefox_path)
    if port is not None:
        opts.set_port(port)
    opts.set_timeouts(base=10, page_load=bidi_timeout, script=bidi_timeout)
    fp_ctx = _apply_fingerprint(opts, profile_dir)
    _apply_proxy(opts, user_dir=profile_dir, proxy_url=effective_proxy)
    opts.set_user_dir(str(profile_dir))

    page = FirefoxPage(opts)
    page_closed = False
    if fp_ctx:
        fp_ctx.apply_emulation(page, logger=logger.info)
    captured_cookie = _install_cookie_header_capture(page)
    target = url or cfg.target_url
    headless_on = cfg.headless if headless is None else headless
    logger.info("fetch start url=%s headless=%s timeout=%s", target, headless_on, wait_timeout)

    try:
        page.get(target, wait="interactive", timeout=_remaining(10))
        if _proxy_auth_popup(page):
            raise G2PageError(
                "proxy_auth",
                page.url or "",
                page.title or "",
                "proxy authentication failed; check PROXY_URL",
            )

        page.wait.doc_loaded(timeout=_remaining(10))

        slider_attempts = 0
        while time.time() < session_deadline:
            state, cur_url, cur_title = _page_state(page)

            if state == "blocked":
                raise G2PageError(
                    "blocked", cur_url, cur_title,
                    "page was blocked by DataDome",
                )

            session = _try_finish_session(
                page,
                profile_dir,
                cur_url,
                captured_cookie.get("cookie", ""),
            )
            if session:
                session["slider_attempts"] = slider_attempts
                logger.info(
                    "cookie ok (cookie bundle) url=%s",
                    cur_url[:80],
                )
                return session

            if state == "challenge":
                if _is_g2_reviews_url(cur_url) and _extract_datadome_cookie(page):
                    if not _has_visible_slider(page):
                        session = _try_finish_session(
                            page,
                            profile_dir,
                            cur_url,
                            captured_cookie.get("cookie", ""),
                        )
                        if session:
                            session["slider_attempts"] = slider_attempts
                            logger.info(
                                "cookie ok (reviews+cookie, no visible slider) url=%s",
                                cur_url[:80],
                            )
                            return session
                if slider_attempts < 2 and _try_solve_datadome_slider(page):
                    slider_attempts += 1
                    logger.info("slider attempt %d", slider_attempts)
                    time.sleep(2)
                    continue
                if slider_attempts < 2:
                    slider_attempts += 1
                    time.sleep(2)
                    continue
                raise G2PageError(
                    "blocked", cur_url, cur_title,
                    "slider challenge was not solved",
                )

            if state == "ready":
                session = _try_finish_session(
                    page,
                    profile_dir,
                    cur_url,
                    captured_cookie.get("cookie", ""),
                )
                if session:
                    session["slider_attempts"] = slider_attempts
                    logger.info(
                        "cookie ok headless=%s slider_attempts=%d url=%s",
                        headless_on, slider_attempts, cur_url[:80],
                    )
                    return session

            time.sleep(_POLL_INTERVAL_SEC)

        try:
            cur_url = _page_url_bidi(page)
            user_agent = page.user_agent or ""
        except Exception:
            cur_url = ""
            user_agent = ""
        try:
            page.quit()
            page_closed = True
            time.sleep(0.5)
        except Exception:
            pass
        if _cookie_header_complete(captured_cookie.get("cookie", "")):
            logger.info("cookie ok (timeout network header) url=%s", cur_url[:80])
            return {
                "cookie": captured_cookie["cookie"],
                "user_agent": user_agent,
                "url": captured_cookie.get("url") or cur_url,
                "slider_attempts": slider_attempts,
            }

        values = _extract_cookie_values_sqlite(profile_dir, _G2_COOKIE_NAMES)
        if values.get("datadome") and values.get("cf_clearance"):
            cookie_header = "; ".join(
                f"{name}={values[name]}"
                for name in _G2_COOKIE_NAMES
                if values.get(name)
            )
            logger.info("cookie ok (timeout sqlite salvage) url=%s", cur_url[:80])
            return {
                "cookie": cookie_header,
                "user_agent": user_agent,
                "url": cur_url,
                "slider_attempts": slider_attempts,
            }

        raise G2PageError(
            "timeout", "", "",
            f"timed out waiting for G2 page and datadome cookie ({wait_timeout}s)",
        )
    finally:
        if not page_closed:
            page.quit()
