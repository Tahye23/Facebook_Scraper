"""Response Classifier — classification fine des echecs scrape TikTok.

Composant independant du worker/scraper (Phase 0 anti-detection-v2).
Ne change PAS la logique de scrape : il mappe des signaux observés vers
une classe actionnable + une action recommandee.

Classes (Claude MVP):
  proxy_infra       — tunnel/502/504, TikTok non atteint
  rate_limited      — 403/429 apres page autrement normale
  soft_block        — HTTP 200, coquille SSR, 0 posts / 0 XHR
  hard_block        — captcha / challenge / chrome-error
  structural_change — SSR present mais cles attendues absentes (besoin alerte)
  platform_change_suspected — soft_block reproduit sur N IPs/pays
  attempt_timeout   — budget temps depasse (PAS un TLS/NAV)
  auth              — credentials proxy 407
  success           — posts trouves
  unknown           — non classe
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ScrapeClass(str, Enum):
    SUCCESS = "success"
    PROXY_INFRA = "proxy_infra"
    RATE_LIMITED = "rate_limited"
    SOFT_BLOCK = "soft_block"
    HARD_BLOCK = "hard_block"
    STRUCTURAL_CHANGE = "structural_change"
    PLATFORM_CHANGE_SUSPECTED = "platform_change_suspected"
    ATTEMPT_TIMEOUT = "attempt_timeout"
    AUTH = "auth"
    UNKNOWN = "unknown"


class RecommendedAction(str, Enum):
    NONE = "none"
    RETRY_SAME_IDENTITY = "retry_same_identity"
    ROTATE_IDENTITY = "rotate_identity"
    BACKOFF = "backoff"
    QUARANTINE_IDENTITY = "quarantine_identity"
    ALERT_HUMAN = "alert_human"
    PAUSE_JOB_CLASS = "pause_job_class"
    ABORT_CREDENTIAL = "abort_credential"


@dataclass
class ClassificationResult:
    scrape_class: ScrapeClass
    action: RecommendedAction
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)
    mark_waf: bool = False
    # Compat legacy error string for existing retry/blacklist code paths.
    legacy_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scrape_class"] = self.scrape_class.value
        d["action"] = self.action.value
        return d


# Signaux SSR "normaux" attendus sur un profil public.
_EXPECTED_SCOPE_KEYS = frozenset(
    {
        "webapp.user-detail",
        "webapp.user-post-list",
    }
)


def classify_attempt(
    *,
    error: str | None = None,
    http_status: int | None = None,
    posts_count: int = 0,
    ssr_universal_len: int = 0,
    ssr_has_user_info: bool | None = None,
    ssr_has_post_list: bool | None = None,
    ssr_scope_keys: list[str] | None = None,
    item_list_xhr_seen: bool | None = None,
    fetch_failed: bool | None = None,
    challenge_detected: bool = False,
    chrome_error: bool = False,
    consecutive_soft_block_countries: int = 0,
) -> ClassificationResult:
    """Classe une tentative a partir de signaux explicites.

    Priorite des regles (premiere match gagne apres success):
      auth > proxy_infra > hard_block > attempt_timeout > rate_limited
      > structural_change > platform_change_suspected > soft_block > unknown
    """
    err = (error or "").strip().lower()
    signals: dict[str, Any] = {
        "http_status": http_status,
        "posts_count": posts_count,
        "ssr_universal_len": ssr_universal_len,
        "ssr_has_user_info": ssr_has_user_info,
        "ssr_has_post_list": ssr_has_post_list,
        "ssr_scope_keys": list(ssr_scope_keys or [])[:20],
        "item_list_xhr_seen": item_list_xhr_seen,
        "fetch_failed": fetch_failed,
        "challenge_detected": challenge_detected,
        "chrome_error": chrome_error,
        "consecutive_soft_block_countries": consecutive_soft_block_countries,
        "raw_error": (error or "")[:160],
    }

    if posts_count > 0 and not err:
        return ClassificationResult(
            scrape_class=ScrapeClass.SUCCESS,
            action=RecommendedAction.NONE,
            reason="posts_extracted",
            signals=signals,
            mark_waf=False,
            legacy_error="",
        )

    # --- auth (407) ---
    if _is_auth(err, http_status):
        return ClassificationResult(
            scrape_class=ScrapeClass.AUTH,
            action=RecommendedAction.ABORT_CREDENTIAL,
            reason="proxy_auth_failure",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "proxy_blocked:auth",
        )

    # --- proxy infra ---
    if _is_proxy_infra(err, http_status):
        return ClassificationResult(
            scrape_class=ScrapeClass.PROXY_INFRA,
            action=RecommendedAction.RETRY_SAME_IDENTITY,
            reason="proxy_tunnel_or_gateway",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "proxy_blocked:proxy_infra",
        )

    # --- hard block ---
    if challenge_detected or chrome_error or _is_hard_block(err):
        return ClassificationResult(
            scrape_class=ScrapeClass.HARD_BLOCK,
            action=RecommendedAction.QUARANTINE_IDENTITY,
            reason="challenge_or_chrome_error",
            signals=signals,
            mark_waf=True,
            legacy_error=error or "challenge_detected",
        )

    # --- attempt timeout (NOT tls/nav) ---
    if _is_attempt_timeout(err):
        return ClassificationResult(
            scrape_class=ScrapeClass.ATTEMPT_TIMEOUT,
            action=RecommendedAction.ROTATE_IDENTITY,
            reason="scrape_budget_exceeded",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "proxy_blocked:attempt_timeout",
        )

    # --- rate limited ---
    if _is_rate_limited(err, http_status):
        return ClassificationResult(
            scrape_class=ScrapeClass.RATE_LIMITED,
            action=RecommendedAction.BACKOFF,
            reason="http_403_or_429",
            signals=signals,
            mark_waf=True,
            legacy_error=error or f"proxy_blocked:http_{http_status or 429}",
        )

    # Infer SSR flags from scope keys when not explicit.
    scope = set(ssr_scope_keys or [])
    if ssr_has_user_info is None and scope:
        ssr_has_user_info = "webapp.user-detail" in scope
    if ssr_has_post_list is None and scope:
        ssr_has_post_list = "webapp.user-post-list" in scope

    # --- structural change: big SSR shell but missing expected keys ---
    if (
        ssr_universal_len > 50000
        and ssr_has_user_info is True
        and ssr_has_post_list is False
        and item_list_xhr_seen is False
        and posts_count == 0
    ):
        # Reproduit sur plusieurs pays → platform_change_suspected
        if consecutive_soft_block_countries >= 3:
            return ClassificationResult(
                scrape_class=ScrapeClass.PLATFORM_CHANGE_SUSPECTED,
                action=RecommendedAction.ALERT_HUMAN,
                reason="soft_shell_reproduced_across_countries",
                signals=signals,
                mark_waf=False,
                legacy_error=error or "EMPTY_FEED_OR_SOFTBLOCK",
            )
        return ClassificationResult(
            scrape_class=ScrapeClass.STRUCTURAL_CHANGE,
            action=RecommendedAction.ALERT_HUMAN,
            reason="ssr_userinfo_without_post_list_no_xhr",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "EMPTY_FEED_OR_SOFTBLOCK",
        )

    # --- soft block (HTTP 200 empty feed, generic) ---
    if _is_soft_empty(err) or (
        posts_count == 0
        and (http_status in (None, 200))
        and ssr_universal_len > 50000
        and not challenge_detected
    ):
        if consecutive_soft_block_countries >= 3:
            return ClassificationResult(
                scrape_class=ScrapeClass.PLATFORM_CHANGE_SUSPECTED,
                action=RecommendedAction.ALERT_HUMAN,
                reason="empty_feed_multi_country",
                signals=signals,
                mark_waf=False,
                legacy_error=error or "EMPTY_FEED_OR_SOFTBLOCK",
            )
        return ClassificationResult(
            scrape_class=ScrapeClass.SOFT_BLOCK,
            action=RecommendedAction.ROTATE_IDENTITY,
            reason="http200_empty_feed",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "EMPTY_FEED_OR_SOFTBLOCK",
        )

    # Real TLS/nav timeout (goto), distinct from attempt budget.
    if _is_tls_nav_timeout(err):
        return ClassificationResult(
            scrape_class=ScrapeClass.PROXY_INFRA,
            action=RecommendedAction.ROTATE_IDENTITY,
            reason="tls_or_nav_timeout",
            signals=signals,
            mark_waf=False,
            legacy_error=error or "proxy_blocked:tls_timeout",
        )

    return ClassificationResult(
        scrape_class=ScrapeClass.UNKNOWN,
        action=RecommendedAction.ROTATE_IDENTITY,
        reason="unclassified",
        signals=signals,
        mark_waf=False,
        legacy_error=error or "unknown",
    )


def classify_from_result(
    result: dict[str, Any] | None,
    *,
    consecutive_soft_block_countries: int = 0,
) -> ClassificationResult:
    """Helper: classifie un dict retourne par scrape_tiktok_page / scrape_once."""
    result = result or {}
    posts = result.get("posts") or []
    posts_count = len(posts) if isinstance(posts, list) else int(result.get("total") or 0)
    diag = result.get("classification_signals") or result.get("diag") or {}
    if not isinstance(diag, dict):
        diag = {}

    return classify_attempt(
        error=str(result.get("error") or "") or None,
        http_status=_as_int(result.get("http_status") or diag.get("http_status")),
        posts_count=posts_count,
        ssr_universal_len=int(
            diag.get("ssr_universal_len")
            or result.get("ssr_universal_len")
            or 0
        ),
        ssr_has_user_info=diag.get("ssr_has_user_info"),
        ssr_has_post_list=diag.get("ssr_has_post_list"),
        ssr_scope_keys=diag.get("ssr_scope_keys") or result.get("ssr_scope_keys"),
        item_list_xhr_seen=diag.get("item_list_xhr_seen"),
        fetch_failed=diag.get("fetch_failed"),
        challenge_detected=bool(
            diag.get("challenge_detected")
            or str(result.get("error") or "").lower() == "challenge_detected"
        ),
        chrome_error=bool(diag.get("chrome_error")),
        consecutive_soft_block_countries=consecutive_soft_block_countries,
    )


def missing_expected_scope_keys(scope_keys: list[str] | None) -> list[str]:
    present = set(scope_keys or [])
    return sorted(_EXPECTED_SCOPE_KEYS - present)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_auth(err: str, status: int | None) -> bool:
    if status == 407:
        return True
    return any(
        tok in err
        for tok in (
            "proxy_blocked:auth",
            "err_invalid_auth",
            "invalid_auth_credentials",
            "proxy authentication",
            "407",
        )
    )


def _is_proxy_infra(err: str, status: int | None) -> bool:
    if status in (502, 504):
        return True
    return any(
        tok in err
        for tok in (
            "proxy_infra",
            "err_tunnel",
            "err_proxy_connection",
            "err_connection_refused",
            "err_connection_reset",
            "err_empty_response",
            "bad gateway",
            "gateway timeout",
        )
    )


def _is_hard_block(err: str) -> bool:
    return any(
        tok in err
        for tok in (
            "challenge_detected",
            "captcha",
            "chrome-error",
            "chromewebdata",
        )
    )


def _is_attempt_timeout(err: str) -> bool:
    return "attempt_timeout" in err


def _is_tls_nav_timeout(err: str) -> bool:
    if "attempt_timeout" in err:
        return False
    return any(
        tok in err
        for tok in (
            "tls_timeout",
            "err_timed_out",
            "net::err_timed_out",
            "timeout",
        )
    ) and "http_" not in err


def _is_rate_limited(err: str, status: int | None) -> bool:
    if status in (403, 429):
        return True
    return any(
        tok in err
        for tok in (
            "http_403",
            "http_429",
            "http 403",
            "http 429",
            "err_http_response_code_failure",
            "rate_limited",
        )
    )


def _is_soft_empty(err: str) -> bool:
    return any(
        tok in err
        for tok in (
            "empty_feed_or_softblock",
            "no_posts_found",
            "soft_block",
        )
    )
