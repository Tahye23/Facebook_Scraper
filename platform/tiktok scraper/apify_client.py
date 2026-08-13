"""Client Apify pour le moteur TikTok alternatif (TIKTOK_ENGINE=apify).

Actor: clockworks/tiktok-profile-scraper (id 0FXVyOXXEmdGcV88a).
- Sync: POST .../run-sync-get-dataset-items
- Fallback async: start run → poll status → fetch dataset items
- Mapping → snake_case attendu par worker.normalize_post
- Quota journalier local (apify_quota.json)

Sécurité: APIFY_TOKEN jamais loggé (URLs redactées).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="apify_client")

_LOCK = threading.Lock()
_DEFAULT_ACTOR_ID = "0FXVyOXXEmdGcV88a"
_DEFAULT_QUOTA_FILE = "apify_quota.json"
_API_BASE = "https://api.apify.com/v2"


class ApifyQuotaExceeded(Exception):
    """Quota journalier APIFY_DAILY_VIDEO_LIMIT atteint."""


class ApifyConfigError(Exception):
    """Configuration Apify manquante / invalide."""


class ApifyRunError(Exception):
    """Echec d'un run Apify (HTTP, FAILED, TIMED-OUT, ...)."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def apify_token() -> str:
    return (os.getenv("APIFY_TOKEN") or "").strip()


def apify_actor_id() -> str:
    return (os.getenv("APIFY_ACTOR_ID") or _DEFAULT_ACTOR_ID).strip() or _DEFAULT_ACTOR_ID


def daily_video_limit() -> int:
    return max(1, _env_int("APIFY_DAILY_VIDEO_LIMIT", 33))


def poll_interval_s() -> float:
    return max(1.0, _env_float("APIFY_POLL_INTERVAL_S", 5.0))


def run_timeout_s() -> float:
    return max(30.0, _env_float("APIFY_RUN_TIMEOUT_S", 180.0))


def quota_exceeded_message(used: int, requested: int, limit: int) -> str:
    return (
        f"Quota Apify journalier atteint ({used}/{limit} vidéos). "
        f"Demande refusée ({requested} vidéo(s) supplémentaires feraient "
        f"{used + requested}/{limit}). Réessayez demain ou augmentez "
        f"APIFY_DAILY_VIDEO_LIMIT si votre plan Apify le permet."
    )

def _quota_path() -> Path:
    raw = (os.getenv("APIFY_QUOTA_FILE") or _DEFAULT_QUOTA_FILE).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _load_quota() -> dict[str, Any]:
    path = _quota_path()
    if not path.exists():
        return {"date": _today_utc(), "count": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"date": _today_utc(), "count": 0}
        date = str(data.get("date") or "")
        count = int(data.get("count") or 0)
        if date != _today_utc():
            return {"date": _today_utc(), "count": 0}
        return {"date": date, "count": max(0, count)}
    except Exception:
        LOGGER.debug("apify quota load failed", exc_info=True)
        return {"date": _today_utc(), "count": 0}


def _save_quota(data: dict[str, Any]) -> None:
    path = _quota_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        LOGGER.debug("apify quota save failed", exc_info=True)


def get_daily_usage() -> int:
    with _LOCK:
        return int(_load_quota().get("count") or 0)


def check_quota_or_raise(requested: int) -> None:
    """Bloque AVANT l'appel si count + demande > limite (jamais de dépassement)."""
    requested = max(0, int(requested))
    limit = daily_video_limit()
    with _LOCK:
        used = int(_load_quota().get("count") or 0)
        if used + requested > limit:
            msg = quota_exceeded_message(used, requested, limit)
            LOGGER.warning("[APIFY] %s", msg)
            raise ApifyQuotaExceeded(msg)


def record_quota_usage(n_items: int) -> int:
    """Incremente le compteur avec le nb reel d'items. Retourne le nouveau total."""
    n = max(0, int(n_items))
    with _LOCK:
        data = _load_quota()
        data["count"] = int(data.get("count") or 0) + n
        data["date"] = _today_utc()
        _save_quota(data)
        total = int(data["count"])
    LOGGER.info(
        "[APIFY] quota updated +%s → %s/%s (date=%s)",
        n,
        total,
        daily_video_limit(),
        _today_utc(),
    )
    return total


def _redact_url(url: str) -> str:
    """Masque token=... dans les URLs pour les logs."""
    if "token=" not in (url or ""):
        return url or ""
    try:
        parts = url.split("token=", 1)
        rest = parts[1]
        amp = rest.find("&")
        if amp >= 0:
            return f"{parts[0]}token=***{rest[amp:]}"
        return f"{parts[0]}token=***"
    except Exception:
        return "<redacted>"


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
    # Fallback: username nu
    cleaned = (url or "").strip().lstrip("@")
    if cleaned and "/" not in cleaned and " " not in cleaned:
        return cleaned.lower()
    return ""


