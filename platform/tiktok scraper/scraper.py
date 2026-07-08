import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright
from pathlib import Path
from video_analysis import analyze_tiktok_video, build_small_video_report

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger, with_context


COOKIES_FILE = "tiktok_cookies.json"
LOGGER = get_logger(__name__, platform="tiktok", service="scraper")


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


def load_cookies() -> list:
    """Charge et normalise les cookies TikTok depuis `tiktok_cookies.json`.

    Objectif:
    - Accepter un export JSON de cookies (liste d'objets).
    - Garder uniquement les champs utiles pour Playwright.
    - Retourner une liste prete pour `context.add_cookies(...)`.
    """
    if not os.path.exists(COOKIES_FILE):
        return []

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        LOGGER.warning("Failed to read cookies file", exc_info=True)
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

    Retourne une URL sans query string ni fragment pour eviter les doublons.
    """
    parsed = urlparse(url)
    if "tiktok.com" not in parsed.netloc:
        raise ValueError("URL TikTok invalide")
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return clean


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
    """Injecte un script de camouflage navigateur au demarrage des pages.

    But: reduire quelques signaux anti-bot evidents sans ajouter de dependances
    lourdes.
    """
    # Keep this lightweight to reduce easy bot fingerprints without heavy dependencies.
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
        window.chrome = window.chrome || { runtime: {} };
        """
    )


def _build_proxy_config() -> dict | None:
    """Construit une configuration proxy unique a partir des variables env."""
    server = (os.getenv("TIKTOK_PROXY_SERVER") or "").strip()
    if not server:
        return None

    proxy = {"server": server}
    username = (os.getenv("TIKTOK_PROXY_USERNAME") or "").strip()
    password = os.getenv("TIKTOK_PROXY_PASSWORD") or ""
    if username:
        proxy["username"] = username
        proxy["password"] = password
    return proxy


def _build_user_data_dir() -> str:
    """Retourne le dossier profil navigateur persistant (ou chaine vide)."""
    return (os.getenv("TIKTOK_USER_DATA_DIR") or "").strip()


def _should_apply_stealth(user_data_dir: str) -> bool:
    """Decide si le mode stealth doit etre applique.

    En profil persistant reel, on desactive par defaut le stealth agressif,
    sauf si la variable d'env force son activation.
    """
    # In persistent real-profile mode, aggressive stealth patches can look less natural.
    default = False if user_data_dir else True
    return _env_bool("TIKTOK_APPLY_STEALTH", default)


