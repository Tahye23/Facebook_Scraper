import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright
from pathlib import Path
from video_analysis import analyze_tiktok_video, build_small_video_report

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger, with_context
import sticky_sessions
from browser_session import (
    AntibotBlockedException,
    ProxyBlockedException,
    browser_launch_args,
    force_www_tiktok_url,
    goto_catch_http,
    goto_strict,
    install_resource_blocker,
    install_webdriver_mask,
    is_antibot_http_status,
    is_http_blocked_status,
    is_http_nav_failure,
    is_proxy_auth_error,
    nav_timeout_ms,
    parse_playwright_proxy,
    prepare_virgin_sticky_for_next_ip,
    purge_sticky_after_failure,
    raise_antibot_blocked,
    raise_proxy_blocked,
    sanitize_playwright_proxy,
)
from oembed import fetch_oembed_fallback
from profile_extractor import (
    extract_posts_from_hydration,
    extract_posts_from_hydration_payload,
    extract_posts_from_profile_page,
    post_has_usable_metrics,
    probe_hydration_signals,
)
from response_classifier import classify_from_result
import scrape_metrics
import identity_pool
from tiktok_parser import normalize_www_tiktok_url, posts_have_profile_metrics


COOKIES_FILE = "tiktok_cookies.json"
# Fichier JSON persistant des proxies mis en pause (cooldown 24h par defaut).
# Cle = identite sticky Webshare (username), valeur = {until, reason}.
PROXY_BLACKLIST_FILE = "proxy_blacklist.json"
_PROXY_BLACKLIST_LOCK = threading.Lock()
LOGGER = get_logger(__name__, platform="tiktok", service="scraper")
# Derniere tentative (diagnostic_runner lit bandwidth apres scrape).
_LAST_ATTEMPT_BANDWIDTH: dict = {"bytes": 0, "aborted": 0}

_MOBILE_UA_DEFAULT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
_MOBILE_SEC_CH_UA = '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'


def _cookies_file_path() -> str:
    """Chemin cookies: TIKTOK_COOKIES_FILE / TIKTOK_DIAG_SESSION_COOKIES_FILE / defaut."""
    for key in ("TIKTOK_DIAG_SESSION_COOKIES_FILE", "TIKTOK_COOKIES_FILE"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            return raw
    return COOKIES_FILE


def _env_bool(name: str, default: bool) -> bool:
    """Lit une variable d'environnement booleenne avec valeur par defaut.

    Valeurs considerees comme vraies: 1, true, yes, y, on.
    Si la variable n'existe pas, retourne `default`.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    """Lit une variable d'environnement entiere avec valeur de secours."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _finalize_classified_result(
    result: dict | None,
    *,
    country: str = "",
    identity: str = "",
    record: bool = True,
) -> dict:
    """Attache classification fine + metriques (Phase 0) sans changer le scrape."""
    out = dict(result or {})
    soft_cc = scrape_metrics.consecutive_soft_block_country_count()
    clf = classify_from_result(out, consecutive_soft_block_countries=soft_cc)
    out["classification"] = clf.scrape_class.value
    out["classification_action"] = clf.action.value
    out["classification_reason"] = clf.reason
    if clf.signals:
        merged = dict(out.get("classification_signals") or {})
        for key, value in clf.signals.items():
            if key not in merged or merged.get(key) in (None, "", [], 0):
                merged[key] = value
        out["classification_signals"] = merged

    LOGGER.info(
        "[CLASSIFIER] class=%s action=%s reason=%s country=%s",
        clf.scrape_class.value,
        clf.action.value,
        clf.reason,
        (country or "xx"),
        extra={
            "url": out.get("url"),
            "post_id": (identity or "")[:80] or None,
        },
    )

    if record and not _env_bool("TIKTOK_DIAG_MODE", False):
        posts = out.get("posts") or []
        success = bool(posts) and not str(out.get("error") or "").strip()
        # Succes partiel (posts + warning) compte comme succes metrique.
        if posts and str(out.get("warning") or "").strip():
            success = True
        scrape_metrics.record_attempt(
            country=country or "xx",
            scrape_class=clf.scrape_class.value,
            success=success,
            identity=identity,
            reason=clf.reason,
        )
        # Identity pool lifecycle (Phase 1)
        try:
            iid = identity_pool.identity_id_from_proxy_username(identity) if identity else ""
            if iid:
                identity_pool.apply_classification(
                    iid,
                    clf.scrape_class.value,
                    success=success,
                )
        except Exception:
            LOGGER.debug("identity_pool update failed", exc_info=True)
    return out


def _collect_page_classification_signals(page, base: dict | None = None) -> dict:
    """Signaux SSR/DOM pour le classifier (page encore ouverte)."""
    signals = dict(base or {})
    if page is None:
        return signals
    try:
        probed = probe_hydration_signals(page)
        if isinstance(probed, dict):
            for key, value in probed.items():
                if value not in (None, "", [], 0) or key not in signals:
                    signals[key] = value
    except Exception:
        LOGGER.debug("probe_hydration_signals failed", exc_info=True)
    try:
        signals["chrome_error"] = bool(_page_is_chrome_error(page))
    except Exception:
        signals.setdefault("chrome_error", False)
    try:
        if _looks_like_tiktok_challenge(page):
            signals["challenge_detected"] = True
    except Exception:
        pass
    return signals


def _to_iso_datetime(raw_value) -> str | None:
    """Normalise une date TikTok en ISO UTC si possible."""
    if raw_value in (None, ""):
        return None

    if isinstance(raw_value, (int, float)):
        ts = int(raw_value)
        # TikTok peut parfois renvoyer en millisecondes.
        if ts > 10_000_000_000:
            ts = ts // 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None

    text = str(raw_value).strip()
    if not text:
        return None

    if text.isdigit():
        return _to_iso_datetime(int(text))

    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _is_post_within_hours(post: dict, hours: int) -> bool | None:
    """Retourne True si le post est dans la fenetre temporelle, False sinon, None si inconnu."""
    if hours <= 0:
        return True

    published_iso = _to_iso_datetime(post.get("published_at"))
    if not published_iso:
        return None

    try:
        published_dt = datetime.fromisoformat(published_iso)
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        return published_dt >= cutoff_dt
    except ValueError:
        return None


def load_cookies() -> list:
    """Charge et normalise les cookies TikTok depuis le fichier cookies configure.

    Objectif:
    - Accepter un export JSON de cookies (liste d'objets).
    - Garder uniquement les champs utiles pour Playwright.
    - Retourner une liste prete pour `context.add_cookies(...)`.
    """
    path = _cookies_file_path()
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        LOGGER.warning("Failed to read cookies file %s", path, exc_info=True)
        return []

    if not isinstance(raw, list):
        return []

    cookies = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not name or value is None or not domain:
            continue

        cookie = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }

        same_site = str(c.get("sameSite", "None")).capitalize()
        if same_site not in ("Lax", "Strict", "None"):
            same_site = "None"
        cookie["sameSite"] = same_site

        expires = c.get("expires", c.get("expirationDate"))
        if expires is not None:
            try:
                cookie["expires"] = int(float(expires))
            except (TypeError, ValueError):
                LOGGER.debug("Invalid cookie expires value ignored", extra={"post_id": None})

        cookies.append(cookie)

    return cookies


def _normalize_profile_url(url: str) -> str:
    """Valide et nettoie une URL TikTok de profil.

    Par defaut www uniquement. Si TIKTOK_ALLOW_MOBILE_HOST=true (diagnostic
    mobile_ua), conserve m.tiktok.com.
    """
    allow_mobile = _env_bool("TIKTOK_ALLOW_MOBILE_HOST", False)
    raw = (url or "").strip()
    if allow_mobile and "m.tiktok.com" in raw.lower():
        parsed = urlparse(raw)
        username = ""
        for segment in (parsed.path or "").split("/"):
            if segment.startswith("@"):
                username = segment[1:].lower()
                break
        if username:
            return f"https://m.tiktok.com/@{username}"
        return f"https://m.tiktok.com{(parsed.path or '').rstrip('/')}" or "https://m.tiktok.com"

    # Interdit m.tiktok.com (404 natif) → www + username minuscule.
    forced = force_www_tiktok_url(url) or url
    normalized = normalize_www_tiktok_url(forced)
    parsed = urlparse(normalized or forced)
    if "tiktok.com" not in (parsed.netloc or "").lower():
        raise ValueError("URL TikTok invalide")
    username = ""
    for segment in (parsed.path or "").split("/"):
        if segment.startswith("@"):
            username = segment[1:].lower()
            break
    if username:
        return f"https://www.tiktok.com/@{username}"
    clean = f"https://www.tiktok.com{(parsed.path or '').rstrip('/')}"
    return clean or "https://www.tiktok.com"


def _username_from_url(url: str) -> str:
    """Extrait le handle (@username) d'une URL de profil TikTok, en minuscules.

    Ex: "https://www.tiktok.com/@Tawatur" -> "tawatur". Retourne "" si absent.
    """
    try:
        path = urlparse(url).path
    except Exception:
        path = url or ""
    for segment in path.split("/"):
        segment = segment.strip()
        if segment.startswith("@"):
            return segment[1:].lower()
    return ""


def _card_belongs_to_profile(card: dict, target_username: str) -> bool:
    """Vrai si le post appartient bien au profil cible.

    Garde-fou contre la fuite de videos "For You": `window.SIGI_STATE.ItemModule`
    conserve en memoire les videos du feed d'accueil charge pendant le warmup,
    et `_extract_video_cards` les remonte sans distinction d'auteur. On ne
    conserve donc un post que si:
    - source reseau profile-grid (`_source=network_api`) apres clear homepage, ou
    - son auteur correspond au handle cible (insensible a la casse), ou
    - son `post_url` contient `/@<handle>/` (grille du profil), ou
    - on ne connait pas le handle cible (target vide -> on ne filtre pas).

    Les posts DOM/SSR sans auteur ET sans handle dans l'URL sont rejetes.
    """
    if not target_username:
        return True

    # Posts interceptes via /api/preload|post/item_list/ sur le profil: fiables
    # (le buffer homepage a ete vide avant la nav profil).
    if str(card.get("_source") or "") == "network_api":
        return True

    author = str(card.get("author") or "").strip().lower()
    if author and author == target_username:
        return True

    post_url = str(card.get("post_url") or card.get("video_url") or "").strip().lower()
    if f"/@{target_username}/" in post_url:
        return True

    return False


def _stamp_network_api_post(card: dict, target_username: str) -> dict:
    """Normalise un post capture via XHR grille profil (auteur + URL + source)."""
    out = dict(card) if isinstance(card, dict) else {}
    out["_source"] = "network_api"
    handle = (target_username or "").strip().lstrip("@").lower()
    author = str(out.get("author") or "").strip().lstrip("@")
    if not author and handle:
        out["author"] = handle
        author = handle
    post_id = str(out.get("post_id") or out.get("id") or "").strip()
    if post_id and not out.get("post_url"):
        if author:
            out["post_url"] = f"https://www.tiktok.com/@{author}/video/{post_id}"
        else:
            out["post_url"] = f"https://www.tiktok.com/video/{post_id}"
    if post_id and not out.get("post_id"):
        out["post_id"] = post_id
    return out


def _video_signature(video: dict) -> str:
    """Construit une signature stable pour dedupliquer les videos.

    Priorite:
    1) post_id
    2) URL du post
    """
    vid = str(video.get("post_id") or "").strip()
    if vid:
        return f"id:{vid}"
    return f"u:{video.get('post_url', '')}"


def _human_pause(base: float = 1.0, jitter: float = 0.6):
    """Ajoute une pause pseudo-humaine pour reduire les patterns robotiques."""
    time.sleep(base + random.uniform(0.0, jitter))


def _install_stealth_scripts(context):
    """Injecte le camouflage anti-empreinte AVANT creation de page / navigation.

    Couvre webdriver, chrome.app, languages, plugins, permissions.
    Toujours applique (sticky virgin inclus) — pas d'opt-out par defaut.
    """
    install_webdriver_mask(context)
    context.add_init_script(
        """
        (() => {
          try { Object.defineProperty(navigator, 'platform', { get: () => 'Win32' }); } catch (e) {}
          try { Object.defineProperty(navigator, 'language', { get: () => 'en-US' }); } catch (e) {}
          try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] }); } catch (e) {}
          try {
            Object.defineProperty(navigator, 'plugins', {
              get: () => [1, 2, 3, 4, 5],
            });
          } catch (e) {}
          try {
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
          } catch (e) {}
          try {
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
          } catch (e) {}
        })();
        """
    )


def _force_direct_mode() -> bool:
    """True = IP locale, proxy=None (TIKTOK_FORCE_DIRECT=true)."""
    return _env_bool("TIKTOK_FORCE_DIRECT", False)


def _diag_softblock_enabled() -> bool:
    """Logs diagnostiques soft-block (IP, cookies, HTTP profil, hydration)."""
    return _env_bool("TIKTOK_DIAG_SOFTBLOCK", True)


def _is_webshare_proxy_host(host_or_url: str) -> bool:
    """True pour hosts Webshare (datacenter OU residentiel).

    Exemples acceptes:
      - p.webshare.io
      - p.residential.webshare.io
      - *.residential.webshare.io
    """
    h = (host_or_url or "").strip().lower()
    if not h:
        return False
    return "webshare.io" in h


def _rotating_proxy_mode() -> bool:
    """True si Webshare Rotating Proxy Endpoint (plus de pool 20k).

    Active via TIKTOK_PROXY_MODE=rotate (defaut recommande) ou auto-detect
    username `*-rotate` / host Webshare (datacenter ou residentiel).
    """
    if _force_direct_mode():
        return False
    mode = (os.getenv("TIKTOK_PROXY_MODE") or "rotate").strip().lower()
    if mode in ("rotate", "rotating", "endpoint"):
        return True
    if mode in ("sticky", "list", "file", "pool"):
        return False
    username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip().lower()
    server = (os.getenv("TIKTOK_PROXY_SERVER") or os.getenv("TIKTOK_PROXY_URL") or "").strip().lower()
    host = (os.getenv("TIKTOK_PROXY_HOST") or "").strip().lower()
    if username.endswith("-rotate") or "-rotate" in username:
        return True
    if _is_webshare_proxy_host(server) or _is_webshare_proxy_host(host):
        return True
    return False


def _blacklist_enabled() -> bool:
    """Blacklist locale desactivee en mode rotate / force-direct."""
    if _force_direct_mode() or _rotating_proxy_mode():
        return False
    return _env_bool("TIKTOK_PROXY_BLACKLIST_ENABLED", True)


def _is_real_chrome_channel(channel: str | None = None) -> bool:
    ch = (channel if channel is not None else (os.getenv("TIKTOK_BROWSER_CHANNEL") or "")).strip().lower()
    return ch.startswith("chrome") or ch.startswith("msedge")


def _force_http_playwright_proxy(
    *,
    host: str,
    port: int | None,
    username: str,
    password: str,
    source_scheme: str = "",
) -> dict:
    """Normalise vers le format Playwright HTTP avec auth separee.

    Chromium refuse l'auth SOCKS5:
      "Browser does not support socks5 proxy authentication"
    Toute URL socks5://…:1080 (Webshare) → http://host:80.
    """
    return sanitize_playwright_proxy(
        {
            "server": f"http://{host}:{int(port) if port else 80}",
            "username": username,
            "password": password,
            "_scheme": source_scheme or "http",
        }
    ) or {"server": f"http://{host}:{int(port) if port else 80}"}


def _parse_proxy_url_env() -> dict | None:
    """Parse PROXY_URL / TIKTOK_PROXY_URL → dict Playwright (server + user + pass)."""
    return parse_playwright_proxy(None)


def _webshare_username_base(raw_username: str) -> str:
    """Extrait le username Webshare de base (sans -rotate / -us / session).

    Docs: https://apidocs.webshare.io/proxy-connection
      sdwopfmy-rotate      → sdwopfmy
      sdwopfmy-us-1234     → sdwopfmy
    """
    u = (raw_username or "").strip()
    if not u:
        return "sdwopfmy"
    lower = u.lower()
    if "-rotate" in lower:
        return u[: lower.index("-rotate")].rstrip("-") or "sdwopfmy"
    parts = u.split("-")
    # base-country[-session|-city_...]
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[0] or "sdwopfmy"
    return u


def _webshare_sticky_countries() -> list[str]:
    """Pays cibles (ISO-2) pour sticky Webshare — defaut US/EU."""
    raw = (os.getenv("TIKTOK_PROXY_COUNTRIES") or "us,fr,de,gb").strip()
    countries = [
        c.strip().lower()
        for c in raw.replace(";", ",").split(",")
        if c.strip() and len(c.strip()) == 2
    ]
    return countries or ["us"]


def _assign_webshare_sticky_session(proxy_cfg: dict | None) -> dict | None:
    """Force sticky session Webshare: MEME IP pour warmup + profil.

    Format officiel (NE PAS combiner avec -rotate):
      {base}-{country}-{session_id}
      ex: sdwopfmy-us-84729103

    -rotate change d'IP a CHAQUE requete HTTP → cookies ttwid/msToken
    emis sur IP A, profil vu depuis IP B → squelette / WAF.
    Sticky = 1 session_id = 1 IP pour toute la duree du browser.

    Phase 1: prefere une identite warm du Identity Pool pour le pays choisi;
    sinon cree une sticky virgin et l'enregistre.
    """
    if not proxy_cfg:
        return None
    if not _env_bool("TIKTOK_PROXY_STICKY_SESSION", True):
        return proxy_cfg

    out = dict(proxy_cfg)
    base = _webshare_username_base(str(out.get("username") or ""))
    country = random.choice(_webshare_sticky_countries())
    session_id: str | None = None
    # Reutiliser une sticky warm/active si disponible (meme pays).
    try:
        if identity_pool.enabled():
            reused = identity_pool.acquire_for_country(
                country, base_user=base, prefer_warm=True
            )
            if reused and reused.session_id:
                session_id = reused.session_id
                country = reused.country or country
                LOGGER.info(
                    "[IDENTITY] assigning reused sticky %s",
                    reused.identity_id,
                )
    except Exception:
        LOGGER.debug("identity_pool acquire failed", exc_info=True)

    if not session_id:
        # Session ID numerique (doc Webshare: username-us-1234).
        session_id = str(random.randint(10_000_000, 99_999_999))
        try:
            if identity_pool.enabled():
                identity_pool.create_virgin(
                    base_user=base, country=country, session_id=session_id
                )
        except Exception:
            LOGGER.debug("identity_pool create_virgin failed", exc_info=True)

    sticky_user = f"{base}-{country}-{session_id}"
    out["username"] = sticky_user
    # Conserve le pays pour aligner locale/timezone Playwright (etape 2).
    out["_sticky_country"] = country
    sanitized = sanitize_playwright_proxy(out) or out
    if isinstance(sanitized, dict):
        sanitized["_sticky_country"] = country
    LOGGER.info(
        "Webshare STICKY session — same IP for warmup+profile "
        "(not -rotate; new session_id = new IP next attempt)",
        extra={"url": f"user={sticky_user}"},
    )
    return sanitized


# Geo Playwright aligne sur le pays sticky (locale / TZ / Accept-Language).
# Mis a jour au debut de chaque tentative scrape.
_ACTIVE_BROWSER_GEO: dict = {
    "country": "us",
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "accept_language": "en-US,en;q=0.9",
    "geolocation": {"longitude": -74.006, "latitude": 40.7128},
}

_GEO_BY_COUNTRY: dict[str, dict] = {
    "us": {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "accept_language": "en-US,en;q=0.9",
        "geolocation": {"longitude": -74.006, "latitude": 40.7128},
    },
    "fr": {
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris",
        "accept_language": "fr-FR,fr;q=0.9,en;q=0.8",
        "geolocation": {"longitude": 2.3522, "latitude": 48.8566},
    },
    "de": {
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "accept_language": "de-DE,de;q=0.9,en;q=0.8",
        "geolocation": {"longitude": 13.405, "latitude": 52.52},
    },
    "gb": {
        "locale": "en-GB",
        "timezone_id": "Europe/London",
        "accept_language": "en-GB,en;q=0.9",
        "geolocation": {"longitude": -0.1276, "latitude": 51.5074},
    },
    "uk": {
        "locale": "en-GB",
        "timezone_id": "Europe/London",
        "accept_language": "en-GB,en;q=0.9",
        "geolocation": {"longitude": -0.1276, "latitude": 51.5074},
    },
}


def _sticky_country_from_proxy(proxy_cfg: dict | None) -> str:
    """Extrait le code pays ISO-2 depuis sticky username ou `_sticky_country`."""
    if not proxy_cfg:
        return "us"
    tagged = str(proxy_cfg.get("_sticky_country") or "").strip().lower()
    if len(tagged) == 2:
        return tagged
    username = str(proxy_cfg.get("username") or "").strip().lower()
    # Formats: base-us-84729103 | base-us-rotate | base-us
    parts = username.split("-")
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[1]
    return "us"


def _browser_geo_for_country(country: str) -> dict:
    """Mappe un pays sticky vers locale / timezone / Accept-Language / geoloc."""
    cc = (country or "us").strip().lower() or "us"
    base = dict(_GEO_BY_COUNTRY.get(cc) or _GEO_BY_COUNTRY["us"])
    base["country"] = cc if cc in _GEO_BY_COUNTRY else "us"
    return base


def _set_active_browser_geo(geo: dict | None) -> None:
    global _ACTIVE_BROWSER_GEO
    if not geo:
        return
    _ACTIVE_BROWSER_GEO = dict(geo)


def _active_accept_language() -> str:
    return str(
        _ACTIVE_BROWSER_GEO.get("accept_language") or "en-US,en;q=0.9"
    )


def _build_rotating_proxy_config() -> dict | None:
    """Config de base Playwright pour endpoint Webshare p.webshare.io:80.

    Le username final sticky (country+session) est assigne par
    `_assign_webshare_sticky_session` a chaque tentative.
    """
    if _env_bool("TIKTOK_PROXY_PREFER_SOCKS5", False):
        LOGGER.warning(
            "TIKTOK_PROXY_PREFER_SOCKS5=true ignored — Chromium requires HTTP proxy auth"
        )

    parsed = _parse_proxy_url_env()
    if parsed:
        # Normaliser: garder server/password, base username sans -rotate.
        base = _webshare_username_base(str(parsed.get("username") or ""))
        parsed = dict(parsed)
        parsed["username"] = base
        server = str(parsed.get("server") or "")
        LOGGER.info(
            "Webshare proxy base ready (sticky sessions applied per attempt)",
            extra={
                "url": server,
                "post_id": "user_base=" + base,
            },
        )
        return sanitize_playwright_proxy(parsed) or parsed

    username = _webshare_username_base(
        (os.getenv("TIKTOK_PROXY_USERNAME") or "sdwopfmy").strip()
    )
    password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
    host = (
        os.getenv("TIKTOK_PROXY_HOST") or "p.webshare.io"
    ).strip() or "p.webshare.io"
    try:
        port = int((os.getenv("TIKTOK_PROXY_PORT") or "80").strip() or "80")
    except ValueError:
        port = 80

    proxy = sanitize_playwright_proxy(
        {"server": f"http://{host}:{port}", "username": username, "password": password}
    )
    LOGGER.info(
        "Webshare proxy base ready (sticky sessions applied per attempt)",
        extra={"url": str((proxy or {}).get("server") or "")},
    )
    return proxy


