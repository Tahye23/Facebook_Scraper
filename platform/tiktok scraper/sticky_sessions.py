"""Sessions sticky TikTok: 1 proxy sticky = 1 profil Chrome persistant.

Pourquoi:
  Injecter le meme `tiktok_cookies.json` sur des IP aleatoires declenche le
  soft-block TikTok (grille vide). Ici chaque identite Webshare (`sdwopfmy-N`)
  a son propre dossier de profil; les cookies naissent et vivent avec CETTE IP.

LRU:
  On borne le nombre de dossiers (defaut 50). Avant d'en creer un nouveau, on
  supprime les plus anciens (moins recemment utilises), sauf ceux lockes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="sticky_sessions")
_LOCK = threading.Lock()
_LAST_USED_NAME = ".last_used"
_LOCK_NAME = ".in_use"


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
            # 10 min suffit: un scrape sticky dure rarement plus longtemps.
            # Les locks laisses par un conteneur recreate sont toujours "stale".
            if age < 600:
                try:
                    old_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
                except ValueError:
                    old_pid = 0
                # Si le PID existe encore sur CETTE machine, le profil est vraiment pris.
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
    """Supprime les fichiers Singleton* laisses par un Chrome tue / crash.

    Sans ca, le prochain launch_persistent_context echoue immediatement avec:
    "The profile appears to be in use by another Google Chrome process".
    """
    removed: list[str] = []
    if not session_dir.exists():
        return removed
    names = (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "lockfile",
    )
    for name in names:
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
    """Plus petit = plus ancien / moins utilise (candidat eviction)."""
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


def enforce_lru_limit(exclude: Path | None = None) -> list[str]:
    """Supprime les sessions les plus anciennes si on depasse max_sessions.

    Ne touche pas aux dossiers lockes ni a `exclude` (session qu'on s'apprete
    a utiliser). Retourne les identites effacees.
    """
    limit = max_sessions()
    deleted: list[str] = []
    with _LOCK:
        dirs = list_session_dirs()
        if len(dirs) <= limit:
            return deleted

        # Plus ancien d'abord.
        ranked = sorted(dirs, key=_session_score)
        for session_dir in ranked:
            if len(list_session_dirs()) <= limit:
                break
            if exclude is not None and session_dir.resolve() == exclude.resolve():
                continue
            if _lock_path(session_dir).exists():
                continue
            try:
                # rmtree manuel pour eviter d'importer shutil en haut si inutile
                import shutil

                shutil.rmtree(session_dir, ignore_errors=False)
                deleted.append(session_dir.name)
                LOGGER.info(
                    "LRU evicted sticky session",
                    extra={"url": session_dir.name},
                )
            except OSError:
                LOGGER.warning("Failed to evict sticky session", exc_info=True)
    return deleted


def prepare_session_dir(identity: str) -> Path:
    """Cree/touche le dossier sticky, applique le LRU, retourne le chemin."""
    session_dir = session_dir_for_identity(identity)
    sessions_root().mkdir(parents=True, exist_ok=True)
    enforce_lru_limit(exclude=session_dir if session_dir.exists() else None)
    session_dir.mkdir(parents=True, exist_ok=True)
    touch_session(session_dir)
    return session_dir


def is_cold_session(identity: str) -> bool:
    """True si le profil sticky n'a encore jamais ete chauffe (pas de cookies)."""
    cookies = cookies_path_for_identity(identity)
    try:
        if cookies.exists() and cookies.stat().st_size > 80:
            return False
    except OSError:
        pass
    session_dir = session_dir_for_identity(identity)
    warmed_marker = session_dir / ".warmed"
    if warmed_marker.exists():
        return False
    # Apres un 1er run Chrome, le sous-dossier Default apparait avec des cookies.
    default_dir = session_dir / "Default"
    chrome_cookie_candidates = [
        default_dir / "Cookies",
        default_dir / "Network" / "Cookies",
    ]
    for candidate in chrome_cookie_candidates:
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                return False
        except OSError:
            continue
    return True


def mark_session_warmed(identity: str, cookies: list[dict] | None = None) -> None:
    """Persiste les cookies du navigateur + touch LRU apres un chauffe auto."""
    session_dir = prepare_session_dir(identity)
    if cookies:
        try:
            save_sticky_cookies(identity, cookies)
        except OSError:
            LOGGER.warning("Failed to persist auto-warm cookies", exc_info=True)
    marker = session_dir / ".warmed"
    try:
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    touch_session(session_dir)


def warmed_identities() -> set[str]:
    """Identites sticky qui ont deja un profil non vide / marque chauffe."""
    result = set()
    for p in list_session_dirs():
        if (p / ".warmed").exists() or (p / "cookies.json").exists():
            result.add(p.name)
            continue
        if any(p.iterdir()):
            # Profil Chrome deja cree (Default/...) meme sans marqueur.
            result.add(p.name)
    return result


def cookies_path_for_identity(identity: str) -> Path:
    """Fichier cookies optionnel lie a UNE sticky (warmup / export)."""
    return session_dir_for_identity(identity) / "cookies.json"


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
    """Persiste un export cookies dans le dossier sticky."""
    session_dir = prepare_session_dir(identity)
    path = session_dir / "cookies.json"
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    touch_session(session_dir)
    return path
