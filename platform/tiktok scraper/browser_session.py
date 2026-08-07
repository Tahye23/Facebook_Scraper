"""Helpers navigation Playwright: proxy auth, fail-fast HTTP/TLS, stealth webdriver."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="browser_session")

# 403/429/503 antibot + 404 (souvent soft-block / profil mobile) → rotate IP.
_BLOCKED_HTTP_STATUSES = frozenset({403, 404, 405, 429, 500, 502, 503, 400, 401, 451})
_ANTIBOT_HTTP_STATUSES = frozenset({403, 404, 429, 503})


class ProxyBlockedException(Exception):
    """HTTP antibot / TLS timeout / auth proxy — IP compromisee, fail-fast.

    Attributes:
        kind: "http_403" | "http_429" | "tls_timeout" | "auth" | "http_block" | ...
    """

    def __init__(self, message: str, *, kind: str = "blocked") -> None:
        super().__init__(message)
        self.kind = kind


class AntibotBlockedException(ProxyBlockedException):
    """HTTP 403/404/429/503 — change d'IP immédiatement (pas de fallback mobile)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        kind = f"http_{status}" if status is not None else "antibot_blocked"
        super().__init__(message, kind=kind)
        self.status = status


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def nav_timeout_ms() -> int:
    """Timeout page.goto (PAGE_GOTO_TIMEOUT ou TIKTOK_PAGE_GOTO_TIMEOUT_MS, max 15s)."""
    raw = _env_int("PAGE_GOTO_TIMEOUT", 0) or _env_int("TIKTOK_PAGE_GOTO_TIMEOUT_MS", 15000)
    return max(3000, min(15000, int(raw)))


def force_www_tiktok_url(url: str) -> str:
    """Remplace toute occurrence de m.tiktok.com par www.tiktok.com."""
    raw = (url or "").strip()
    if not raw:
        return raw
    if "m.tiktok.com" in raw.lower():
        return re.sub(r"(?i)m\.tiktok\.com", "www.tiktok.com", raw)
    return raw


def parse_playwright_proxy(raw: str | dict | None = None) -> dict | None:
    """Extrait server / username / password séparés pour Playwright.

    Sources (dans l'ordre si raw est None):
      PROXY_URL, TIKTOK_PROXY_URL, TIKTOK_PROXY_SERVER (+ USERNAME/PASSWORD)

    Format Playwright OBLIGATOIRE (Chromium refuse socks5+auth):
      {"server": "http://host:PORT", "username": "...", "password": "..."}
    Les credentials ne doivent JAMAIS rester dans `server`.
    """
    if isinstance(raw, dict):
        return sanitize_playwright_proxy(raw)

    url = (raw or "").strip()
    if not url:
        url = (
            os.getenv("PROXY_URL")
            or os.getenv("TIKTOK_PROXY_URL")
            or os.getenv("TIKTOK_PROXY_SERVER")
            or ""
        ).strip()
    if not url:
        username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
        password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
        host = (os.getenv("TIKTOK_PROXY_HOST") or "").strip()
        if not host or not username:
            return None
        try:
            port = int((os.getenv("TIKTOK_PROXY_PORT") or "80").strip() or "80")
        except ValueError:
            port = 80
        return sanitize_playwright_proxy(
            {"server": f"http://{host}:{port}", "username": username, "password": password}
        )

    # Format Webshare brut host:port:user:pass
    if "://" not in url and "|" not in url:
        parts = url.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = (p.strip() for p in parts)
            return sanitize_playwright_proxy(
                {
                    "server": f"http://{host}:{port}",
                    "username": username,
                    "password": password,
                }
            )

    if "|" in url:
        parts = [p.strip() for p in url.split("|")]
        server = parts[0] if parts else ""
        username = parts[1] if len(parts) > 1 else ""
        password = parts[2] if len(parts) > 2 else ""
        return sanitize_playwright_proxy(
            {"server": server, "username": username, "password": password}
        )

    try:
        parsed = urlsplit(url if "://" in url else f"http://{url}")
    except Exception:
        LOGGER.warning("Failed to parse proxy URL", extra={"url": url[:80]})
        return None
    if not parsed.hostname:
        return None

    username = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password is not None else ""
    if not username:
        username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
    if not password:
        password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""

    port = parsed.port
    if port is None:
        try:
            port = int((os.getenv("TIKTOK_PROXY_PORT") or "80").strip() or "80")
        except ValueError:
            port = 80

    return sanitize_playwright_proxy(
        {
            "server": f"http://{parsed.hostname}:{port}",
            "username": username,
            "password": password,
            "_scheme": parsed.scheme or "http",
        }
    )