def _parse_proxy_spec(spec: str) -> dict | None:
    """Parse une ligne proxy en plusieurs formats supportes.

    Formats acceptes:
    - host:port|username|password
    - scheme://username:password@host:port
    - valeur brute (server)
    """
    raw = (spec or "").strip()
    if not raw:
        return None

    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        server = parts[0] if parts else ""
        if not server:
            return None
        proxy = {"server": server}
        if len(parts) > 1 and parts[1]:
            proxy["username"] = parts[1]
            proxy["password"] = parts[2] if len(parts) > 2 else ""
        return proxy

    parsed = urlsplit(raw)
    if parsed.scheme and parsed.hostname:
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"

        proxy = {"server": urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
            proxy["password"] = unquote(parsed.password or "")
        return proxy

    return {"server": raw}


def _load_proxy_candidates() -> list[dict | None]:
    """Construit la liste de proxies candidats avec deduplication.

    Sources:
    - TIKTOK_PROXY_SERVER (+ username/password)
    - TIKTOK_PROXY_LIST (multi-lignes)

    Retourne `[None]` si aucun proxy n'est configure (mode direct).
    """
    candidates = []
    seen = set()

    inline_proxy = _build_proxy_config()
    if inline_proxy:
        key = json.dumps(inline_proxy, sort_keys=True)
        seen.add(key)
        candidates.append(inline_proxy)

    raw_list = (os.getenv("TIKTOK_PROXY_LIST") or "").replace(";", "\n")
    for line in raw_list.splitlines():
        proxy = _parse_proxy_spec(line)
        if not proxy:
            continue
        key = json.dumps(proxy, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(proxy)

    if not candidates:
        return [None]

    if _env_bool("TIKTOK_TRY_DIRECT_AFTER_PROXIES", False):
        candidates.append(None)
    return candidates


def _describe_proxy(proxy_cfg: dict | None) -> str:
    """Retourne une description lisible du mode reseau (proxy/direct)."""
    if not proxy_cfg:
        return "direct"
    return proxy_cfg.get("server") or "proxy"


def _save_challenge_artifacts(page):
    """Sauvegarde des artefacts de debug quand un challenge TikTok est detecte.

    Fichiers produits:
    - screenshot PNG
    - HTML complet de la page
    - contexte URL + titre dans les logs
    """
    try:
        page.screenshot(path="tiktok_challenge.png", full_page=True)
    except Exception as exc:
        LOGGER.warning("Failed to save challenge screenshot", exc_info=True)

    try:
        html = page.content()
        with open("tiktok_challenge.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        LOGGER.warning("Failed to save challenge HTML", exc_info=True)

    try:
        current_url = page.url
    except Exception:
        current_url = "unknown"

    try:
        title = page.title()
    except Exception:
        title = "unknown"



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
        desc = item.get("desc") or ""
        post_url = f"https://www.tiktok.com/@{author}/video/{post_id}" if author else ""

        posts.append(
            {
                "post_id": post_id,
                "post_url": post_url,
                "message": desc,
                "author": author,
                "published_at": None,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                "likes": stats.get("diggCount"),
                "comments_count": stats.get("commentCount"),
                "shares": stats.get("shareCount"),
                "views": stats.get("playCount"),
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
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



def _merge_post_data(base: dict, extra: dict) -> dict:
    """Fusionne des metadonnees de post sans ecraser les valeurs deja presentes.

    On complete seulement les champs vides dans `base` avec les donnees de `extra`.
    """
    merged = dict(base)
    for key in ("author", "message", "published_at", "likes", "comments_count", "shares", "views"):
        current = merged.get(key)
        incoming = extra.get(key)
        if (current is None or current == "") and incoming not in (None, ""):
            merged[key] = incoming
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

    Le JS embarque tente d'abord `window.SIGI_STATE`, puis le payload de
    rehydratation SSR, et retourne un objet minimal si rien n'est disponible.
    """
    return page.evaluate(
        r"""
        () => {
            const fromStats = (stats, author = '', desc = '') => ({
                author: author || '',
                message: desc || '',
                likes: stats?.diggCount ?? null,
                comments_count: stats?.commentCount ?? null,
                shares: stats?.shareCount ?? null,
                views: stats?.playCount ?? null,
                published_at: null,
            });

            const urlIdMatch = (location.href || '').match(/\/video\/(\d+)/);
            const urlId = urlIdMatch ? urlIdMatch[1] : '';

            const sigi = window.SIGI_STATE;
            const itemModule = sigi && sigi.ItemModule ? sigi.ItemModule : null;
            if (itemModule && typeof itemModule === 'object') {
                const exact = urlId && itemModule[urlId] ? itemModule[urlId] : null;
                const candidate = exact || Object.values(itemModule)[0] || null;
                if (candidate && typeof candidate === 'object') {
                    return fromStats(candidate.stats || {}, candidate.author || '', candidate.desc || '');
                }
            }

            const script = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (script && script.textContent) {
                try {
                    const hydration = JSON.parse(script.textContent);
                    let found = null;
                    const collect = (node) => {
                        if (!node || typeof node !== 'object' || found) return;
                        const item = node.itemStruct || node.item || null;
                        if (item && item.id) {
                            const itemId = String(item.id || '');
                            if (!urlId || itemId === urlId) {
                                const author = item.author?.uniqueId || item.author?.nickname || '';
                                found = fromStats(item.stats || {}, author, item.desc || '');
                                return;
                            }
                        }
                        for (const v of Object.values(node)) {
                            if (v && typeof v === 'object') collect(v);
                        }
                    };
                    collect(hydration);
                    if (found) return found;
                } catch {
                    // ignore parse errors
                }
            }

            return {
                author: '',
                message: '',
                likes: null,
                comments_count: null,
                shares: null,
                views: null,
                published_at: null,
            };
        }
        """
    )


def _enrich_posts_from_video_pages(context, posts: list[dict]) -> list[dict]:
    """Enrichit les posts en ouvrant les pages video une a une.

    Utilise `_extract_video_detail_from_page` pour completer les stats/auteur
    quand le listing profil est incomplet.
    """
    if not posts:
        return posts

    enabled = _env_bool("TIKTOK_ENRICH_POST_DETAILS", True)
    if not enabled:
        return posts

    raw_limit = (os.getenv("TIKTOK_ENRICH_POST_DETAILS_LIMIT") or "6").strip()
    try:
        limit = max(0, int(raw_limit))
    except ValueError:
        limit = 6

    if limit == 0:
        return posts

    enriched = []
    for idx, post in enumerate(posts):
        if idx >= limit:
            enriched.append(post)
            continue

        post_url = str(post.get("post_url") or "").strip()
        if not post_url or "/video/" not in post_url:
            enriched.append(post)
            continue

        detail_page = None
        try:
            detail_page = context.new_page()
            detail_page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            _human_pause(0.8, 0.6)
            detail = _extract_video_detail_from_page(detail_page)
            enriched.append(_merge_post_data(post, detail))
        except Exception as exc:
            enriched.append(post)
        finally:
            if detail_page is not None:
                try:
                    detail_page.close()
                except Exception as exc:
                    LOGGER.warning("Failed to close detail page", exc_info=True)

    return enriched


def _scrape_with_browser(
    playwright,
    profile_url: str,
    max_posts: int,
    on_post,
    headless: bool,
    slow_mo_ms: int,
    proxy_cfg: dict | None,
    analyze_video_content: bool,
) -> dict:
    """Pipeline principal de scraping via Playwright.

    Etapes:
    1) Ouvrir un contexte navigateur (persistant ou temporaire).
    2) Injecter headers/stealth/cookies selon la config.
    3) Naviguer vers le profil et collecter les posts (DOM + reseau).
    4) Gérer challenge/fallback HTTP si necessaire.
    5) Enrichir et analyser les posts avant retour.
    """
    browser = None
    user_data_dir = _build_user_data_dir()
    browser_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1365,768",
    ]
    context_options = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1365, "height": 768},
        "locale": "en-US",
        "timezone_id": "Europe/Paris",
    }

    # Log informatif si un proxy est actif pour cette tentative.
    if proxy_cfg:
        LOGGER.info("Using proxy candidate", extra={"url": _describe_proxy(proxy_cfg)})

    # Deux modes de contexte:
    # - persistent_context: reutilise un profil navigateur reel
    # - new_context: session propre, ephemere
    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        launch_persistent_args = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            "slow_mo": slow_mo_ms,
            "args": browser_args,
            **context_options,
        }
        if proxy_cfg:
            launch_persistent_args["proxy"] = proxy_cfg
        context = playwright.chromium.launch_persistent_context(**launch_persistent_args)
    else:
        launch_args = {
            "headless": headless,
            "slow_mo": slow_mo_ms,
            "args": browser_args,
        }
        if proxy_cfg:
            launch_args["proxy"] = proxy_cfg
        browser = playwright.chromium.launch(**launch_args)
        context = browser.new_context(**context_options)

    # Headers additionnels pour mimer un trafic navigateur classique.
    context.set_extra_http_headers(
        {
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Sec-CH-UA": '"Chromium";v="124", "Not:A-Brand";v="99"',
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Mobile": "?0",
        }
    )
    if _should_apply_stealth(user_data_dir):
        _install_stealth_scripts(context)

    # Injection de cookies si contexte non persistant, ou si forçage explicite.
    force_cookie_injection = _env_bool("TIKTOK_FORCE_COOKIE_INJECTION", False)
    if not user_data_dir or force_cookie_injection:
        cookies = load_cookies()
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                LOGGER.warning("Failed to inject cookies", exc_info=True)

    page = context.new_page()

    try:
        network_posts = []

        # Capture passive des reponses JSON reseau pour recuperer des posts
        # parfois absents du DOM rendu.
        def handle_response(response):
            rurl = response.url.lower()
            if not any(k in rurl for k in ("item_list", "aweme", "post/item", "user/post")):
                return

            try:
                ctype = response.headers.get("content-type", "").lower()
                if "json" not in ctype:
                    return
            except Exception:
                return

            try:
                payload = response.json()
                parsed = _extract_posts_from_json_payload(payload)
                if parsed:
                    network_posts.extend(parsed)
            except Exception:
                LOGGER.debug("Failed to parse network JSON response", exc_info=True)

        page.on("response", handle_response)

        _warmup_and_open_profile(page, profile_url)

        # Si challenge detecte, on laisse une fenetre de resolution manuelle.
        if _looks_like_tiktok_challenge(page):
            _manual_solve_wait_if_enabled(user_data_dir, headless)
            _wait_for_challenge_resolution(page, user_data_dir, headless)
            try:
                _warmup_and_open_profile(page, profile_url)
            except Exception as retry_err:
                LOGGER.warning("Challenge retry warmup failed", exc_info=True)

        all_posts = []
        seen = set()

        # Scroll progressif pour charger davantage de posts.
        for i in range(8):
            cards = _extract_video_cards(page, profile_url)
            batch = cards + network_posts
            network_posts = []

            for card in batch:
                sig = _video_signature(card)
                if sig in seen:
                    continue
                seen.add(sig)
                all_posts.append(card)
                if on_post is not None:
                    try:
                        on_post(dict(card))
                    except Exception:
                        LOGGER.warning("on_post callback error", exc_info=True)
                if len(all_posts) >= max_posts:
                    break

            if len(all_posts) >= max_posts:
                break

            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(1.7, 1.0)

        # Fallback final si aucune video n'a pu etre extraite via navigateur.
        if not all_posts:
            http_posts = _extract_posts_from_html_fallback(profile_url)
            if http_posts:
                if on_post is not None:
                    for post in http_posts[:max_posts]:
                        try:
                            on_post(dict(post))
                        except Exception:
                            LOGGER.warning("on_post callback error", exc_info=True)
                return {"posts": http_posts[:max_posts], "total": min(len(http_posts), max_posts), "url": profile_url}
            if _looks_like_tiktok_challenge(page):
                _save_challenge_artifacts(page)
                return {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}
            return {"posts": [], "total": 0, "error": "no_posts_found", "url": profile_url}

        # Post-traitements: enrichissement detail + analyse IA optionnelle.
        enriched_posts = _enrich_posts_from_video_pages(context, all_posts)
        if analyze_video_content:
            analyzed_posts = _attach_video_analysis(enriched_posts, on_post=None)
        else:
            analyzed_posts = enriched_posts

        return {
            "posts": analyzed_posts,
            "total": len(analyzed_posts),
            "url": profile_url,
            "page_report_docx": None,
            "page_report_pdf": None,
        }
    except Exception as e:
        LOGGER.exception("Browser scraping pipeline failed")
        return {"posts": [], "error": str(e), "url": profile_url}
    finally:
        try:
            context.close()
        except Exception as exc:
            LOGGER.warning("Failed to close browser context", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                LOGGER.warning("Failed to close browser", exc_info=True)


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

                const textNode = a.querySelector('[data-e2e="video-desc"]') || a.querySelector('img[alt]');
                const text = textNode ? (textNode.innerText || textNode.getAttribute('alt') || '').trim() : '';

                upsert({
                    post_id: postId,
                    post_url: absUrl,
                    message: text,
                    author: '',
                    likes: null,
                    comments_count: null,
                    shares: null,
                    views: null,
                });
            }

            // Fallback 1: état TikTok en mémoire (souvent présent sur desktop).
            const sigi = window.SIGI_STATE;
            const itemModule = sigi && sigi.ItemModule ? sigi.ItemModule : null;
            if (itemModule) {
                for (const [id, item] of Object.entries(itemModule)) {
                    const author = (item && item.author) || '';
                    const desc = (item && item.desc) || '';
                    const stats = (item && item.stats) || {};
                    const absUrl = author ? `https://www.tiktok.com/@${author}/video/${id}` : '';
                    if (!absUrl) {
                        continue;
                    }
                    upsert({
                        post_id: String(id || ''),
                        post_url: absUrl,
                        message: desc,
                        author,
                        likes: stats.diggCount ?? null,
                        comments_count: stats.commentCount ?? null,
                        shares: stats.shareCount ?? null,
                        views: stats.playCount ?? null,
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
                    const stats = item.stats || {};
                    const absUrl = author ? `https://www.tiktok.com/@${author}/video/${id}` : '';
                    if (absUrl) {
                        upsert({
                            post_id: id,
                            post_url: absUrl,
                            message: desc,
                            author,
                            likes: stats.diggCount ?? null,
                            comments_count: stats.commentCount ?? null,
                            shares: stats.shareCount ?? null,
                            views: stats.playCount ?? null,
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
                "published_at": None,
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

                    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
                    desc = item.get("desc") or item.get("description") or ""
                    post_url = f"https://www.tiktok.com/@{author}/video/{post_id}" if author else ""

                    posts.append(
                        {
                            "post_id": post_id,
                            "post_url": post_url,
                            "message": desc,
                            "author": author,
                            "published_at": None,
                            "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                            "likes": stats.get("diggCount"),
                            "comments_count": stats.get("commentCount"),
                            "shares": stats.get("shareCount"),
                            "views": stats.get("playCount"),
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
    """Ferme la banniere cookies si elle apparait."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        '[data-e2e="cookie-banner-accept"]',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                return
        except Exception:
            continue


