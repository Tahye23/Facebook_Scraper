"""Parser TikTok profile-first: hydration SSR → posts + métriques.

TikTok ne livre plus les stats fiables sur `/video/<id>` seul.
On lit `__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE` sur le PROFIL
et on mappe `itemModule` / `itemList` → diggCount, playCount, etc.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse, urlunsplit

from logging_setup import get_logger
from profile_extractor import (
    extract_posts_from_hydration,
    extract_posts_from_hydration_payload,
    item_to_post,
    post_has_usable_metrics,
)

LOGGER = get_logger(__name__, platform="tiktok", service="tiktok_parser")

_UNIVERSAL_RE = re.compile(
    r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_SIGI_RE = re.compile(
    r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def normalize_www_tiktok_url(url: str) -> str:
    """Force https://www.tiktok.com (jamais m.tiktok.com) + @username minuscule."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.netloc or "www.tiktok.com").lower()
        if host.startswith("m.") or "m.tiktok.com" in host or host in {"tiktok.com", "www.tiktok.com"}:
            host = "www.tiktok.com"
        parts: list[str] = []
        for part in (parsed.path or "").split("/"):
            if not part:
                continue
            if part.startswith("@"):
                parts.append("@" + part[1:].lower())
            else:
                parts.append(part)
        path = "/" + "/".join(parts) if parts else "/"
        return urlunsplit(("https", host, path, "", ""))
    except Exception:
        return re.sub(r"(?i)m\.tiktok\.com", "www.tiktok.com", raw)


def extract_posts_from_html(html: str, profile_url: str = "") -> list[dict]:
    """Parse HTML profil: UNIVERSAL_DATA puis SIGI_STATE → posts normalisés."""
    text = html or ""
    if not text.strip():
        return []

    for pattern, label in ((_UNIVERSAL_RE, "UNIVERSAL_DATA"), (_SIGI_RE, "SIGI_STATE")):
        match = pattern.search(text)
        if not match:
            continue
        raw = (match.group(1) or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to JSON-decode %s payload", label)
            continue
        posts = extract_posts_from_hydration_payload(payload, profile_url=profile_url)
        if posts:
            LOGGER.info(
                "Profile-first HTML parse (%s): %d posts with metrics",
                label,
                len(posts),
            )
            return posts
    return []


def extract_posts_from_page(page: Any, profile_url: str = "") -> tuple[int, list[dict]]:
    """In-browser: lit hydration + retourne (universalLen, posts)."""
    return extract_posts_from_hydration(page, profile_url=profile_url)


def posts_have_profile_metrics(posts: list[dict]) -> bool:
    """True si tous les posts ont déjà diggCount/playCount (skip /video/<id>)."""
    if not posts:
        return False
    return all(post_has_usable_metrics(p) for p in posts)


__all__ = [
    "normalize_www_tiktok_url",
    "extract_posts_from_html",
    "extract_posts_from_page",
    "extract_posts_from_hydration",
    "extract_posts_from_hydration_payload",
    "item_to_post",
    "post_has_usable_metrics",
    "posts_have_profile_metrics",
]
