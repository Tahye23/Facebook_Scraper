"""Sessions sticky TikTok: 1 adresse IP (sticky Webshare) = 1 slot cookies.

Modele (slots LRU, defaut 50):
  - Chaque IP `sdwopfmy-N` a son dossier `tiktok_sessions/<id>/` + cookies.json
  - Si on prend une IP qui a deja des cookies valides -> on les reutilise
  - Si l'IP existe mais cookies absents/invalides -> on chauffe et on remplace
    les cookies DE CETTE IP
  - Si l'IP est nouvelle et le plafond de slots est atteint -> on supprime le
    slot le plus ancien (LRU / .last_used), on chauffe la nouvelle IP, on
    stocke ses cookies a la place
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="sticky_sessions")
_LOCK = threading.Lock()
_LAST_USED_NAME = ".last_used"
_LOCK_NAME = ".in_use"
_WARMED_NAME = ".warmed"
_WAF_CHALLENGED_NAME = ".waf_challenged"
_LAST_RESULT_NAME = ".last_result.json"
# Dedup purge/WAF logs: 1 action / identity / fenetre courte.
_PURGE_ONCE_TS: dict[str, float] = {}
_WAF_ONCE_TS: dict[str, float] = {}
_ACTION_DEDUP_S = 3.0

# Noms de cookies TikTok qui indiquent une session exploitable.
_VALID_COOKIE_NAMES = frozenset(
    {
        "ttwid",
        "msToken",
        "tt_chain_token",
        "sessionid",
        "sid_tt",
        "sid_guard",
    }
)


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


def sticky_enabled() -> bool:
    """Active par defaut: profil persistant lie au proxy sticky."""
    return _env_bool("TIKTOK_STICKY_SESSIONS_ENABLED", True)


def sessions_root() -> Path:
    """Racine des profils sticky (volume Docker recommande)."""
    raw = (os.getenv("TIKTOK_STICKY_SESSIONS_DIR") or "tiktok_sessions").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def max_sessions() -> int:
    return max(1, _env_int("TIKTOK_STICKY_MAX_SESSIONS", 50))


def safe_identity(identity: str) -> str:
    """Nom de dossier safe (username sticky Webshare)."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (identity or "").strip())
    return cleaned or "direct"


def session_dir_for_identity(identity: str) -> Path:
    return sessions_root() / safe_identity(identity)


def session_exists(identity: str) -> bool:
    path = session_dir_for_identity(identity)
    return path.is_dir() and any(path.iterdir())


def cookies_path_for_identity(identity: str) -> Path:
    """Fichier cookies lie a UNE sticky (warmup / export)."""
    return session_dir_for_identity(identity) / "cookies.json"


def _last_used_path(session_dir: Path) -> Path:
    return session_dir / _LAST_USED_NAME


def _lock_path(session_dir: Path) -> Path:
    return session_dir / _LOCK_NAME


def touch_session(session_dir: Path) -> None:
    """Met a jour l'horodatage d'utilisation (pour le LRU)."""
    session_dir.mkdir(parents=True, exist_ok=True)
    marker = _last_used_path(session_dir)
    now = str(time.time())
    try:
        marker.write_text(now, encoding="utf-8")
    except OSError:
        LOGGER.warning("Failed to touch sticky session marker", extra={"url": str(session_dir)})