def _build_proxy_config() -> dict | None:
    """Construit la config proxy unique (rotate endpoint ou PROXY_SERVER legacy)."""
    if _rotating_proxy_mode():
        return _build_rotating_proxy_config()

    parsed = _parse_proxy_url_env()
    if parsed:
        return parsed

    server = (os.getenv("TIKTOK_PROXY_SERVER") or "").strip()
    if not server:
        return None

    # Forcer HTTP meme si server etait socks5://…
    source_scheme = "http"
    try:
        parsed_srv = urlsplit(server if "://" in server else f"http://{server}")
        host = parsed_srv.hostname or server
        port = parsed_srv.port
        username = unquote(parsed_srv.username) if parsed_srv.username else ""
        password = unquote(parsed_srv.password) if parsed_srv.password is not None else ""
        source_scheme = parsed_srv.scheme or "http"
    except Exception:
        host = server
        port = None
        username = ""
        password = ""
    if not username:
        username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
    if not password:
        password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
    return _force_http_playwright_proxy(
        host=host,
        port=port,
        username=username,
        password=password,
        source_scheme=source_scheme,
    )


def _proxy_identity(proxy_cfg: dict | None) -> str:
    """Cle stable d'un proxy pour logs / sticky / dedup.

    Rotate: username `…-rotate` (Webshare change l'IP a chaque session).
    Sticky legacy: username `sdwopfmy-N` = 1 IP.
    """
    if not proxy_cfg:
        return "direct"
    username = str(proxy_cfg.get("username") or "").strip()
    server = str(proxy_cfg.get("server") or "").strip()
    if username:
        return username
    return server or "proxy"


def _blacklist_path() -> Path:
    """Chemin du fichier blacklist (a cote du scraper, pas du cwd)."""
    raw = (os.getenv("TIKTOK_PROXY_BLACKLIST_FILE") or PROXY_BLACKLIST_FILE).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def _load_blacklist() -> dict:
    """Charge la blacklist et purge les entrees expirees."""
    path = _blacklist_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to read proxy blacklist; starting empty", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}

    now = datetime.now(tz=timezone.utc)
    alive = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        until_raw = entry.get("until")
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if until > now:
            alive[str(key)] = {"until": until.isoformat(), "reason": entry.get("reason") or ""}
    return alive


def _save_blacklist(data: dict) -> None:
    path = _blacklist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        LOGGER.warning("Failed to persist proxy blacklist", exc_info=True)


def _is_blacklisted(proxy_cfg: dict | None) -> bool:
    if not proxy_cfg or not _blacklist_enabled():
        return False
    with _PROXY_BLACKLIST_LOCK:
        return _proxy_identity(proxy_cfg) in _load_blacklist()


def _blacklist_proxy(proxy_cfg: dict | None, reason: str, hours: int | None = None) -> None:
    """Met un proxy en cooldown. No-op en mode rotating endpoint Webshare.

    `hours` override le defaut env. Soft-block (no_posts) = cooldown court;
    auth/tunnel morts = cooldown long.
    """
    if not _blacklist_enabled():
        LOGGER.info(
            "Blacklist skipped (Webshare rotating endpoint — IP rotation is provider-side)",
            extra={"error": reason, "url": _describe_proxy(proxy_cfg) if proxy_cfg else "n/a"},
        )
        return
    if not proxy_cfg:
        return
    if hours is None:
        hours = _blacklist_hours_for_reason(reason)
    hours = max(1, int(hours))
    key = _proxy_identity(proxy_cfg)
    until = datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    with _PROXY_BLACKLIST_LOCK:
        data = _load_blacklist()
        data[key] = {"until": until.isoformat(), "reason": (reason or "")[:200]}
        _save_blacklist(data)
    LOGGER.warning(
        "Proxy blacklisted for cooldown (%sh)",
        hours,
        extra={"url": _describe_proxy(proxy_cfg), "error": reason, "post_id": key},
    )


def _blacklist_hours_for_reason(reason: str) -> int:
    err = (reason or "").strip().lower()
    # Timeout TLS / HTTP antibot: cooldown long (24h) — IP brulee.
    if any(
        tok in err
        for tok in (
            "tls_timeout",
            "proxy_blocked",
            "err_timed_out",
            "timeout",
            "http_403",
            "http 403",
            "err_http_response_code_failure",
            "429",
            "503",
        )
    ):
        return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_HOURS", 24))
    if "no_posts_found" in err or "challenge_detected" in err:
        return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_SOFT_HOURS", 2))
    if "invalid_auth" in err:
        return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_AUTH_HOURS", 6))
    return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_HOURS", 24))


def _should_blacklist_error(error_code: str) -> bool:
    """True si l'erreur justifie un cooldown 24h (timeout / 403 / no_posts / nav).

    Les collisions de profil Chrome (SingletonLock) ne blacklisent PAS le proxy:
    l'IP est saine, seul le dossier session est momentanement pris.
    Mode rotate: toujours False (blacklist locale desactivee).
    """
    if not _blacklist_enabled():
        return False
    err = (error_code or "").strip().lower()
    if _is_profile_lock_error(err):
        return False
    return _is_retryable_soft_error(error_code)


def _is_profile_lock_error(error_code: str) -> bool:
    err = (error_code or "").strip().lower()
    return any(
        tok in err
        for tok in (
            "sticky_profile_in_use",
            "profile appears to be in use",
            "singletonlock",
            "process_singleton",
            # Crash local Chrome/Xvfb: ce n'est PAS la faute du proxy.
            "target page, context or browser has been closed",
            "targetclosederror",
            "browser has been closed",
            "launch_persistent_context",
            "xserver running",
            "headed browser without having a xserver",
        )
    )


def _is_fatal_proxy_auth_error(error_code: str) -> bool:
    """Auth/tunnel proxy: ne pas boucler sur la meme credential (abort retries)."""
    err = (error_code or "").strip().lower()
    return any(
        tok in err
        for tok in (
            "proxy_blocked:auth",
            "err_proxy_connection_failed",
            "err_invalid_auth",
            "invalid_auth_credentials",
            "proxy authentication",
        )
    )


def _is_retryable_soft_error(error_code: str) -> bool:
    """Erreurs pour lesquelles on doit retenter / changer de proxy (pas abandonner).

    Inclut aussi les erreurs Playwright de navigation (ex: "interrupted by
    another navigation") qui ne contiennent pas `net::ERR_...` mais sont
    typiques d'un anti-bot / redirect TikTok — sans ca, on arretait apres
    1 seul proxy au lieu d'essayer les 4.

    Auth proxy (ERR_PROXY_CONNECTION_FAILED): NON retryable ici — le caller
    abort pour ne pas boucler sur la meme credential.
    """
    err = (error_code or "").strip().lower()
    if not err:
        return False
    if _is_fatal_proxy_auth_error(err):
        return False
    if err in {
        "challenge_detected",
        "no_posts_found",
        "empty_feed_or_softblock",
        "proxy_blocked",
        "proxy_soft_blocked",
        "all_proxies_blocked",
        "sticky_profile_in_use",
        "antibot_blocked",
    }:
        return True
    if (
        "proxy_blocked" in err
        or "proxy_soft_blocked" in err
        or "antibot" in err
        or "empty_feed_or_softblock" in err
    ):
        return True
    if _is_profile_lock_error(err):
        return True
    tokens = (
        "net::err",
        "err_timed_out",
        "err_http_response_code_failure",
        "err_connection",
        "err_tunnel",
        "err_aborted",
        "err_name_not_resolved",
        "err_address_unreachable",
        "403",
        "404",
        "405",
        "429",
        "503",
        "interrupted by another navigation",
        "navigation to",
        "page.goto",
        "timeout",
        "target closed",
        "browser has been closed",
        "connection closed",
        "launch_persistent_context",
        "http_404",
        "http_403",
        "http_429",
        "http_503",
    )
    return any(tok in err for tok in tokens)


def _resolve_proxy_file_path() -> Path | None:
    """Chemin du fichier pool Webshare (20k lignes), ou None si absent."""
    raw = (os.getenv("TIKTOK_PROXY_FILE") or "webshare_residential_proxies.txt").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path if path.exists() else None


