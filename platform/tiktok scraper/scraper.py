import json
import os
import random
import re
import sys
import threading
import time
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


COOKIES_FILE = "tiktok_cookies.json"
# Fichier JSON persistant des proxies mis en pause (cooldown 24h par defaut).
# Cle = identite sticky Webshare (username), valeur = {until, reason}.
PROXY_BLACKLIST_FILE = "proxy_blacklist.json"
_PROXY_BLACKLIST_LOCK = threading.Lock()
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
    - son auteur correspond au handle cible (insensible a la casse), ou
    - son `post_url` contient `/@<handle>/` (grille du profil), ou
    - on ne connait pas le handle cible (target vide -> on ne filtre pas).

    Les posts sans auteur ET sans handle dans l'URL sont consideres ambigus et
    rejetes en mode filtre (ils proviennent quasi toujours de l'etat "For You").
    """
    if not target_username:
        return True

    author = str(card.get("author") or "").strip().lower()
    if author and author == target_username:
        return True

    post_url = str(card.get("post_url") or "").strip().lower()
    if f"/@{target_username}/" in post_url:
        return True

    return False


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


def _proxy_identity(proxy_cfg: dict | None) -> str:
    """Cle stable d'un proxy pour blacklist / dedup.

    Pour Webshare sticky (`sdwopfmy-N`), le username est l'identite IP.
    Fallback: server + username.
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
    if not proxy_cfg:
        return False
    with _PROXY_BLACKLIST_LOCK:
        return _proxy_identity(proxy_cfg) in _load_blacklist()


def _blacklist_proxy(proxy_cfg: dict | None, reason: str, hours: int | None = None) -> None:
    """Met un proxy en cooldown.

    `hours` override le defaut env. Soft-block (no_posts) = cooldown court;
    auth/tunnel morts = cooldown long.
    """
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
    if "no_posts_found" in err or "challenge_detected" in err:
        return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_SOFT_HOURS", 2))
    if "invalid_auth" in err:
        return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_AUTH_HOURS", 6))
    return max(1, _env_int("TIKTOK_PROXY_BLACKLIST_HOURS", 24))


def _should_blacklist_error(error_code: str) -> bool:
    """True si l'erreur justifie un cooldown 24h (timeout / 403 / no_posts / nav).

    Les collisions de profil Chrome (SingletonLock) ne blacklisent PAS le proxy:
    l'IP est saine, seul le dossier session est momentanement pris.
    """
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


def _is_retryable_soft_error(error_code: str) -> bool:
    """Erreurs pour lesquelles on doit retenter / changer de proxy (pas abandonner).

    Inclut aussi les erreurs Playwright de navigation (ex: "interrupted by
    another navigation") qui ne contiennent pas `net::ERR_...` mais sont
    typiques d'un anti-bot / redirect TikTok — sans ca, on arretait apres
    1 seul proxy au lieu d'essayer les 4.
    """
    err = (error_code or "").strip().lower()
    if not err:
        return False
    if err in {
        "challenge_detected",
        "no_posts_found",
        "all_proxies_blocked",
        "sticky_profile_in_use",
    }:
        return True
    if _is_profile_lock_error(err):
        return True
    tokens = (
        "net::err",
        "err_timed_out",
        "err_http_response_code_failure",
        "err_connection",
        "err_tunnel",
        "err_proxy",
        "err_aborted",
        "err_name_not_resolved",
        "err_address_unreachable",
        "403",
        "interrupted by another navigation",
        "navigation to",
        "page.goto",
        "timeout",
        "target closed",
        "browser has been closed",
        "connection closed",
        "launch_persistent_context",
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
    """Collecte les specs proxy: fichier 20k + TIKTOK_PROXY_LIST + PROXY_SERVER.

    On NE met PAS les 20k dans le .env: le fichier est lu a la volee, puis on
    tire un echantillon aleatoire (voir `_select_proxy_candidates`).
    """
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
    """Tire N proxies NON blacklists.

    Modele cookies-par-IP:
      1) Preferer les IP qui ont DEJA un slot cookies valide sur disque
      2) Completer avec de nouvelles IP du pool sticky (seront chauffees a
         l'usage; si plafond de slots atteint -> LRU remplace le plus ancien)
    """
    if limit is None:
        limit = max(1, _env_int("TIKTOK_PROXY_MAX_PER_JOB", 4))

    pool = _parse_all_proxies()
    if not pool:
        return [None]

    by_id: dict[str, dict] = {}
    for proxy in pool:
        identity = _proxy_identity(proxy)
        if identity and identity not in by_id:
            by_id[identity] = proxy

    with _PROXY_BLACKLIST_LOCK:
        blocked = set(_load_blacklist().keys())

    available = [p for p in pool if _proxy_identity(p) not in blocked]
    if not available:
        LOGGER.warning(
            "All proxies are blacklisted; sampling from full pool anyway",
            extra={"post_id": None},
        )
        available = pool

    # Pool sticky dedie optionnel: priorise les N premieres sessions du fichier,
    # mais SI trop peu restent (blacklist), complete depuis le reste du pool.
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
    # IP hors pool dedie mais avec cookies: les garder en tete aussi.
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
    """Compat: sample dynamique pour un job (4 par defaut), pas toute la liste."""
    return _select_proxy_candidates()


def get_proxy_pool() -> list[dict]:
    """Echantillon de proxies pour les lanes batch (hors blacklist).

    Ne retourne PAS les 20k: seulement assez pour concurrency + reserve
    (defaut: max(concurrency*2, PROXY_MAX_PER_JOB)).
    """
    concurrency = max(1, _env_int("TIKTOK_BATCH_CONCURRENCY", 6))
    sample_size = max(concurrency * 2, _env_int("TIKTOK_PROXY_MAX_PER_JOB", 4))
    return [p for p in _select_proxy_candidates(limit=sample_size) if p is not None]


def pick_replacement_proxy(exclude: set[str] | None = None) -> dict | None:
    """Tire UNE nouvelle IP hors blacklist et hors `exclude` (pour releve CSV).

    Preferre une IP qui a deja des cookies valides; sinon une IP neuve
    (chauffee a l'usage, slot LRU si besoin).
    """
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
        blocked = set(_load_blacklist().keys())

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
    - host:port:username:password (export brut par defaut du dashboard
      Webshare, ex: "31.59.20.176:6754:sdwopfmy:v78vnzm50pdj") - permet de
      copier/coller directement le fichier telecharge sans le reformater.
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

    if "://" not in raw:
        # Format Webshare brut "host:port:username:password" (4 champs). On
        # ne le detecte que si le 2e champ est bien un port numerique, pour
        # ne jamais casser un format "host:port" simple (2 champs) qui doit
        # rester gere par le fallback `{"server": raw}` plus bas.
        parts = raw.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = (part.strip() for part in parts)
            if host and port and username:
                return {"server": f"{host}:{port}", "username": username, "password": password}

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
    for key in ("author", "message", "published_at", "likes", "comments_count", "shares", "views"):
        current = merged.get(key)
        incoming = extra.get(key)
        if key == "published_at" and incoming not in (None, ""):
            incoming = _to_iso_datetime(incoming) or incoming
        if key in ("likes", "comments_count", "shares", "views") and incoming not in (None, ""):
            incoming = _parse_count_value(incoming)
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

    wait_ms = max(2000, _env_int("TIKTOK_ENRICH_WAIT_MS", 12000))
    max_attempts = max(1, _env_int("TIKTOK_ENRICH_ATTEMPTS", 3))
    enriched = []
    for idx, post in enumerate(posts):
        if idx >= limit:
            enriched.append(post)
            continue

        post_url = str(post.get("post_url") or "").strip()
        if not post_url or "/video/" not in post_url:
            enriched.append(post)
            continue

        # Deja complet: pas besoin de rouvrir la page video.
        if all(post.get(k) not in (None, "") for k in ("likes", "views", "comments_count")):
            enriched.append(post)
            continue

        merged = dict(post)
        last_error = None
        for attempt in range(1, max_attempts + 1):
            detail_page = None
            try:
                detail_page = context.new_page()
                detail_page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
                # Attendre le JSON SSR ou au moins un compteur visible.
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
                    _human_pause(1.5, 0.8)
                else:
                    _human_pause(0.6, 0.4)

                detail = _extract_video_detail_from_page(detail_page) or {}
                merged = _merge_post_data(post, detail)
                if merged.get("likes") is None and merged.get("views") is None:
                    last_error = "no_metrics_in_page"
                    LOGGER.warning(
                        "Video detail enrichment returned no metrics (attempt %s/%s)",
                        attempt,
                        max_attempts,
                        extra={"url": post_url, "post_id": str(post.get("post_id") or "")},
                    )
                    if attempt < max_attempts:
                        _human_pause(1.2, 0.6)
                        continue
                else:
                    LOGGER.info(
                        "Video detail enrichment ok likes=%s views=%s comments=%s",
                        merged.get("likes"),
                        merged.get("views"),
                        merged.get("comments_count"),
                        extra={"url": post_url, "post_id": str(post.get("post_id") or "")},
                    )
                    last_error = None
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
                    _human_pause(1.5, 0.8)
                    continue
            finally:
                if detail_page is not None:
                    try:
                        detail_page.close()
                    except Exception:
                        LOGGER.warning("Failed to close detail page", exc_info=True)

        if last_error and merged.get("likes") is None and merged.get("views") is None:
            LOGGER.warning(
                "Video detail enrichment exhausted retries (%s)",
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

    # Défini avant le bloc try: même si une exception survient tôt (warmup,
    # ouverture de contexte, etc.), on doit pouvoir renvoyer les posts déjà
    # collectés au lieu de les perdre silencieusement.
    all_posts: list[dict] = []
    # Handle du profil cible: sert a rejeter les videos "For You" qui trainent
    # dans l'etat JS (SIGI_STATE.ItemModule) apres le warmup homepage.
    target_username = _username_from_url(profile_url)

    # --- Session sticky: 1 proxy = 1 profil Chrome persistant -----------------
    # Si active et qu'un proxy est fourni, on ignore TIKTOK_USER_DATA_DIR global
    # au profit de tiktok_sessions/<sticky_id>/ (cookies nes sous CETTE IP).
    sticky_identity = _proxy_identity(proxy_cfg) if proxy_cfg else ""
    sticky_session_dir: Path | None = None
    sticky_lock_held = False
    use_sticky = bool(
        sticky_sessions.sticky_enabled()
        and proxy_cfg is not None
        and sticky_identity
        and sticky_identity != "direct"
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
    using_real_chrome = bool(browser_channel)
    browser_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1365,768",
    ]
    # Playwright ajoute "--enable-automation" par defaut -> navigator.webdriver.
    ignored_default_args = ["--enable-automation"]

    context_options = {
        "viewport": {"width": 1365, "height": 768},
        "locale": "en-US",
        "timezone_id": "Europe/Paris",
    }
    # UA falsifie UNIQUEMENT pour Chromium embarque. Avec vrai Chrome: UA natif.
    if not using_real_chrome:
        context_options["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
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

    def handle_response(response):
        """Capture passive des reponses JSON reseau (posts parfois absents du DOM)."""
        rurl = response.url.lower()
        # NE JAMAIS capturer le feed de recommandations "For You" (charge par le
        # warmup homepage) ni l'onglet Explore: ce ne sont PAS les videos du
        # profil cible. Sans ce garde-fou, `/api/recommend/item_list/` matchait
        # "item_list" et polluait les resultats avec des videos etrangeres.
        if any(feed in rurl for feed in ("recommend", "/explore", "for_you", "foryou")):
            return
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
            if proxy_cfg:
                launch_persistent_args["proxy"] = proxy_cfg
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
            if proxy_cfg:
                launch_args["proxy"] = proxy_cfg
            browser = playwright.chromium.launch(**launch_args)
            context = browser.new_context(**context_options)

        # IMPORTANT: avec un VRAI navigateur (channel="chrome"/"msedge"), on ne
        # falsifie NI les client-hints Sec-CH-UA NI le fingerprint via stealth.
        if not browser_channel:
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

        # Cookies:
        # - mode sticky: on n'injecte PAS le fichier global tiktok_cookies.json
        #   (il a ete cree sous une autre IP). On peut injecter cookies.json
        #   LOCAL au profil sticky s'il existe (produit par warm_sticky_session).
        # - mode classique: injection globale si pas de profil OU force.
        if use_sticky and sticky_identity:
            sticky_cookies = sticky_sessions.load_sticky_cookies(sticky_identity)
            if sticky_cookies:
                try:
                    context.add_cookies(sticky_cookies)
                except Exception:
                    LOGGER.warning("Failed to inject sticky cookies", exc_info=True)
        else:
            force_cookie_injection = _env_bool("TIKTOK_FORCE_COOKIE_INJECTION", False)
            if not user_data_dir or force_cookie_injection:
                cookies = load_cookies()
                if cookies:
                    try:
                        context.add_cookies(cookies)
                    except Exception:
                        LOGGER.warning("Failed to inject cookies", exc_info=True)

        page = context.new_page()
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
        _open_context_and_page()

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

        grid_ready = _warmup_and_open_profile(
            page,
            profile_url,
            sticky_identity=sticky_identity if use_sticky else None,
        )

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
                    grid_ready = _warmup_and_open_profile(
                        page,
                        profile_url,
                        sticky_identity=sticky_identity if use_sticky else None,
                    )
                except Exception:
                    LOGGER.warning("Challenge retry warmup failed", exc_info=True)

        # Purge des reponses reseau captees pendant le warmup (home "For You",
        # etc.): a partir d'ici, on ne veut compter QUE les videos du profil
        # cible chargees dans la boucle de scroll ci-dessous.
        network_posts.clear()

        seen = set()

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
            return cards

        # Soft-block (squelette): extract HTML/DOM immediat, puis 1 scroll max.
        if not grid_ready:
            LOGGER.info(
                "Profile grid soft-blocked (no /video/ links); fail-fast extract",
                extra={"url": profile_url},
            )
            max_scroll_iterations = 1
            max_consecutive_empty_scrolls = 1
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
                    "Recovered %d posts from soft-blocked page HTML/DOM",
                    len(all_posts),
                    extra={"url": profile_url},
                )

        # Scroll progressif pour charger davantage de posts.
        iteration = 0
        while iteration < max_scroll_iterations and len(all_posts) < max_posts:
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
            batch = cards + network_posts
            network_posts = []

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
                _warmup_and_open_profile(page, profile_url)  # 4) on recharge la page profil dans ce nouveau contexte
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

            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(1.7, 1.0)

            if _looks_like_tiktok_challenge(page):
                if all_posts:
                    LOGGER.warning("Challenge detected after scroll; stopping current page with partial posts")
                    break
                _save_challenge_artifacts(page)
                return {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}

        # Fallback final si aucune video n'a pu etre extraite via navigateur.
        if not all_posts:
            # Si la grille n'est jamais apparue, un 2e wait de 25s est inutile.
            if grid_ready:
                _wait_for_profile_video_links(page)
            else:
                _wait_for_profile_video_links(page, timeout_ms=max(2000, _env_int("TIKTOK_WAIT_VIDEO_LINKS_SOFT_MS", 5000)))
            _human_pause(0.8, 0.4)
            late_cards = _extract_video_cards(page, profile_url)
            late_html = _extract_posts_from_page_html(page, profile_url)
            for card in late_cards + late_html + network_posts:
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
                return {"posts": http_posts[:max_posts], "total": min(len(http_posts), max_posts), "url": profile_url}
            if _looks_like_tiktok_challenge(page):
                _save_challenge_artifacts(page)
                return {"posts": [], "total": 0, "error": "challenge_detected", "url": profile_url}
            # Aucun challenge reconnu, mais aucune video non plus: capturer
            # quand meme un screenshot/HTML, ce cas etant souvent aussi
            # difficile a diagnostiquer qu'un challenge classique (page vide,
            # redirection silencieuse, geo-restriction du proxy...).
            _save_challenge_artifacts(page, label="no_posts_found")
            return {"posts": [], "total": 0, "error": "no_posts_found", "url": profile_url}

        # Post-traitements: enrichissement detail + analyse IA optionnelle.
        enriched_posts = _enrich_posts_from_video_pages(context, all_posts)
        if analyze_video_content:
            analyzed_posts = _attach_video_analysis(enriched_posts, on_post=None)
        else:
            analyzed_posts = enriched_posts

        warning = "challenge_detected_partial" if _looks_like_tiktok_challenge(page) else None
        return _build_partial_result(analyzed_posts, warning=warning)
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
            return {
                "posts": all_posts,
                "total": len(all_posts),
                "url": profile_url,
                "error": str(e),
                "warning": "exception_with_partial_posts",
            }
        return {"posts": [], "error": str(e), "url": profile_url}
    finally:
        # `context`/`browser` peuvent etre None si une exception est survenue
        # PENDANT une rotation IPv6 (entre la fermeture de l'ancien contexte
        # et l'ouverture du nouveau) - on protege donc ces appels.
        if use_sticky and sticky_identity and context is not None:
            # Persiste les cookies du run. Marque ".warmed" UNIQUEMENT si on a
            # vraiment recupere des videos: sinon on prefererait a tort des
            # sticky soft-blockees (grille vide) au prochain job.
            try:
                cookies = context.cookies()
                sticky_sessions.save_sticky_cookies(sticky_identity, cookies)
                if all_posts:
                    sticky_sessions.mark_session_warmed(sticky_identity, cookies)
            except Exception:
                LOGGER.debug("Failed to persist sticky cookies after run", exc_info=True)
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
        if sticky_session_dir is not None and sticky_lock_held:
            sticky_sessions.touch_session(sticky_session_dir)
            sticky_sessions.release_session_lock(sticky_session_dir)
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


def _auto_warm_sticky_session(page, identity: str) -> None:
    """Chauffe une IP sans cookies valides, puis stocke cookies.json pour CETTE IP.

    Visite TikTok via LE proxy du profil persistant, accepte cookies, scrolle
    legerement pour que Chrome ecrive ttwid/msToken/etc. Pas de login manuel.
    """
    LOGGER.info("Warming cookies for sticky IP (missing/invalid)", extra={"url": identity})
    page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
    _dismiss_cookie_banner(page)
    _human_pause(2.0, 1.0)
    try:
        page.mouse.wheel(0, 1200)
    except Exception:
        pass
    _human_pause(1.5, 0.8)
    try:
        page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=45000)
        _dismiss_cookie_banner(page)
        _human_pause(1.5, 0.8)
    except Exception:
        LOGGER.debug("Auto-warm foryou navigation failed", exc_info=True)
    try:
        cookies = page.context.cookies()
        sticky_sessions.mark_session_warmed(identity, cookies)
        LOGGER.info(
            "Stored warmed cookies for sticky IP",
            extra={"url": f"{identity} cookies={len(cookies)}"},
        )
    except Exception:
        LOGGER.warning("Failed to persist warmed cookies for sticky IP", exc_info=True)
        sticky_sessions.mark_session_warmed(identity, None)


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


def _warmup_and_open_profile(page, profile_url: str, sticky_identity: str | None = None) -> bool:
    """Ouvre le profil: reutilise cookies IP si valides, sinon chauffe + stocke.

    Retourne True si au moins un lien /video/ est apparu (grille hydratee).
    """
    if sticky_identity and sticky_sessions.sticky_enabled():
        if sticky_sessions.has_valid_cookies(sticky_identity):
            LOGGER.info(
                "Reusing valid cookies for sticky IP",
                extra={"url": sticky_identity},
            )
        else:
            # IP connue sans cookies, ou IP nouvelle (slot deja reserve via LRU).
            _auto_warm_sticky_session(page, sticky_identity)

    page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
    _dismiss_cookie_banner(page)
    _human_pause(1.2, 0.8)
    page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    _dismiss_cookie_banner(page)
    links_ok = _wait_for_profile_video_links(page)
    # Souvent le profil est la avec un spinner: la grille hydrate 10-30s plus tard.
    if not links_ok and _profile_has_loading_skeleton(page):
        extra_ms = max(5000, _env_int("TIKTOK_WAIT_VIDEO_LINKS_EXTEND_MS", 20000))
        LOGGER.info(
            "Profile skeleton still loading; extended wait for video grid",
            extra={"url": f"timeout_ms={extra_ms}"},
        )
        try:
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        links_ok = _wait_for_profile_video_links(page, timeout_ms=extra_ms)
        if not links_ok:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                _dismiss_cookie_banner(page)
                links_ok = _wait_for_profile_video_links(page, timeout_ms=extra_ms)
            except Exception:
                LOGGER.debug("Profile reload during hydration wait failed", exc_info=True)
    _human_pause(1.0 if links_ok else 0.4, 0.4)
    return links_ok


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
    # Sample dynamique: N proxies aleatoires hors blacklist (pas toute la liste
    # .env / fichier 20k). `proxy_override` (lanes batch) force UN seul proxy.
    attempts_per_proxy = max(1, _env_int("TIKTOK_PROXY_ATTEMPTS_PER_PROXY", 2))
    if proxy_override is not None:
        proxy_candidates = [proxy_override]
    else:
        proxy_candidates = _select_proxy_candidates()

    # Rotation IPv6 "toutes les N videos" (0 = desactivee). Voir
    # `_rotate_ipv6_identity` et `tools/ipv6_rotating_proxy.py` pour le
    # mecanisme complet. Lu ici (et non code en dur) pour pouvoir l'activer
    # uniquement sur le serveur qui a le proxy IPv6 en place, sans toucher au
    # code sur les autres environnements (ex: poste de dev local).
    rotate_every_n_posts = _env_int("TIKTOK_IPV6_ROTATE_EVERY_N_POSTS", 0)

    with sync_playwright() as p:
        last_result = None
        tried = 0
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
                )
                last_result = result
                error_code = str(result.get("error") or "").strip().lower()
                retryable_soft = _is_retryable_soft_error(error_code)

                if result.get("posts") or not retryable_soft:
                    if result.get("error"):
                        scoped_logger.warning(
                            "Scrape finished with error",
                            extra={"has_partial_posts": bool(result.get("posts"))},
                        )
                    else:
                        scoped_logger.info("Scrape finished successfully")
                    return result

                # Soft failure: retente le MEME proxy SEULEMENT pour erreurs
                # reseau flaky (tunnel/timeout). Soft-block TikTok / auth morte
                # -> rotation immediate (retry meme IP = perte de temps +
                # souvent ERR_TUNNEL juste apres).
                rotate_now = any(
                    tok in error_code
                    for tok in (
                        "no_posts_found",
                        "challenge_detected",
                        "invalid_auth",
                        "err_invalid_auth_credentials",
                    )
                )
                if try_index < attempts_per_proxy and not rotate_now:
                    # Timeout/tunnel flaky: garder les cookies, retenter la meme IP.
                    # N'invalider QUE si auth proxy morte (credentials).
                    if proxy_cfg is not None and sticky_sessions.sticky_enabled():
                        if "invalid_auth" in error_code or "err_invalid_auth_credentials" in error_code:
                            sticky_sessions.invalidate_sticky_cookies(_proxy_identity(proxy_cfg))
                    scoped_logger.warning(
                        "Soft failure on proxy; retrying same proxy",
                        extra={"error": error_code[:120], "url": _describe_proxy(proxy_cfg)},
                    )
                    continue

                # Soft-block / captcha: NE PAS supprimer cookies.json.
                # Les cookies chauffes restent utiles quand l'IP sort du cooldown.
                # Invalider seulement auth morte.
                if proxy_cfg is not None and sticky_sessions.sticky_enabled():
                    if "invalid_auth" in error_code or "err_invalid_auth_credentials" in error_code:
                        sticky_sessions.invalidate_sticky_cookies(_proxy_identity(proxy_cfg))

                if _should_blacklist_error(error_code):
                    _blacklist_proxy(proxy_cfg, error_code)
                scoped_logger.warning(
                    "Soft failure detected, rotating proxy candidate",
                    extra={"error": error_code[:120]},
                )
                break

        # Aucun proxy n'a donne de posts: message clair pour l'API utilisateur.
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
