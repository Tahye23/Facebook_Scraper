"""Identity Pool minimal — mapping session sticky → statut (Phase 1).

Pas de pool de fingerprints pre-genere. Une identite =
  identity_id = "{country}-{session_id}"
  proxy username = "{base}-{country}-{session_id}"

Statuts: virgin | warm | active | quarantine | dead
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="identity_pool")

_LOCK = threading.Lock()
_DEFAULT_FILE = "identity_pool.json"

Status = str  # virgin|warm|active|quarantine|dead


@dataclass
class Identity:
    identity_id: str
    base_user: str
    country: str
    session_id: str
    status: Status = "virgin"
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    bandwidth_used_mb: float = 0.0
    failure_history: list[str] = field(default_factory=list)
    proxy_username: str = ""
    in_use: bool = False  # checkout court (evite double-assign dans un job)
    # Epoch seconds; 0 = pas de cooldown. Skip checkout si > now.
    quarantine_until: float = 0.0
    # Soft-blocks successifs sans succes entre-temps; 2+ → status=dead.
    consecutive_soft_blocks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Identity":
        return cls(
            identity_id=str(raw.get("identity_id") or ""),
            base_user=str(raw.get("base_user") or ""),
            country=str(raw.get("country") or "us").lower(),
            session_id=str(raw.get("session_id") or ""),
            status=str(raw.get("status") or "virgin"),
            created_at=float(raw.get("created_at") or time.time()),
            last_used_at=float(raw.get("last_used_at") or time.time()),
            bandwidth_used_mb=float(raw.get("bandwidth_used_mb") or 0.0),
            failure_history=list(raw.get("failure_history") or [])[-20:],
            proxy_username=str(raw.get("proxy_username") or ""),
            in_use=bool(raw.get("in_use") or False),
            quarantine_until=float(raw.get("quarantine_until") or 0.0),
            consecutive_soft_blocks=max(0, int(raw.get("consecutive_soft_blocks") or 0)),
        )


def _pool_path() -> Path:
    raw = (os.getenv("TIKTOK_IDENTITY_POOL_FILE") or _DEFAULT_FILE).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _quarantine_hours() -> float:
    """Duree avant quarantine status → dead (hard block)."""
    return max(1.0, _env_float("TIKTOK_IDENTITY_QUARANTINE_HOURS", 24.0))


def _soft_cooldown_hours() -> float:
    """Cooldown reuse apres soft_block / structural_change (defaut 2h)."""
    return max(0.1, _env_float("TIKTOK_QUARANTINE_COOLDOWN_HOURS", 2.0))


def _load() -> dict[str, Any]:
    path = _pool_path()
    if not path.exists():
        return {"identities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("identities", {})
            return data
    except Exception:
        LOGGER.debug("identity_pool load failed", exc_info=True)
    return {"identities": {}}


def _save(store: dict[str, Any]) -> None:
    path = _pool_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        LOGGER.debug("identity_pool save failed", exc_info=True)


def enabled() -> bool:
    raw = os.getenv("TIKTOK_IDENTITY_POOL_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def make_identity_id(country: str, session_id: str) -> str:
    return f"{(country or 'us').strip().lower()}-{str(session_id).strip()}"


def register_identity(
    *,
    base_user: str,
    country: str,
    session_id: str,
    status: Status = "virgin",
) -> Identity:
    """Enregistre (ou met a jour) une sticky session dans le pool.

    Ne ressuscite JAMAIS une identite `dead` (sticky brulee) — le caller doit
    generer un nouveau session_id.
    """
    cc = (country or "us").strip().lower()
    sid = str(session_id).strip()
    iid = make_identity_id(cc, sid)
    username = f"{base_user}-{cc}-{sid}"
    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        existing = identities.get(iid)
        if existing:
            ident = Identity.from_dict(existing)
            if ident.status == "dead":
                LOGGER.warning(
                    "[IDENTITY_POOL] refuse resurrect dead %s — keep dead",
                    iid,
                )
                return ident
            ident.last_used_at = time.time()
            identities[iid] = ident.to_dict()
            _save(store)
            return ident
        ident = Identity(
            identity_id=iid,
            base_user=base_user,
            country=cc,
            session_id=sid,
            status=status,
            proxy_username=username,
        )
        identities[iid] = ident.to_dict()
        _save(store)
        LOGGER.info(
            "[IDENTITY_POOL] registered %s status=%s",
            iid,
            status,
        )
        return ident


def acquire_for_country(
    country: str,
    *,
    base_user: str,
    prefer_warm: bool = True,
) -> Identity | None:
    """Retourne une identite warm/active reutilisable pour ce pays, ou None.

    Skip: in_use, cooldown, dead, quarantine.
    """
    if not enabled():
        return None
    cc = (country or "").strip().lower()
    if not cc:
        return None
    now = time.time()
    q_secs = _quarantine_hours() * 3600.0

    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        # Expire quarantine → dead
        for key, raw in list(identities.items()):
            ident = Identity.from_dict(raw)
            if ident.status == "quarantine":
                if now - float(ident.last_used_at) > q_secs:
                    ident.status = "dead"
                    identities[key] = ident.to_dict()
        candidates: list[Identity] = []
        skipped_cooldown = 0
        for raw in identities.values():
            ident = Identity.from_dict(raw)
            if ident.country != cc:
                continue
            if ident.status == "dead":
                continue
            if ident.in_use:
                continue
            if float(ident.quarantine_until or 0.0) > now:
                skipped_cooldown += 1
                continue
            if prefer_warm and ident.status in ("warm", "active"):
                candidates.append(ident)
        if skipped_cooldown:
            LOGGER.info(
                "[IDENTITY_POOL] skipped %d identities still in cooldown for country=%s",
                skipped_cooldown,
                cc,
            )
        if not candidates:
            _save(store)
            return None
        # LRU: plus ancienne last_used d'abord (repartir la charge).
        candidates.sort(key=lambda i: i.last_used_at)
        chosen = candidates[0]
        chosen.last_used_at = now
        chosen.in_use = True
        identities[chosen.identity_id] = chosen.to_dict()
        _save(store)
        LOGGER.info(
            "[IDENTITY_POOL] reuse+checkout %s status=%s consecutive_soft_blocks=%s",
            chosen.identity_id,
            chosen.status,
            chosen.consecutive_soft_blocks,
        )
        return chosen


def create_virgin(
    *,
    base_user: str,
    country: str,
    session_id: str | None = None,
) -> Identity:
    """Cree une sticky virgin. Si collision avec un dead, regenere un nouvel id."""
    for _ in range(8):
        sid = session_id or str(random.randint(10_000_000, 99_999_999))
        ident = register_identity(
            base_user=base_user,
            country=country,
            session_id=sid,
            status="virgin",
        )
        if ident.status != "dead":
            return ident
        # Collision avec un dead: forcer un nouvel id aleatoire.
        session_id = None
    # Dernier recours (extremement improbable).
    sid = str(random.randint(10_000_000, 99_999_999))
    return register_identity(
        base_user=base_user,
        country=country,
        session_id=sid,
        status="virgin",
    )


def apply_classification(
    identity_id: str,
    scrape_class: str,
    *,
    success: bool = False,
) -> Identity | None:
    """Met a jour le cycle de vie selon la classe Response Classifier.

    Soft-block:
      1er → warm + cooldown (TIKTOK_QUARANTINE_COOLDOWN_HOURS)
      2e consecutif (sans succes entre-temps) → dead (plus jamais auto-reuse)
    Succes → consecutive_soft_blocks=0, status=active.
    """
    if not enabled() or not identity_id:
        return None
    cls = (scrape_class or "unknown").strip().lower()
    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        raw = identities.get(identity_id)
        if not raw:
            # tenter match par proxy_username prefix
            for key, row in identities.items():
                if str(row.get("proxy_username") or "") == identity_id or key in identity_id:
                    raw = row
                    identity_id = key
                    break
        if not raw:
            return None
        ident = Identity.from_dict(raw)
        now = time.time()
        ident.last_used_at = now
        ident.in_use = False  # release checkout
        if success or cls == "success":
            ident.status = "active"
            ident.quarantine_until = 0.0
            ident.consecutive_soft_blocks = 0
        else:
            hist = list(ident.failure_history or [])
            hist.append(cls)
            ident.failure_history = hist[-20:]
            if cls in ("hard_block", "auth", "rate_limited"):
                ident.status = "quarantine"
                ident.quarantine_until = now + (_quarantine_hours() * 3600.0)
            elif cls == "proxy_infra":
                # garder warm/virgin — retry same identity recommande
                if ident.status not in ("quarantine", "dead"):
                    ident.status = "warm" if ident.status == "active" else ident.status
            elif cls == "soft_block":
                ident.consecutive_soft_blocks = int(ident.consecutive_soft_blocks or 0) + 1
                if ident.consecutive_soft_blocks >= 2:
                    ident.status = "dead"
                    ident.quarantine_until = 0.0
                    LOGGER.warning(
                        "[IDENTITY_POOL] %s → status=dead after consecutive_soft_blocks=%s",
                        ident.identity_id,
                        ident.consecutive_soft_blocks,
                    )
                else:
                    if ident.status == "virgin":
                        ident.status = "warm"
                    elif ident.status == "active":
                        ident.status = "warm"
                    ident.quarantine_until = now + (_soft_cooldown_hours() * 3600.0)
                    LOGGER.info(
                        "[IDENTITY_POOL] cooldown %s until +%.1fh after class=%s "
                        "(consecutive_soft_blocks=%s)",
                        ident.identity_id,
                        _soft_cooldown_hours(),
                        cls,
                        ident.consecutive_soft_blocks,
                    )
            elif cls in ("structural_change", "platform_change_suspected"):
                # Soft cooldown reuse (pas status=quarantine dure pour structural).
                if ident.status == "virgin":
                    ident.status = "warm"
                elif ident.status == "active":
                    ident.status = "warm"
                ident.quarantine_until = now + (_soft_cooldown_hours() * 3600.0)
                LOGGER.info(
                    "[IDENTITY_POOL] cooldown %s until +%.1fh after class=%s",
                    ident.identity_id,
                    _soft_cooldown_hours(),
                    cls,
                )
            elif cls == "attempt_timeout":
                if ident.status == "active":
                    ident.status = "warm"
            elif cls == "unknown":
                pass
        identities[ident.identity_id] = ident.to_dict()
        _save(store)
        LOGGER.info(
            "[IDENTITY_POOL] %s → status=%s quarantine_until=%.0f consecutive_soft_blocks=%s after class=%s",
            ident.identity_id,
            ident.status,
            float(ident.quarantine_until or 0.0),
            int(ident.consecutive_soft_blocks or 0),
            cls,
        )
        return ident


def add_bandwidth_mb(identity_id: str, mb: float) -> None:
    if not identity_id or mb <= 0:
        return
    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        raw = identities.get(identity_id)
        if not raw:
            return
        ident = Identity.from_dict(raw)
        ident.bandwidth_used_mb = float(ident.bandwidth_used_mb or 0.0) + float(mb)
        identities[identity_id] = ident.to_dict()
        _save(store)


def mark_warm(identity_id: str) -> None:
    if not identity_id:
        return
    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        raw = identities.get(identity_id)
        if not raw:
            return
        ident = Identity.from_dict(raw)
        if ident.status not in ("quarantine", "dead"):
            ident.status = "warm"
            ident.last_used_at = time.time()
            identities[identity_id] = ident.to_dict()
            _save(store)


def dead_identities(*, purge: bool = False) -> list[str]:
    """Liste (et optionnellement marque) les identites dead."""
    now = time.time()
    q_secs = _quarantine_hours() * 3600.0
    dead: list[str] = []
    with _LOCK:
        store = _load()
        identities = store.setdefault("identities", {})
        for key, raw in list(identities.items()):
            ident = Identity.from_dict(raw)
            if ident.status == "quarantine" and now - ident.last_used_at > q_secs:
                ident.status = "dead"
                identities[key] = ident.to_dict()
            if ident.status == "dead":
                dead.append(ident.identity_id)
                if purge:
                    del identities[key]
        _save(store)
    return dead


def identity_id_from_proxy_username(username: str) -> str:
    """sdwopfmy-fr-482910 → fr-482910."""
    parts = (username or "").strip().split("-")
    if len(parts) >= 3 and len(parts[1]) == 2 and parts[1].isalpha():
        return f"{parts[1].lower()}-{parts[2]}"
    return (username or "").strip()


def summary_by_status() -> dict[str, int]:
    with _LOCK:
        store = _load()
    counts: dict[str, int] = {}
    for raw in (store.get("identities") or {}).values():
        st = str((raw or {}).get("status") or "unknown")
        counts[st] = counts.get(st, 0) + 1
    return counts
