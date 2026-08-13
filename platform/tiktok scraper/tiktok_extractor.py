"""Moteur d'extraction TikTok base sur TikTokApi (davidteather).

Remplace la pipeline Playwright SSR/XHR/DOM maison tout en preservant le
schema de sortie attendu par worker.py / response_classifier.py :

  scrape(profile_url, max_posts, max_age_hours, ...) -> {
    "posts": [snake_case post dicts],
    "total": int,
    "url": str,
    "error": optional,
    "classification_signals": optional,
  }

Proxy (plan Webshare Residential Rotating):
  - NE PAS utiliser proxyproviders.Webshare (API /proxy/list = produit Proxy List
    statique, incompatible avec p.webshare.io sticky username/password).
  - Injecter le sticky `{base}-{country}-{sessionId}` via `proxies=[...]` et/ou
    `browser_context_factory` (TikTokApi >= 7.2).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger
from profile_extractor import item_to_post

LOGGER = get_logger(__name__, platform="tiktok", service="tiktok_extractor")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def username_from_profile_url(url: str) -> str:
    """Extrait le handle (@user) d'une URL profil TikTok."""
    try:
        path = urlparse(url or "").path
    except Exception:
        path = url or ""
    for segment in (path or "").split("/"):
        segment = (segment or "").strip()
        if segment.startswith("@"):
            return segment[1:].lower()
    return ""


def _ms_token_from_cookies(cookies: list[dict] | None) -> str | None:
    if not cookies:
        return None
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        if name == "msToken" or name.startswith("msToken"):
            value = str(cookie.get("value") or "").strip()
            if value:
                return value
    return None


def resolve_ms_tokens(
    *,
    proxy_identity: str = "",
    cookies: list[dict] | None = None,
) -> list[str] | None:
    """Recupere ms_token — par defaut: None (TikTokApi le genere via sleep_after).

    Un ms_token statique (TIKTOK_MS_TOKEN dans .env) est souvent desynchronise
    du proxy sticky / fingerprint courant → EmptyResponseException.
    Opt-in uniquement via TIKTOK_MS_TOKEN_FORCE=true.
    """
    if _env_bool("TIKTOK_MS_TOKEN_FORCE", False):
        for key in ("TIKTOK_MS_TOKEN", "ms_token", "MS_TOKEN"):
            raw = (os.getenv(key) or "").strip()
            if raw:
                LOGGER.info("[TikTokApi] using forced static ms_token from env")
                return [raw]

    # Cookies explicitement fournis pour CETTE session uniquement.
    token = _ms_token_from_cookies(cookies)
    if token:
        return [token]

    # Reuse sticky/global desactive par defaut (mismatch IP virgin frequente).
    if not _env_bool("TIKTOK_MS_TOKEN_REUSE_STICKY", False):
        return None

    if proxy_identity:
        try:
            import sticky_sessions

            sticky = sticky_sessions.load_sticky_cookies(proxy_identity)
            token = _ms_token_from_cookies(sticky)
            if token:
                return [token]
        except Exception:
            LOGGER.debug("sticky msToken load failed", exc_info=True)

    try:
        import json

        for key in ("TIKTOK_DIAG_SESSION_COOKIES_FILE", "TIKTOK_COOKIES_FILE"):
            path = (os.getenv(key) or "").strip()
            if path:
                break
        else:
            path = "tiktok_cookies.json"
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                token = _ms_token_from_cookies(raw)
                if token:
                    return [token]
    except Exception:
        LOGGER.debug("global cookies msToken load failed", exc_info=True)

    return None


def api_browser_candidates() -> list[str]:
    """Navigateurs a essayer (rotation soft_block). Defaut: chromium puis webkit."""
    raw = (os.getenv("TIKTOK_API_BROWSER_CANDIDATES") or "").strip()
    if raw:
        out = [b.strip().lower() for b in raw.replace(";", ",").split(",") if b.strip()]
        return out or ["chromium", "webkit"]
    single = (os.getenv("TIKTOK_API_BROWSER") or "").strip().lower()
    if single in ("webkit", "chromium", "chrome", "firefox"):
        return [single]
    # Suggestion native TikTokApi EmptyResponseException: essayer webkit.
    return ["chromium", "webkit"]


def _normalize_api_browser(name: str) -> str:
    n = (name or "chromium").strip().lower()
    if n in ("chrome", "chrome-beta", "msedge"):
        return "chromium"
    if n in ("webkit", "firefox", "chromium"):
        return n
    return "chromium"