def sanitize_playwright_proxy(proxy_cfg: dict | None) -> dict | None:
    """Normalise un dict proxy Playwright: server HTTP sans credentials inline.

    - socks5:// → http://host:80 (Chromium: no SOCKS5 auth)
    - http://user:pass@host:port → server=http://host:port + username/password
    """
    if not proxy_cfg:
        return None
    server = str(proxy_cfg.get("server") or "").strip()
    username = str(proxy_cfg.get("username") or "").strip()
    password = proxy_cfg.get("password")
    if password is None:
        password = ""
    else:
        password = str(password)
    scheme_hint = str(proxy_cfg.get("_scheme") or "").strip().lower()

    if not server:
        return None

    # Credentials éventuels encore collés dans server.
    try:
        parsed = urlsplit(server if "://" in server else f"http://{server}")
    except Exception:
        parsed = None

    if parsed is not None and parsed.hostname:
        if parsed.username and not username:
            username = unquote(parsed.username)
        if parsed.password is not None and not password:
            password = unquote(parsed.password)
        host = parsed.hostname
        port = parsed.port
        scheme = (parsed.scheme or scheme_hint or "http").lower()
        if scheme.startswith("socks") or (port in (1080, 1081) and username):
            LOGGER.warning(
                "SOCKS5 proxy auth unsupported by Chromium — forcing HTTP :80",
                extra={"url": f"http://{host}:80"},
            )
            port = 80
            scheme = "http"
        if port is None:
            port = 80
        server = f"http://{host}:{port}"
    elif not server.startswith("http://") and not server.startswith("https://"):
        server = f"http://{server}"

    out: dict[str, str] = {"server": server}
    if username:
        out["username"] = username
        out["password"] = password
    else:
        LOGGER.warning(
            "Playwright proxy has no username — Webshare auth will fail "
            "(set PROXY_URL=http://user:pass@host:port or TIKTOK_PROXY_USERNAME)"
        )
    return out


def is_http_blocked_status(status: int | None) -> bool:
    if status is None:
        return False
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    if code in _BLOCKED_HTTP_STATUSES:
        return True
    return code >= 400


def is_antibot_http_status(status: int | None) -> bool:
    """True pour 403 / 404 / 429 / 503 → ANTIBOT_BLOCKED + rotate IP."""
    if status is None:
        return False
    try:
        return int(status) in _ANTIBOT_HTTP_STATUSES
    except (TypeError, ValueError):
        return False


def is_timeout_nav_error(exc: BaseException | str) -> bool:
    """Timeout Playwright / TLS / navigation (traite comme blocage IP)."""
    err = str(exc or "").lower()
    return any(
        tok in err
        for tok in (
            "timeout",
            "timed_out",
            "err_timed_out",
            "err_connection_timed_out",
            "err_ssl",
            "err_ssl_protocol_error",
            "err_ssl_version_or_cipher_mismatch",
            "err_cert",
            "err_tunnel_connection_failed",
            "net::err_connection_closed",
            "navigation timeout",
            "exceeded",
        )
    )


def is_proxy_auth_error(exc: BaseException | str | None) -> bool:
    """ERR_PROXY_CONNECTION_FAILED / auth invalide — credentials ou endpoint."""
    err = str(exc or "").lower()
    return any(
        tok in err
        for tok in (
            "err_proxy_connection_failed",
            "err_invalid_auth_credentials",
            "err_tunnel_connection_failed",
            "invalid_auth",
            "proxy authentication required",
            "407",
        )
    )


def is_blocked_nav_error(exc: BaseException | str) -> bool:
    err = str(exc or "").lower()
    return any(
        tok in err
        for tok in (
            "err_http_response_code_failure",
            "err_invalid_auth_credentials",
            "err_tunnel_connection_failed",
            "err_proxy_connection_failed",
        )
    )


def is_http_nav_failure(exc: BaseException | str | None, status: int | None = None) -> bool:
    """True pour 403/429/ERR_HTTP_RESPONSE_CODE_FAILURE (CDN antibot), pas TLS/auth."""
    if is_http_blocked_status(status):
        return True
    if exc is None:
        return False
    err = str(exc or "").lower()
    if "err_http_response_code_failure" in err:
        return True
    if is_proxy_auth_error(exc):
        return False
    code = status_from_nav_exception(exc) if not isinstance(exc, str) else status_from_nav_exception(Exception(exc))
    return is_http_blocked_status(code)