def acquire_session_lock(session_dir: Path) -> bool:
    """Pose un lock soft pour eviter 2 Chrome sur le meme profil.

    Si le lock est stale (process mort / ancien conteneur), on le reprend.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(session_dir)
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < 600:
                try:
                    old_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
                except ValueError:
                    old_pid = 0
                if old_pid and _pid_alive(old_pid):
                    return False
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def release_session_lock(session_dir: Path) -> None:
    lock = _lock_path(session_dir)
    try:
        if lock.exists():
            lock.unlink()
    except OSError:
        pass


def clear_chrome_profile_locks(session_dir: Path) -> list[str]:
    """Supprime les fichiers Singleton* laisses par un Chrome tue / crash."""
    removed: list[str] = []
    if not session_dir.exists():
        return removed
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        path = session_dir / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
                removed.append(name)
        except OSError:
            LOGGER.debug("Failed to remove chrome lock %s", name, exc_info=True)
    return removed


def is_session_busy(identity: str) -> bool:
    """True si une autre lane/job tient deja ce profil sticky."""
    session_dir = session_dir_for_identity(identity)
    lock = _lock_path(session_dir)
    if not lock.exists():
        return False
    try:
        age = time.time() - lock.stat().st_mtime
        if age >= 600:
            return False
        old_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    return bool(old_pid and _pid_alive(old_pid))


def _session_score(session_dir: Path) -> float:
    """Plus petit = plus ancien / moins utilise (candidat eviction LRU)."""
    marker = _last_used_path(session_dir)
    try:
        if marker.exists():
            return float(marker.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        pass
    try:
        return session_dir.stat().st_mtime
    except OSError:
        return 0.0


def list_session_dirs() -> list[Path]:
    root = sessions_root()
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def has_valid_cookies(identity: str) -> bool:
    """True si cette IP a un `cookies.json` TikTok exploitable.

    IMPORTANT: ne PAS se fier au fichier SQLite Chrome `Default/Cookies`.
    Chrome le cree vide des le launch_persistent_context -> faux positif
    "Reusing valid cookies" sur une IP toute neuve, sans jamais chauffer.
    """
    cookies_path = cookies_path_for_identity(identity)
    try:
        if not cookies_path.exists() or cookies_path.stat().st_size <= 80:
            return False
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(raw, list) or not raw:
        return False
    names = {str(c.get("name") or "") for c in raw if isinstance(c, dict)}
    # ttwid / msToken = signaux reels d'une session TikTok chauffee.
    return bool(names & _VALID_COOKIE_NAMES)


def invalidate_sticky_cookies(identity: str) -> None:
    """Supprime cookies.json + .warmed pour forcer un re-warm au prochain essai."""
    session_dir = session_dir_for_identity(identity)
    for name in ("cookies.json", _WARMED_NAME):
        path = session_dir / name
        try:
            if path.exists():
                path.unlink()
        except OSError:
            LOGGER.debug("Failed to invalidate sticky cookie file %s", name, exc_info=True)
    LOGGER.info("Invalidated sticky cookies for IP", extra={"url": safe_identity(identity)})


def mark_attempt_result(identity: str, *, success: bool, reason: str = "") -> None:
    """Enregistre le dernier resultat scrape pour cette identite sticky."""
    if not identity or identity == "direct":
        return
    session_dir = ensure_slot_for_identity(identity)
    path = session_dir / _LAST_RESULT_NAME
    try:
        path.write_text(
            json.dumps(
                {
                    "success": bool(success),
                    "ts": time.time(),
                    "reason": (reason or "")[:240],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        LOGGER.debug("Failed to write last_result for sticky", exc_info=True)


def last_attempt_succeeded(identity: str) -> bool | None:
    """True/False si un .last_result.json existe, sinon None."""
    if not identity:
        return None
    path = session_dir_for_identity(identity) / _LAST_RESULT_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or "success" not in raw:
        return None
    return bool(raw.get("success"))


def should_reuse_sticky_cookies(identity: str) -> bool:
    """True seulement si cookies valides ET dernier statut != success=False.

    TIKTOK_REUSE_STICKY_COOKIES ne doit PAS reutiliser une session dont le
    dernier essai a echoue (soft-block / HTTP / timeout).
    """
    if not identity or identity == "direct":
        return False
    if not has_valid_cookies(identity):
        return False
    if is_waf_challenged(identity):
        return False
    last = last_attempt_succeeded(identity)
    if last is False:
        return False
    return True


def purge_sticky_cookies(
    identity: str,
    *,
    reason: str = "",
    mark_waf: bool = False,
) -> None:
    """Purge immediate cookies + profil Chrome → etat VIRGIN pour le prochain essai.

    `mark_waf=True` UNIQUEMENT pour un vrai blocage (captcha / 403 / 429 /
    chrome-error). Un profil vide HTTP 200 ne doit PAS etre WAF-challenged.

    Dedup: une seule purge / identity dans une fenetre de ~3s (evite cascades
    de logs depuis navigate + finally + outer loop).
    """
    if not identity or identity == "direct":
        return
    key = safe_identity(identity)
    now = time.time()
    last = _PURGE_ONCE_TS.get(key, 0.0)
    if now - last < _ACTION_DEDUP_S:
        LOGGER.debug(
            "Skipping duplicate purge_sticky_cookies for %s (%.1fs ago)",
            key,
            now - last,
        )
        return
    _PURGE_ONCE_TS[key] = now

    mark_attempt_result(identity, success=False, reason=reason or "purged")
    if mark_waf:
        try:
            mark_waf_challenged(identity, reason=reason or "purged")
        except Exception:
            pass
    invalidate_sticky_cookies(identity)
    reset_session_to_virgin(identity)
    LOGGER.info(
        "Purged sticky cookies → VIRGIN (mark_waf=%s)",
        mark_waf,
        extra={"url": key, "error": (reason or "")[:120]},
    )


def purge_session(identity: str) -> bool:
    """Supprime entierement le dossier sticky d'une IP compromise (soft-block / HTTP)."""
    if not identity or identity == "direct":
        return False
    session_dir = session_dir_for_identity(identity)
    if not session_dir.exists():
        return False
    try:
        release_session_lock(session_dir)
    except Exception:
        pass
    try:
        shutil.rmtree(session_dir, ignore_errors=False)
        LOGGER.info(
            "Purged sticky session for compromised IP",
            extra={"url": safe_identity(identity)},
        )
        return True
    except OSError:
        LOGGER.warning(
            "Failed to purge sticky session",
            extra={"url": safe_identity(identity)},
            exc_info=True,
        )
        return False