def _proxy_identity(proxy_cfg: dict | None) -> str:
    if not isinstance(proxy_cfg, dict):
        return ""
    return str(proxy_cfg.get("username") or proxy_cfg.get("server") or "").strip()


def _to_playwright_proxy(proxy_cfg: dict | None) -> dict | None:
    """Normalise un dict proxy vers le format attendu par TikTokApi/Playwright."""
    if not proxy_cfg:
        return None
    server = (proxy_cfg.get("server") or "").strip()
    if not server:
        host = (proxy_cfg.get("host") or "").strip()
        port = proxy_cfg.get("port") or 80
        if host:
            server = f"http://{host}:{port}"
    if not server:
        return None
    out: dict[str, Any] = {"server": server}
    username = (proxy_cfg.get("username") or "").strip()
    password = proxy_cfg.get("password")
    if username:
        out["username"] = username
    if password is not None and str(password) != "":
        out["password"] = str(password)
    return out


def resolve_sticky_proxy(proxy_cfg: dict | None = None) -> dict | None:
    """Resolut un proxy sticky residential (meme IP pour toute la session TikTokApi)."""
    if _env_bool("TIKTOK_FORCE_DIRECT", False):
        return None

    cfg = _to_playwright_proxy(proxy_cfg) if proxy_cfg else None

    if cfg is None:
        try:
            from browser_session import parse_playwright_proxy

            cfg = _to_playwright_proxy(parse_playwright_proxy(None))
        except Exception:
            LOGGER.debug("parse_playwright_proxy failed", exc_info=True)

    if cfg is None:
        server = (
            os.getenv("TIKTOK_PROXY_SERVER")
            or os.getenv("TIKTOK_PROXY_URL")
            or ""
        ).strip()
        host = (os.getenv("TIKTOK_PROXY_HOST") or "p.webshare.io").strip()
        port = (os.getenv("TIKTOK_PROXY_PORT") or "80").strip() or "80"
        username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
        password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
        if not server and host:
            server = f"http://{host}:{port}"
        if server and (username or "@" in server):
            cfg = _to_playwright_proxy(
                {
                    "server": server,
                    "username": username,
                    "password": password,
                }
            )

    if not cfg:
        return None

    # Toujours forcer sticky `{base}-{country}-{session}` (jamais -rotate seul).
    try:
        from scraper import _assign_webshare_sticky_session

        username = str(cfg.get("username") or "")
        already_sticky = (
            username.count("-") >= 2
            and not username.endswith("-rotate")
            and "-rotate-" not in username
        )
        if already_sticky:
            return cfg
        sticky = _assign_webshare_sticky_session(dict(cfg))
        return _to_playwright_proxy(sticky) or cfg
    except Exception:
        LOGGER.debug("sticky assign failed; using raw proxy", exc_info=True)
        return cfg


def _heavy_asset_types() -> list[str] | None:
    """Types Playwright a bloquer (miroir TIKTOK_BLOCK_HEAVY_ASSETS de l'ancien moteur)."""
    if not _env_bool("TIKTOK_BLOCK_HEAVY_ASSETS", True):
        return None
    return ["image", "media", "font"]


def _make_browser_context_factory(
    proxy: dict | None,
    *,
    headless: bool,
    browser_name: str = "chromium",
    block_types: list[str] | None = None,
) -> Callable[[Any], Awaitable[Any]]:
    """Factory TikTokApi: lance chromium|webkit|firefox + context sticky.

    NOTE API TikTokApi: `browser_context_factory(playwright)` doit retourner un
    **BrowserContext** (stocke ensuite dans `api.browser` — naming trompeur).

    Blocage assets: route context-level (image/media/font) en plus de
    `suppress_resource_load_types` passe a create_sessions — pour ne pas perdre
    le comportement de l'ancien install_resource_blocker.
    """

    engine = _normalize_api_browser(browser_name)
    blocked = list(block_types or [])

    async def factory(playwright):
        launch_kwargs: dict[str, Any] = {
            "headless": bool(headless),
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        channel = ""
        if engine == "webkit":
            browser = await playwright.webkit.launch(**launch_kwargs)
        elif engine == "firefox":
            browser = await playwright.firefox.launch(**launch_kwargs)
        else:
            channel = (os.getenv("TIKTOK_BROWSER_CHANNEL") or "").strip().lower()
            want_chrome = (
                (browser_name or "").strip().lower() in ("chrome", "chrome-beta", "msedge")
                or _env_bool("TIKTOK_API_USE_CHROME_CHANNEL", False)
            )
            if want_chrome and channel in ("chrome", "chrome-beta", "msedge"):
                launch_kwargs["channel"] = channel
            elif want_chrome and not channel:
                launch_kwargs["channel"] = "chrome"
                channel = "chrome"
            else:
                channel = ""
            browser = await playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {}
        if proxy:
            context_kwargs["proxy"] = proxy
        context = await browser.new_context(**context_kwargs)

        if blocked:
            async def _abort_heavy(route, request):
                if request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _abort_heavy)

        try:
            setattr(context, "_tiktok_launcher_browser", browser)
        except Exception:
            pass
        LOGGER.info(
            "[TikTokApi] browser_context_factory ready proxy=%s headless=%s browser=%s channel=%s block=%s",
            (proxy or {}).get("username") or (proxy or {}).get("server") or "none",
            headless,
            engine,
            channel or "-",
            ",".join(blocked) if blocked else "off",
        )
        return context

    return factory