def goto_catch_http(
    page: Any,
    target_url: str,
    *,
    timeout: int | None = None,
    wait_until: str = "domcontentloaded",
) -> tuple[Any | None, BaseException | None, int | None]:
    """page.goto qui N'eleve PAS sur HTTP block — retourne (response, exc, status).

    Auth / ERR_PROXY_CONNECTION_FAILED → raise immédiat (pas de boucle IP).
    403/404/429/503 → retourne status pour que le caller lève ANTIBOT_BLOCKED.
    """
    nav_timeout = timeout if timeout is not None else nav_timeout_ms()
    url = force_www_tiktok_url((target_url or "").strip())

    try:
        response = page.goto(url, wait_until=wait_until, timeout=nav_timeout)
    except Exception as exc:
        err = str(exc or "").lower()
        if is_proxy_auth_error(exc):
            LOGGER.warning(
                "[PROXY] Webshare AUTH/CONNECTION FAILURE — abort retry for this IP "
                "(check PROXY_URL server+username+password): %s",
                str(exc).splitlines()[0][:160],
                extra={"url": url},
            )
            raise_proxy_blocked(url, str(exc).splitlines()[0][:160], kind="auth")
        # HTTP CDN block: retourner pour fail-fast caller.
        if is_http_nav_failure(exc):
            return None, exc, status_from_nav_exception(exc)
        if is_timeout_nav_error(exc) and "err_http_response_code_failure" not in err:
            raise_proxy_blocked(url, str(exc).splitlines()[0][:160], kind="tls_timeout")
        return None, exc, status_from_nav_exception(exc)

    status = getattr(response, "status", None) if response is not None else None
    if is_antibot_http_status(status):
        return response, None, int(status)
    if is_http_blocked_status(status):
        return response, None, int(status) if status is not None else None
    return response, None, int(status) if status is not None else None