def fetch_fresh_posts_from_gateway(
    author: str,
    *,
    max_posts: int = 20,
    max_age_hours: int | None = None,
) -> dict[str, Any] | None:
    """Posts avec metrics fraiches (TTL) via gateway — skip Apify / pas de quota."""
    handle = (author or "").strip().lstrip("@")
    if not handle:
        return None
    base_url = (os.getenv("GATEWAY_INTERNAL_URL") or "http://gateway:8080").rstrip("/")
    endpoint = f"{base_url}/internal/results/fresh"
    token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    body: dict[str, Any] = {
        "platform": "tiktok",
        "author": handle,
        "max_posts": max_posts,
    }
    if max_age_hours:
        body["max_age_hours"] = int(max_age_hours)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Internal-Token"] = token
    try:
        resp = requests.post(endpoint, json=body, headers=headers, timeout=10)
        if resp.status_code >= 400:
            LOGGER.warning("[APIFY] fresh cache HTTP %s", resp.status_code)
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except requests.RequestException as exc:
        LOGGER.warning("[APIFY] fresh cache lookup failed: %s", exc)
        return None


def _to_iso(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str) and "T" in raw:
        return raw.strip()
    try:
        ts = int(raw)
        if ts > 10_000_000_000:  # ms
            ts = ts // 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return None


def apify_item_to_post(raw_item: dict, fallback_author: str = "") -> dict | None:
    """Mappe un item dataset Apify → snake_case compatible worker.normalize_post."""
    if not isinstance(raw_item, dict):
        return None

    post_id = str(
        raw_item.get("id")
        or raw_item.get("videoId")
        or raw_item.get("aweme_id")
        or ""
    ).strip()
    if not post_id:
        return None

    author_meta = raw_item.get("authorMeta") if isinstance(raw_item.get("authorMeta"), dict) else {}
    author = str(
        author_meta.get("name")
        or author_meta.get("nickName")
        or raw_item.get("author")
        or fallback_author
        or ""
    ).lstrip("@").strip()

    text = raw_item.get("text") or raw_item.get("desc") or ""
    if not isinstance(text, str):
        text = str(text or "")
    text = text.strip()

    published_at = (
        _to_iso(raw_item.get("createTimeISO"))
        or _to_iso(raw_item.get("createTime"))
        or _to_iso(raw_item.get("create_time"))
    )

    post_url = str(raw_item.get("webVideoUrl") or "").strip()
    if not post_url and author:
        post_url = f"https://www.tiktok.com/@{author.lower()}/video/{post_id}"
    elif not post_url:
        post_url = f"https://www.tiktok.com/video/{post_id}"

    likes = _safe_int(raw_item.get("diggCount"))
    comments = _safe_int(raw_item.get("commentCount"))
    shares = _safe_int(raw_item.get("shareCount"))
    views = _safe_int(raw_item.get("playCount"))

    return {
        "post_id": post_id,
        "id": post_id,
        "post_url": post_url,
        "author": author,
        "message": text,
        "text": text,
        "text_content": text,
        "published_at": published_at,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "likes": likes,
        "comments_count": comments,
        "shares": shares,
        "views": views,
        "metrics": {
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "views": views,
        },
    }


def build_actor_input(
    username: str,
    max_posts: int,
    *,
    max_age_hours: int | None = None,
) -> dict[str, Any]:
    """Input officiel clockworks/tiktok-profile-scraper (sans download media).

    Champs schema confirmes (console Apify):
      - resultsPerPage: nb de posts par profil (FIX A: = max_posts demande)
      - oldestPostDateUnified: filtre date natif (ISO YYYY-MM-DD ou jours "1"/"2")
        → utilise quand max_age_hours est fourni (CSV 24h) pour economiser le quota
      - profileSorting=latest: requis pour que les filtres de date fonctionnent
    """
    handle = (username or "").lstrip("@").strip()
    # Fallback 20 UNIQUEMENT si max_posts absent/invalid — jamais ecraser un 2 explicite.
    try:
        requested = int(max_posts) if max_posts is not None else 20
    except (TypeError, ValueError):
        requested = 20
    results = max(1, min(requested if requested > 0 else 20, 200))

    body: dict[str, Any] = {
        "profiles": [handle],
        "profileScrapeSections": ["videos"],
        "profileSorting": "latest",
        "resultsPerPage": results,
        "excludePinnedPosts": False,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadAvatars": False,
        "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
    }

    # Filtre date natif Apify (add-on payant mais economise les items hors fenetre).
    # Filet de securite: le worker refiltre encore cote Python sur published_at.
    if max_age_hours is not None and int(max_age_hours) > 0:
        hours = int(max_age_hours)
        oldest = datetime.now(tz=timezone.utc).timestamp() - (hours * 3600)
        oldest_date = datetime.fromtimestamp(oldest, tz=timezone.utc).strftime("%Y-%m-%d")
        body["oldestPostDateUnified"] = oldest_date

    return body