def _make_page_factory(
    block_types: list[str] | None = None,
) -> Callable[[Any], Awaitable[Any]] | None:
    """page_factory optionnel: new_page + route assets (ceinture + bretelles).

    Desactive par defaut: TikTokApi applique deja stealth + goto + suppress
    quand page_factory est absent. Active via TIKTOK_API_USE_PAGE_FACTORY=true.
    """
    if not _env_bool("TIKTOK_API_USE_PAGE_FACTORY", False):
        return None

    blocked = list(block_types or [])

    async def page_factory(context):
        page = await context.new_page()
        if blocked:
            async def _abort_heavy(route, request):
                if request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _abort_heavy)
        try:
            from playwright_stealth import stealth_async

            await stealth_async(page)
        except Exception:
            LOGGER.debug("playwright_stealth unavailable in page_factory", exc_info=True)
        await page.goto("https://www.tiktok.com", wait_until="domcontentloaded")
        return page

    return page_factory


def _video_count_from_user_info(user_data: Any) -> int | None:
    """Extrait videoCount depuis la reponse user.info() (structure variable)."""
    if not isinstance(user_data, dict):
        return None

    def _pick(node: dict) -> int | None:
        for key in ("videoCount", "video_count", "awemeCount", "aweme_count"):
            raw = node.get(key)
            if raw is None or raw == "":
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return None

    for path in (
        user_data,
        user_data.get("userInfo") if isinstance(user_data.get("userInfo"), dict) else None,
        (user_data.get("userInfo") or {}).get("stats")
        if isinstance(user_data.get("userInfo"), dict)
        else None,
        user_data.get("stats") if isinstance(user_data.get("stats"), dict) else None,
        user_data.get("user") if isinstance(user_data.get("user"), dict) else None,
        (user_data.get("user") or {}).get("stats")
        if isinstance(user_data.get("user"), dict)
        else None,
    ):
        if isinstance(path, dict):
            found = _pick(path)
            if found is not None:
                return found
    return None


def _is_post_within_hours(post: dict, hours: int) -> bool | None:
    if hours <= 0:
        return True
    published = post.get("published_at")
    if not published:
        return None
    try:
        published_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        return published_dt >= cutoff
    except (TypeError, ValueError):
        return None


def _map_video_to_post(video: Any, fallback_author: str) -> dict | None:
    """Convertit un Video TikTokApi → dict snake_case scraper."""
    raw: dict | None = None
    if hasattr(video, "as_dict"):
        try:
            raw = video.as_dict
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        if isinstance(video, dict):
            raw = video
        else:
            return None

    for wrap in ("itemStruct", "itemInfo", "item"):
        nested = raw.get(wrap)
        if isinstance(nested, dict) and (
            nested.get("id") or nested.get("aweme_id") or nested.get("video")
        ):
            raw = nested
            break

    return item_to_post(raw, fallback_author=fallback_author)