def status_from_nav_exception(exc: BaseException) -> int | None:
    match = re.search(r"\b([45]\d{2})\b", str(exc or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def classify_block_kind(exc_or_status: BaseException | int | str | None) -> str:
    """Classe le type de blocage pour logs (TLS timeout vs HTTP 403)."""
    if isinstance(exc_or_status, int):
        return f"http_{exc_or_status}"
    text = str(exc_or_status or "").lower()
    if is_proxy_auth_error(text):
        return "auth"
    if is_timeout_nav_error(text) and "err_http_response_code_failure" not in text:
        return "tls_timeout"
    status = status_from_nav_exception(exc_or_status) if not isinstance(exc_or_status, str) else None
    if status is None and isinstance(exc_or_status, str):
        status = status_from_nav_exception(Exception(exc_or_status))
    if status is not None and status in _ANTIBOT_HTTP_STATUSES:
        return f"http_{status}"
    if status is not None:
        return f"http_{status}"
    if "err_http_response_code_failure" in text:
        return "http_block"
    return "blocked"


def raise_antibot_blocked(target_url: str, status: int | None, reason: str = "") -> None:
    """Lève AntibotBlockedException pour 403/404/429/503 → rotate IP immédiat."""
    code = int(status) if status is not None else None
    msg = (reason or f"HTTP {code}").strip()
    full = f"{msg} on {target_url}"
    LOGGER.warning(
        "[ANTIBOT_BLOCKED] HTTP %s — %s — rotate IP immediately (no m.tiktok fallback)",
        code if code is not None else "?",
        full,
    )
    raise AntibotBlockedException(full, status=code)


def raise_proxy_blocked(target_url: str, reason: str, *, kind: str = "blocked") -> None:
    msg = f"{reason} on {target_url}"
    if kind.startswith("http_") or kind == "antibot_blocked":
        status = None
        if kind.startswith("http_"):
            try:
                status = int(kind.split("_", 1)[1])
            except (IndexError, ValueError):
                status = None
        if status in _ANTIBOT_HTTP_STATUSES:
            raise_antibot_blocked(target_url, status, reason)
            return
        LOGGER.warning(
            "[ANTIBOT] HTTP BLOCK (%s) — %s — purge sticky + blacklist 24h + rotate",
            kind,
            msg,
        )
    elif kind == "tls_timeout":
        LOGGER.warning(
            "[ANTIBOT] TLS/NAV TIMEOUT — %s — purge sticky + blacklist 24h + rotate",
            msg,
        )
    elif kind == "auth":
        LOGGER.warning(
            "[PROXY] AUTH FAILURE — %s — abort retries for this credential/IP "
            "(verify PROXY_URL / TIKTOK_PROXY_USERNAME+PASSWORD)",
            msg,
        )
    else:
        LOGGER.warning("[ANTIBOT] %s (%s) — fail-fast, purge/rotate required", msg, kind)
    raise ProxyBlockedException(msg, kind=kind)


def goto_strict(
    page: Any,
    target_url: str,
    *,
    timeout: int | None = None,
    wait_until: str = "domcontentloaded",
) -> Any:
    """page.goto strict: HTTP bloque / timeout 15s / ERR_* => ProxyBlockedException."""
    nav_timeout = timeout if timeout is not None else nav_timeout_ms()
    url = force_www_tiktok_url((target_url or "").strip())

    try:
        response = page.goto(url, wait_until=wait_until, timeout=nav_timeout)
    except Exception as exc:
        kind = classify_block_kind(exc)
        if is_proxy_auth_error(exc):
            LOGGER.warning(
                "[PROXY] Webshare AUTH/CONNECTION FAILURE on goto: %s",
                str(exc).splitlines()[0][:160],
            )
            raise_proxy_blocked(url, str(exc).splitlines()[0][:160], kind="auth")
        if (
            is_timeout_nav_error(exc)
            or is_blocked_nav_error(exc)
            or is_http_blocked_status(status_from_nav_exception(exc))
        ):
            if is_timeout_nav_error(exc) and "err_http_response_code_failure" not in str(exc).lower():
                kind = "tls_timeout"
            raise_proxy_blocked(
                url,
                f"{str(exc).splitlines()[0][:160]}",
                kind=kind,
            )
        raise

    status = getattr(response, "status", None) if response is not None else None
    if is_antibot_http_status(status):
        raise_antibot_blocked(url, int(status), f"HTTP {status}")
    if is_http_blocked_status(status):
        kind = classify_block_kind(status)
        raise_proxy_blocked(url, f"HTTP {status}", kind=kind)
    return response


def install_webdriver_mask(context: Any) -> None:
    """Masque navigator.webdriver + shim chrome.app AVANT toute navigation.

    Doit etre appele juste apres creation du browserContext / persistent
    context, avant new_page() / goto().
    """
    context.add_init_script(
        """
        (() => {
          try {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          } catch (e) {}
          window.chrome = window.chrome || {};
          window.chrome.runtime = window.chrome.runtime || {};
          window.chrome.app = window.chrome.app || {
            isInstalled: false,
            InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
            getDetails: function () { return null; },
            getIsInstalled: function () { return false; },
          };
          window.chrome.csi = window.chrome.csi || function () { return {}; };
          window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
          try {
            const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (originalQuery) {
              window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                  ? Promise.resolve({ state: Notification.permission })
                  : originalQuery(parameters)
              );
            }
          } catch (e) {}
        })();
        """
    )


def browser_launch_args() -> list[str]:
    """Args Chrome anti-empreinte / proxies datacenter."""
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        "--disable-http2",
        "--ignore-certificate-errors",
        "--window-size=1365,768",
    ]


def prepare_virgin_sticky_for_next_ip(identity: str, *, force: bool = True) -> None:
    """Prepare un profil sticky VIERGE avant chaque tentative.

    Par defaut (`force=True`): TOUJOURS purger cookies.json + profil Chrome
    (Default/), meme si d'anciens cookies "valides" existent — ils sont
    souvent deja marques soft-block par TikTok.

    Si `force=False` (REUSE=true): ne conserve les cookies QUE si
    `should_reuse_sticky_cookies` (dernier statut != success=False).
    """
    try:
        import sticky_sessions
    except Exception:
        LOGGER.debug("sticky_sessions unavailable for virgin prep", exc_info=True)
        return

    if not identity or identity == "direct":
        return

    if not force and sticky_sessions.should_reuse_sticky_cookies(identity):
        LOGGER.info(
            "Keeping warmed sticky cookies (REUSE=true, last success != False)",
            extra={"url": sticky_sessions.safe_identity(identity)},
        )
        return

    if not force and sticky_sessions.has_valid_cookies(identity):
        LOGGER.info(
            "Sticky cookies present but NOT reusable (last success=False / WAF) — forcing VIRGIN",
            extra={"url": sticky_sessions.safe_identity(identity)},
        )

    sticky_sessions.reset_session_to_virgin(identity)
    LOGGER.info(
        "Sticky profile forced VIRGIN (no persistent cookies / clean Chrome profile)",
        extra={"url": sticky_sessions.safe_identity(identity)},
    )


def purge_sticky_after_failure(
    identity: str,
    reason: str = "",
    *,
    mark_waf: bool = False,
) -> None:
    """Purge cookies sticky apres echec nav (dedupe via sticky_sessions)."""
    if not identity or identity == "direct":
        return
    try:
        import sticky_sessions
    except Exception:
        LOGGER.debug("sticky_sessions unavailable for purge", exc_info=True)
        return
    try:
        sticky_sessions.purge_sticky_cookies(
            identity, reason=reason, mark_waf=mark_waf
        )
    except Exception:
        LOGGER.warning("purge_sticky_cookies failed", exc_info=True)
