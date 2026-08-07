#!/usr/bin/env python3
"""Dashboard leger metriques scrape TikTok (Phase 4).

Usage:
  python metrics_dashboard.py
  python metrics_dashboard.py --json

Affiche succes par pays + alertes derive / bandwidth.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRAPER_DIR = Path(__file__).resolve().parent
if str(SCRAPER_DIR) not in sys.path:
    sys.path.insert(0, str(SCRAPER_DIR))

import identity_pool  # noqa: E402
import scrape_metrics  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="TikTok scrape metrics dashboard")
    parser.add_argument("--json", action="store_true", help="Dump JSON brut")
    args = parser.parse_args()

    rates = scrape_metrics.success_rate_by_country()
    soft_cc = scrape_metrics.consecutive_soft_block_country_count()
    id_summary = identity_pool.summary_by_status()

    store_path = scrape_metrics._metrics_path()  # noqa: SLF001
    store = {}
    if store_path.exists():
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            store = {}

    payload = {
        "by_country": rates,
        "soft_block_countries_recent": soft_cc,
        "bandwidth_mb_total": store.get("bandwidth_mb_total") or 0.0,
        "bandwidth_by_country": store.get("bandwidth_by_country") or {},
        "by_class": store.get("by_class") or {},
        "alerts": (store.get("alerts") or [])[-10:],
        "identity_pool": id_summary,
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("=== TikTok scrape metrics ===")
    print(f"soft/structural countries (window): {soft_cc}")
    print(f"bandwidth total: {float(payload['bandwidth_mb_total']):.2f} MB")
    print()
    print("Success by country:")
    if not rates:
        print("  (no data yet)")
    for cc, row in sorted(rates.items()):
        print(
            f"  {cc}: rate={row['rate']:.0%} "
            f"(ok={int(row['success'])} fail={int(row['fail'])})"
        )
    print()
    print("Classes:")
    for cls, n in sorted((payload["by_class"] or {}).items(), key=lambda x: -x[1]):
        print(f"  {cls}: {n}")
    print()
    print("Identity pool:")
    for st, n in sorted(id_summary.items()):
        print(f"  {st}: {n}")
    print()
    print("Recent alerts:")
    alerts = payload["alerts"] or []
    if not alerts:
        print("  (none)")
    for a in alerts:
        print(f"  - {a.get('type')}: {a.get('message')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