def _iter_proxy_specs_from_sources() -> list[str]:
    """Collecte les specs proxy (mode legacy pool fichier / LIST).

    En mode rotate, cette liste n'est PAS utilisee — voir
    `_build_rotating_proxy_config`.
    """
    if _rotating_proxy_mode():
        return []

    specs: list[str] = []

    proxy_file = _resolve_proxy_file_path()
    if proxy_file is not None:
        try:
            with open(proxy_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        specs.append(line)
        except OSError:
            LOGGER.warning("Failed to read proxy file", extra={"url": str(proxy_file)}, exc_info=True)

    raw_list = (os.getenv("TIKTOK_PROXY_LIST") or "").replace(";", "\n")
    for line in raw_list.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            specs.append(line)

    inline = (os.getenv("TIKTOK_PROXY_SERVER") or "").strip()
    if inline:
        # Reconstruit une spec host:port:user:pass si user/pass fournis a part.
        username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
        password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
        if username and "://" not in inline and "|" not in inline and inline.count(":") == 1:
            specs.append(f"{inline}:{username}:{password}")
        else:
            specs.append(inline)

    return specs


def _parse_all_proxies() -> list[dict]:
    """Parse + deduplique toutes les specs disponibles (fichier + env)."""
    candidates: list[dict] = []
    seen: set[str] = set()
    for spec in _iter_proxy_specs_from_sources():
        proxy = _parse_proxy_spec(spec)
        if not proxy:
            continue
        key = _proxy_identity(proxy)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(proxy)
    return candidates


def _select_proxy_candidates(limit: int | None = None) -> list[dict | None]:
    """Candidats proxy pour un job.

    Mode rotate (defaut): endpoint Webshare unique repete N fois — chaque
    tentative ouvre une session navigateur neuve; Webshare fournit une nouvelle
    IP residentielle. Warmup homepage conserve sur chaque tentative.

    Mode legacy (TIKTOK_PROXY_MODE=pool): echantillon depuis fichier 20k.
    """
    if _force_direct_mode():
        LOGGER.warning(
            "TIKTOK_FORCE_DIRECT=true — scraping via IP locale (proxy=None, no rotation)"
        )
        return [None]

    if limit is None:
        limit = max(
            1,
            _env_int("MAX_PROXY_RETRIES", 0) or _env_int("TIKTOK_PROXY_MAX_PER_JOB", 5),
        )

    if _rotating_proxy_mode():
        cfg = _build_rotating_proxy_config()
        if cfg is None:
            LOGGER.warning("Rotating proxy mode enabled but no proxy config resolved")
            return [None]
        # N sticky sessions distinctes (= N IPs fixes warmup+profil, pays US/EU).
        selected = [_assign_webshare_sticky_session(dict(cfg)) for _ in range(limit)]
        countries = ",".join(_webshare_sticky_countries())
        LOGGER.info(
            "Using Webshare STICKY sessions (same IP warmup+profile; rotate session_id on fail)",
            extra={
                "post_id": None,
                "url": (
                    f"server={cfg.get('server')} base_user={cfg.get('username')} "
                    f"countries={countries} attempts={limit} sticky=on"
                ),
            },
        )
        if _env_bool("TIKTOK_TRY_DIRECT_AFTER_PROXIES", False):
            selected = list(selected) + [None]
        return selected

    pool = _parse_all_proxies()
    if not pool:
        return [None]

    by_id: dict[str, dict] = {}
    for proxy in pool:
        identity = _proxy_identity(proxy)
        if identity and identity not in by_id:
            by_id[identity] = proxy

    with _PROXY_BLACKLIST_LOCK:
        blocked = set(_load_blacklist().keys()) if _blacklist_enabled() else set()

    available = [p for p in pool if _proxy_identity(p) not in blocked]
    if not available:
        LOGGER.warning(
            "All proxies are blacklisted; sampling from full pool anyway",
            extra={"post_id": None},
        )
        available = pool

    sticky_pool_size = _env_int("TIKTOK_STICKY_POOL_SIZE", 0)
    if sticky_pool_size > 0:
        dedicated_ids = {_proxy_identity(p) for p in pool[:sticky_pool_size]}
        dedicated = [p for p in available if _proxy_identity(p) in dedicated_ids]
        if dedicated:
            if len(dedicated) < limit:
                others = [p for p in available if _proxy_identity(p) not in dedicated_ids]
                random.shuffle(others)
                available = dedicated + others
            else:
                available = dedicated

    if sticky_sessions.sticky_enabled():
        free = [p for p in available if not sticky_sessions.is_session_busy(_proxy_identity(p))]
        if free:
            available = free

    cookie_ids = (
        sticky_sessions.identities_with_valid_cookies()
        if sticky_sessions.sticky_enabled()
        else set()
    )
    with_cookies: list[dict] = []
    seen_ids: set[str] = set()
    for identity in cookie_ids:
        if identity in blocked or sticky_sessions.is_session_busy(identity):
            continue
        proxy = by_id.get(identity)
        if proxy is None:
            continue
        with_cookies.append(proxy)
        seen_ids.add(identity)
    for proxy in available:
        identity = _proxy_identity(proxy)
        if identity in cookie_ids and identity not in seen_ids:
            with_cookies.append(proxy)
            seen_ids.add(identity)

    without_cookies = [p for p in available if _proxy_identity(p) not in seen_ids]
    random.shuffle(with_cookies)
    random.shuffle(without_cookies)
    selected = (with_cookies + without_cookies)[:limit]

    LOGGER.info(
        "Selected proxy sample for job",
        extra={
            "post_id": None,
            "url": (
                f"pool={len(pool)} available={len(available)} "
                f"with_cookies={len(with_cookies)} selected={len(selected)} "
                f"mode=cookie_slots"
            ),
        },
    )

    if _env_bool("TIKTOK_TRY_DIRECT_AFTER_PROXIES", False):
        selected = list(selected) + [None]
    return selected


def _load_proxy_candidates() -> list[dict | None]:
    """Compat: candidats proxy pour un job."""
    return _select_proxy_candidates()


def get_proxy_pool() -> list[dict]:
    """Proxies pour les lanes batch.

    Mode rotate: N copies du meme endpoint (1 par lane). Webshare alloue une
    IP residentielle distincte par connexion navigateur.
    """
    concurrency = max(1, _env_int("TIKTOK_BATCH_CONCURRENCY", 6))
    if _rotating_proxy_mode():
        cfg = _build_rotating_proxy_config()
        if cfg is None:
            return []
        # Une sticky session distincte par lane (IP fixe dans la lane).
        return [_assign_webshare_sticky_session(dict(cfg)) for _ in range(concurrency)]

    sample_size = max(
        concurrency * 2,
        _env_int("MAX_PROXY_RETRIES", 0) or _env_int("TIKTOK_PROXY_MAX_PER_JOB", 10),
    )
    return [p for p in _select_proxy_candidates(limit=sample_size) if p is not None]


def pick_replacement_proxy(exclude: set[str] | None = None) -> dict | None:
    """Remplacement pour releve CSV.

    Mode rotate: nouvelle sticky session Webshare (= nouvelle IP geo).
    Mode pool: tire une IP hors blacklist / hors exclude.
    """
    if _rotating_proxy_mode():
        return _assign_webshare_sticky_session(_build_rotating_proxy_config())

    excluded = set(exclude or set())
    pool = _parse_all_proxies()
    if not pool:
        return None
    by_id: dict[str, dict] = {}
    for proxy in pool:
        identity = _proxy_identity(proxy)
        if identity and identity not in by_id:
            by_id[identity] = proxy
    with _PROXY_BLACKLIST_LOCK:
        blocked = set(_load_blacklist().keys()) if _blacklist_enabled() else set()

    if sticky_sessions.sticky_enabled():
        hot = []
        for identity in sticky_sessions.identities_with_valid_cookies():
            if identity in blocked or identity in excluded:
                continue
            proxy = by_id.get(identity)
            if proxy is None or sticky_sessions.is_session_busy(identity):
                continue
            hot.append(proxy)
        if hot:
            return random.choice(hot)

    available = [
        p for p in pool
        if _proxy_identity(p) not in blocked and _proxy_identity(p) not in excluded
    ]
    if sticky_sessions.sticky_enabled():
        free = [p for p in available if not sticky_sessions.is_session_busy(_proxy_identity(p))]
        if free:
            available = free
    if not available:
        return None
    return random.choice(available)


def _describe_proxy(proxy_cfg: dict | None) -> str:
    """Retourne une description lisible du mode reseau (proxy/direct)."""
    if not proxy_cfg:
        return "direct"
    identity = _proxy_identity(proxy_cfg)
    server = proxy_cfg.get("server") or "proxy"
    if identity and identity not in (server, "proxy"):
        return f"{server} ({identity})"
    return server


def _build_user_data_dir() -> str:
    """Retourne le dossier profil navigateur persistant (ou chaine vide).

    Un chemin relatif est resolu par rapport au dossier de ce fichier (et non
    au cwd du process), afin que le profil persistant fonctionne de maniere
    identique quel que soit l'endroit d'ou `worker.py` est lance sur le
    serveur (systemd, docker, shell interactif, etc.).
    """
    raw = (os.getenv("TIKTOK_USER_DATA_DIR") or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path)


def _should_apply_stealth(user_data_dir: str) -> bool:
    """Stealth: OFF sur vrai Chrome (mesure historique: overlays → grid vide).

    Sur Chromium embarque: opt-in via TIKTOK_APPLY_STEALTH (defaut false).
    """
    if _is_real_chrome_channel():
        return False
    return _env_bool("TIKTOK_APPLY_STEALTH", False)


def _parse_proxy_spec(spec: str) -> dict | None:
    """Parse une ligne proxy → dict Playwright (server HTTP + user/pass separes).

    Formats acceptes:
    - host:port|username|password
    - scheme://username:password@host:port
    - host:port:username:password (export Webshare brut)
    """
    return parse_playwright_proxy(spec)


def _rotate_ipv6_identity() -> str | None:
    """Demande au proxy local de rotation IPv6 de changer d'adresse source.

    Explication pour bien comprendre le mecanisme complet:
    - Playwright n'a AUCUNE option pour choisir l'IP source d'une connexion
      (il n'existe pas de parametre "local_address"). La seule chose que
      Playwright sait faire, c'est parler a un proxy via l'option `proxy`.
    - On utilise donc un petit proxy SOCKS5 qui tourne en local sur le
      serveur (voir tools/ipv6_rotating_proxy.py). C'est LUI qui choisit
      une adresse IPv6 aleatoire dans le bloc /64 et qui "bind" dessus avant
      de se connecter a TikTok.
    - Cette fonction appelle simplement l'URL de controle de ce proxy
      (http://127.0.0.1:8091/rotate par defaut) pour lui dire: "a partir de
      maintenant, choisis une NOUVELLE adresse aleatoire". Le proxy
      lui-meme ne change rien tout seul: c'est nous (le scraper) qui
      decidons du bon moment (toutes les N videos, voir plus bas).
    - Important: changer l'adresse cote proxy ne suffit pas. Les connexions
      TCP DEJA ouvertes par le navigateur gardent leur ancienne IP source
      jusqu'a leur fermeture. C'est pour ca qu'on ferme et qu'on recree le
      contexte Playwright juste apres cet appel (voir `_scrape_with_browser`):
      ca force le navigateur a ouvrir des connexions toutes neuves, qui
      passeront donc par la nouvelle adresse.

    Best-effort: si TIKTOK_IPV6_ROTATE_CONTROL_URL n'est pas configuree, ou
    si l'appel echoue (proxy pas demarre, reseau...), on log un warning et on
    continue le scraping normalement (sans rotation) plutot que de faire
    planter tout le job pour ca.

    Retourne la nouvelle adresse (texte brut renvoye par le proxy) ou None.
    """
    control_url = (os.getenv("TIKTOK_IPV6_ROTATE_CONTROL_URL") or "").strip()
    if not control_url:
        return None
    try:
        response = requests.get(control_url, timeout=5)
        body = (response.text or "").strip()
        LOGGER.info("IPv6 identity rotated via control endpoint", extra={"proxy_response": body[:200]})
        return body
    except Exception:
        LOGGER.warning(
            "Failed to rotate IPv6 identity (proxy control endpoint unreachable?); continuing without rotation",
            exc_info=True,
        )
        return None


def _save_challenge_artifacts(page, label: str = "challenge"):
    """Sauvegarde des artefacts de debug pour inspecter visuellement ce que
    TikTok a renvoye (utile en debug local, ex: proxy Webshare suspect).

    `label` differencie le scenario (fichiers "tiktok_{label}.png/.html"):
    - "challenge": un challenge/captcha a ete detecte explicitement.
    - "no_posts_found": la page a charge sans challenge detecte, mais aucune
      video n'a pu etre extraite (page vide, geo-restriction, session/proxy
      incoherents, changement de markup TikTok...). Ce cas est souvent le
      plus difficile a diagnostiquer sans regarder le HTML/screenshot, d'ou
      cette capture systematique meme en l'absence de "challenge" reconnu.

    Fichiers produits: screenshot PNG + HTML complet de la page, plus
    URL/titre courants dans les logs (pour reperer une redirection silencieuse,
    ex: renvoi vers la home page ou une page de restriction regionale).
    """
    safe_label = re.sub(r"[^a-z0-9_-]+", "_", (label or "challenge").lower()) or "challenge"

    try:
        page.screenshot(path=f"tiktok_{safe_label}.png", full_page=True)
    except Exception:
        LOGGER.warning("Failed to save %s screenshot", safe_label, exc_info=True)

    try:
        html = page.content()
        with open(f"tiktok_{safe_label}.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        LOGGER.warning("Failed to save %s HTML", safe_label, exc_info=True)

    try:
        current_url = page.url
    except Exception:
        current_url = "unknown"

    try:
        title = page.title()
    except Exception:
        title = "unknown"

    LOGGER.info(
        "Saved debug artifacts",
        extra={"label": safe_label, "page_url": current_url, "page_title": title},
    )



def _extract_posts_from_sigi_state(payload: object) -> list:
    """Extrait les posts depuis `SIGI_STATE` (etat JS TikTok embarque)."""
    if not isinstance(payload, dict):
        return []

    item_module = payload.get("ItemModule")
    if not isinstance(item_module, dict):
        return []

    posts = []
    seen = set()
    for raw_id, item in item_module.items():
        if not isinstance(item, dict):
            continue

        post_id = str(item.get("id") or raw_id or "").strip()
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)

        author = str(item.get("author") or "").strip()
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        stats_fields = _stats_from_item(item)
        desc = item.get("desc") or ""
        published_at = _to_iso_datetime(item.get("createTime") or item.get("create_time") or item.get("create_time_stamp"))
        post_url = f"https://www.tiktok.com/@{author}/video/{post_id}" if author else ""

        posts.append(
            {
                "post_id": post_id,
                "post_url": post_url,
                "message": desc,
                "author": author,
                "published_at": published_at,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                "likes": stats_fields["likes"],
                "comments_count": stats_fields["comments_count"],
                "shares": stats_fields["shares"],
                "views": stats_fields["views"],
            }
        )
    return posts


def _extract_posts_from_html_fallback(profile_url: str) -> list:
    """Fallback HTTP sans navigateur pour recuperer les posts d'un profil.

    Strategie:
    1) Telecharger la page HTML.
    2) Tenter `__UNIVERSAL_DATA_FOR_REHYDRATION__`.
    3) Sinon tenter `SIGI_STATE`.
    """
    headers = {
        "User-Agent": _chrome_user_agent(),
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    try:
        response = requests.get(profile_url, headers=headers, timeout=30)
    except Exception as exc:
        return []

    html = response.text or ""
    if not html:
        return []

    # Prefer universal rehydration payload.
    uni_match = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if uni_match:
        raw = uni_match.group(1).strip()
        try:
            payload = json.loads(raw)
            posts = _extract_posts_from_json_payload(payload)
            if posts:
                return posts
        except Exception as exc:
            LOGGER.warning("Failed to parse rehydration payload", exc_info=True)

    sigi_match = re.search(
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if sigi_match:
        raw = sigi_match.group(1).strip()
        try:
            payload = json.loads(raw)
            posts = _extract_posts_from_sigi_state(payload)
            if posts:
                return posts
        except Exception as exc:
            LOGGER.warning("Failed to parse SIGI_STATE payload", exc_info=True)

    return []


def _extract_posts_from_page_html(page, profile_url: str) -> list:
    """Fallback: parse le HTML rendu du navigateur pour les liens /@user/video/id.

    Utile quand la grille est encore en squelette cote images mais que les
    ancres `a[href*="/video/"]` sont deja presentes dans le DOM (cas frequent
    du soft-block TikTok ou d'une hydration lente).
    """
    try:
        html = page.content() or ""
    except Exception:
        return []
    if not html:
        return []

    target = _username_from_url(profile_url)
    posts: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"https?://(?:www\.)?tiktok\.com/@([^/\"'?#]+)/video/(\d+)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        author = (match.group(1) or "").strip().lower()
        post_id = (match.group(2) or "").strip()
        if not post_id or post_id in seen:
            continue
        if target and author and author != target:
            continue
        seen.add(post_id)
        posts.append(
            {
                "post_id": post_id,
                "post_url": f"https://www.tiktok.com/@{author or target}/video/{post_id}",
                "message": "",
                "author": author or target,
                "published_at": None,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                "likes": None,
                "comments_count": None,
                "shares": None,
                "views": None,
            }
        )
    return posts


def _wait_for_profile_video_links(page, timeout_ms: int | None = None) -> bool:
    """Attend que la grille profil expose au moins un lien /video/.

    Retourne True si au moins une ancre est apparue. Ne leve pas d'exception:
    un timeout = soft-block / page incomplete, le caller decide.
    """
    if timeout_ms is None:
        timeout_ms = max(3000, _env_int("TIKTOK_WAIT_VIDEO_LINKS_MS", 25000))
    try:
        page.wait_for_selector('a[href*="/video/"]', timeout=timeout_ms)
        return True
    except Exception:
        try:
            if page.locator('a[href*="/video/"]').count() > 0:
                return True
        except Exception:
            pass
        LOGGER.info(
            "Video grid links did not appear within wait window",
            extra={"url": f"timeout_ms={timeout_ms}"},
        )
        return False


def _is_profile_grid_api_url(url: str) -> bool:
    """True pour les XHR grille profil (pas le feed For You / explore)."""
    rurl = (url or "").lower()
    if any(feed in rurl for feed in ("recommend", "/explore", "for_you", "foryou")):
        return False
    return any(
        key in rurl
        for key in (
            "/api/post/item_list",
            "/api/preload/item_list",
            "/api/user/detail",
            "/api/user/post",
            "post/item_list",
            "preload/item_list",
            "user/detail",
            "item_list",
            "aweme/post",
            "user/post",
        )
    )


def _extract_sec_uid_from_page(page) -> str | None:
    """secUid depuis __UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE."""
    try:
        sec = page.evaluate(
            """() => {
              const pick = (node) => {
                if (!node || typeof node !== 'object') return null;
                const u = (node.userInfo && node.userInfo.user)
                  || (node.userInfo && node.userInfo.userInfo && node.userInfo.userInfo.user)
                  || node.user
                  || null;
                if (u && (u.secUid || u.sec_uid)) return String(u.secUid || u.sec_uid);
                return null;
              };
              try {
                const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                if (el && el.textContent) {
                  const data = JSON.parse(el.textContent);
                  const scope = data.__DEFAULT_SCOPE__ || data;
                  for (const key of [
                    'webapp.user-detail',
                    'webapp.user-detail.page',
                    'webapp.user-post-list',
                  ]) {
                    const got = pick(scope[key]);
                    if (got) return got;
                  }
                }
              } catch (e) {}
              try {
                if (window.SIGI_STATE) {
                  const ud = window.SIGI_STATE.UserModule
                    || window.SIGI_STATE.UserPage
                    || {};
                  const users = ud.users || ud;
                  if (users && typeof users === 'object') {
                    for (const v of Object.values(users)) {
                      if (v && (v.secUid || v.sec_uid)) return String(v.secUid || v.sec_uid);
                    }
                  }
                }
              } catch (e) {}
              return null;
            }"""
        )
    except Exception:
        LOGGER.debug("secUid extract failed", exc_info=True)
        return None
    if isinstance(sec, str) and sec.strip():
        return sec.strip()
    return None


def _force_fetch_item_list_from_page(
    page,
    *,
    sec_uid: str,
    count: int = 30,
    cursor: int = 0,
) -> dict:
    """Appel /api/post/item_list/ depuis le contexte page (cookies + fetch hooke).

    TikTok hooke `window.fetch` via webmssdk pour injecter msToken / X-Bogus /
    X-Gnarly. Un fetch in-page herite donc de la signature client — plus robuste
    qu'un requests Python externe (TLS/fingerprint differents).
    """
    try:
        result = page.evaluate(
            """async ({ secUid, count, cursor }) => {
              const lang = (navigator.language || 'en-US');
              const langShort = lang.slice(0, 2) || 'en';
              const params = new URLSearchParams({
                WebIdLastTime: String(Math.floor(Date.now() / 1000)),
                aid: '1988',
                app_language: langShort,
                app_name: 'tiktok_web',
                browser_language: lang,
                browser_name: 'Mozilla',
                browser_online: String(navigator.onLine !== false),
                browser_platform: navigator.platform || 'Win32',
                browser_version: navigator.userAgent || '',
                channel: 'tiktok_web',
                cookie_enabled: String(navigator.cookieEnabled !== false),
                count: String(count),
                cursor: String(cursor),
                device_platform: 'web_pc',
                focus_state: 'true',
                from_page: 'user',
                history_len: String((history && history.length) || 2),
                is_fullscreen: 'false',
                is_page_visible: 'true',
                language: langShort,
                os: 'windows',
                priority_region: '',
                referer: '',
                region: '',
                screen_height: String((screen && screen.height) || 768),
                screen_width: String((screen && screen.width) || 1365),
                secUid: String(secUid),
                tz_name: (Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC',
                webcast_language: langShort,
              });
              try {
                const m = document.cookie.match(/(?:^|;\\s*)msToken=([^;]+)/);
                if (m && m[1]) params.set('msToken', decodeURIComponent(m[1]));
              } catch (e) {}
              const url = '/api/post/item_list/?' + params.toString();
              const res = await fetch(url, {
                method: 'GET',
                credentials: 'include',
                headers: {
                  'Accept': 'application/json, text/plain, */*',
                },
              });
              const text = await res.text();
              let json = null;
              try { json = JSON.parse(text); } catch (e) { json = null; }
              const itemList = (json && Array.isArray(json.itemList)) ? json.itemList : [];
              return {
                status: res.status,
                bodyLen: text.length,
                itemListLen: itemList.length,
                statusCode: (json && (json.statusCode ?? json.status_code ?? null)),
                hasItemList: itemList.length > 0,
                finalUrl: (typeof res.url === 'string') ? res.url.slice(0, 180) : '',
                payload: json,
              };
            }""",
            {"secUid": sec_uid, "count": int(count), "cursor": int(cursor)},
        )
    except Exception as exc:
        LOGGER.warning(
            "Forced item_list fetch raised: %s",
            str(exc)[:200],
        )
        return {
            "status": 0,
            "bodyLen": 0,
            "itemListLen": 0,
            "statusCode": None,
            "hasItemList": False,
            "payload": None,
            "error": str(exc)[:200],
        }
    return result if isinstance(result, dict) else {}


def _ingest_forced_item_list(
    page,
    network_posts: list,
    *,
    profile_url: str,
    target_username: str,
    count: int | None = None,
) -> int:
    """Si le SPA n'a pas emis item_list: fetch signe in-page → buffer reseau.

    Retourne le nombre de posts ajoutes au buffer. Loggue status HTTP,
    taille body, presence itemList (checklist Claude etape 1).
    """
    if not _env_bool("TIKTOK_FORCE_ITEM_LIST_FETCH", True):
        LOGGER.info("TIKTOK_FORCE_ITEM_LIST_FETCH=false — skip forced item_list")
        return 0

    sec_uid = _extract_sec_uid_from_page(page)
    if not sec_uid:
        LOGGER.warning(
            "Forced item_list skipped — secUid missing from rehydration",
            extra={"url": profile_url},
        )
        return 0

    fetch_count = int(count or _env_int("TIKTOK_FORCE_ITEM_LIST_COUNT", 30))
    LOGGER.info(
        "Forced item_list fetch (page.evaluate) secUid=%s… count=%s",
        sec_uid[:18],
        fetch_count,
        extra={"url": profile_url},
    )
    result = _force_fetch_item_list_from_page(
        page, sec_uid=sec_uid, count=fetch_count, cursor=0
    )
    http_status = int(result.get("status") or 0)
    body_len = int(result.get("bodyLen") or 0)
    item_len = int(result.get("itemListLen") or 0)
    status_code = result.get("statusCode")
    fetch_err = str(result.get("error") or "")
    LOGGER.info(
        "Forced item_list result: HTTP=%s bodyLen=%s itemListLen=%s "
        "tiktokStatus=%s hasItemList=%s",
        http_status,
        body_len,
        item_len,
        status_code,
        bool(result.get("hasItemList")),
        extra={"url": profile_url},
    )

    payload = result.get("payload")
    if not isinstance(payload, dict):
        if fetch_err or http_status == 0:
            return -1  # fetch failed (TypeError / network) — signal classifier
        return 0
    if not _api_payload_status_ok(payload) and item_len <= 0:
        LOGGER.warning(
            "Forced item_list payload not OK (statusCode=%s)",
            status_code,
            extra={"url": profile_url},
        )
        return 0

    parsed: list[dict] = []
    try:
        parsed = extract_posts_from_hydration_payload(payload, profile_url=profile_url)
    except Exception:
        parsed = []
    if not parsed:
        try:
            parsed = _extract_posts_from_json_payload(payload)
        except Exception:
            parsed = []

    if not parsed:
        return 0

    stamped = [
        _stamp_network_api_post(p, target_username)
        for p in parsed
        if isinstance(p, dict)
    ]
    network_posts.extend(stamped)
    LOGGER.info(
        "Forced item_list ingested %d posts into network buffer (buffer=%d)",
        len(stamped),
        len(network_posts),
        extra={"url": profile_url},
    )
    return len(stamped)


def _page_is_chrome_error(page) -> bool:
    try:
        href = (page.url or "").lower()
    except Exception:
        return False
    return href.startswith("chrome-error:") or "chromewebdata" in href


def _is_real_waf_block(
    page=None,
    *,
    status: int | None = None,
    error: str = "",
) -> bool:
    """True uniquement pour un VRAI blocage WAF/captcha — pas un profil vide HTTP 200.

    Criteres:
      - captcha / challenge / VNS visible
      - HTTP 403 / 429 (ou err_http_response_code_failure)
      - page chrome-error://
    """
    err = (error or "").lower()
    if status in (403, 429) or is_http_blocked_status(status):
        if status in (403, 429, 401, 451):
            return True
    if any(
        tok in err
        for tok in (
            "http_403",
            "http_429",
            "err_http_response_code_failure",
            "challenge_detected",
            "captcha",
            "chrome-error",
            "chromewebdata",
        )
    ):
        # "no_posts_found" / soft empty profile → PAS un WAF.
        if "no_posts" in err and "403" not in err and "429" not in err and "captcha" not in err:
            return False
        if "no_posts" not in err:
            return True

    if page is not None:
        if _page_is_chrome_error(page):
            return True
        try:
            if _looks_like_tiktok_challenge(page):
                return True
        except Exception:
            pass
    return False


def _api_payload_status_ok(payload: object) -> bool:
    """True si le JSON TikTok indique statusCode/status_code/code == 0 (ou absent)."""
    if not isinstance(payload, dict):
        return False
    for key in ("statusCode", "status_code", "code", "status"):
        if key not in payload:
            continue
        try:
            return int(payload[key]) == 0
        except (TypeError, ValueError):
            return str(payload[key]).strip() in ("0", "success", "ok", "")
    # Certains payloads n'ont pas de status mais portent itemList.
    return any(k in payload for k in ("itemList", "item_list", "items", "userInfo", "userInfoList"))


# UA Windows Chrome recent (aligne Sec-CH-UA / Playwright Chromium ~131+).
_CHROME_UA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_CHROME_SEC_CH_UA = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'


def _chrome_user_agent() -> str:
    """User-Agent Chrome recent (override possible via TIKTOK_USER_AGENT)."""
    return (os.getenv("TIKTOK_USER_AGENT") or "").strip() or _CHROME_UA_WINDOWS


def _sec_ch_ua() -> str:
    return (os.getenv("TIKTOK_SEC_CH_UA") or "").strip() or _CHROME_SEC_CH_UA


def _sec_ch_ua_mobile() -> str:
    return (os.getenv("TIKTOK_SEC_CH_UA_MOBILE") or "").strip() or "?0"


def _sec_ch_ua_platform() -> str:
    return (os.getenv("TIKTOK_SEC_CH_UA_PLATFORM") or "").strip() or '"Windows"'


def _browser_viewport() -> dict:
    w = max(320, _env_int("TIKTOK_VIEWPORT_WIDTH", 1365))
    h = max(480, _env_int("TIKTOK_VIEWPORT_HEIGHT", 768))
    return {"width": w, "height": h}


def _resolve_headless_channel(headless: bool, browser_channel: str) -> str:
    """Pour headless=True sans channel: force `chromium` (= new headless Playwright).

    channel='chromium' opte pour le new headless (vrai Chrome headless),
    pas le vieux chromium-headless-shell. channel='chrome' l'utilise deja.
    """
    channel = (browser_channel or "").strip()
    if headless and not channel and _env_bool("TIKTOK_HEADLESS_NEW", True):
        return "chromium"
    return channel


def _passive_settle_after_profile_nav(page) -> None:
    """Settle apres goto profil — volontairement court (perf / anti-403).

    Ancien comportement: 3–5s. Trop lent → CDN timeout. Defaut: no-op
    (TIKTOK_PROFILE_SETTLE_MS=0). Max autorise 500ms si force.
    """
    wait_ms = max(0, min(500, _env_int("TIKTOK_PROFILE_SETTLE_MS", 0)))
    if wait_ms <= 0:
        return
    LOGGER.debug("Short settle after profile navigation (%dms)", wait_ms)
    try:
        page.wait_for_timeout(wait_ms)
    except Exception:
        pass


def _grid_wait_ms() -> int:
    """Fenetre max attente item_list/DOM (defaut 8s, max 20s)."""
    return max(1500, min(20000, _env_int("TIKTOK_WAIT_VIDEO_LINKS_SOFT_MS", 8000)))


def _attempt_timeout_s() -> float:
    """Timeout global par tentative scrape (warmup+profil), defaut 35s."""
    return float(max(15, min(60, _env_int("TIKTOK_ATTEMPT_TIMEOUT_S", 35))))


class AttemptTimeoutError(Exception):
    """Budget temps de la tentative scrape depasse."""


def _check_attempt_deadline(deadline: float | None, *, phase: str = "") -> None:
    if deadline is None:
        return
    if time.monotonic() >= deadline:
        raise AttemptTimeoutError(
            f"attempt_timeout after {_attempt_timeout_s():.0f}s"
            + (f" ({phase})" if phase else "")
        )


def _random_mouse_jitter(page) -> None:
    """Petits mouvements souris aleatoires (zone centrale viewport)."""
    try:
        vp = page.viewport_size or {"width": 1365, "height": 768}
        w = int(vp.get("width") or 1365)
        h = int(vp.get("height") or 768)
        x = random.randint(int(w * 0.25), int(w * 0.75))
        y = random.randint(int(h * 0.25), int(h * 0.65))
        page.mouse.move(x, y)
        page.mouse.move(
            x + random.randint(-60, 90),
            y + random.randint(-40, 70),
            steps=random.randint(8, 18),
        )
        page.wait_for_timeout(random.randint(80, 220))
    except Exception:
        LOGGER.debug("Mouse jitter failed", exc_info=True)


def _humanize_profile_interaction(page) -> None:
    """Scroll leger rapide (lazy-load) — budget court pour perf."""
    try:
        page.mouse.wheel(0, random.randint(300, 450))
        page.wait_for_timeout(random.randint(80, 180))
    except Exception:
        LOGGER.debug("Humanize profile interaction failed", exc_info=True)
        try:
            page.evaluate(
                """(y) => { window.scrollBy({ top: y, left: 0, behavior: 'instant' }); }""",
                random.randint(300, 500),
            )
        except Exception:
            pass


def _human_scroll_page(page) -> None:
    """Scroll de feed progressif (remplace window.scrollTo bottom instantane)."""
    _random_mouse_jitter(page)
    try:
        for _ in range(random.randint(2, 5)):
            page.mouse.wheel(0, random.randint(380, 520))
            page.wait_for_timeout(random.randint(220, 480))
            if random.random() < 0.4:
                _random_mouse_jitter(page)
    except Exception:
        LOGGER.debug("Human scroll failed; fallback scrollBy", exc_info=True)
        try:
            page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
        except Exception:
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
    _human_pause(1.2, 0.8)


def _dom_has_video_links(page) -> bool:
    try:
        return page.locator('a[href*="/video/"]').count() > 0
    except Exception:
        return False


def _wait_for_profile_grid_data(
    page,
    network_posts: list | None,
    timeout_ms: int | None = None,
) -> bool:
    """Attend DOM `/video/` OU JSON API grille (`/api/post/item_list/`, user/detail).

    Poll actif reseau + DOM. Timeout = TIKTOK_WAIT_VIDEO_LINKS_SOFT_MS (defaut 8s).
    """
    if timeout_ms is None:
        timeout_ms = _grid_wait_ms()
    else:
        # Respecter le timeout demande (plafonne a 20s, min 1.5s).
        timeout_ms = max(1500, min(20000, int(timeout_ms)))
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll_ms = max(100, min(250, _env_int("TIKTOK_GRID_POLL_MS", 150)))

    while time.monotonic() < deadline:
        if network_posts:
            LOGGER.info(
                "Profile grid ready via API JSON (%d posts captured)",
                len(network_posts),
            )
            return True
        if _dom_has_video_links(page):
            LOGGER.info("Profile grid ready via DOM /video/ links")
            return True
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)

    ready = bool(network_posts) or _dom_has_video_links(page)
    if not ready:
        LOGGER.info(
            "Profile grid soft-block window elapsed (no API item_list / no DOM links)",
            extra={"url": f"timeout_ms={timeout_ms}"},
        )
    return ready


def _manual_solve_wait_if_enabled(user_data_dir: str, headless: bool):
    """Pause volontaire pour laisser l'utilisateur resoudre un challenge.

    Active en mode non-headless. Un profil persistant reste recommande,
    mais n'est plus strictement requis pour laisser le temps de resoudre
    le challenge visible dans la fenetre navigateur.
    """
    if headless:
        return

    seconds = _env_int("TIKTOK_MANUAL_SOLVE_WAIT_SECONDS", 12)

    if seconds <= 0 and not user_data_dir:
        seconds = 12

    if seconds <= 0:
        return

    time.sleep(seconds)


def _wait_for_challenge_resolution(page, user_data_dir: str, headless: bool):
    """Attend une resolution manuelle du challenge jusqu'au timeout configure."""
    if headless:
        return

    max_seconds = _env_int("TIKTOK_WAIT_CHALLENGE_RESOLVE_SECONDS", 90)

    if max_seconds <= 0 and not user_data_dir:
        max_seconds = 90

    if max_seconds <= 0:
        return

    start = time.time()
    while (time.time() - start) < max_seconds:
        if not _looks_like_tiktok_challenge(page):
            return
        _human_pause(1.5, 0.8)



def _parse_count_value(raw) -> int | None:
    """Normalise un compteur TikTok (int, str numerique, ou '1.2K'/'3.4M')."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    text = str(raw).strip().upper().replace(",", "").replace(" ", "")
    if not text or text in {"-", "N/A", "NULL"}:
        return None
    mult = 1
    if text.endswith("K"):
        mult = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        mult = 1_000_000
        text = text[:-1]
    elif text.endswith("B"):
        mult = 1_000_000_000
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        digits = re.sub(r"[^\d]", "", str(raw))
        return int(digits) if digits else None


def _stats_from_item(item: dict | None) -> dict:
    """Extrait likes/comments/shares/views depuis stats ou statsV2 d'un itemStruct."""
    if not isinstance(item, dict):
        return {
            "likes": None,
            "comments_count": None,
            "shares": None,
            "views": None,
        }
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    stats_v2 = item.get("statsV2") if isinstance(item.get("statsV2"), dict) else {}

    def pick(*keys: str):
        for key in keys:
            for source in (stats_v2, stats):
                if key in source and source.get(key) not in (None, ""):
                    parsed = _parse_count_value(source.get(key))
                    if parsed is not None:
                        return parsed
        return None

    return {
        "likes": pick("diggCount", "likeCount", "likes"),
        "comments_count": pick("commentCount", "comments"),
        "shares": pick("shareCount", "shares"),
        "views": pick("playCount", "viewCount", "views"),
    }


def _merge_post_data(base: dict, extra: dict) -> dict:
    """Fusionne des metadonnees de post sans ecraser les valeurs deja presentes.

    On complete seulement les champs vides dans `base` avec les donnees de `extra`.
    """
    merged = dict(base)
    for key in (
        "author",
        "message",
        "text_content",
        "text",
        "published_at",
        "likes",
        "comments_count",
        "shares",
        "views",
    ):
        current = merged.get(key)
        incoming = extra.get(key)
        if key == "published_at" and incoming not in (None, ""):
            incoming = _to_iso_datetime(incoming) or incoming
        if key in ("likes", "comments_count", "shares", "views") and incoming not in (None, ""):
            incoming = _parse_count_value(incoming)
        if (current is None or current == "") and incoming not in (None, ""):
            merged[key] = incoming

    base_metrics = merged.get("metrics") if isinstance(merged.get("metrics"), dict) else {}
    extra_metrics = extra.get("metrics") if isinstance(extra.get("metrics"), dict) else {}
    metrics = dict(base_metrics)
    for mk, flat in (
        ("likes", "likes"),
        ("views", "views"),
        ("comments", "comments_count"),
        ("shares", "shares"),
    ):
        if metrics.get(mk) not in (None, ""):
            continue
        incoming = extra_metrics.get(mk)
        if incoming in (None, ""):
            incoming = extra.get(flat)
        if incoming not in (None, ""):
            metrics[mk] = _parse_count_value(incoming)
    if metrics.get("likes") in (None, "") and merged.get("likes") not in (None, ""):
        metrics["likes"] = merged.get("likes")
    if metrics.get("views") in (None, "") and merged.get("views") not in (None, ""):
        metrics["views"] = merged.get("views")
    if metrics.get("comments") in (None, "") and merged.get("comments_count") not in (None, ""):
        metrics["comments"] = merged.get("comments_count")
    if metrics.get("shares") in (None, "") and merged.get("shares") not in (None, ""):
        metrics["shares"] = merged.get("shares")
    if metrics:
        merged["metrics"] = metrics
    if not merged.get("text_content") and merged.get("message"):
        merged["text_content"] = merged.get("message")
    if not merged.get("message") and merged.get("text_content"):
        merged["message"] = merged.get("text_content")
    return merged


def _attach_video_analysis(posts: list[dict], on_post=None) -> list[dict]:
    """Ajoute une analyse video IA (optionnelle) sur les posts collectes.

    Comportement:
    - Controle par variables d'environnement.
    - Limite configurable du nombre de videos analysees.
    - Peut emettre chaque post via callback `on_post`.
    """
    if not posts:
        return posts

    enabled = _env_bool("TIKTOK_ANALYZE_VIDEO_CONTENT", False)
    if not enabled:
        if on_post is not None:
            for post in posts:
                try:
                    on_post(dict(post))
                except Exception as cb_err:
                    LOGGER.warning("on_post callback error", exc_info=True)
        return posts

    raw_limit = (os.getenv("TIKTOK_ANALYZE_VIDEO_LIMIT") or "2").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 2

    # 0 disables analysis; negative values mean unlimited (analyze all posts).
    if limit == 0:
        return posts

    output_dir = os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports"
    updated = []

    for idx, post in enumerate(posts):
        post_copy = dict(post)
        post_url = str(post_copy.get("post_url") or "").strip()

        if (limit > 0 and idx >= limit) or not post_url or "/video/" not in post_url:
            updated.append(post_copy)
            if on_post is not None:
                try:
                    on_post(dict(post_copy))
                except Exception as cb_err:
                    LOGGER.warning("on_post callback error", exc_info=True)
            continue

        try:
            report = analyze_tiktok_video(
                video_url=post_url,
                output_dir=output_dir,
                save_json_report=True,
                description_text=str(post_copy.get("message") or ""),
            )
            post_copy["source_media_url"] = report.get("video_metadata", {}).get("media_url")
            post_copy["media_path"] = report.get("artifacts", {}).get("video_path")
            post_copy["video_report"] = build_small_video_report(report)
            post_copy["message"] = post_copy.get("message") or report.get("transcript_excerpt") or ""
        except Exception as exc:
            post_copy["video_report"] = {
                "executive_summary": ["Analyse video indisponible."],
                "transcript_excerpt": "",
                "themes": [],
                "visual_elements_detected": [],
                "keywords": [],
                "confidence_and_limits": {
                    "score": 0.0,
                    "level": "low",
                    "limits": [f"video_analysis_error: {exc}"],
                },
            }

        updated.append(post_copy)
        if on_post is not None:
            try:
                on_post(dict(post_copy))
            except Exception as cb_err:
                LOGGER.warning("on_post callback error", exc_info=True)

    return updated


def _extract_video_detail_from_page(page) -> dict:
    """Extrait les metadonnees detaillees d'une page video TikTok.

    Ordre de priorite (TikTok 2025/2026):
    1) `__UNIVERSAL_DATA_FOR_REHYDRATION__` -> `__DEFAULT_SCOPE__` ->
       `webapp.video-detail` -> `itemInfo.itemStruct` (`stats` / `statsV2`)
    2) `window.SIGI_STATE.ItemModule` (legacy)
    3) Compteurs DOM `data-e2e` (like-count, comment-count, ...)
    """
    return page.evaluate(
        r"""
        () => {
            const toInt = (raw) => {
                if (raw === null || raw === undefined || raw === '') return null;
                if (typeof raw === 'number' && Number.isFinite(raw)) return Math.trunc(raw);
                let text = String(raw).trim().toUpperCase().replace(/,/g, '').replace(/\s+/g, '');
                if (!text || text === '-' || text === 'N/A') return null;
                let mult = 1;
                if (text.endsWith('K')) { mult = 1e3; text = text.slice(0, -1); }
                else if (text.endsWith('M')) { mult = 1e6; text = text.slice(0, -1); }
                else if (text.endsWith('B')) { mult = 1e9; text = text.slice(0, -1); }
                const n = Number.parseFloat(text);
                if (Number.isFinite(n)) return Math.trunc(n * mult);
                const digits = String(raw).replace(/[^\d]/g, '');
                return digits ? Number.parseInt(digits, 10) : null;
            };

            const fromItem = (item) => {
                if (!item || typeof item !== 'object') return null;
                const stats = (item.stats && typeof item.stats === 'object') ? item.stats : {};
                const statsV2 = (item.statsV2 && typeof item.statsV2 === 'object') ? item.statsV2 : {};
                const pick = (...keys) => {
                    for (const key of keys) {
                        for (const source of [statsV2, stats]) {
                            if (source[key] !== undefined && source[key] !== null && source[key] !== '') {
                                const parsed = toInt(source[key]);
                                if (parsed !== null) return parsed;
                            }
                        }
                    }
                    return null;
                };
                const author = item.author?.uniqueId || item.author?.nickname || item.author || '';
                return {
                    author: typeof author === 'string' ? author : '',
                    message: item.desc || item.description || '',
                    likes: pick('diggCount', 'likeCount', 'likes'),
                    comments_count: pick('commentCount', 'comments'),
                    shares: pick('shareCount', 'shares'),
                    views: pick('playCount', 'viewCount', 'views'),
                    published_at: item.createTime ?? item.create_time ?? null,
                };
            };

            const urlIdMatch = (location.href || '').match(/\/video\/(\d+)/);
            const urlId = urlIdMatch ? urlIdMatch[1] : '';

            // 1) Payload SSR moderne
            const script = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (script && script.textContent) {
                try {
                    const hydration = JSON.parse(script.textContent);
                    const scope = hydration?.__DEFAULT_SCOPE__ || hydration || {};
                    const detail = scope['webapp.video-detail'] || scope['webapp.reflow.video.detail'] || null;
                    const item = detail?.itemInfo?.itemStruct || detail?.itemStruct || null;
                    const fromDetail = fromItem(item);
                    if (fromDetail && (fromDetail.likes != null || fromDetail.views != null || fromDetail.message)) {
                        return fromDetail;
                    }

                    // Parcours generique itemStruct si le chemin direct est vide
                    let found = null;
                    const walk = (node) => {
                        if (!node || typeof node !== 'object' || found) return;
                        const candidate = node.itemStruct || node.item || null;
                        if (candidate && candidate.id) {
                            const itemId = String(candidate.id || '');
                            if (!urlId || itemId === urlId) {
                                found = fromItem(candidate);
                                return;
                            }
                        }
                        for (const v of Object.values(node)) {
                            if (v && typeof v === 'object') walk(v);
                        }
                    };
                    walk(hydration);
                    if (found && (found.likes != null || found.views != null || found.message)) {
                        return found;
                    }
                } catch {
                    // ignore parse errors
                }
            }

            // 2) Legacy SIGI_STATE
            const sigi = window.SIGI_STATE;
            const itemModule = sigi && sigi.ItemModule ? sigi.ItemModule : null;
            if (itemModule && typeof itemModule === 'object') {
                const exact = urlId && itemModule[urlId] ? itemModule[urlId] : null;
                const candidate = exact || Object.values(itemModule)[0] || null;
                const fromSigi = fromItem(candidate);
                if (fromSigi && (fromSigi.likes != null || fromSigi.views != null || fromSigi.message)) {
                    return fromSigi;
                }
            }

            // 3) DOM data-e2e (souvent present meme si le JSON est strippe)
            const readE2e = (...names) => {
                for (const name of names) {
                    const el = document.querySelector(`[data-e2e="${name}"]`);
                    if (!el) continue;
                    const parsed = toInt(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                    if (parsed !== null) return parsed;
                }
                return null;
            };
            const descEl = document.querySelector('[data-e2e="browse-video-desc"], [data-e2e="video-desc"]');
            return {
                author: '',
                message: descEl ? (descEl.innerText || '').trim() : '',
                likes: readE2e('like-count', 'browse-like-count'),
                comments_count: readE2e('comment-count', 'browse-comment-count'),
                shares: readE2e('share-count', 'browse-share-count'),
                views: readE2e('video-views', 'browse-video-views', 'view-count'),
                published_at: null,
            };
        }
        """
    )


def _enrich_posts_from_video_pages(context, posts: list[dict]) -> list[dict]:
    """Enrichit UNIQUEMENT les posts incomplets; oEmbed en dernier recours.

    Profile-first: si diggCount/playCount deja presents depuis le profil SSR,
    on ne navigue PAS sur /video/<id> (TikTok ne livre plus les details fiables).
    """
    if not posts:
        return posts

    always = _env_bool("ENRICH_VIDEO_PAGE_ALWAYS", False)
    enabled = _env_bool("TIKTOK_ENRICH_POST_DETAILS", True)
    oembed_on = _env_bool("TIKTOK_OEMBED_FALLBACK", True)

    if not always and posts_have_profile_metrics(posts):
        LOGGER.info(
            "Skipping video-page enrichment — profile-first metrics present (%d posts)",
            len(posts),
        )
        if not oembed_on:
            return posts
        out = []
        for post in posts:
            if str(post.get("text_content") or post.get("message") or "").strip():
                out.append(post)
            else:
                out.append(_merge_post_data(post, fetch_oembed_fallback(str(post.get("post_url") or ""))))
        return out

    if not always and all(post_has_usable_metrics(p) for p in posts):
        LOGGER.info(
            "Skipping video-page enrichment — profile-first metrics present (%d posts)",
            len(posts),
        )
        if not oembed_on:
            return posts
        out = []
        for post in posts:
            if str(post.get("text_content") or post.get("message") or "").strip():
                out.append(post)
            else:
                out.append(_merge_post_data(post, fetch_oembed_fallback(str(post.get("post_url") or ""))))
        return out

    if not enabled and not always:
        if oembed_on:
            return [
                _merge_post_data(p, fetch_oembed_fallback(str(p.get("post_url") or "")))
                if not str(p.get("text_content") or p.get("message") or "").strip()
                else p
                for p in posts
            ]
        return posts

    raw_limit = (os.getenv("TIKTOK_ENRICH_POST_DETAILS_LIMIT") or "6").strip()
    try:
        limit = max(0, int(raw_limit))
    except ValueError:
        limit = 6

    wait_ms = max(2000, _env_int("TIKTOK_ENRICH_WAIT_MS", 12000))
    max_attempts = max(1, _env_int("TIKTOK_ENRICH_ATTEMPTS", 2))
    enriched: list[dict] = []

    for idx, post in enumerate(posts):
        if not always and post_has_usable_metrics(post):
            enriched.append(post)
            continue

        post_url = normalize_www_tiktok_url(str(post.get("post_url") or "").strip())
        if idx >= limit or not post_url or "/video/" not in post_url:
            if oembed_on and not str(post.get("text_content") or post.get("message") or "").strip():
                enriched.append(_merge_post_data(post, fetch_oembed_fallback(post_url)))
            else:
                enriched.append(post)
            continue

        merged = dict(post)
        last_error = None
        for attempt in range(1, max_attempts + 1):
            detail_page = None
            try:
                detail_page = context.new_page()
                try:
                    goto_strict(detail_page, post_url, timeout=nav_timeout_ms())
                except (ProxyBlockedException, AntibotBlockedException) as blocked:
                    last_error = str(blocked)[:160]
                    LOGGER.warning(
                        "[ANTIBOT] Video page blocked — oEmbed fallback: %s",
                        last_error,
                        extra={"url": post_url},
                    )
                    break
                try:
                    detail_page.wait_for_function(
                        """() => {
                            const script = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
                            if (script && script.textContent && script.textContent.includes('playCount')) return true;
                            const like = document.querySelector('[data-e2e="like-count"], [data-e2e="browse-like-count"]');
                            return !!(like && (like.innerText || '').trim());
                        }""",
                        timeout=wait_ms,
                    )
                except Exception:
                    _human_pause(1.0, 0.5)

                detail = _extract_video_detail_from_page(detail_page) or {}
                merged = _merge_post_data(post, detail)
                if post_has_usable_metrics(merged) or str(
                    merged.get("text_content") or merged.get("message") or ""
                ).strip():
                    last_error = None
                    break
                last_error = "no_metrics_in_page"
                if attempt < max_attempts:
                    _human_pause(1.0, 0.5)
            except (ProxyBlockedException, AntibotBlockedException) as blocked:
                last_error = str(blocked)[:160]
                break
            except Exception as exc:
                last_error = str(exc).splitlines()[0] if str(exc) else "enrich_failed"
                LOGGER.warning(
                    "Failed to enrich video detail page (attempt %s/%s): %s",
                    attempt,
                    max_attempts,
                    last_error,
                    extra={"url": post_url},
                )
                if attempt < max_attempts:
                    _human_pause(1.2, 0.6)
            finally:
                if detail_page is not None:
                    try:
                        detail_page.close()
                    except Exception:
                        LOGGER.warning("Failed to close detail page", exc_info=True)

        if oembed_on and (
            not post_has_usable_metrics(merged)
            or not str(merged.get("text_content") or merged.get("message") or "").strip()
        ):
            oembed = fetch_oembed_fallback(post_url)
            if oembed:
                merged = _merge_post_data(merged, oembed)
            elif last_error:
                LOGGER.warning(
                    "Video detail enrichment exhausted (%s)",
                    last_error,
                    extra={"url": post_url, "post_id": str(post.get("post_id") or "")},
                )
        enriched.append(merged)

    return enriched


def _scrape_with_browser(
    playwright,
    profile_url: str,
    max_posts: int,
    max_age_hours: int | None,
    on_post,
    headless: bool,
    slow_mo_ms: int,
    proxy_cfg: dict | None,
    analyze_video_content: bool,
    rotate_every_n_posts: int = 0,
    *,
    skip_homepage_warmup: bool = False,
) -> dict:
    """Pipeline principal de scraping via Playwright.

    Etapes:
    1) Ouvrir un contexte navigateur (persistant ou temporaire).
    2) Injecter headers/stealth/cookies selon la config.
    3) Naviguer vers le profil et collecter les posts (DOM + reseau), avec
       une rotation d'identite IPv6 toutes les `rotate_every_n_posts` videos
       si ce parametre est configure (0 = rotation desactivee).
    4) Gérer challenge/fallback HTTP si necessaire.
    5) Enrichir et analyser les posts avant retour.
    """
    # Ces trois variables representent "le navigateur actuel". Elles sont
    # reassignees a chaque rotation d'IPv6 (fermeture + reouverture), d'ou le
    # `nonlocal` utilise dans les fonctions imbriquees ci-dessous.
    browser = None
    context = None
    page = None

    # Playwright exige server HTTP + username/password SEPARES (jamais inline).
    if proxy_cfg and not _force_direct_mode():
        sticky_cc_keep = proxy_cfg.get("_sticky_country")
        proxy_cfg = sanitize_playwright_proxy(proxy_cfg)
        if proxy_cfg and sticky_cc_keep:
            proxy_cfg["_sticky_country"] = sticky_cc_keep
        if not proxy_cfg or not proxy_cfg.get("username"):
            LOGGER.warning(
                "[PROXY] Invalid Webshare proxy config after sanitize — "
                "expected server=http://host:port + username/password"
            )

    # Défini avant le bloc try: même si une exception survient tôt (warmup,
    # ouverture de contexte, etc.), on doit pouvoir renvoyer les posts déjà
    # collectés au lieu de les perdre silencieusement.
    all_posts: list[dict] = []
    # True uniquement si le scrape a produit des posts (autorise eventuelle persistance).
    scrape_succeeded = False
    # Handle du profil cible: sert a rejeter les videos "For You" qui trainent
    # dans l'etat JS (SIGI_STATE.ItemModule) apres le warmup homepage.
    target_username = _username_from_url(profile_url)

    # Test IP locale: proxy=None mais sticky "direct-local" pour persister cookies.
    if _force_direct_mode():
        proxy_cfg = None
        rotate_every_n_posts = 0
        LOGGER.warning(
            "TIKTOK_FORCE_DIRECT — browser/context launched with proxy=None (local IP)"
        )

    # --- Session sticky / warmup ---------------------------------------------
    # Mode rotate: chaque tentative = nouvelle IP Webshare → cookies TOUJOURS
    # frais (profil ephemere + VIRGIN). Jamais reinjecter cookies d'une autre IP.
    reuse_cookies = _env_bool("TIKTOK_REUSE_STICKY_COOKIES", True)
    sticky_identity = _proxy_identity(proxy_cfg) if proxy_cfg else ""
    if _force_direct_mode():
        sticky_identity = "direct-local"
    elif _rotating_proxy_mode() and sticky_identity and sticky_identity != "direct":
        # Force: 1 session Chrome = 1 IP = 1 jeu de cookies neufs.
        reuse_cookies = False
        sticky_identity = f"{sticky_identity}-{uuid.uuid4().hex[:10]}"
        LOGGER.info(
            "Rotate mode: fresh cookies for this IP session (no REUSE across IPs)",
            extra={"url": sticky_identity},
        )
    elif (
        sticky_identity
        and sticky_identity != "direct"
        and not reuse_cookies
    ):
        sticky_identity = f"{sticky_identity}-{uuid.uuid4().hex[:10]}"
    sticky_session_dir: Path | None = None
    sticky_lock_held = False
    use_sticky = bool(
        sticky_sessions.sticky_enabled()
        and sticky_identity
        and sticky_identity != "direct"
        and (proxy_cfg is not None or _force_direct_mode())
    )
    if use_sticky:
        sticky_session_dir = sticky_sessions.prepare_session_dir(sticky_identity)
        if not sticky_sessions.acquire_session_lock(sticky_session_dir):
            LOGGER.warning(
                "Sticky profile already in use; rotating proxy",
                extra={"url": sticky_identity},
            )
            return {
                "posts": [],
                "total": 0,
                "error": "sticky_profile_in_use",
                "url": profile_url,
            }
        sticky_lock_held = True
        # REUSE=true n'autorise PAS la reinjection si last success=False / WAF.
        if reuse_cookies and not sticky_sessions.should_reuse_sticky_cookies(sticky_identity):
            LOGGER.info(
                "TIKTOK_REUSE_STICKY_COOKIES ignored — last success=False or WAF; VIRGIN start",
                extra={"url": sticky_identity},
            )
            reuse_cookies = False
        # REUSE=false / invalide: wipe virgin. REUSE=true + OK: garder cookies.
        prepare_virgin_sticky_for_next_ip(sticky_identity, force=not reuse_cookies)
        sticky_session_dir = sticky_sessions.session_dir_for_identity(sticky_identity)
        # Locks Chrome laisses par un crash / ancien conteneur -> fail immédiat
        # "profile appears to be in use by another Google Chrome process".
        removed = sticky_sessions.clear_chrome_profile_locks(sticky_session_dir)
        if removed:
            LOGGER.info(
                "Cleared stale Chrome profile locks",
                extra={"url": f"{sticky_identity}: {','.join(removed)}"},
            )
        user_data_dir = str(sticky_session_dir)
    else:
        user_data_dir = _build_user_data_dir()

    browser_channel = (os.getenv("TIKTOK_BROWSER_CHANNEL") or "").strip()
    # headless + pas de channel → channel=chromium (new headless Playwright).
    browser_channel = _resolve_headless_channel(headless, browser_channel)
    using_real_chrome = browser_channel in (
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    )
    using_new_headless = headless and browser_channel == "chromium"
    browser_args = browser_launch_args()
    # Playwright ajoute "--enable-automation" par defaut -> navigator.webdriver.
    ignored_default_args = ["--enable-automation"]

    # Locale / TZ / Accept-Language coherents avec le pays sticky (IP proxy).
    sticky_cc = _sticky_country_from_proxy(proxy_cfg)
    browser_geo = _browser_geo_for_country(sticky_cc)
    _set_active_browser_geo(browser_geo)
    context_options = {
        "viewport": _browser_viewport(),
        "locale": browser_geo["locale"],
        "timezone_id": browser_geo["timezone_id"],
        "ignore_https_errors": True,
        "is_mobile": _env_bool("TIKTOK_IS_MOBILE", False),
        "has_touch": _env_bool("TIKTOK_HAS_TOUCH", False),
    }
    # UA override aussi sur vrai Chrome en mode diagnostic mobile.
    if _env_bool("TIKTOK_FORCE_USER_AGENT", False) or not using_real_chrome:
        context_options["user_agent"] = _chrome_user_agent()
    # Geoloc optionnelle (permissions + coords) — alignee sur le pays sticky.
    if _env_bool("TIKTOK_BROWSER_GEOLOCATION", True):
        context_options["geolocation"] = dict(browser_geo["geolocation"])
        context_options["permissions"] = ["geolocation"]
    LOGGER.info(
        "Browser geo aligned to sticky country=%s locale=%s tz=%s lang=%s",
        browser_geo.get("country"),
        browser_geo.get("locale"),
        browser_geo.get("timezone_id"),
        browser_geo.get("accept_language"),
        extra={"url": profile_url},
    )
    if headless and using_new_headless:
        LOGGER.info(
            "Launching Chromium new headless (channel=chromium)",
            extra={"url": profile_url},
        )

    # Log informatif si un proxy est actif pour cette tentative.
    if proxy_cfg:
        LOGGER.info(
            "Using proxy candidate",
            extra={
                "url": (
                    f"{_describe_proxy(proxy_cfg)}"
                    + (f" sticky={sticky_session_dir.name}" if sticky_session_dir else "")
                )
            },
        )

    # Tampon partage par le listener reseau (`handle_response`, defini plus
    # bas) entre deux extractions de cartes video. Reste valide a travers les
    # rotations puisqu'on ne fait qu'ajouter/vider son contenu (pas de
    # reassignation), donc pas besoin de `nonlocal` pour lui.
    network_posts: list[dict] = []
    # Signaux Response Classifier (Phase 0) — mutables pendant la tentative.
    attempt_signals: dict = {
        "item_list_xhr_seen": False,
        "fetch_failed": None,
        "ssr_universal_len": 0,
        "country": sticky_cc,
    }
    # Cell mutable: install_resource_blocker retourne le dict mute par les routes.
    bandwidth_cell: list[dict] = [{"aborted": 0, "bytes": 0}]

    def handle_response(response):
        """Capture active des JSON grille profil (`/api/post/item_list/`, user/detail).

        Preferer le JSON (statusCode==0 + itemList) au parse DOM quand le grid
        est soft-bloque (squelette sans ancres `/video/`).
        """
        rurl = response.url
        if not _is_profile_grid_api_url(rurl):
            return
        attempt_signals["item_list_xhr_seen"] = True
        try:
            http_status = int(getattr(response, "status", 0) or 0)
        except (TypeError, ValueError):
            http_status = 0
        if http_status and http_status >= 400:
            return
        try:
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:
            ctype = ""
        # TikTok renvoie parfois JSON sans content-type fiable — tenter quand meme.
        if ctype and ("json" not in ctype and "javascript" not in ctype and "text/plain" not in ctype):
            return
        try:
            payload = response.json()
        except Exception:
            LOGGER.debug("Failed to read grid API JSON body", exc_info=True)
            return
        if not _api_payload_status_ok(payload):
            LOGGER.info(
                "Profile grid API returned non-zero status (soft-block signal)",
                extra={"url": rurl.split("?")[0][:120]},
            )
            return
        try:
            # Priorite: parse hydration-aware (itemList + stats), puis walk generique.
            parsed = []
            try:
                from profile_extractor import extract_posts_from_hydration_payload

                parsed = extract_posts_from_hydration_payload(payload, profile_url=profile_url)
            except Exception:
                parsed = []
            if not parsed:
                parsed = _extract_posts_from_json_payload(payload)
            if parsed:
                stamped = [
                    _stamp_network_api_post(p, target_username)
                    for p in parsed
                    if isinstance(p, dict)
                ]
                network_posts.extend(stamped)
                LOGGER.info(
                    "Captured %d posts from grid API %s (buffer=%d)",
                    len(stamped),
                    rurl.split("?")[0][-48:],
                    len(network_posts),
                    extra={"url": profile_url},
                )
        except Exception:
            LOGGER.debug("Failed to parse network JSON response", exc_info=True)

    def _open_context_and_page() -> None:
        """(Ré)ouvre un contexte Playwright + une page.

        Appelee une premiere fois au demarrage, puis re-appelee a chaque
        rotation d'IPv6 (apres avoir ferme l'ancien contexte). La config
        (user-agent, proxy, cookies, stealth) est identique a chaque appel:
        seule l'identite reseau change, cote proxy local (voir
        `_rotate_ipv6_identity`), pas cote Playwright.
        """
        nonlocal browser, context, page

        # Canal deja calcule plus haut (browser_channel / using_real_chrome).

        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            if sticky_session_dir is not None:
                sticky_sessions.clear_chrome_profile_locks(sticky_session_dir)
            launch_persistent_args = {
                "user_data_dir": user_data_dir,
                "headless": headless,
                "slow_mo": slow_mo_ms,
                "args": browser_args,
                "ignore_default_args": ignored_default_args,
                **context_options,
            }
            if browser_channel:
                launch_persistent_args["channel"] = browser_channel
            if proxy_cfg and not _force_direct_mode():
                launch_persistent_args["proxy"] = sanitize_playwright_proxy(proxy_cfg)
            else:
                launch_persistent_args.pop("proxy", None)
            context = playwright.chromium.launch_persistent_context(**launch_persistent_args)
            browser = None
        else:
            launch_args = {
                "headless": headless,
                "slow_mo": slow_mo_ms,
                "args": browser_args,
                "ignore_default_args": ignored_default_args,
            }
            if browser_channel:
                launch_args["channel"] = browser_channel
            if proxy_cfg and not _force_direct_mode():
                launch_args["proxy"] = sanitize_playwright_proxy(proxy_cfg)
            browser = playwright.chromium.launch(**launch_args)
            context = browser.new_context(**context_options)

        # Empreinte: sur VRAI Chrome, AUCUN overlay (historique 3e1b957:
        # Sec-CH-UA / webdriver mask / stealth → grille profil vide).
        accept_lang = _active_accept_language()
        if using_real_chrome:
            LOGGER.info(
                "Real Chrome channel — native fingerprint (no stealth/webdriver/Sec-CH-UA overlays)"
            )
            context.set_extra_http_headers({"Accept-Language": accept_lang})
        else:
            if _should_apply_stealth(user_data_dir):
                _install_stealth_scripts(context)
            else:
                install_webdriver_mask(context)
            chrome_headers = {
                "Accept-Language": accept_lang,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8,"
                    "application/signed-exchange;v=b3;q=0.7"
                ),
                "Upgrade-Insecure-Requests": "1",
                "Sec-CH-UA": _sec_ch_ua(),
                "Sec-CH-UA-Platform": _sec_ch_ua_platform(),
                "Sec-CH-UA-Mobile": _sec_ch_ua_mobile(),
            }
            context.set_extra_http_headers(chrome_headers)

        # Cookies sticky: reinjection UNIQUEMENT si REUSE + should_reuse (pas success=False).
        if use_sticky and sticky_identity:
            if reuse_cookies and sticky_sessions.should_reuse_sticky_cookies(sticky_identity):
                sticky_cookies = sticky_sessions.load_sticky_cookies(sticky_identity)
                if sticky_cookies:
                    try:
                        context.add_cookies(sticky_cookies)
                        LOGGER.info(
                            "Reusing sticky cookies for IP (last success OK)",
                            extra={"url": sticky_identity},
                        )
                    except Exception:
                        LOGGER.warning("Failed to inject sticky cookies", exc_info=True)
            else:
                LOGGER.info(
                    "Opening sticky context VIRGIN (no cookie reinjection)",
                    extra={"url": sticky_identity},
                )
        else:
            force_cookie_injection = _env_bool("TIKTOK_FORCE_COOKIE_INJECTION", False)
            if force_cookie_injection:
                cookies = load_cookies()
                if cookies:
                    try:
                        context.add_cookies(cookies)
                    except Exception:
                        LOGGER.warning("Failed to inject cookies", exc_info=True)

        # Diagnostic logged_in: injecter un cookie jar session (hors sticky reuse).
        diag_cookies_path = (os.getenv("TIKTOK_DIAG_SESSION_COOKIES_FILE") or "").strip()
        if diag_cookies_path:
            try:
                diag_cookies = load_cookies()
                if diag_cookies:
                    context.add_cookies(diag_cookies)
                    LOGGER.info(
                        "[DIAG] Injected %d session cookies from %s",
                        len(diag_cookies),
                        diag_cookies_path,
                    )
            except Exception:
                LOGGER.warning("[DIAG] Failed to inject session cookies", exc_info=True)

        # Phase 3: bloquer image/media/font (10GB/mois Webshare).
        # Toggle runtime: TIKTOK_BLOCK_HEAVY_ASSETS=true|false (pas de rebuild).
        try:
            bandwidth_cell[0] = install_resource_blocker(context)
        except Exception:
            LOGGER.debug("resource blocker install failed", exc_info=True)

        page = context.new_page()
        # playwright-stealth: jamais sur vrai Chrome (casse item_list).
        if not using_real_chrome and _should_apply_stealth(user_data_dir):
            try:
                from playwright_stealth import stealth_sync  # type: ignore

                stealth_sync(page)
                LOGGER.info("playwright-stealth applied on page")
            except Exception:
                LOGGER.debug("playwright-stealth not available; built-in masks active")
        page.on("response", handle_response)

    def _close_current_context() -> None:
        """Ferme proprement le contexte/navigateur courant avant une rotation.

        Fait "au mieux": une erreur de fermeture ne doit jamais empecher la
        suite du scraping (on log juste un warning).
        """
        nonlocal browser, context, page
        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.warning("Failed to close context during IPv6 rotation", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                LOGGER.warning("Failed to close browser during IPv6 rotation", exc_info=True)
        context = None
        browser = None
        page = None

    try:
        # IMPORTANT: le launch doit etre DANS le try. Sinon un SingletonLock
        # Chrome (TargetClosedError) remonte au worker au lieu d'etre traite
        # comme soft-failure + rotation de proxy.
        attempt_deadline = time.monotonic() + _attempt_timeout_s()
        LOGGER.info(
            "Scrape attempt budget=%.0fs",
            _attempt_timeout_s(),
            extra={"url": profile_url},
        )
        _open_context_and_page()
        _check_attempt_deadline(attempt_deadline, phase="after_launch")

        def _build_partial_result(posts: list[dict], warning: str | None = None) -> dict:
            payload = {
                "posts": posts,
                "total": len(posts),
                "url": profile_url,
                "page_report_docx": None,
                "page_report_pdf": None,
            }
            if warning:
                payload["warning"] = warning
            return payload

        ssr_posts_out: list[dict] = []
        # Bypass homepage UNIQUEMENT si on reinjecte vraiment des cookies sticky
        # d'un SUCCESS precedent. Jamais skip warmup en VIRGIN (sinon WAF ~700B).
        can_reuse_sticky = bool(
            reuse_cookies
            and use_sticky
            and sticky_identity
            and sticky_sessions.should_reuse_sticky_cookies(sticky_identity)
            and sticky_sessions.last_attempt_succeeded(sticky_identity) is True
        )
        skip_home = can_reuse_sticky
        if skip_homepage_warmup and not can_reuse_sticky:
            LOGGER.info(
                "Ignoring skip_homepage_warmup — VIRGIN session needs homepage cookies first",
                extra={"url": profile_url},
            )
        if skip_home:
            LOGGER.info(
                "Skipping homepage warmup — reusing sticky cookies from prior SUCCESS",
                extra={"url": profile_url},
            )
        grid_ready = _warmup_and_open_profile(
            page,
            profile_url,
            sticky_identity=sticky_identity if use_sticky else None,
            network_posts=network_posts,
            ssr_posts_out=ssr_posts_out,
            skip_homepage_warmup=skip_home,
        )
        _check_attempt_deadline(attempt_deadline, phase="after_warmup_profile")

        # Captcha modal reel: inutile de re-attendre 25s / re-warmup.
        # Soft-block squelette (sans captcha) -> on tente l'extract ci-dessous.
        if _looks_like_tiktok_challenge(page):
            has_captcha_ui = False
            try:
                has_captcha_ui = page.locator(
                    'iframe[src*="captcha"], [id*="captcha"], [class*="captcha"]'
                ).count() > 0
            except Exception:
                has_captcha_ui = False
            if has_captcha_ui and not grid_ready:
                _manual_solve_wait_if_enabled(user_data_dir, headless)
                _wait_for_challenge_resolution(page, user_data_dir, headless)
                if _looks_like_tiktok_challenge(page):
                    _save_challenge_artifacts(page)
                    return {
                        "posts": [],
                        "total": 0,
                        "error": "challenge_detected",
                        "url": profile_url,
                    }
                try:
                    ssr_posts_out.clear()
                    # Apres captcha: forcer un vrai warmup homepage.
                    grid_ready = _warmup_and_open_profile(
                        page,
                        profile_url,
                        sticky_identity=sticky_identity if use_sticky else None,
                        network_posts=network_posts,
                        ssr_posts_out=ssr_posts_out,
                        skip_homepage_warmup=False,
                    )
                except Exception:
                    LOGGER.warning("Challenge retry warmup failed", exc_info=True)

        # Priorite ABSOLUE: buffer reseau (preload/item_list) deja rempli
        # pendant warmup/nav — SUCCESS direct, zero wait/scroll/rotation.
        seen = set()
        ssr_uni_len = 0

        def _ingest_network_posts(*, force: bool = False) -> int:
            """Drain `network_posts` → `all_posts`. Preserve les non-ingeres."""
            ingested = 0
            remaining: list[dict] = []
            for card in list(network_posts):
                if not isinstance(card, dict):
                    continue
                if len(all_posts) >= max_posts:
                    remaining.append(card)
                    continue
                stamped = (
                    _stamp_network_api_post(card, target_username)
                    if force or str(card.get("_source") or "") == "network_api"
                    else card
                )
                sig = _video_signature(stamped)
                if not sig or sig in seen:
                    continue
                if not force and not _card_belongs_to_profile(stamped, target_username):
                    remaining.append(stamped)
                    continue
                seen.add(sig)
                all_posts.append(stamped)
                ingested += 1
            network_posts.clear()
            network_posts.extend(remaining)
            return ingested

        # 1) Buffer reseau d'abord (souvent deja rempli avant cette ligne).
        api_ingested = _ingest_network_posts(force=True)
        if api_ingested:
            LOGGER.info(
                "Network buffer SUCCESS: ingested %d posts (skip SSR wait/scroll)",
                api_ingested,
                extra={"url": profile_url},
            )

        # 2) SSR seulement si buffer reseau vide.
        if not all_posts:
            try:
                if ssr_posts_out:
                    profile_posts = list(ssr_posts_out)
                    ssr_uni_len = _probe_universal_len(page)
                else:
                    ssr_uni_len, profile_posts = extract_posts_from_hydration(
                        page, profile_url=profile_url
                    )
                    if profile_posts:
                        ssr_posts_out.extend(profile_posts)
            except Exception:
                LOGGER.warning("Profile-first hydration extract failed", exc_info=True)
                profile_posts = []
                ssr_uni_len = _probe_universal_len(page)

            for card in profile_posts:
                sig = _video_signature(card)
                if sig in seen or not _card_belongs_to_profile(card, target_username):
                    seen.add(sig)
                    continue
                seen.add(sig)
                all_posts.append(card)
                if len(all_posts) >= max_posts:
                    break

        # 3) Attente item_list UNIQUEMENT si toujours 0 post.
        if not all_posts and (ssr_uni_len > 50000 or grid_ready):
            wait_ms = _grid_wait_ms()
            LOGGER.info(
                "No posts yet (universalLen=%s) — waiting up to %sms for item_list/DOM",
                ssr_uni_len,
                wait_ms,
                extra={"url": profile_url},
            )
            _wait_for_profile_grid_data(page, network_posts, timeout_ms=wait_ms)
            api_ingested = _ingest_network_posts(force=True)
            if api_ingested:
                LOGGER.info(
                    "Ingested %d posts from profile grid API after wait",
                    api_ingested,
                    extra={"url": profile_url},
                )

        # 3b) SPA n'a jamais emis item_list → fetch signe in-page (etape 1 Claude).
        attempt_signals["ssr_universal_len"] = int(ssr_uni_len or 0)
        if not all_posts and (ssr_uni_len > 50000 or grid_ready):
            forced_n = _ingest_forced_item_list(
                page,
                network_posts,
                profile_url=profile_url,
                target_username=target_username,
                count=min(30, max_posts) if max_posts else 30,
            )
            if forced_n == -1:
                attempt_signals["fetch_failed"] = True
            elif forced_n:
                attempt_signals["fetch_failed"] = False
                api_ingested = _ingest_network_posts(force=True)
                if api_ingested:
                    LOGGER.info(
                        "Ingested %d posts from FORCED item_list fetch",
                        api_ingested,
                        extra={"url": profile_url},
                    )

        # Fast-path si posts presents (reseau ou SSR).
        ssr_fast_path = bool(all_posts)
        if all_posts:
            LOGGER.info(
                "Profile extract ready with %d posts (network/SSR) — no soft-failure",
                len(all_posts),
                extra={"url": profile_url},
            )

        stop_due_to_age = False

        # --- Rotation IPv6 "toutes les N videos" -----------------------------
        # `posts_at_last_rotation` retient combien de posts on avait deja
        # collecte au moment de la derniere rotation (0 au debut). Des que
        # `len(all_posts) - posts_at_last_rotation >= rotate_every_n_posts`,
        # on change d'adresse IPv6 et on repart avec un navigateur neuf.
        posts_at_last_rotation = 0

        # La boucle etait auparavant limitee a 8 scrolls fixes. Avec la
        # rotation activee (et des `max_posts` eleves, ex: 200 pour un
        # rapport 24h), il faut pouvoir scroller beaucoup plus longtemps.
        # On borne maintenant la boucle par un nombre max d'iterations
        # configurable, et on s'arrete plus tot si plusieurs scrolls de suite
        # ne rapportent aucune video nouvelle (plus la peine de continuer).
        max_scroll_iterations = _env_int("TIKTOK_MAX_SCROLL_ITERATIONS", 60)
        max_consecutive_empty_scrolls = _env_int("TIKTOK_MAX_EMPTY_SCROLLS", 2)
        consecutive_empty_scrolls = 0

        if ssr_fast_path:
            LOGGER.info(
                "API/SSR fast-path: skipping scroll (posts=%s buffer_left=%s)",
                len(all_posts),
                len(network_posts),
                extra={"url": profile_url},
            )
            max_scroll_iterations = 0
        elif ssr_uni_len > 50000 and not all_posts:
            max_scroll_iterations = max(
                1, min(2, _env_int("TIKTOK_SSR_EMPTY_MAX_SCROLLS", 2))
            )
            max_consecutive_empty_scrolls = 1
            LOGGER.info(
                "SSR empty of posts — allowing %d minimal scroll(s) for item_list",
                max_scroll_iterations,
                extra={"url": profile_url},
            )

        def _safe_collect_from_page() -> list[dict]:
            cards: list[dict] = []
            try:
                cards.extend(_extract_video_cards(page, profile_url))
            except Exception:
                LOGGER.debug("DOM extract failed during soft-block recovery", exc_info=True)
            try:
                cards.extend(_extract_posts_from_page_html(page, profile_url))
            except Exception:
                LOGGER.debug("HTML extract failed during soft-block recovery", exc_info=True)
            cards.extend(list(network_posts))
            return cards

        # Grille pas encore prete: extract API/DOM (ne PAS vider le buffer avant ingest).
        if not all_posts and (not grid_ready or ssr_uni_len > 50000):
            LOGGER.info(
                "Profile grid not ready yet — API/DOM extract + short wait (not WAF)",
                extra={"url": profile_url},
            )
            if max_scroll_iterations == 0:
                max_scroll_iterations = 1
            max_consecutive_empty_scrolls = max(1, max_consecutive_empty_scrolls)
            _ingest_network_posts(force=True)
            for card in _safe_collect_from_page():
                sig = _video_signature(card)
                if sig in seen or not _card_belongs_to_profile(card, target_username):
                    seen.add(sig)
                    continue
                seen.add(sig)
                all_posts.append(card)
                if len(all_posts) >= max_posts:
                    break
            if all_posts:
                LOGGER.info(
                    "Recovered %d posts from API/HTML/DOM (no WAF mark)",
                    len(all_posts),
                    extra={"url": profile_url},
                )
                ssr_fast_path = True
                max_scroll_iterations = 0

        # Scroll progressif pour charger davantage de posts.
        iteration = 0
        while iteration < max_scroll_iterations and len(all_posts) < max_posts:
            _check_attempt_deadline(attempt_deadline, phase=f"scroll_{iteration}")
            iteration += 1

            if _looks_like_tiktok_challenge(page):
                if all_posts:
                    LOGGER.warning("Challenge detected during scroll; returning partial posts")
                    break
                _manual_solve_wait_if_enabled(user_data_dir, headless)
                _wait_for_challenge_resolution(page, user_data_dir, headless)
                if _looks_like_tiktok_challenge(page):
                    _save_challenge_artifacts(page)
                    return {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}

            cards = _extract_video_cards(page, profile_url)
            # Ingerer d'abord le buffer reseau (ne jamais le drop avant append).
            _ingest_network_posts(force=True)
            batch = cards

            new_posts_this_round = 0
            for card in batch:
                sig = _video_signature(card)
                if sig in seen:
                    continue

                # Rejet des videos qui n'appartiennent pas au profil cible
                # (fuite du feed "For You" via SIGI_STATE.ItemModule).
                if not _card_belongs_to_profile(card, target_username):
                    seen.add(sig)
                    continue

                if max_age_hours is not None and max_age_hours > 0:
                    in_window = _is_post_within_hours(card, max_age_hours)
                    if in_window is False:
                        stop_due_to_age = True
                        break
                    if in_window is None:
                        # In time-window mode, skip posts with unknown publish timestamp.
                        continue

                seen.add(sig)
                all_posts.append(card)
                new_posts_this_round += 1
                if on_post is not None:
                    try:
                        on_post(dict(card))
                    except Exception:
                        LOGGER.warning("on_post callback error", exc_info=True)
                if len(all_posts) >= max_posts:
                    break

            if stop_due_to_age:
                break

            if len(all_posts) >= max_posts:
                break

            # --- Point de decision de la rotation IPv6 ---------------------
            # On ne verifie qu'ICI (apres avoir traite un lot de cartes, avant
            # de scroller) pour ne jamais rater le seuil de 30 videos, meme si
            # un lot en ramene plusieurs d'un coup.
            if (
                rotate_every_n_posts > 0
                and (len(all_posts) - posts_at_last_rotation) >= rotate_every_n_posts
            ):
                LOGGER.info(
                    "IPv6 rotation triggered after reaching threshold",
                    extra={
                        "posts_collected": len(all_posts),
                        "rotate_every_n_posts": rotate_every_n_posts,
                    },
                )
                _rotate_ipv6_identity()  # 1) le proxy local choisit une nouvelle IPv6
                _close_current_context()  # 2) on ferme le navigateur actuel (et ses connexions "vieille IP")
                _open_context_and_page()  # 3) on ouvre un navigateur neuf: ses connexions utiliseront la nouvelle IPv6
                _warmup_and_open_profile(
                    page,
                    profile_url,
                    sticky_identity=sticky_identity if use_sticky else None,
                    network_posts=network_posts,
                    skip_homepage_warmup=False,
                )  # 4) recharge profil dans contexte neuf (warmup homepage OK)
                posts_at_last_rotation = len(all_posts)
                consecutive_empty_scrolls = 0
                # On ne scrolle pas immediatement: la page vient d'etre
                # rechargee depuis le debut, le prochain tour de boucle va
                # deja trouver du contenu (les videos deja vues seront
                # ignorees grace a `seen`).
                continue

            if new_posts_this_round == 0:
                consecutive_empty_scrolls += 1
                if consecutive_empty_scrolls >= max_consecutive_empty_scrolls:
                    LOGGER.info(
                        "No new posts after %d scrolls in a row; stopping scroll loop",
                        consecutive_empty_scrolls,
                    )
                    break
            else:
                consecutive_empty_scrolls = 0

            _human_scroll_page(page)

            if _looks_like_tiktok_challenge(page):
                if all_posts:
                    LOGGER.warning("Challenge detected after scroll; stopping current page with partial posts")
                    break
                _save_challenge_artifacts(page)
                return {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}

        # Posts API arrives souvent APRES le 1er rendu — drain buffer avant abandon.
        late_api = _ingest_network_posts(force=True)
        if late_api:
            LOGGER.info(
                "Ingested %d late grid API posts after scroll loop",
                late_api,
                extra={"url": profile_url},
            )

        # Fallback final si aucune video n'a pu etre extraite via navigateur.
        if not all_posts:
            # Si le buffer a encore des posts: SUCCESS, jamais soft-failure.
            if network_posts:
                _ingest_network_posts(force=True)
            if not all_posts:
                wait_ms = _grid_wait_ms()
                LOGGER.info(
                    "No posts yet — final %sms wait for item_list/DOM (not WAF)",
                    wait_ms,
                    extra={"url": profile_url},
                )
                _wait_for_profile_grid_data(page, network_posts, timeout_ms=wait_ms)
                _ingest_network_posts(force=True)
                _human_pause(0.5, 0.3)
                late_cards = _extract_video_cards(page, profile_url)
                late_html = _extract_posts_from_page_html(page, profile_url)
                for card in late_cards + late_html:
                    sig = _video_signature(card)
                    if sig in seen:
                        continue
                    if not _card_belongs_to_profile(card, target_username):
                        seen.add(sig)
                        continue
                    seen.add(sig)
                    all_posts.append(card)
                    if len(all_posts) >= max_posts:
                        break
                _ingest_network_posts(force=True)

        # Securite finale: buffer reseau non vide ⇒ SUCCESS (jamais soft-failure/WAF).
        if not all_posts and network_posts:
            _ingest_network_posts(force=True)
            LOGGER.info(
                "Final network buffer drain → %d posts (prevent soft-failure)",
                len(all_posts),
                extra={"url": profile_url},
            )

        if not all_posts:
            http_posts = _extract_posts_from_html_fallback(profile_url)
            if http_posts:
                if max_age_hours is not None and max_age_hours > 0:
                    filtered = []
                    for post in http_posts:
                        in_window = _is_post_within_hours(post, max_age_hours)
                        if in_window is False:
                            break
                        if in_window is None:
                            continue
                        filtered.append(post)
                    http_posts = filtered

                if on_post is not None:
                    for post in http_posts[:max_posts]:
                        try:
                            on_post(dict(post))
                        except Exception:
                            LOGGER.warning("on_post callback error", exc_info=True)
                scrape_succeeded = True
                return {"posts": http_posts[:max_posts], "total": min(len(http_posts), max_posts), "url": profile_url}
            if _looks_like_tiktok_challenge(page):
                _save_challenge_artifacts(page)
                return {
                    "posts": [],
                    "total": 0,
                    "error": "challenge_detected",
                    "url": profile_url,
                    "http_status": 200,
                    "classification_signals": _collect_page_classification_signals(
                        page,
                        {**attempt_signals, "challenge_detected": True},
                    ),
                }
            # Aucun challenge reconnu, mais aucune video non plus: capturer
            # quand meme un screenshot/HTML, ce cas etant souvent aussi
            # difficile a diagnostiquer qu'un challenge classique (page vide,
            # redirection silencieuse, geo-restriction du proxy...).
            # Ne JAMAIS arriver ici si network_posts avait des elements.
            _save_challenge_artifacts(page, label="no_posts_found")
            return {
                "posts": [],
                "total": 0,
                "error": "no_posts_found",
                "url": profile_url,
                "http_status": 200,
                "classification_signals": _collect_page_classification_signals(
                    page, attempt_signals
                ),
            }

        # Post-traitements: enrichment conditionnel (skip si metrics profil
        # OU si le budget 35s est deja epuise — on renvoie les posts bruts).
        try:
            _check_attempt_deadline(attempt_deadline, phase="before_enrich")
            enrich_ok = True
        except AttemptTimeoutError:
            LOGGER.warning(
                "[PERF] Skipping enrichment — attempt budget already spent (%d posts kept)",
                len(all_posts),
                extra={"url": profile_url},
            )
            enrich_ok = False

        if enrich_ok:
            enriched_posts = _enrich_posts_from_video_pages(context, all_posts)
            if analyze_video_content:
                analyzed_posts = _attach_video_analysis(enriched_posts, on_post=None)
            else:
                analyzed_posts = enriched_posts
        else:
            analyzed_posts = all_posts[:max_posts]

        warning = "challenge_detected_partial" if _looks_like_tiktok_challenge(page) else None
        if not enrich_ok:
            warning = (warning + ";attempt_timeout_no_enrich") if warning else "attempt_timeout_no_enrich"
        scrape_succeeded = True
        return _build_partial_result(analyzed_posts, warning=warning)
    except AttemptTimeoutError as e:
        LOGGER.warning(
            "[PERF] Attempt budget exceeded (%.0fs) — rotate proxy: %s",
            _attempt_timeout_s(),
            str(e)[:120],
            extra={"url": profile_url},
        )
        if all_posts:
            scrape_succeeded = True
            return {
                "posts": all_posts[:max_posts],
                "total": min(len(all_posts), max_posts),
                "url": profile_url,
                "warning": "attempt_timeout_partial",
            }
        if use_sticky and sticky_identity:
            purge_sticky_after_failure(
                sticky_identity,
                reason="attempt_timeout",
                mark_waf=False,
            )
        return {
            "posts": [],
            "total": 0,
            "error": "proxy_blocked:attempt_timeout",
            "error_detail": str(e)[:160],
            "url": profile_url,
            "classification_signals": _collect_page_classification_signals(
                page, attempt_signals
            ),
        }
    except (ProxyBlockedException, AntibotBlockedException) as e:
        kind = getattr(e, "kind", None) or "blocked"
        error_code = f"proxy_blocked:{kind}"
        # Timeout goto / TLS: toujours soft — le caller rotationne l'IP Webshare.
        if kind == "auth" or is_proxy_auth_error(str(e)):
            LOGGER.warning(
                "[PROXY] AUTH FAILURE — sticky invalidate + abort IP retries "
                "(worker continues next job)",
                extra={"error": str(e)[:160], "url": profile_url},
            )
            error_code = "proxy_blocked:auth"
        elif kind == "tls_timeout":
            LOGGER.warning(
                "[ANTIBOT] TLS/NAV TIMEOUT on goto — invalidate sticky + rotate next IP "
                "(worker continues)",
                extra={"error": str(e)[:160], "url": profile_url},
            )
        elif str(kind).startswith("http_") or isinstance(e, AntibotBlockedException):
            LOGGER.warning(
                "[ANTIBOT_BLOCKED] HTTP %s on IP — purge sticky + rotate",
                kind,
                extra={"error": str(e)[:160], "url": profile_url},
            )
        else:
            LOGGER.warning(
                "[ANTIBOT] Proxy blocked (%s); purge+rotate required",
                kind,
                extra={"error": str(e)[:160], "url": profile_url},
            )
        if use_sticky and sticky_identity:
            # Timeout: invalider cookies (prochain essai = warmup homepage),
            # mais WAF mark seulement pour HTTP block reel.
            purge_sticky_after_failure(
                sticky_identity,
                reason=error_code,
                mark_waf=(kind != "tls_timeout")
                and (
                    _is_real_waf_block(page, error=error_code)
                    or str(kind) in ("http_403", "http_429", "http_404", "http_503", "auth")
                    or isinstance(e, AntibotBlockedException)
                ),
            )
        return {
            "posts": [],
            "total": 0,
            "error": error_code,
            "error_detail": str(e)[:200],
            "url": profile_url,
            "classification_signals": _collect_page_classification_signals(
                page, attempt_signals
            ),
        }
    except Exception as e:
        # Beaucoup d'exceptions ici sont ATTENDUES et deja gerees par la rotation
        # de proxy en amont (proxy lent/mort/bloque): net::ERR_TIMED_OUT,
        # ERR_CONNECTION_CLOSED, ERR_HTTP_RESPONSE_CODE_FAILURE (403 anti-bot)...
        # Pour ces cas on log UNE seule ligne WARNING lisible (sans stack trace),
        # et on reserve la trace ERROR complete aux vraies erreurs inattendues
        # (bug de code, etc.) qui, elles, meritent un diagnostic detaille.
        err_text = str(e)
        if _is_retryable_soft_error(err_text):
            # `splitlines()[0]` = juste "Page.goto: ..." sans dumper toute la
            # stack Playwright (bruit inutile dans les logs).
            LOGGER.warning(
                "Browser attempt failed (%s); rotating proxy candidate",
                err_text.splitlines()[0] if err_text else "unknown network error",
            )
        else:
            LOGGER.exception("Browser scraping pipeline failed")
        # Ne jamais jeter les posts deja collectes avant l'exception (ex: timeout
        # d'enrichissement, crash de page suite a une detection tardive, etc.).
        # Le caller (worker) decide s'il s'agit d'un succes partiel ou d'un echec sec.
        if all_posts:
            LOGGER.warning(
                "Exception occurred but %d posts were already collected; returning them as partial result",
                len(all_posts),
            )
            scrape_succeeded = True
            return {
                "posts": all_posts,
                "total": len(all_posts),
                "url": profile_url,
                "error": str(e),
                "warning": "exception_with_partial_posts",
            }
        if use_sticky and sticky_identity:
            reason = err_text.splitlines()[0][:160] if err_text else "exception"
            purge_sticky_after_failure(
                sticky_identity,
                reason=reason,
                mark_waf=_is_real_waf_block(page, error=reason),
            )
        return {"posts": [], "error": str(e), "url": profile_url}
    finally:
        # Bandwidth attempt (Phase 0/3) — Content-Length approx + assets abortes.
        try:
            bw = bandwidth_cell[0] if bandwidth_cell else {}
            nbytes = int(bw.get("bytes") or 0)
            naborted = int(bw.get("aborted") or 0)
            mb = nbytes / (1024.0 * 1024.0)
            _LAST_ATTEMPT_BANDWIDTH["bytes"] = nbytes
            _LAST_ATTEMPT_BANDWIDTH["aborted"] = naborted
            LOGGER.info(
                "[BANDWIDTH] attempt bytes≈%s (%.2f MB) aborted_assets=%s",
                nbytes,
                mb,
                naborted,
                extra={"url": profile_url},
            )
            if sticky_identity and mb > 0:
                identity_pool.add_bandwidth_mb(
                    identity_pool.identity_id_from_proxy_username(sticky_identity),
                    mb,
                )
            if not _env_bool("TIKTOK_DIAG_MODE", False):
                scrape_metrics.record_bandwidth_mb(sticky_cc, mb)
        except Exception:
            LOGGER.debug("bandwidth accounting failed", exc_info=True)

        # Cycle de vie cookies:
        # - Succes (+ REUSE): persister ttwid/msToken + mark success=True
        # - Echec: mark success=False + purge VIRGIN (pas de reinjection au retry)
        reuse_ok = _env_bool("TIKTOK_REUSE_STICKY_COOKIES", True)
        if use_sticky and sticky_identity:
            try:
                sticky_sessions.mark_attempt_result(
                    sticky_identity,
                    success=bool(scrape_succeeded),
                    reason="scrape_ok" if scrape_succeeded else "scrape_failed",
                )
            except Exception:
                LOGGER.debug("Failed to mark sticky attempt result", exc_info=True)

        if use_sticky and sticky_identity and context is not None and scrape_succeeded:
            try:
                cookies = context.cookies()
                names = {str(c.get("name") or "") for c in (cookies or [])}
                warm = "ttwid" in names and (
                    "msToken" in names or any(n.startswith("msToken") for n in names)
                )
                if reuse_ok and warm:
                    sticky_sessions.save_sticky_cookies(sticky_identity, cookies)
                    sticky_sessions.mark_session_warmed(sticky_identity, cookies)
                    LOGGER.info(
                        "Persisted sticky cookies (reuse=%s success=%s)",
                        reuse_ok,
                        scrape_succeeded,
                        extra={"url": sticky_identity},
                    )
            except Exception:
                LOGGER.debug("Failed to persist sticky cookies", exc_info=True)

        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.warning("Failed to close browser context", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                LOGGER.warning("Failed to close browser", exc_info=True)

        # Wipe disque apres echec reseau/WAF. no_posts HTTP 200 ≠ WAF.
        if use_sticky and sticky_identity and not scrape_succeeded:
            should_purge = _env_bool("PURGE_STICKY_ON_BLOCK", True) or (not reuse_ok)
            if should_purge:
                try:
                    sticky_sessions.purge_sticky_cookies(
                        sticky_identity,
                        reason="post_attempt_failure",
                        mark_waf=_is_real_waf_block(page, error="post_attempt_failure"),
                    )
                except Exception:
                    LOGGER.debug("Failed to wipe sticky after attempt", exc_info=True)

        if sticky_session_dir is not None and sticky_lock_held:
            try:
                if sticky_session_dir.exists():
                    sticky_sessions.touch_session(sticky_session_dir)
                    sticky_sessions.release_session_lock(sticky_session_dir)
            except Exception:
                LOGGER.debug("Failed to release sticky lock", exc_info=True)
            sticky_sessions.enforce_lru_limit()


def _extract_video_cards(page, profile_url: str) -> list:
    """Extrait les cartes video depuis le DOM et les etats JS de la page profil.

    La logique JS fusionne plusieurs sources pour limiter les trous de donnees:
    - liens /video/ visibles
    - `window.SIGI_STATE`
    - payload `__UNIVERSAL_DATA_FOR_REHYDRATION__`
    """
    data = page.evaluate(
        r"""
        () => {
            const byKey = new Map();

            const upsert = (raw) => {
                const postId = String(raw.post_id || '').trim();
                const postUrl = String(raw.post_url || '').trim();
                if (!postId && !postUrl) return;

                const key = postId ? `id:${postId}` : `url:${postUrl}`;
                const current = byKey.get(key) || {
                    post_id: postId,
                    post_url: postUrl,
                    message: '',
                    author: '',
                    published_at: null,
                    likes: null,
                    comments_count: null,
                    shares: null,
                    views: null,
                };

                const pick = (a, b) => (a !== null && a !== undefined && a !== '') ? a : b;
                current.post_id = pick(current.post_id, postId);
                current.post_url = pick(current.post_url, postUrl);
                current.message = pick(current.message, raw.message || '');
                current.author = pick(current.author, raw.author || '');
                current.published_at = pick(current.published_at, raw.published_at ?? null);
                current.likes = pick(current.likes, raw.likes ?? null);
                current.comments_count = pick(current.comments_count, raw.comments_count ?? null);
                current.shares = pick(current.shares, raw.shares ?? null);
                current.views = pick(current.views, raw.views ?? null);

                byKey.set(key, current);
            };

            const seenHref = new Set();

            const anchors = Array.from(document.querySelectorAll('a[href*="/video/"]'));
            for (const a of anchors) {
                const href = a.getAttribute('href') || '';
                if (!href || seenHref.has(href)) {
                    continue;
                }
                seenHref.add(href);

                const absUrl = href.startsWith('http') ? href : `https://www.tiktok.com${href}`;
                const idMatch = absUrl.match(/\/video\/(\d+)/);
                const postId = idMatch ? idMatch[1] : '';
                const authorMatch = absUrl.match(/@([^/]+)/);
                const author = authorMatch ? authorMatch[1] : '';

                const textNode = a.querySelector('[data-e2e="video-desc"]') || a.querySelector('img[alt]');
                const text = textNode ? (textNode.innerText || textNode.getAttribute('alt') || '').trim() : '';

                // Compteur de vues affiche sur la carte profil (souvent un <strong>).
                let cardViews = null;
                const card = a.closest('[data-e2e="user-post-item"]') || a.parentElement;
                if (card) {
                    const strong = card.querySelector('strong');
                    if (strong) {
                        const raw = (strong.innerText || strong.textContent || '').trim();
                        // Evite de prendre un badge "Pinned"/texte non numerique.
                        if (/[\d]/.test(raw)) {
                            let t = raw.toUpperCase().replace(/,/g, '').replace(/\s+/g, '');
                            let mult = 1;
                            if (t.endsWith('K')) { mult = 1e3; t = t.slice(0, -1); }
                            else if (t.endsWith('M')) { mult = 1e6; t = t.slice(0, -1); }
                            else if (t.endsWith('B')) { mult = 1e9; t = t.slice(0, -1); }
                            const n = Number.parseFloat(t);
                            if (Number.isFinite(n)) cardViews = Math.trunc(n * mult);
                        }
                    }
                }

                upsert({
                    post_id: postId,
                    post_url: absUrl,
                    message: text,
                    author,
                    published_at: null,
                    likes: null,
                    comments_count: null,
                    shares: null,
                    views: cardViews,
                });
            }

            // Fallback 1: état TikTok en mémoire (souvent présent sur desktop).
            const toInt = (raw) => {
                if (raw === null || raw === undefined || raw === '') return null;
                if (typeof raw === 'number' && Number.isFinite(raw)) return Math.trunc(raw);
                let text = String(raw).trim().toUpperCase().replace(/,/g, '').replace(/\s+/g, '');
                if (!text) return null;
                let mult = 1;
                if (text.endsWith('K')) { mult = 1e3; text = text.slice(0, -1); }
                else if (text.endsWith('M')) { mult = 1e6; text = text.slice(0, -1); }
                else if (text.endsWith('B')) { mult = 1e9; text = text.slice(0, -1); }
                const n = Number.parseFloat(text);
                return Number.isFinite(n) ? Math.trunc(n * mult) : null;
            };
            const statsFromItem = (item) => {
                const stats = (item && item.stats) || {};
                const statsV2 = (item && item.statsV2) || {};
                const pick = (...keys) => {
                    for (const key of keys) {
                        for (const source of [statsV2, stats]) {
                            if (source[key] !== undefined && source[key] !== null && source[key] !== '') {
                                const parsed = toInt(source[key]);
                                if (parsed !== null) return parsed;
                            }
                        }
                    }
                    return null;
                };
                return {
                    likes: pick('diggCount', 'likeCount', 'likes'),
                    comments_count: pick('commentCount', 'comments'),
                    shares: pick('shareCount', 'shares'),
                    views: pick('playCount', 'viewCount', 'views'),
                };
            };

            const sigi = window.SIGI_STATE;
            const itemModule = sigi && sigi.ItemModule ? sigi.ItemModule : null;
            if (itemModule) {
                for (const [id, item] of Object.entries(itemModule)) {
                    const author = (item && item.author) || '';
                    const desc = (item && item.desc) || '';
                    const absUrl = author ? `https://www.tiktok.com/@${author}/video/${id}` : '';
                    if (!absUrl) {
                        continue;
                    }
                    const st = statsFromItem(item);
                    upsert({
                        post_id: String(id || ''),
                        post_url: absUrl,
                        message: desc,
                        author,
                        published_at: item.createTime ?? item.create_time ?? null,
                        likes: st.likes,
                        comments_count: st.comments_count,
                        shares: st.shares,
                        views: st.views,
                    });
                }
            }

            // Fallback 2: JSON de rehydration (script tag) pour pages avec hydration SSR.
            const script = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (!script || !script.textContent) {
                return Array.from(byKey.values());
            }

            let hydration;
            try {
                hydration = JSON.parse(script.textContent);
            } catch {
                return Array.from(byKey.values());
            }

            const collect = (node) => {
                if (!node || typeof node !== 'object') return;

                const item = node.itemStruct || node.item || null;
                if (item && item.id) {
                    const id = String(item.id || '');
                    const author = (item.author && item.author.uniqueId) || '';
                    const desc = item.desc || '';
                    const absUrl = author ? `https://www.tiktok.com/@${author}/video/${id}` : '';
                    if (absUrl) {
                        const st = statsFromItem(item);
                        upsert({
                            post_id: id,
                            post_url: absUrl,
                            message: desc,
                            author,
                            published_at: item.createTime ?? item.create_time ?? null,
                            likes: st.likes,
                            comments_count: st.comments_count,
                            shares: st.shares,
                            views: st.views,
                        });
                    }
                }

                for (const val of Object.values(node)) {
                    if (val && typeof val === 'object') {
                        collect(val);
                    }
                }
            };

            collect(hydration);
            return Array.from(byKey.values());
        }
        """
    )

    videos = []
    for item in data:
        post_url = item.get("post_url") or profile_url
        videos.append(
            {
                "post_id": item.get("post_id") or "",
                "post_url": post_url,
                "message": item.get("message") or "",
                "author": item.get("author") or "",
                "published_at": _to_iso_datetime(item.get("published_at") or item.get("createTime") or item.get("create_time")),
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                "likes": item.get("likes"),
                "comments_count": item.get("comments_count"),
                "shares": item.get("shares"),
                "views": item.get("views"),
            }
        )
    return videos


def _extract_posts_from_json_payload(payload: object) -> list:
    """Parcourt recursivement un JSON TikTok et extrait les objets posts.

    Supporte les variantes frequentes de structure (`itemStruct`, `item`,
    `aweme_info`).
    """
    posts = []
    seen_ids = set()

    def collect(node: object):
        if isinstance(node, dict):
            # Variantes courantes de structure TikTok web
            item = node.get("itemStruct") or node.get("item") or node.get("aweme_info")
            if isinstance(item, dict):
                post_id = str(item.get("id") or item.get("aweme_id") or "").strip()
                if post_id and post_id not in seen_ids:
                    seen_ids.add(post_id)

                    author = ""
                    author_node = item.get("author")
                    if isinstance(author_node, dict):
                        author = (
                            author_node.get("uniqueId")
                            or author_node.get("unique_id")
                            or author_node.get("nickname")
                            or ""
                        )

                    stats_fields = _stats_from_item(item)
                    desc = item.get("desc") or item.get("description") or ""
                    published_at = _to_iso_datetime(item.get("createTime") or item.get("create_time") or item.get("create_time_stamp"))
                    post_url = f"https://www.tiktok.com/@{author}/video/{post_id}" if author else ""

                    posts.append(
                        {
                            "post_id": post_id,
                            "post_url": post_url,
                            "message": desc,
                            "author": author,
                            "published_at": published_at,
                            "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                            "likes": stats_fields["likes"],
                            "comments_count": stats_fields["comments_count"],
                            "shares": stats_fields["shares"],
                            "views": stats_fields["views"],
                        }
                    )

            for val in node.values():
                collect(val)

        elif isinstance(node, list):
            for val in node:
                collect(val)

    collect(payload)
    return posts


def _looks_like_tiktok_challenge(page) -> bool:
    """Heuristique de detection challenge/captcha TikTok.

    Verifie:
    - presence normale de signaux de posts
    - URL challenge/captcha/checkpoint
    - mots-cles dans le body
    - selecteurs captcha courants
    """
    try:
        has_posts_signal = page.evaluate(
            r"""
            () => {
                const anchors = document.querySelectorAll('a[href*="/video/"]').length;
                if (anchors > 0) return true;
                const sigi = window.SIGI_STATE;
                const itemModule = sigi && sigi.ItemModule ? sigi.ItemModule : null;
                return !!(itemModule && Object.keys(itemModule).length > 0);
            }
            """
        )
        if has_posts_signal:
            return False

        current_url = (page.url or "").lower()
        if any(token in current_url for token in ("/challenge", "/captcha", "/verify", "/checkpoint")):
            return True

        body_text = ""
        try:
            body_text = (page.locator("body").inner_text(timeout=2000) or "").lower()
        except Exception as exc:
            LOGGER.debug("Failed to read page body text", exc_info=True)

        if any(
            token in body_text
            for token in (
                "verify to continue",
                "security check",
                "unusual traffic",
                "complete the captcha",
                "something went wrong",
                "something went wrong. please try again",
                "an unexpected error occurred",
                "an unexpected error occurred. please try again",
            )
        ):
            return True

        for selector in (
            'iframe[src*="captcha"]',
            '[id*="captcha"]',
            '[class*="captcha"]',
            '[data-e2e*="captcha"]',
            '[data-e2e*="verify"]',
        ):
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue

        return False
    except Exception:
        return False


def _dismiss_cookie_banner(page):
    """Ferme la banniere cookies si elle apparait (plusieurs langues)."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        'button:has-text("Accept")',
        'button:has-text("Tout accepter")',
        'button:has-text("Autoriser")',
        '[data-e2e="cookie-banner-accept"]',
        'button[data-e2e="cookie-banner-btn"]',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.click(timeout=2000)
                return
        except Exception:
            continue


def _diag_log_external_ip(page, phase: str) -> None:
    """IP externe vue via le contexte navigateur (passe par le proxy si actif)."""
    if not _diag_softblock_enabled():
        return
    ip = "?"
    try:
        resp = page.context.request.get("https://api.ipify.org?format=json", timeout=10000)
        try:
            payload = resp.json()
            ip = str((payload or {}).get("ip") or resp.text() or "?")
        except Exception:
            ip = (resp.text() or "?")[:64]
        LOGGER.warning(
            "[DIAG] %s external_ip=%s http=%s",
            phase,
            ip,
            getattr(resp, "status", "?"),
        )
    except Exception as exc:
        LOGGER.warning("[DIAG] %s external_ip fetch failed: %s", phase, str(exc)[:120])


def _diag_log_cookies(page, phase: str) -> None:
    """Liste detaillee des cookies TikTok apres warmup / profil."""
    if not _diag_softblock_enabled():
        return
    try:
        cookies = page.context.cookies() or []
    except Exception as exc:
        LOGGER.warning("[DIAG] %s cookies read failed: %s", phase, str(exc)[:120])
        return
    names = sorted({str(c.get("name") or "") for c in cookies if c.get("name")})
    key_names = (
        "ttwid",
        "msToken",
        "tt_chain_token",
        "tt_csrf_token",
        "s_v_web_id",
        "odin_tt",
        "passport_csrf_token",
        "sid_tt",
        "sessionid",
    )
    detail = {}
    for c in cookies:
        name = str(c.get("name") or "")
        if name in key_names:
            val = str(c.get("value") or "")
            detail[name] = {
                "len": len(val),
                "domain": c.get("domain"),
                "path": c.get("path"),
                "preview": (val[:12] + "…") if len(val) > 12 else val,
            }
    LOGGER.warning(
        "[DIAG] %s cookies_total=%d has_ttwid=%s has_msToken=%s names=%s key=%s",
        phase,
        len(cookies),
        "ttwid" in names,
        any(n.startswith("msToken") or n == "msToken" for n in names),
        names[:40],
        detail,
    )


def _diag_log_hydration(page, phase: str) -> None:
    """Presence __UNIVERSAL_DATA_FOR_REHYDRATION__ / __INIT_DATA__ / liens video."""
    if not _diag_softblock_enabled():
        return
    try:
        flags = page.evaluate(
            """() => {
              const uni = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
              const init = document.getElementById('__INIT_DATA__')
                || document.querySelector('[id*="__INIT_DATA__"]');
              const sigi = document.getElementById('SIGI_STATE');
              let uniLen = 0;
              try { uniLen = uni && uni.textContent ? uni.textContent.length : 0; } catch (e) {}
              return {
                universal: !!uni,
                universalLen: uniLen,
                initData: !!init,
                sigi: !!sigi,
                videoLinks: document.querySelectorAll('a[href*="/video/"]').length,
                bodyLen: (document.body && document.body.innerText || '').length,
                webdriver: navigator.webdriver,
                ua: navigator.userAgent || '',
                url: location.href || '',
              };
            }"""
        )
        LOGGER.warning("[DIAG] %s hydration=%s", phase, flags)
    except Exception as exc:
        LOGGER.warning("[DIAG] %s hydration probe failed: %s", phase, str(exc)[:120])


def _diag_log_profile_http(response, profile_url: str, exc: BaseException | None = None) -> None:
    """Code HTTP + headers bruts de la navigation profil."""
    if not _diag_softblock_enabled():
        return
    if response is None:
        LOGGER.warning(
            "[DIAG] profile_http response=None url=%s exc=%s",
            profile_url,
            (str(exc).splitlines()[0][:200] if exc else None),
        )
        return
    try:
        headers = {}
        try:
            headers = dict(response.headers)
        except Exception:
            headers = {}
        interesting = {
            k: headers.get(k)
            for k in (
                "content-type",
                "content-length",
                "server",
                "x-cache",
                "x-tt-logid",
                "x-forbidden-reason",
                "akamai-grn",
                "set-cookie",
            )
            if headers.get(k) is not None
        }
        LOGGER.warning(
            "[DIAG] profile_http status=%s ok=%s url=%s headers=%s",
            getattr(response, "status", None),
            getattr(response, "ok", None),
            (getattr(response, "url", None) or profile_url)[:180],
            interesting or {k: headers[k] for k in list(headers)[:12]},
        )
    except Exception as exc:
        LOGGER.warning("[DIAG] profile_http log failed: %s", str(exc)[:120])


def _cookies_have_tiktok_warmup(cookies: list | None) -> bool:
    names = {str(c.get("name") or "") for c in (cookies or []) if isinstance(c, dict)}
    has_ttwid = "ttwid" in names
    has_ms = "msToken" in names or any(n.startswith("msToken") for n in names)
    return has_ttwid and has_ms


def _persist_warmup_cookies(page, sticky_identity: str | None) -> None:
    """Sauvegarde ttwid/msToken apres warmup pour reemploi sur la meme IP."""
    if not sticky_identity or sticky_identity == "direct":
        return
    if not _env_bool("TIKTOK_REUSE_STICKY_COOKIES", True):
        return
    try:
        cookies = page.context.cookies() or []
    except Exception:
        return
    if not _cookies_have_tiktok_warmup(cookies):
        LOGGER.warning(
            "Warmup cookies incomplete — not persisting yet",
            extra={"url": sticky_identity},
        )
        return
    try:
        sticky_sessions.save_sticky_cookies(sticky_identity, cookies)
        sticky_sessions.mark_session_warmed(sticky_identity, cookies)
        LOGGER.info(
            "Persisted warmup cookies for sticky IP (ttwid+msToken)",
            extra={"url": sticky_identity},
        )
    except Exception:
        LOGGER.debug("Failed to persist warmup cookies", exc_info=True)


def warmup_session(page, sticky_identity: str | None = None) -> None:
    """Chauffe homepage rapidement AVANT le profil (max ~8s cookies).

    Sequence:
      1) goto https://www.tiktok.com/
      2) mini-scroll souris leger
      3) poll ttwid+msToken (max 8s) — enchaine DES QUE presents (pas de sleep fixe)
      4) persister cookies sticky si REUSE=true
    """
    LOGGER.info(
        "Warmup session: homepage landing + cookie wait (sticky=%s)",
        sticky_identity or "none",
        extra={"url": sticky_identity or "direct"},
    )
    # 1) Homepage — fail-fast si HTTP block / TLS timeout.
    home_resp = goto_strict(page, "https://www.tiktok.com/", timeout=nav_timeout_ms())
    if _diag_softblock_enabled():
        try:
            LOGGER.warning(
                "[DIAG] warmup_home HTTP status=%s url=%s",
                getattr(home_resp, "status", None),
                getattr(home_resp, "url", "https://www.tiktok.com/"),
            )
        except Exception:
            pass
    _dismiss_cookie_banner(page)

    # 2) Micro-interaction (pas de pause longue).
    try:
        page.mouse.move(random.randint(120, 500), random.randint(140, 420))
        page.mouse.wheel(0, random.randint(150, 350))
    except Exception:
        LOGGER.debug("Warmup landing mouse/scroll failed", exc_info=True)

    # 3) Poll cookies WAF — plafond 8s, sortie immédiat si prêts.
    cookie_budget_ms = max(2000, min(8000, _env_int("TIKTOK_WARMUP_WAIT_MS", 8000)))
    poll_ms = 250
    deadline = time.monotonic() + (cookie_budget_ms / 1000.0)
    warm_ok = False
    while time.monotonic() < deadline:
        try:
            cookies = page.context.cookies() or []
            if _cookies_have_tiktok_warmup(cookies):
                warm_ok = True
                break
        except Exception:
            pass
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)

    if warm_ok:
        LOGGER.info(
            "Warmup: ttwid+msToken ready — chaining to profile immediately",
            extra={"url": sticky_identity or "direct"},
        )
    else:
        LOGGER.warning(
            "Warmup: ttwid/msToken not both ready within %dms — continuing anyway",
            cookie_budget_ms,
            extra={"url": sticky_identity or "direct"},
        )

    try:
        cookies = page.context.cookies() or []
        names = {str(c.get("name") or "") for c in cookies}
        warm_ok = _cookies_have_tiktok_warmup(cookies)
        LOGGER.info(
            "Warmup homepage OK — cookies=%s ttwid=%s msToken=%s warm_ok=%s",
            len(names),
            "ttwid" in names,
            "msToken" in names or any(n.startswith("msToken") for n in names),
            warm_ok,
            extra={"url": sticky_identity or "direct"},
        )
    except Exception:
        LOGGER.info("Warmup homepage OK", extra={"url": sticky_identity or "direct"})

    _diag_log_external_ip(page, "warmup_home")
    _diag_log_cookies(page, "warmup_home")
    _diag_log_hydration(page, "warmup_home")
    _persist_warmup_cookies(page, sticky_identity)


def _profile_has_loading_skeleton(page) -> bool:
    """True si le profil est la mais la grille semble encore en chargement."""
    try:
        return bool(
            page.evaluate(
                r"""
                () => {
                  const anchors = document.querySelectorAll('a[href*="/video/"]').length;
                  if (anchors > 0) return false;
                  const body = (document.body && document.body.innerText) || '';
                  const hasProfile = body.includes('Followers') || body.includes('Following')
                    || body.includes('Likes') || !!document.querySelector('[data-e2e="user-page"]');
                  if (!hasProfile) return false;
                  // Spinner / skeleton frequent sur soft-block lent.
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def _apply_chrome_profile_nav_headers(page, *, referer: str = "https://www.tiktok.com/") -> None:
    """Headers avant navigation profil.

    Vrai Chrome: Referer + Accept-Language UNIQUEMENT (pas de Sec-CH-UA forge —
    regression historique documentee: overlays → soft-block grille).
    Chromium embarque: jeu complet Client-Hints.
    Accept-Language suit le pays sticky (_ACTIVE_BROWSER_GEO).
    """
    accept_lang = _active_accept_language()
    if _is_real_chrome_channel():
        headers = {
            "Referer": referer or "https://www.tiktok.com/",
            "Accept-Language": accept_lang,
        }
    else:
        headers = {
            "Referer": referer or "https://www.tiktok.com/",
            "Accept-Language": accept_lang,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Sec-CH-UA": _CHROME_SEC_CH_UA,
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Mobile": "?0",
            "User-Agent": _chrome_user_agent(),
        }
    try:
        page.set_extra_http_headers(headers)
    except Exception:
        LOGGER.debug("Failed to set Chrome nav headers on page", exc_info=True)


def _profile_page_landed(page, username: str) -> bool:
    """True si l'URL courante ressemble au profil cible (meme apres soft HTTP)."""
    if not username:
        return False
    try:
        current = (page.url or "").lower()
    except Exception:
        return False
    if current.startswith("chrome-error:") or "chromewebdata" in current:
        return False
    handle = username.lower().lstrip("@")
    if f"/@{handle}" in current:
        return True
    # Parfois query/hash; accepter prefix path.
    return f"tiktok.com/@{handle}" in current.replace("www.", "")


def _wait_profile_navigation_settle(page, timeout_ms: int | None = None) -> None:
    """Attend fin de navigation (court) — evite race Execution context destroyed."""
    t = min(5000, timeout_ms if timeout_ms is not None else nav_timeout_ms())
    try:
        page.wait_for_load_state("domcontentloaded", timeout=t)
    except Exception:
        pass
    try:
        page.wait_for_timeout(random.randint(80, 200))
    except Exception:
        pass


def _is_hard_cdn_nav_failure(
    page=None,
    *,
    exc: BaseException | str | None = None,
    status: int | None = None,
) -> bool:
    """True pour ERR_HTTP / 403/404/429/503 / chrome-error → fail-fast."""
    if status in (403, 404, 429, 401, 451, 503):
        return True
    err = str(exc or "").lower()
    if any(
        tok in err
        for tok in (
            "err_http_response_code_failure",
            "http_403",
            "http_404",
            "http_429",
            "http_503",
            "http 403",
            "chrome-error",
            "chromewebdata",
        )
    ):
        return True
    if page is not None and _page_is_chrome_error(page):
        return True
    return False


def _response_content_length(response) -> int | None:
    if response is None:
        return None
    try:
        raw = (response.headers or {}).get("content-length")
        if raw is None:
            return None
        return int(raw)
    except (TypeError, ValueError, AttributeError):
        return None


def _probe_universal_len(page) -> int:
    """Taille du script #__UNIVERSAL_DATA_FOR_REHYDRATION__ (0 si absent)."""
    try:
        return int(
            page.evaluate(
                """() => {
                  const uni = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                  return uni && uni.textContent ? uni.textContent.length : 0;
                }"""
            )
            or 0
        )
    except Exception:
        return 0


def _response_is_silent_soft_block(response, page=None) -> bool:
    """HTTP 200 + HTML minuscule / pas d'hydration = soft-block silencieux.

    IMPORTANT: le header Content-Length est souvent trompeur (compression,
    CL partiel, mismatch). Si le DOM contient deja un gros payload
    __UNIVERSAL_DATA_FOR_REHYDRATION__, ce n'est PAS un soft-block.
    """
    # Priorite absolue: hydration SSR presente → jamais soft-block.
    if page is not None:
        uni_len = _probe_universal_len(page)
        if uni_len > 50000:
            return False
        if uni_len > 2000:
            return False

    cl = _response_content_length(response)
    if page is None:
        # Sans page, CL seul est un signal faible — seulement les vrais squelettes.
        return cl is not None and cl < 5000

    try:
        info = page.evaluate(
            """() => {
              const uni = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
              const uniLen = uni && uni.textContent ? uni.textContent.length : 0;
              const bodyLen = (document.body && document.body.innerText || '').length;
              const videos = document.querySelectorAll('a[href*="/video/"]').length;
              return { uniLen, bodyLen, videos };
            }"""
        )
        if not isinstance(info, dict):
            return False
        uni_len = int(info.get("uniLen") or 0)
        videos = int(info.get("videos") or 0)
        body_len = int(info.get("bodyLen") or 0)
        # Soft-block reel: pas d'hydration ET pas de videos ET body vide.
        # Un CL header < 5k ne suffit PAS si uniLen / body disent le contraire.
        if uni_len > 0 or videos > 0:
            return False
        if cl is not None and cl < 5000 and body_len < 200:
            return True
        return uni_len == 0 and videos == 0 and body_len < 200
    except Exception:
        # En cas d'echec probe: ne pas purger sur CL seul si douteux.
        return False


def _profile_page_usable(page, username: str) -> bool:
    """True si le profil est vraiment hydrate (pas chrome-error / squelette vide).

    Les logs DIAG ont montre: in-page click → URL @user OK mais universal=False /
    videoLinks=0 → faux succes puis soft-block. On exige un signal de contenu.
    """
    if not _profile_page_landed(page, username):
        return False
    try:
        info = page.evaluate(
            """() => {
              const href = location.href || '';
              if (href.startsWith('chrome-error:') || href.includes('chromewebdata')) {
                return {ok: false, reason: 'chrome_error', href};
              }
              const uni = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
              const uniLen = uni && uni.textContent ? uni.textContent.length : 0;
              const videos = document.querySelectorAll('a[href*="/video/"]').length;
              const body = (document.body && document.body.innerText) || '';
              const hasProfile = /Followers|Following|Likes|Abonn[eé]s|J'aime/i.test(body)
                || !!document.querySelector(
                  '[data-e2e="user-page"], [data-e2e="user-title"], [data-e2e="user-post-item"]'
                );
              const challenge = /captcha|verify you|unusual traffic|drag the/i.test(body);
              const ok = !challenge && (uniLen > 2000 || videos > 0 || (hasProfile && body.length > 200));
              return {
                ok, reason: ok ? 'content' : (challenge ? 'challenge' : 'skeleton'),
                uniLen, videos, hasProfile, challenge, bodyLen: body.length, href
              };
            }"""
        )
        if _diag_softblock_enabled():
            LOGGER.warning("[DIAG] profile_usable=%s", info)
        return bool(isinstance(info, dict) and info.get("ok"))
    except Exception as exc:
        LOGGER.warning("[DIAG] profile_usable probe failed: %s", str(exc)[:160])
        return False


def _ensure_on_tiktok_home(page) -> None:
    """Revient sur la homepage si on n'y est plus (contexte conserve)."""
    try:
        current = (page.url or "").lower()
    except Exception:
        current = ""
    if current.startswith("https://www.tiktok.com/") and "/@" not in current.split("?", 1)[0]:
        if "/search" not in current:
            return
    _apply_chrome_profile_nav_headers(page, referer="https://www.tiktok.com/")
    resp, exc, status = goto_catch_http(
        page,
        "https://www.tiktok.com/",
        timeout=nav_timeout_ms(),
        wait_until="domcontentloaded",
    )
    if exc is not None and not is_http_nav_failure(exc, status):
        LOGGER.debug("Homepage re-nav soft failure: %s", exc)


def _navigate_profile_via_inpage_click(page, profile_url: str, username: str) -> bool:
    """Clic sur lien interne depuis la homepage — Referer same-origin naturel.

    Succes UNIQUEMENT si la page est utilisable (hydration/profil), pas juste l'URL.
    """
    LOGGER.info(
        "Profile CDN blocked — trying in-page link navigation (referrer bypass)",
        extra={"url": profile_url},
    )
    _ensure_on_tiktok_home(page)
    _dismiss_cookie_banner(page)
    _apply_chrome_profile_nav_headers(page, referer="https://www.tiktok.com/")
    try:
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=nav_timeout_ms(),
        ):
            page.evaluate(
                """(url) => {
                  const a = document.createElement('a');
                  a.href = url;
                  a.rel = 'noopener';
                  a.style.display = 'none';
                  document.body.appendChild(a);
                  a.click();
                }""",
                profile_url,
            )
    except Exception:
        LOGGER.debug("In-page click navigation failed; trying location.assign", exc_info=True)
        try:
            page.evaluate("(url) => { window.location.assign(url); }", profile_url)
        except Exception:
            return False
    _wait_profile_navigation_settle(page)
    _diag_log_hydration(page, "profile_after_inpage")
    if _profile_page_usable(page, username):
        return True
    if _profile_page_landed(page, username):
        LOGGER.warning(
            "In-page reached @url but profile unusable (skeleton/soft-block) — will try search UI",
            extra={"url": profile_url},
        )
    return False