def _auth_params() -> dict[str, str]:
    token = apify_token()
    if not token:
        raise ApifyConfigError("APIFY_TOKEN manquant dans .env")
    return {"token": token}


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Accept": "application/json"}


def _run_sync_get_items(actor_input: dict[str, Any], timeout_s: float) -> list[dict]:
    actor_id = apify_actor_id()
    url = f"{_API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
    # DEBUG: payload complet (sans token) pour verifier resultsPerPage / date filter.
    LOGGER.info(
        "[APIFY] sync run actor=%s payload=%s timeout=%.0fs",
        actor_id,
        json.dumps(actor_input, ensure_ascii=False),
        timeout_s,
    )
    try:
        resp = requests.post(
            url,
            params=_auth_params(),
            headers=_headers(),
            json=actor_input,
            timeout=timeout_s,
        )
    except requests.Timeout as exc:
        raise TimeoutError(f"apify sync timeout after {timeout_s}s") from exc
    except requests.RequestException as exc:
        raise ApifyRunError(f"apify sync request failed: {exc}") from exc

    if resp.status_code >= 400:
        # Ne jamais logger resp.url (contient le token).
        body_preview = (resp.text or "")[:300].replace(apify_token(), "***")
        raise ApifyRunError(f"apify sync HTTP {resp.status_code}: {body_preview}")

    data = resp.json()
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [x for x in data["data"] if isinstance(x, dict)]
    return []


def _start_async_run(actor_input: dict[str, Any]) -> str:
    actor_id = apify_actor_id()
    url = f"{_API_BASE}/acts/{actor_id}/runs"
    LOGGER.info("[APIFY] async start actor=%s", actor_id)
    try:
        resp = requests.post(
            url,
            params=_auth_params(),
            headers=_headers(),
            json=actor_input,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise ApifyRunError(f"apify start run failed: {exc}") from exc

    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:300].replace(apify_token(), "***")
        raise ApifyRunError(f"apify start HTTP {resp.status_code}: {body_preview}")

    payload = resp.json() if resp.content else {}
    data = payload.get("data") if isinstance(payload, dict) else None
    run_id = ""
    if isinstance(data, dict):
        run_id = str(data.get("id") or "").strip()
    if not run_id:
        raise ApifyRunError("apify start: missing run id")
    LOGGER.info("[APIFY] async run started id=%s", run_id)
    return run_id


def _poll_run(run_id: str, timeout_s: float) -> dict[str, Any]:
    url = f"{_API_BASE}/actor-runs/{run_id}"
    deadline = time.time() + timeout_s
    interval = poll_interval_s()
    last_status = ""
    while time.time() < deadline:
        try:
            resp = requests.get(url, params=_auth_params(), timeout=30)
        except requests.RequestException as exc:
            LOGGER.warning("[APIFY] poll error run=%s: %s", run_id, exc)
            time.sleep(interval)
            continue
        if resp.status_code >= 400:
            raise ApifyRunError(f"apify poll HTTP {resp.status_code}")
        payload = resp.json() if resp.content else {}
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
        status = str(data.get("status") or "").upper()
        if status != last_status:
            LOGGER.info("[APIFY] run=%s status=%s", run_id, status)
            last_status = status
        if status == "SUCCEEDED":
            return data
        if status in ("FAILED", "TIMED-OUT", "ABORTED"):
            raise ApifyRunError(f"apify run {run_id} ended with status={status}")
        time.sleep(interval)
    raise TimeoutError(f"apify async poll timeout after {timeout_s}s (run={run_id})")


