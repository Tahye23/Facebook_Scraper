"""Fallback oEmbed TikTok (text_content / author) — URL username en minuscules."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunsplit

import requests

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="oembed")


def normalize_tiktok_oembed_url(video_url: str) -> str:
    """Force www + @username en minuscules (evite HTTP 400 oEmbed).

    Ex: https://www.tiktok.com/@TawaturNet/video/123
     →  https://www.tiktok.com/@tawaturnet/video/123
    """
    raw = (video_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        # Jamais m.tiktok.com — oEmbed exige www + username lowercase.
        host = "www.tiktok.com"
        parts = [p for p in (parsed.path or "").split("/") if p]
        norm_parts: list[str] = []
        for part in parts:
            if part.startswith("@"):
                # Casse OEMBED: @TawaturNet → @tawaturnet (HTTP 400 sinon).
                norm_parts.append("@" + part[1:].lower())
            else:
                norm_parts.append(part)
        path_norm = "/" + "/".join(norm_parts) if norm_parts else "/"
        return urlunsplit(("https", host, path_norm, "", ""))
    except Exception:
        lowered = raw.lower().replace("m.tiktok.com", "www.tiktok.com")
        return lowered


def fetch_oembed_fallback(video_url: str) -> dict[str, Any]:
    """Dernier recours: title + author_name via API publique oEmbed."""
    url = (video_url or "").strip()
    if not url or "/video/" not in url:
        return {}
    normalized_url = normalize_tiktok_oembed_url(url)
    if not normalized_url:
        return {}
    # Double-check: username segment must be lowercase.
    try:
        path_parts = [p for p in urlparse(normalized_url).path.split("/") if p]
        if path_parts and path_parts[0].startswith("@") and path_parts[0] != path_parts[0].lower():
            normalized_url = normalize_tiktok_oembed_url(normalized_url)
    except Exception:
        pass
    try:
        res = requests.get(
            "https://www.tiktok.com/oembed",
            params={"url": normalized_url},
            timeout=5,
        )
        if res.status_code != 200:
            LOGGER.warning(
                "Oembed fallback HTTP %s for %s",
                res.status_code,
                normalized_url,
            )
            return {}
        try:
            data = res.json() if res.content else {}
        except ValueError as exc:
            LOGGER.warning("Oembed JSONDecodeError for %s: %s", normalized_url, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        text = str(data.get("title") or "").strip()
        author = str(data.get("author_name") or data.get("author_unique_id") or "").strip()
        out: dict[str, Any] = {}
        if text:
            out["text_content"] = text
            out["message"] = text
        if author:
            out["author"] = author.lstrip("@")
        return out
    except Exception as exc:
        LOGGER.warning("Oembed fallback failed for %s: %s", normalized_url, exc)
        return {}
