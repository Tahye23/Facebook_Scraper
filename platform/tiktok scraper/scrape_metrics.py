"""Metriques legeres scrape TikTok (Phase 0/4 MVP).

Persistance JSON locale — pas de stack observability externe.
Suivi:
  - succes / echecs par pays sticky
  - compteur classes (soft_block, structural_change, …)
  - alerte derive globale si succes chute sur >=3 pays
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="scrape_metrics")

_LOCK = threading.Lock()
_DEFAULT_FILE = "scrape_metrics.json"


def _metrics_path() -> Path:
    raw = (os.getenv("TIKTOK_SCRAPE_METRICS_FILE") or _DEFAULT_FILE).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _empty_store() -> dict[str, Any]:
    return {
        "updated_at": None,
        "by_country": {},
        "by_class": {},
        "bandwidth_mb_total": 0.0,
        "bandwidth_by_country": {},
        "recent": [],  # last N attempts
        "alerts": [],
    }


def _load() -> dict[str, Any]:
    path = _metrics_path()
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("by_country", {})
            data.setdefault("by_class", {})
            data.setdefault("recent", [])
            data.setdefault("alerts", [])
            return data
    except Exception:
        LOGGER.debug("scrape_metrics load failed", exc_info=True)
    return _empty_store()


def _save(store: dict[str, Any]) -> None:
    path = _metrics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        store["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        LOGGER.debug("scrape_metrics save failed", exc_info=True)


def record_attempt(
    *,
    country: str,
    scrape_class: str,
    success: bool,
    identity: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Enregistre une tentative et retourne un event d'alerte eventuel."""
    cc = (country or "xx").strip().lower()[:8] or "xx"
    cls = (scrape_class or "unknown").strip().lower()
    alert: dict[str, Any] | None = None

    with _LOCK:
        store = _load()
        by_cc = store["by_country"].setdefault(
            cc, {"success": 0, "fail": 0, "classes": {}}
        )
        if success:
            by_cc["success"] = int(by_cc.get("success") or 0) + 1
        else:
            by_cc["fail"] = int(by_cc.get("fail") or 0) + 1
        classes = by_cc.setdefault("classes", {})
        classes[cls] = int(classes.get(cls) or 0) + 1

        store["by_class"][cls] = int(store["by_class"].get(cls) or 0) + 1

        recent = store.setdefault("recent", [])
        recent.append(
            {
                "ts": time.time(),
                "country": cc,
                "class": cls,
                "success": success,
                "identity": (identity or "")[:80],
                "reason": (reason or "")[:120],
            }
        )
        # garder fenetre glissante ~200
        if len(recent) > 200:
            del recent[:-200]

        alert = _maybe_drift_alert(store)
        if alert:
            store.setdefault("alerts", []).append(alert)
            if len(store["alerts"]) > 50:
                del store["alerts"][:-50]

        _save(store)

    if alert:
        LOGGER.warning(
            "[METRICS_ALERT] %s — %s",
            alert.get("type"),
            alert.get("message"),
        )
    return alert or {}


def _maybe_drift_alert(store: dict[str, Any]) -> dict[str, Any] | None:
    """Alerte si echec soft/structural sur >=3 pays dans la fenetre recente."""
    window_s = max(300, int(os.getenv("TIKTOK_METRICS_DRIFT_WINDOW_S") or "3600"))
    min_countries = max(2, int(os.getenv("TIKTOK_METRICS_DRIFT_MIN_COUNTRIES") or "3"))
    now = time.time()
    bad_classes = {
        "soft_block",
        "structural_change",
        "platform_change_suspected",
        "empty_feed_or_softblock",
    }
    countries: set[str] = set()
    for row in store.get("recent") or []:
        if now - float(row.get("ts") or 0) > window_s:
            continue
        if row.get("success"):
            continue
        if str(row.get("class") or "").lower() in bad_classes:
            countries.add(str(row.get("country") or ""))

    countries.discard("")
    if len(countries) < min_countries:
        return None

    # Eviter spam: pas plus d'1 alerte / 15 min
    for prev in reversed(store.get("alerts") or []):
        if prev.get("type") == "platform_drift":
            if now - float(prev.get("ts") or 0) < 900:
                return None
            break

    return {
        "type": "platform_drift",
        "ts": now,
        "message": (
            f"Empty-feed/soft-block on {len(countries)} countries "
            f"in last {window_s}s: {sorted(countries)}"
        ),
        "countries": sorted(countries),
    }


def success_rate_by_country() -> dict[str, dict[str, float]]:
    """Snapshot taux de succes par pays."""
    with _LOCK:
        store = _load()
    out: dict[str, dict[str, float]] = {}
    for cc, row in (store.get("by_country") or {}).items():
        ok = float(row.get("success") or 0)
        fail = float(row.get("fail") or 0)
        total = ok + fail
        out[cc] = {
            "success": ok,
            "fail": fail,
            "total": total,
            "rate": (ok / total) if total else 0.0,
        }
    return out


def record_bandwidth_mb(country: str, mb: float) -> None:
    """Cumule le bandwidth approx (Content-Length) pour le quota 10GB/mois."""
    if mb <= 0:
        return
    cc = (country or "xx").strip().lower()[:8] or "xx"
    with _LOCK:
        store = _load()
        store["bandwidth_mb_total"] = float(store.get("bandwidth_mb_total") or 0.0) + float(mb)
        by_cc = store.setdefault("bandwidth_by_country", {})
        by_cc[cc] = float(by_cc.get(cc) or 0.0) + float(mb)
        # Alerte soft si on approche du plafond mensuel (defaut 9 GB / 10).
        cap = float(os.getenv("TIKTOK_BANDWIDTH_CAP_MB") or "9000")
        total = float(store["bandwidth_mb_total"])
        if total >= cap:
            now = time.time()
            spam = False
            for prev in reversed(store.get("alerts") or []):
                if prev.get("type") == "bandwidth_cap":
                    if now - float(prev.get("ts") or 0) < 3600:
                        spam = True
                    break
            if not spam:
                alert = {
                    "type": "bandwidth_cap",
                    "ts": now,
                    "message": f"Bandwidth cumulative {total:.1f} MB >= cap {cap:.0f} MB",
                }
                store.setdefault("alerts", []).append(alert)
                LOGGER.warning("[METRICS_ALERT] %s — %s", alert["type"], alert["message"])
        _save(store)


def consecutive_soft_block_country_count() -> int:
    """Nombre de pays distincts en soft/structural dans la fenetre recente."""
    window_s = max(300, int(os.getenv("TIKTOK_METRICS_DRIFT_WINDOW_S") or "3600"))
    bad = {
        "soft_block",
        "structural_change",
        "platform_change_suspected",
    }
    now = time.time()
    countries: set[str] = set()
    with _LOCK:
        store = _load()
        for row in store.get("recent") or []:
            if now - float(row.get("ts") or 0) > window_s:
                continue
            if str(row.get("class") or "").lower() in bad:
                countries.add(str(row.get("country") or ""))
    countries.discard("")
    return len(countries)