def _navigate_profile_via_search(page, profile_url: str, username: str) -> bool:
    """Passe par /search/user?q=… puis clic sur le resultat profil."""
    handle = (username or "").lower().lstrip("@")
    if not handle:
        return False
    LOGGER.info(
        "Profile CDN blocked — trying TikTok search UI navigation",
        extra={"url": profile_url},
    )
    _ensure_on_tiktok_home(page)
    _apply_chrome_profile_nav_headers(page, referer="https://www.tiktok.com/")
    search_url = f"https://www.tiktok.com/search/user?q={handle}"
    _resp, exc, status = goto_catch_http(
        page,
        search_url,
        timeout=nav_timeout_ms(),
        wait_until="domcontentloaded",
    )
    if exc is not None and is_http_nav_failure(exc, status) and is_http_blocked_status(status):
        LOGGER.warning("Search page also HTTP-blocked (%s)", status)
        return False
    _dismiss_cookie_banner(page)
    _human_pause(0.8, 0.4)
    selectors = [
        f'a[href="/@{handle}"]',
        f'a[href*="/@{handle}?"]',
        f'a[href*="/@{handle}"]',
        f'[data-e2e="search-user-container"] a[href*="/@{handle}"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            _apply_chrome_profile_nav_headers(
                page,
                referer=search_url,
            )
            with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=nav_timeout_ms(),
            ):
                loc.first.click(timeout=5000)
            _wait_profile_navigation_settle(page)
            _diag_log_hydration(page, "profile_after_search_click")
            if _profile_page_usable(page, handle):
                return True
        except Exception:
            LOGGER.debug("Search result click failed for %s", sel, exc_info=True)
    # Dernier recours: goto profil avec Referer = page search.
    _apply_chrome_profile_nav_headers(page, referer=search_url)
    _resp, exc, status = goto_catch_http(
        page,
        profile_url,
        timeout=nav_timeout_ms(),
        wait_until="domcontentloaded",
    )
    _wait_profile_navigation_settle(page)
    _diag_log_hydration(page, "profile_after_search_goto")
    if is_http_nav_failure(exc, status) and not _profile_page_usable(page, handle):
        return False
    return _profile_page_usable(page, handle)