def reset_session_to_virgin(identity: str) -> Path:
    """Recree un profil sticky VIERGE (pas de cookies persistants / Chrome vide).

    Utilise pour l'IP suivante apres un blocage: le contexte Playwright ne doit
    PAS recharger d'anciens cookies d'une session compromise.
    """
    session_dir = ensure_slot_for_identity(identity)
    # Supprimer cookies exportes + marqueurs.
    for name in ("cookies.json", _WARMED_NAME):
        path = session_dir / name
        try:
            if path.exists():
                path.unlink()
        except OSError:
            LOGGER.debug("Failed to remove %s during virgin reset", name, exc_info=True)

    # Nettoyer le profil Chrome persistant (Cookies SQLite, Local Storage, etc.)
    # sans supprimer le dossier sticky (locks / structure).
    for child_name in ("Default", "ShaderCache", "GrShaderCache", "GraphiteDawnCache"):
        child = session_dir / child_name
        if child.exists():
            try:
                shutil.rmtree(child, ignore_errors=True)
            except OSError:
                LOGGER.debug("Failed to wipe Chrome subdir %s", child_name, exc_info=True)

    clear_chrome_profile_locks(session_dir)
    touch_session(session_dir)
    LOGGER.info(
        "Sticky session reset to VIRGIN profile",
        extra={"url": safe_identity(identity)},
    )
    return session_dir


def identities_with_valid_cookies() -> set[str]:
    """Identites sticky qui ont un slot cookies valide."""
    return {p.name for p in list_session_dirs() if has_valid_cookies(p.name)}


def _can_evict(session_dir: Path, exclude: Path | None) -> bool:
    if exclude is not None and session_dir.resolve() == exclude.resolve():
        return False
    if _lock_path(session_dir).exists():
        return False
    return True


def enforce_lru_limit(exclude: Path | None = None) -> list[str]:
    """Si on depasse max_sessions, supprime le(s) slot(s) le(s) plus ancien(s).

    Regle utilisateur: le plus ancien (`.last_used`) part pour faire de la place
    a une nouvelle IP. On ne touche pas aux dossiers lockes ni a `exclude`.
    """
    limit = max_sessions()
    deleted: list[str] = []
    with _LOCK:
        while True:
            dirs = list_session_dirs()
            if len(dirs) <= limit:
                break
            ranked = sorted(
                [d for d in dirs if _can_evict(d, exclude)],
                key=_session_score,
            )
            if not ranked:
                break
            session_dir = ranked[0]
            try:
                shutil.rmtree(session_dir, ignore_errors=False)
                deleted.append(session_dir.name)
                LOGGER.info(
                    "LRU evicted oldest sticky cookie slot",
                    extra={"url": session_dir.name},
                )
            except OSError:
                LOGGER.warning("Failed to evict sticky session", exc_info=True)
                break
    return deleted