def _warmup_and_open_profile(page, profile_url: str):
    """Fait un warmup TikTok puis ouvre le profil cible.

    Le passage par la homepage peut aider certaines sessions a etre plus stables
    avant l'ouverture de la page profil.
    """
    page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
    _dismiss_cookie_banner(page)
    _human_pause(1.2, 0.8)
    page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    _human_pause(1.8, 0.9)


def scrape_tiktok_page(
    url: str,
    max_posts: int = 20,
    on_post=None,
    headless_override: bool | None = None,
    analyze_video_content: bool | None = None,
) -> dict:
    """Point d'entree public: scrape une page TikTok et retourne un resultat.

    Orchestration:
    - normalise l'URL
    - decide headless/headed
    - prepare les candidats proxy
    - tente le scraping sur chaque candidat jusqu'au succes
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
    proxy_candidates = _load_proxy_candidates()

    with sync_playwright() as p:
        last_result = None
        for attempt_index, proxy_cfg in enumerate(proxy_candidates, start=1):
            scoped_logger.info("Scrape attempt started", extra={"post_id": None})
            # Rotation proxy: si challenge detecte, on passe au candidat suivant.
            result = _scrape_with_browser(
                p,
                profile_url,
                max_posts,
                on_post,
                headless,
                slow_mo_ms,
                proxy_cfg,
                analyze_video_content,
            )
            last_result = result
            if result.get("error") != "challenge_detected":
                if result.get("error"):
                    scoped_logger.warning("Scrape finished with error")
                else:
                    scoped_logger.info("Scrape finished successfully")
                return result
            if attempt_index < len(proxy_candidates):
                scoped_logger.warning("Challenge detected, rotating proxy candidate")

        return last_result or {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}