def _navigate_to_profile_resilient(
    page,
    profile_url: str,
    sticky_identity: str | None = None,
    ssr_posts_out: list | None = None,
) -> bool:
    """Goto profil resilient. Retourne True si page usable / SSR OK.

    Fast-fail (<1s) si ERR_HTTP_RESPONSE_CODE_FAILURE / 403 / chrome-error:
      → PAS de settle / diag / in-page / search
      → raise_proxy_blocked pour rotation immediate

    Si goto1 livre deja __UNIVERSAL_DATA_FOR_REHYDRATION__ (>50k):
      → extraire et return True immediatement
    """
    target = _normalize_profile_url(profile_url)
    username = _username_from_url(target)
    timeout = nav_timeout_ms()

    def _accept_ssr_goto(phase: str) -> bool:
        """True seulement si posts SSR deja presents (fast-path).

        Gros SSR (uniLen>50k) SANS posts = squelette profil (pas SUCCESS) —
        on laisse le caller attendre item_list / scroll.
        """
        uni_len, ssr_posts = _read_universal_ssr_posts(page, target)
        if ssr_posts:
            LOGGER.info(
                "Profile %s SSR posts OK (universalLen=%s, posts=%s) — fast-path",
                phase,
                uni_len,
                len(ssr_posts),
                extra={"url": target},
            )
            if ssr_posts_out is not None:
                ssr_posts_out.extend(ssr_posts)
            return True
        if uni_len > 50000:
            LOGGER.info(
                "Profile %s SSR skeleton (universalLen=%s, posts=0) — "
                "NOT success yet; wait item_list/DOM",
                phase,
                uni_len,
                extra={"url": target},
            )
        return False

    def _raise_cdn_fast(exc, status) -> None:
        """Invalide IP + raise immédiatement (<1s, sans settle)."""
        reason = (
            f"cdn_http_block status={status} "
            f"{(str(exc).splitlines()[0][:120] if exc else '')}"
        ).strip()
        LOGGER.warning(
            "[ANTIBOT] Hard CDN/HTTP failure — fail-fast rotate <1s (no settle/in-page): %s",
            reason[:160],
            extra={"url": target},
        )
        if sticky_identity:
            purge_sticky_after_failure(
                sticky_identity, reason=reason[:160], mark_waf=True
            )
        if is_antibot_http_status(status):
            raise_antibot_blocked(target, int(status), reason[:160])
        if exc is not None and "err_http_response_code_failure" in str(exc).lower():
            raise_proxy_blocked(target, reason[:160], kind="http_block")
        kind = "http_403" if (status == 403 or (status is None and exc)) else "http_block"
        if status == 429:
            kind = "http_429"
        elif status == 404:
            kind = "http_404"
        elif status == 503:
            kind = "http_503"
        raise_proxy_blocked(target, reason[:160], kind=kind)

    _apply_chrome_profile_nav_headers(page, referer="https://www.tiktok.com/")

    # --- Tentative unique: goto direct (pas de fallback UI lent) ---
    LOGGER.info("Profile nav attempt 1: goto domcontentloaded", extra={"url": target})
    _resp, exc, status = goto_catch_http(
        page, target, timeout=timeout, wait_until="domcontentloaded"
    )

    # Fast-fail IMMEDIAT sur ERR_HTTP / 403/404/429/503 / chrome-error — avant settle/diag.
    if is_antibot_http_status(status) or is_http_nav_failure(exc, status) or _is_hard_cdn_nav_failure(
        page, exc=exc, status=status
    ):
        _raise_cdn_fast(exc, status)

    _wait_profile_navigation_settle(page, timeout_ms=min(3000, timeout))
    _diag_log_profile_http(_resp, target, exc=exc)
    _diag_log_cookies(page, "profile_after_goto1")
    _diag_log_hydration(page, "profile_after_goto1")

    # Extraction prioritaire immediate (AVANT soft-block check).
    if _accept_ssr_goto("goto1"):
        return True

    # Hard CDN / HTTP failure detecte apres settle (chrome-error tardif).
    if _is_hard_cdn_nav_failure(page, exc=exc, status=status):
        _raise_cdn_fast(exc, status)

    silent_sb = _response_is_silent_soft_block(_resp, page)
    cl = _response_content_length(_resp)
    if silent_sb:
        # Silent soft-block (HTML minuscule) = aussi fail-fast, pas de search UI.
        LOGGER.warning(
            "Silent soft-block (content-length=%s) — fail-fast rotate (no in-page/search)",
            cl,
            extra={"url": target},
        )
        if sticky_identity:
            purge_sticky_after_failure(
                sticky_identity,
                reason=f"silent_soft_block cl={cl}",
                mark_waf=True,
            )
        raise_proxy_blocked(
            target,
            f"silent_soft_block content-length={cl}",
            kind="http_403",
        )

    if not is_http_nav_failure(exc, status) and _profile_page_usable(page, username):
        LOGGER.info("Profile goto OK (domcontentloaded + usable)", extra={"url": target})
        return True

    if exc is not None and not is_http_nav_failure(exc, status):
        raise_proxy_blocked(
            target,
            f"{str(exc).splitlines()[0][:160]}",
            kind="blocked",
        )

    # SSR encore present sans posts → OK (caller attendra item_list court).
    if _probe_universal_len(page) > 50000:
        LOGGER.info(
            "SSR still present (universalLen>50k) — treat nav as OK",
            extra={"url": target},
        )
        return True

    LOGGER.warning(
        "[ANTIBOT] Profile navigation failed after warmup — rotate (no UI fallback)",
        extra={"url": target},
    )
    raise_proxy_blocked(
        target,
        f"profile_nav_fail content-length={cl} status={status}",
        kind="http_403",
    )
    return False  # unreachable


