"""ruyiPage + Firefox 访问 G2，提取 datadome cookie。"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from ruyipage import FirefoxOptions, FirefoxPage
from ruyipage._functions.settings import Settings

G2_REVIEWS_RE = re.compile(r"g2\.com/products/.+/reviews", re.I)

logger = logging.getLogger("datadome.browser")

# 5 并行时 BiDi 命令易排队
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
    """子进程内调高 BiDi 超时，避免多 Firefox 并行时单条命令 30s 误杀。"""
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
    """仅可见的验证码 iframe/容器才算 challenge（避免 G2 页残留隐藏节点误判）。"""
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


def _extract_datadome_cookie(page) -> str:
    """BiDi storage.getCookies；不走 document.cookie（datadome 为 HttpOnly）。"""
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


def _try_finish_session(page, cur_url: str = "") -> dict[str, str] | None:
    """G2 reviews 页已就绪且读到 datadome cookie 时返回。"""
    datadome_value = _extract_datadome_cookie(page)
    if not datadome_value:
        return None
    try:
        user_agent = page.user_agent or ""
    except Exception:
        user_agent = ""
    if not cur_url:
        cur_url = _page_url_bidi(page)
    return {
        "cookie": f"datadome={datadome_value}",
        "user_agent": user_agent,
        "url": cur_url,
    }


def _page_url_bidi(page) -> str:
    """用 browsingContext.getTree 取 URL，避免 page.url 走 script.evaluate 卡死。"""
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
    """仅 DataDome 可见 captcha iframe/容器内存在滑块手柄才算有滑块。"""
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

    if _is_g2_reviews_url(cur_url):
        if not _soft_blocked_light(page):
            return "ready", cur_url, ""
        # reviews 已有 cookie 且页面上没有可见滑块 → 视为通过（非用户可见 challenge）
        if _extract_datadome_cookie(page) and not _has_visible_slider(page):
            return "ready", cur_url, ""
        return "challenge", cur_url, ""

    cur_title = page.title or ""
    title_l = cur_title.lower()
    if _hard_blocked_title(title_l):
        return "blocked", cur_url, cur_title

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
    """PROXY_URL 清空时去掉 profile 里残留的 fpfile 代理认证（否则会一直弹框）。"""
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
        raise G2PageError("proxy_invalid", "", "", f"PROXY_URL 无效: {proxy_url}")

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
    """计算滑块拖到轨道右端的视口坐标。"""
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
) -> dict[str, str]:
    profile_dir = (user_dir or cfg.profiles_root / "default").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
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
    _apply_proxy(opts, user_dir=profile_dir, proxy_url=effective_proxy)
    opts.set_user_dir(str(profile_dir))

    page = FirefoxPage(opts)
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
                "代理认证失败：请使用指纹 Firefox(foxprint) 或检查 PROXY_URL",
            )

        page.wait.doc_loaded(timeout=_remaining(10))

        slider_attempts = 0
        while time.time() < session_deadline:
            state, cur_url, cur_title = _page_state(page)

            if state == "blocked":
                raise G2PageError(
                    "blocked", cur_url, cur_title,
                    "页面被 DataDome 风控拦截",
                )

            if state == "challenge":
                if _is_g2_reviews_url(cur_url) and _extract_datadome_cookie(page):
                    if not _has_visible_slider(page):
                        session = _try_finish_session(page, cur_url)
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
                    "滑块验证未通过",
                )

            # 必须等 G2 reviews 页加载完且无验证码 iframe，再取 cookie
            if state == "ready":
                session = _try_finish_session(page, cur_url)
                if session:
                    session["slider_attempts"] = slider_attempts
                    logger.info(
                        "cookie ok headless=%s slider_attempts=%d url=%s",
                        headless_on, slider_attempts, cur_url[:80],
                    )
                    return session

            time.sleep(_POLL_INTERVAL_SEC)

        raise G2PageError(
            "timeout", "", "",
            f"等待 G2 页面加载完成并获取 datadome cookie 超时（{wait_timeout}s）",
        )
    finally:
        page.quit()