def ensure_slot_for_identity(identity: str) -> Path:
    """Reserve le slot cookies pour cette IP (cree ou reutilise).

    - IP deja connue: reutilise son dossier, met a jour `.last_used`
    - IP nouvelle + plafond atteint: evince le slot le plus ancien, puis cree
    """
    session_dir = session_dir_for_identity(identity)
    sessions_root().mkdir(parents=True, exist_ok=True)
    is_new = not session_dir.exists()

    with _LOCK:
        if is_new:
            # Liberer 1 place AVANT creation si on est deja au max.
            while len(list_session_dirs()) >= max_sessions():
                ranked = sorted(
                    [d for d in list_session_dirs() if _can_evict(d, None)],
                    key=_session_score,
                )
                if not ranked:
                    break
                victim = ranked[0]
                try:
                    shutil.rmtree(victim, ignore_errors=False)
                    LOGGER.info(
                        "LRU replaced oldest cookie slot for new IP",
                        extra={"url": f"evicted={victim.name} new={safe_identity(identity)}"},
                    )
                except OSError:
                    LOGGER.warning("Failed to evict oldest sticky for new IP", exc_info=True)
                    break
        session_dir.mkdir(parents=True, exist_ok=True)
        touch_session(session_dir)

    # Filet de securite si d'autres process ont cree des slots en parallele.
    enforce_lru_limit(exclude=session_dir)
    return session_dir


def prepare_session_dir(identity: str) -> Path:
    """Alias public: prepare le slot cookies pour l'IP (LRU si nouvelle)."""
    return ensure_slot_for_identity(identity)


def is_cold_session(identity: str) -> bool:
    """True si cette IP n'a pas encore de cookies valides -> il faut chauffer."""
    return not has_valid_cookies(identity)


def mark_session_warmed(identity: str, cookies: list[dict] | None = None) -> None:
    """Persiste les cookies chauffes pour CETTE IP + marque `.warmed`.

    N'ecrit `.warmed` que si cookies.json contient un signal TikTok (ttwid...).
    """
    session_dir = ensure_slot_for_identity(identity)
    names: set[str] = set()
    if cookies:
        try:
            path = session_dir / "cookies.json"
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            names = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
        except OSError:
            LOGGER.warning("Failed to persist auto-warm cookies", exc_info=True)
    if names & _VALID_COOKIE_NAMES:
        marker = session_dir / _WARMED_NAME
        try:
            marker.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        # Nouvelle chauffe valide: effacer ancien marqueur WAF.
        waf = session_dir / _WAF_CHALLENGED_NAME
        try:
            if waf.exists():
                waf.unlink()
        except OSError:
            pass
    touch_session(session_dir)


def mark_waf_challenged(identity: str, reason: str = "") -> None:
    """Marque la session IP comme soft-block / challenge silencieux Akamai.

    Dedup: un seul log WARNING / identity dans ~3s.
    """
    if not identity:
        return
    key = safe_identity(identity)
    now = time.time()
    last = _WAF_ONCE_TS.get(key, 0.0)
    already = (session_dir_for_identity(identity) / _WAF_CHALLENGED_NAME).exists()
    if already and now - last < _ACTION_DEDUP_S:
        return
    _WAF_ONCE_TS[key] = now

    session_dir = ensure_slot_for_identity(identity)
    path = session_dir / _WAF_CHALLENGED_NAME
    try:
        path.write_text(
            json.dumps({"ts": time.time(), "reason": (reason or "")[:200]}, ensure_ascii=False),
            encoding="utf-8",
        )
        if now - last >= _ACTION_DEDUP_S:
            LOGGER.warning(
                "Sticky session marked WAF-challenged",
                extra={"url": key, "error": (reason or "")[:120]},
            )
    except OSError:
        LOGGER.debug("Failed to write WAF-challenged marker", exc_info=True)


def is_waf_challenged(identity: str) -> bool:
    if not identity:
        return False
    return (session_dir_for_identity(identity) / _WAF_CHALLENGED_NAME).exists()


def proven_warmed_identities() -> set[str]:
    """Identites avec marqueur `.warmed` (chauffe ou scrape reussi)."""
    return {p.name for p in list_session_dirs() if (p / _WARMED_NAME).exists()}


def warmed_identities() -> set[str]:
    """Compat: identites avec cookies valides ou profil non vide."""
    return identities_with_valid_cookies() | {
        p.name for p in list_session_dirs() if any(p.iterdir())
    }


def load_sticky_cookies(identity: str) -> list[dict]:
    """Charge cookies.json du profil sticky s'il existe (sinon [])."""
    path = cookies_path_for_identity(identity)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to read sticky cookies", extra={"url": str(path)}, exc_info=True)
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
                pass
        cookies.append(cookie)
    return cookies


def save_sticky_cookies(identity: str, cookies: list[dict]) -> Path:
    """Persiste un export cookies dans le dossier sticky de CETTE IP."""
    session_dir = ensure_slot_for_identity(identity)
    path = session_dir / "cookies.json"
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    touch_session(session_dir)
    return path