def _read_universal_ssr_posts(page, profile_url: str = "") -> tuple[int, list[dict]]:
    """Lit #__UNIVERSAL_DATA_FOR_REHYDRATION__ et extrait les posts SSR in-browser.

    Retourne (universalLen, posts). Parse dans le navigateur pour eviter
    le transfert CDP fragile du payload ~255KB.
    """
    try:
        return extract_posts_from_hydration(page, profile_url=profile_url)
    except Exception:
        LOGGER.warning("SSR in-browser extract failed", exc_info=True)
        return 0, []


def _warmup_and_open_profile(
    page,
    profile_url: str,
    sticky_identity: str | None = None,
    network_posts: list | None = None,
    ssr_posts_out: list | None = None,
    *,
    skip_homepage_warmup: bool = False,
) -> bool:
    """Warmup homepage (optionnel) puis profil dans le meme contexte.

    Si `skip_homepage_warmup=True` (cookies sticky valides reinjectes):
      → PAS de goto https://www.tiktok.com/ (evite TLS timeout 15s inutile)
      → goto direct @profil

    Sinon:
      1) warmup homepage + persist cookies sticky
      2) goto @username
    """
    if _env_bool("ENABLE_MOBILE_FALLBACK", False):
        LOGGER.warning("ENABLE_MOBILE_FALLBACK ignored — m.tiktok.com disabled")

    target = _normalize_profile_url(profile_url)

    if skip_homepage_warmup:
        LOGGER.info(
            "Skipping homepage warmup — sticky cookies already injected from prior SUCCESS",
            extra={"url": target},
        )
        # Buffer propre avant profil (pas de bruit For You a vider).
        if network_posts is not None:
            network_posts.clear()
    else:
        # 1) Chauffe homepage + cookies WAF (obligatoire en VIRGIN / nouvelle IP).
        warmup_session(page, sticky_identity=sticky_identity)

        # 2) Purger UNIQUEMENT le bruit For You de la homepage.
        if network_posts is not None:
            network_posts.clear()

        LOGGER.info(
            "Warmup OK — navigating to target profile in warm context (resilient)",
            extra={"url": target},
        )

    # 3) Profil cible — SSR / API prioritaire des le 1er goto.
    local_ssr: list[dict] = ssr_posts_out if ssr_posts_out is not None else []
    profile_ok = _navigate_to_profile_resilient(
        page,
        target,
        sticky_identity=sticky_identity,
        ssr_posts_out=local_ssr,
    )

    # Priorite ABSOLUE: posts deja capturés par l'intercepteur pendant la nav.
    if network_posts:
        LOGGER.info(
            "Network buffer already has %d posts after profile nav — SUCCESS "
            "(skip SSR wait/scroll)",
            len(network_posts),
            extra={"url": target},
        )
        if local_ssr and ssr_posts_out is not None and local_ssr is not ssr_posts_out:
            ssr_posts_out.extend(local_ssr)
        return True

    # Posts deja extraits au goto1 → SUCCESS immediat, zero scroll/DOM.
    if local_ssr:
        LOGGER.info(
            "SSR hydration fast-path SUCCESS from goto: posts=%s — "
            "skip DOM /video/ wait and item_list scroll",
            len(local_ssr),
            extra={"url": target},
        )
        if ssr_posts_out is not None and local_ssr is not ssr_posts_out:
            ssr_posts_out.extend(local_ssr)
        return True

    _diag_log_cookies(page, "profile_landed")
    _diag_log_hydration(page, "profile_landed")

    # Re-check buffer avant parse SSR / wait (race: XHR peut arriver ici).
    if network_posts:
        LOGGER.info(
            "Network buffer filled during settle (%d posts) — SUCCESS",
            len(network_posts),
            extra={"url": target},
        )
        return True

    uni_len, ssr_posts = _read_universal_ssr_posts(page, target)
    usable = profile_ok or _profile_page_usable(page, _username_from_url(target))

    if ssr_posts:
        LOGGER.info(
            "SSR hydration fast-path SUCCESS: universalLen=%s posts=%s "
            "— skip DOM /video/ wait and item_list scroll",
            uni_len,
            len(ssr_posts),
            extra={"url": target},
        )
        if ssr_posts_out is not None:
            ssr_posts_out.extend(ssr_posts)
        return True

    # SSR posts=0 est NORMAL sur TikTok (grille via XHR). Attendre item_list
    # seulement si le buffer est encore vide.
    if uni_len > 50000 or usable:
        if network_posts:
            LOGGER.info(
                "Network buffer has %d posts before wait — SUCCESS",
                len(network_posts),
                extra={"url": target},
            )
            return True
        wait_ms = _grid_wait_ms()
        LOGGER.info(
            "SSR/usable OK but posts=0 (universalLen=%s) — waiting %sms for item_list "
            "(NOT a WAF / soft-block)",
            uni_len,
            wait_ms,
            extra={"url": target},
        )
        # Pas de passive settle 4s — enchaine direct sur poll API court.
        try:
            page.mouse.wheel(0, random.randint(200, 400))
        except Exception:
            pass
        grid_ok = _wait_for_profile_grid_data(
            page, network_posts, timeout_ms=wait_ms
        )
        if network_posts:
            LOGGER.info(
                "Grid API captured %d posts after empty-SSR wait — SUCCESS "
                "(buffer preserved for caller)",
                len(network_posts),
                extra={"url": target},
            )
            return True
        # Pas de posts API non plus: page OK, laisser le caller scroller 1–2x.
        # Ne PAS purger / mark_waf (profil vide HTTP 200 ≠ WAF).
        return bool(grid_ok or uni_len > 50000 or usable)

    # Vrai soft-block seulement si captcha / chrome-error / HTTP block.
    if sticky_identity and _is_real_waf_block(page, error="profile_not_usable_after_nav"):
        LOGGER.warning(
            "Real WAF/challenge after nav — purge sticky",
            extra={"url": target},
        )
        purge_sticky_after_failure(
            sticky_identity,
            reason="profile_not_usable_after_nav",
            mark_waf=True,
        )
    else:
        LOGGER.warning(
            "Profile not usable after nav — skip scroll (no WAF mark: empty≠block)",
            extra={"url": target},
        )
    return False