def _fetch_dataset_items(run_id: str) -> list[dict]:
    url = f"{_API_BASE}/actor-runs/{run_id}/dataset/items"
    try:
        resp = requests.get(
            url,
            params={**_auth_params(), "format": "json"},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise ApifyRunError(f"apify dataset fetch failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ApifyRunError(f"apify dataset HTTP {resp.status_code}")
    data = resp.json()
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def scrape_profile(
    profile_url: str,
    *,
    max_posts: int = 20,
    max_age_hours: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Point d'entree: scrape un profil via Apify. Contrat scraper.py.

    Retourne {posts, total, url, error?} — posts en snake_case.
    """
    username = username_from_profile_url(profile_url)
    if not username:
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": "invalid_profile_url",
            "error_code": "invalid_profile_url",
        }

    # Cache Mongo (TTL metrics) — avant tout appel Apify / quota.
    if not force_refresh:
        cached = fetch_fresh_posts_from_gateway(
            username,
            max_posts=max_posts,
            max_age_hours=max_age_hours,
        )
        if cached and cached.get("enough") and cached.get("posts"):
            posts = list(cached.get("posts") or [])
            if not max_age_hours:
                try:
                    posts = posts[: max(1, int(max_posts or 20))]
                except (TypeError, ValueError):
                    posts = posts[:20]
            LOGGER.info(
                "[APIFY] cache hit @%s posts=%s (quota untouched)",
                username,
                len(posts),
            )
            return {
                "posts": posts,
                "total": len(posts),
                "url": profile_url,
                "from_cache": True,
                "classification_signals": {
                    "engine": "apify_cache",
                    "username": username,
                },
            }

    if not apify_token():
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": "APIFY_TOKEN manquant",
            "error_code": "apify_config",
        }

    try:
        requested_max = int(max_posts) if max_posts is not None else 20
    except (TypeError, ValueError):
        requested_max = 20
    if requested_max <= 0:
        requested_max = 20
    max_posts = max(1, min(requested_max, 200))

    age = int(max_age_hours) if max_age_hours else None
    # Fenetre temporelle: resultsPerPage = plafond de fetch (pas la limite metier).
    # Sans fenetre: resultsPerPage = max_posts exact (FIX A).
    if age and age > 0:
        fetch_ceiling = max(
            max_posts,
            max(1, min(_env_int("APIFY_CSV_FETCH_LIMIT", 100), 200)),
        )
    else:
        fetch_ceiling = max_posts

    actor_input = build_actor_input(
        username,
        fetch_ceiling,
        max_age_hours=age,
    )
    LOGGER.info(
        "[APIFY] scrape @%s requested_max_posts=%s fetch_ceiling=%s max_age_hours=%s "
        "resultsPerPage=%s oldestPostDateUnified=%s",
        username,
        max_posts,
        fetch_ceiling,
        age,
        actor_input.get("resultsPerPage"),
        actor_input.get("oldestPostDateUnified"),
    )

    try:
        check_quota_or_raise(int(actor_input["resultsPerPage"]))
    except ApifyQuotaExceeded as exc:
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": str(exc),
            "error_code": "apify_quota_exceeded",
        }

    timeout_s = run_timeout_s()
    raw_items: list[dict] = []
    try:
        try:
            raw_items = _run_sync_get_items(actor_input, timeout_s)
        except TimeoutError:
            LOGGER.warning(
                "[APIFY] sync timeout — falling back to async poll for @%s",
                username,
            )
            run_id = _start_async_run(actor_input)
            _poll_run(run_id, timeout_s)
            raw_items = _fetch_dataset_items(run_id)
    except (ApifyConfigError, ApifyRunError, TimeoutError) as exc:
        LOGGER.warning("[APIFY] scrape failed for @%s: %s", username, exc)
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": f"apify_error:{exc}",
            "error_code": "apify_error",
        }
    except Exception as exc:
        LOGGER.warning("[APIFY] unexpected error for @%s: %s", username, exc, exc_info=True)
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": f"apify_exception:{exc}",
            "error_code": "apify_exception",
        }

    posts: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        post = apify_item_to_post(item, fallback_author=username)
        if not post:
            continue
        pid = str(post.get("post_id") or "").strip()
        if not pid or pid in seen:
            continue
        if age and age > 0:
            published = post.get("published_at")
            if published:
                try:
                    dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(tz=timezone.utc) - dt).total_seconds() > age * 3600:
                        continue
                except Exception:
                    pass
            else:
                # Sans date: en mode fenetre on ne peut pas garantir → skip
                continue
        seen.add(pid)
        posts.append(post)
        # Sans fenetre temporelle: coupe strictement a max_posts.
        # Avec fenetre: on garde tout ce qui est dans la fenetre (plafond = fetch).
        if not age and len(posts) >= max_posts:
            break
        if age and len(posts) >= fetch_ceiling:
            break

    record_quota_usage(len(posts))

    LOGGER.info(
        "[APIFY] @%s done raw_items=%s kept=%s (max_posts=%s age_h=%s)",
        username,
        len(raw_items),
        len(posts),
        max_posts,
        age,
    )

    if not posts:
        return {
            "posts": [],
            "total": 0,
            "url": profile_url,
            "error": "no_posts_found",
            "error_code": "no_posts_found",
            "classification_signals": {
                "engine": "apify",
                "username": username,
                "raw_items": len(raw_items),
                "requested_max_posts": max_posts,
            },
        }

    return {
        "posts": posts,
        "total": len(posts),
        "url": profile_url,
        "classification_signals": {
            "engine": "apify",
            "username": username,
            "raw_items": len(raw_items),
            "requested_max_posts": max_posts,
            "results_per_page_sent": actor_input.get("resultsPerPage"),
        },
    }