async def _extract_async(
    *,
    profile_url: str,
    username: str,
    max_posts: int,
    max_age_hours: int | None,
    headless: bool,
    proxy_cfg: dict | None,
    analyze_video_content: bool,
    browser_name: str | None = None,
) -> dict:
    from TikTokApi import TikTokApi

    sticky = resolve_sticky_proxy(proxy_cfg)
    identity = _proxy_identity(sticky)
    ms_tokens = resolve_ms_tokens(proxy_identity=identity)

    suppress = _heavy_asset_types()

    chosen_browser = _normalize_api_browser(
        browser_name
        or (os.getenv("TIKTOK_API_BROWSER") or "").strip()
        or "chromium"
    )

    num_sessions = max(1, _env_int("TIKTOK_API_NUM_SESSIONS", 2))
    # Laisser TikTok poser msToken sur la homepage (IP sticky courante).
    sleep_after = max(3, _env_int("TIKTOK_API_SLEEP_AFTER", 5))
    session_timeout_ms = max(15_000, _env_int("TIKTOK_API_SESSION_TIMEOUT_MS", 60_000))
    use_context_factory = _env_bool("TIKTOK_API_USE_CONTEXT_FACTORY", True)

    # Recovery TikTokApi >=7.2: une coupure proxy a la creation d'une session
    # ne doit pas tuer toute la tentative si min_sessions restent OK.
    # (enable_session_recovery + allow_partial_sessions + num_sessions>=2)
    session_kwargs: dict[str, Any] = {
        "num_sessions": num_sessions,
        "headless": headless,
        "sleep_after": sleep_after,
        "browser": chosen_browser,
        "enable_session_recovery": True,
        "allow_partial_sessions": True,
        "min_sessions": max(1, min(num_sessions, _env_int("TIKTOK_API_MIN_SESSIONS", 1))),
        "timeout": session_timeout_ms,
        "starting_url": "https://www.tiktok.com",
    }
    # Pas de ms_tokens= fixe sauf opt-in — la lib genere le cookie sur cette session.
    if ms_tokens:
        session_kwargs["ms_tokens"] = ms_tokens
    # Portage de l'ancien install_resource_blocker (image/media/font).
    if suppress:
        session_kwargs["suppress_resource_load_types"] = suppress

    page_factory = _make_page_factory(suppress)
    if page_factory is not None:
        session_kwargs["page_factory"] = page_factory

    if sticky:
        if use_context_factory:
            session_kwargs["browser_context_factory"] = _make_browser_context_factory(
                sticky,
                headless=headless,
                browser_name=chosen_browser,
                block_types=suppress,
            )
            proxy_mode = "sticky_context_factory"
        else:
            session_kwargs["proxies"] = [sticky]
            proxy_mode = "sticky_proxies"
        LOGGER.info(
            "[TikTokApi] sticky mode=%s user=%s browser=%s ms_token=%s sleep_after=%s "
            "timeout_ms=%s recovery=%s partial=%s block_assets=%s",
            proxy_mode,
            identity or sticky.get("server"),
            chosen_browser,
            "forced" if ms_tokens else "auto",
            sleep_after,
            session_timeout_ms,
            True,
            True,
            ",".join(suppress) if suppress else "off",
        )
    else:
        proxy_mode = "direct"
        LOGGER.warning(
            "[TikTokApi] No sticky proxy — scraping from local IP (likely blocked)"
        )

    posts: list[dict] = []
    video_count_meta: int | None = None
    user_info_ok = False
    error: str | None = None
    signals: dict[str, Any] = {
        "engine": "tiktokapi",
        "username": username,
        "ms_token_provided": bool(ms_tokens),
        "ms_token_mode": "forced" if ms_tokens else "auto_generate",
        "browser": chosen_browser,
        "proxy_mode": proxy_mode,
        "proxy_identity": identity[:80] if identity else "",
        "session_timeout_ms": session_timeout_ms,
        "sleep_after": sleep_after,
    }

    try:
        async with TikTokApi() as api:
            await api.create_sessions(**session_kwargs)
            if not getattr(api, "sessions", None):
                return {
                    "posts": [],
                    "total": 0,
                    "url": profile_url,
                    "error": "proxy_blocked:no_sessions",
                    "error_code": "no_sessions",
                    "classification_signals": signals,
                }

            user = api.user(username=username)

            try:
                user_data = await user.info()
                user_info_ok = True
                video_count_meta = _video_count_from_user_info(user_data)
                signals["user_info_ok"] = True
                signals["profile_video_count"] = video_count_meta
            except Exception as exc:
                signals["user_info_ok"] = False
                signals["user_info_error"] = str(exc)[:200]
                LOGGER.warning(
                    "[TikTokApi] user.info failed for @%s: %s",
                    username,
                    str(exc)[:160],
                )

            fetch_count = max_posts
            if max_age_hours and max_age_hours > 0:
                fetch_count = max(
                    max_posts,
                    min(200, max(_env_int("TIKTOK_API_24H_FETCH", 60), max_posts)),
                )

            try:
                async for video in user.videos(count=fetch_count):
                    post = _map_video_to_post(video, fallback_author=username)
                    if not post:
                        continue
                    if max_age_hours and max_age_hours > 0:
                        within = _is_post_within_hours(post, max_age_hours)
                        if within is False:
                            continue
                    posts.append(post)
                    if len(posts) >= max_posts:
                        break
            except Exception as exc:
                error = f"empty_feed_or_softblock:{exc}"
                signals["videos_error"] = str(exc)[:200]
                LOGGER.warning(
                    "[TikTokApi] user.videos failed for @%s: %s",
                    username,
                    str(exc)[:160],
                )

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        err_l = str(exc).lower()
        if any(tok in err_l for tok in ("auth", "407", "proxy", "tunnel", "err_invalid")):
            error = f"proxy_blocked:{exc}"
        elif any(tok in err_l for tok in ("timeout", "timed out")):
            error = f"proxy_blocked:attempt_timeout:{exc}"
        elif any(tok in err_l for tok in ("captcha", "challenge", "verify")):
            error = f"challenge_detected:{exc}"
        else:
            error = f"scrape_exception:{exc}"
        signals["session_error"] = str(exc)[:200]
        LOGGER.warning("[TikTokApi] session/extract failed: %s", str(exc)[:200], exc_info=True)

    seen: set[str] = set()
    unique_posts: list[dict] = []
    for post in posts:
        pid = str(post.get("post_id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        unique_posts.append(post)
    posts = unique_posts[:max_posts]

    if analyze_video_content and posts:
        try:
            from video_analysis import analyze_tiktok_video, build_small_video_report

            for post in posts:
                try:
                    report = analyze_tiktok_video(post.get("post_url") or "")
                    if report:
                        try:
                            post["video_report"] = build_small_video_report(report)
                        except Exception:
                            post["video_report"] = report
                except Exception:
                    LOGGER.debug("video analysis failed", exc_info=True)
        except Exception:
            LOGGER.debug("video_analysis import/run failed", exc_info=True)

    if not posts and not error:
        if user_info_ok and video_count_meta is not None and video_count_meta > 0:
            error = "empty_feed_or_softblock"
            signals["soft_empty"] = True
        elif user_info_ok and video_count_meta == 0:
            error = "no_posts_found"
        else:
            error = "empty_feed_or_softblock"
            signals["soft_empty"] = True

    result: dict[str, Any] = {
        "posts": posts,
        "total": len(posts),
        "url": profile_url,
        "page_report_docx": None,
        "page_report_pdf": None,
        "classification_signals": signals,
        "http_status": 200
        if (posts or user_info_ok)
        and not (error and "proxy_blocked" in str(error))
        else None,
    }
    if error and not posts:
        result["error"] = error
        result["error_code"] = str(error).split(":")[0]
    elif error and posts:
        result["warning"] = error

    LOGGER.info(
        "[TikTokApi] extract done @%s posts=%d videoCount=%s error=%s",
        username,
        len(posts),
        video_count_meta,
        (error or "")[:80] or None,
    )
    return result


def extract_profile(
    profile_url: str,
    *,
    max_posts: int = 20,
    max_age_hours: int | None = None,
    headless: bool = True,
    proxy_cfg: dict | None = None,
    analyze_video_content: bool = False,
    browser_name: str | None = None,
) -> dict:
    """Point d'entree sync: extrait les posts d'un profil via TikTokApi."""
    username = username_from_profile_url(profile_url)
    if not username:
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": "invalid_profile_url",
            "error_code": "invalid_profile_url",
        }

    max_posts = max(1, min(int(max_posts or 20), 200))
    age = int(max_age_hours) if max_age_hours else None
    kwargs = dict(
        profile_url=profile_url,
        username=username,
        max_posts=max_posts,
        max_age_hours=age,
        headless=headless,
        proxy_cfg=proxy_cfg,
        analyze_video_content=bool(analyze_video_content),
        browser_name=browser_name,
    )

    try:
        return asyncio.run(_extract_async(**kwargs))
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc) or "running event loop" in str(exc).lower():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_extract_async(**kwargs))
            finally:
                loop.close()
        raise