def scrape_tiktok_page(
    url: str,
    max_posts: int = 20,
    max_age_hours: int | None = None,
    on_post=None,
    headless_override: bool | None = None,
    analyze_video_content: bool | None = None,
    proxy_override: dict | None = None,
) -> dict:
    """Point d'entree public: scrape une page TikTok et retourne un resultat.

    Orchestration:
    - normalise l'URL
    - decide headless/headed
    - prepare les candidats proxy
    - tente le scraping sur chaque candidat jusqu'au succes

    `proxy_override`: si fourni, on saute completement `_load_proxy_candidates()`
    et on scrape uniquement avec CE proxy (pas de rotation/fallback). Utilise
    par `worker.py` quand un batch de pages est deja reparti sur des "lanes"
    ayant chacune un proxy dedie (voir `get_proxy_pool`) - dans ce cas la
    rotation sequentielle habituelle n'a pas de sens, la page a deja sa propre
    IP assignee pour toute sa lane.
    """
    profile_url = _normalize_profile_url(url)
    scoped_logger = with_context(LOGGER, url=profile_url)
    if headless_override is None:
        headless = _env_bool("TIKTOK_HEADLESS", True)
    else:
        headless = bool(headless_override)
    if analyze_video_content is None:
        analyze_video_content = _env_bool("TIKTOK_ANALYZE_VIDEO_CONTENT", False)
    slow_mo_ms = 0 if headless else 150
    # Mode rotate / pool / force-direct.
    if _force_direct_mode():
        scoped_logger.warning(
            "TIKTOK_FORCE_DIRECT=true — local IP only (proxy=None, no sticky purge/rotation)"
        )
        proxy_candidates = [None]
        attempts_per_proxy = 1
        rotate_every_n_posts = 0
    else:
        default_attempts = 1 if _rotating_proxy_mode() else 2
        attempts_per_proxy = max(1, _env_int("TIKTOK_PROXY_ATTEMPTS_PER_PROXY", default_attempts))
        if proxy_override is not None:
            if _rotating_proxy_mode():
                n = max(1, _env_int("MAX_PROXY_RETRIES", 0) or _env_int("TIKTOK_PROXY_MAX_PER_JOB", 5))
                # Chaque tentative = nouvelle sticky session (nouvelle IP, geo cible).
                proxy_candidates = [
                    _assign_webshare_sticky_session(dict(proxy_override)) for _ in range(n)
                ]
                attempts_per_proxy = 1
            else:
                proxy_candidates = [proxy_override]
        else:
            proxy_candidates = _select_proxy_candidates()
        rotate_every_n_posts = _env_int("TIKTOK_IPV6_ROTATE_EVERY_N_POSTS", 0)

    with sync_playwright() as p:
        last_result = None
        tried = 0
        # Apres echec: TOUJOURS warmup homepage sur la nouvelle IP (cookies frais).
        # Ne jamais skipper le warmup en VIRGIN (cause WAF content-length~700).
        for proxy_cfg in proxy_candidates:
            # Avant d'essayer: verifie que ce proxy n'a pas ete blackliste entre
            # temps (autre job / lane parallele), sinon on saute.
            if proxy_cfg is not None and _is_blacklisted(proxy_cfg):
                scoped_logger.info(
                    "Skipping already-blacklisted proxy",
                    extra={"url": _describe_proxy(proxy_cfg)},
                )
                continue

            for try_index in range(1, attempts_per_proxy + 1):
                tried += 1
                scoped_logger.info(
                    "Scrape attempt started",
                    extra={"post_id": None, "url": f"{_describe_proxy(proxy_cfg)} try={try_index}/{attempts_per_proxy}"},
                )
                result = _scrape_with_browser(
                    p,
                    profile_url,
                    max_posts,
                    max_age_hours,
                    on_post,
                    headless,
                    slow_mo_ms,
                    proxy_cfg,
                    analyze_video_content,
                    rotate_every_n_posts=rotate_every_n_posts,
                    skip_homepage_warmup=False,
                )
                result = _finalize_classified_result(
                    result,
                    country=_sticky_country_from_proxy(proxy_cfg),
                    identity=_proxy_identity(proxy_cfg) if proxy_cfg else "",
                    record=True,
                )
                last_result = result
                error_code = str(result.get("error") or "").strip().lower()
                scrape_class = str(result.get("classification") or "").strip().lower()
                retryable_soft = _is_retryable_soft_error(error_code)

                # Auth Webshare: abort IMMEDIAT — ne pas boucler sur la meme credential.
                if _is_fatal_proxy_auth_error(error_code) or scrape_class == "auth":
                    scoped_logger.warning(
                        "[PROXY] Webshare AUTH/CONNECTION FAILURE — aborting further IP retries "
                        "(fix PROXY_URL / username / password / residential host)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                    return result

                if result.get("posts") or not retryable_soft:
                    if result.get("error"):
                        scoped_logger.warning(
                            "Scrape finished with error",
                            extra={"has_partial_posts": bool(result.get("posts"))},
                        )
                    else:
                        scoped_logger.info("Scrape finished successfully")
                    return result

                # Fail-fast antibot / TLS timeout / auth: purge + blacklist 24h + rotate.
                rotate_now = any(
                    tok in error_code
                    for tok in (
                        "no_posts_found",
                        "empty_feed_or_softblock",
                        "challenge_detected",
                        "proxy_blocked",
                        "tls_timeout",
                        "invalid_auth",
                        "err_invalid_auth_credentials",
                        "err_http_response_code_failure",
                        "err_timed_out",
                        "timeout",
                        "http_block",
                        "http_403",
                        "http_429",
                    )
                ) or scrape_class in {
                    "soft_block",
                    "structural_change",
                    "platform_change_suspected",
                    "hard_block",
                    "attempt_timeout",
                    "proxy_infra",
                    "rate_limited",
                }
                if rotate_now:
                    scoped_logger.info(
                        "Next attempt: new IP + VIRGIN cookies + homepage warmup",
                        extra={"error": error_code[:120]},
                    )

                if try_index < attempts_per_proxy and not rotate_now:
                    if proxy_cfg is not None and sticky_sessions.sticky_enabled():
                        if "invalid_auth" in error_code or "err_invalid_auth_credentials" in error_code:
                            sticky_sessions.invalidate_sticky_cookies(_proxy_identity(proxy_cfg))
                    scoped_logger.warning(
                        "Soft failure on proxy; retrying same proxy",
                        extra={"error": error_code[:120], "url": _describe_proxy(proxy_cfg)},
                    )
                    continue

                # Distinguer attempt_timeout / TLS-NAV / HTTP block (pas le meme signal).
                if scrape_class == "attempt_timeout" or "attempt_timeout" in error_code:
                    scoped_logger.warning(
                        "[PERF] Rotating after ATTEMPT BUDGET TIMEOUT (not TLS/NAV)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                elif scrape_class == "structural_change" or scrape_class == "platform_change_suspected":
                    scoped_logger.warning(
                        "[CLASSIFIER] Rotating after %s — do not quarantine identity as proxy WAF",
                        scrape_class,
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                elif scrape_class == "soft_block" or "empty_feed" in error_code or "no_posts_found" in error_code:
                    scoped_logger.warning(
                        "[CLASSIFIER] Rotating after soft empty feed (HTTP 200, 0 posts)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                elif "tls_timeout" in error_code or (
                    "timeout" in error_code
                    and "http_" not in error_code
                    and "attempt_timeout" not in error_code
                ):
                    scoped_logger.warning(
                        "[ANTIBOT] Rotating after TLS/NAV TIMEOUT (blacklist 24h)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                elif "http_403" in error_code or "http 403" in error_code:
                    scoped_logger.warning(
                        "[ANTIBOT] Rotating after HTTP 403 BLOCK (blacklist 24h)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )
                elif "http_" in error_code or "err_http_response" in error_code:
                    scoped_logger.warning(
                        "[ANTIBOT] Rotating after HTTP BLOCK (blacklist 24h)",
                        extra={"error": error_code[:160], "url": _describe_proxy(proxy_cfg)},
                    )

                # Purge cookies sticky apres echec reseau → VIRGIN.
                # no_posts_found seul ≠ WAF (profil vide HTTP 200).
                if proxy_cfg is not None and sticky_sessions.sticky_enabled():
                    identity = _proxy_identity(proxy_cfg)
                    real_waf_tokens = (
                        "proxy_blocked",
                        "tls_timeout",
                        "err_http_response_code_failure",
                        "err_tunnel_connection_failed",
                        "err_invalid_auth_credentials",
                        "invalid_auth",
                        "challenge_detected",
                        "http_403",
                        "http_429",
                        "http_block",
                        "403",
                        "429",
                    )
                    soft_purge_tokens = (
                        "err_timed_out",
                        "timeout",
                        "503",
                        "no_posts_found",
                        "empty_feed_or_softblock",
                    )
                    # structural_change / platform_change: ne pas mark_waf
                    # (le probleme n'est pas l'identite proxy).
                    is_structural = scrape_class in {
                        "structural_change",
                        "platform_change_suspected",
                    }
                    is_real_waf = (
                        any(tok in error_code for tok in real_waf_tokens)
                        and not is_structural
                        and scrape_class not in ("soft_block", "attempt_timeout")
                    )
                    is_soft = any(tok in error_code for tok in soft_purge_tokens) or scrape_class in {
                        "soft_block",
                        "structural_change",
                        "platform_change_suspected",
                        "attempt_timeout",
                    }
                    if _env_bool("PURGE_STICKY_ON_BLOCK", True) and (is_real_waf or is_soft):
                        sticky_sessions.purge_sticky_cookies(
                            identity,
                            reason=error_code[:160],
                            mark_waf=is_real_waf,
                        )

                # Blacklist 24h pour timeout et HTTP block.
                # Mode rotate: ne PAS blacklister l'identite `-rotate` (meme endpoint,
                # nouvelle IP a chaque session) — on purge sticky et on continue.
                if (
                    _should_blacklist_error(error_code)
                    and not _rotating_proxy_mode()
                ):
                    hours = _blacklist_hours_for_reason(error_code)
                    _blacklist_proxy(proxy_cfg, error_code, hours=hours)
                elif _rotating_proxy_mode() and (
                    "tls_timeout" in error_code
                    or "timeout" in error_code
                    or "http_" in error_code
                    or "err_http" in error_code
                    or "proxy_blocked" in error_code
                ):
                    scoped_logger.info(
                        "Rotate mode: skip blacklist after CDN/HTTP/TLS fail — next session = new IP",
                        extra={"error": error_code[:120]},
                    )
                scoped_logger.warning(
                    "Soft failure detected, rotating proxy candidate (next IP = virgin context)",
                    extra={"error": error_code[:160]},
                )
                break

        # Aucun proxy n'a donne de posts: message clair pour l'API utilisateur.
        if _rotating_proxy_mode():
            friendly = (
                "TikTok a bloque les tentatives via l'endpoint rotatif Webshare "
                "(0 video recuperee). Reessaie: chaque session obtient une nouvelle IP."
            )
        else:
            friendly = (
                "TikTok a bloque les proxies testes (0 video recuperee). "
                "Les proxies en echec sont mis en pause 24h. "
                "Reessaie dans quelques minutes avec d'autres IP du pool."
            )
        if last_result is None:
            return {
                "posts": [],
                "total": 0,
                "error": friendly,
                "error_code": "all_proxies_blocked",
                "url": profile_url,
            }
        last_result["error_code"] = str(last_result.get("error") or "all_proxies_blocked")
        last_result["error"] = friendly
        return last_result
