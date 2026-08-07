#!/usr/bin/env python3
"""Compare les runs diagnostic_results.jsonl pour un meme profil.

Usage:
  python diagnostic_compare.py
  python diagnostic_compare.py --profile bellewarmedia
  python diagnostic_compare.py --file diagnostic_results.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare diagnostic JSONL runs")
    parser.add_argument(
        "--file",
        default=str(HERE / "diagnostic_results.jsonl"),
        help="Path to diagnostic_results.jsonl",
    )
    parser.add_argument("--profile", default="", help="Filter target_profile")
    args = parser.parse_args()

    rows = _load_rows(Path(args.file))
    if args.profile:
        handle = args.profile.lstrip("@").lower()
        rows = [r for r in rows if str(r.get("target_profile") or "").lower() == handle]

    if not rows:
        print("No diagnostic rows found.")
        return 1

    by_profile: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_profile[str(row.get("target_profile") or "?")].append(row)

    for profile, items in sorted(by_profile.items()):
        print(f"\n=== @{profile} ({len(items)} runs) ===")
        header = (
            f"{'mode':<12} {'cc':<4} {'posts':>5} {'itemLen':>7} "
            f"{'xhr':>3} {'fetchErr':>8} {'ssrLen':>8} {'bwMB':>7} {'class':<22} {'ts'}"
        )
        print(header)
        print("-" * len(header))
        # Derniere run par mode (plus recente).
        latest_by_mode: dict[str, dict] = {}
        for row in items:
            mode = str(row.get("test_mode") or "?")
            latest_by_mode[mode] = row
        for mode in ("baseline", "assets_off", "logged_in", "mobile_ua"):
            row = latest_by_mode.get(mode)
            if not row:
                continue
            print(
                f"{mode:<12} {_fmt(row.get('country')):<4} "
                f"{_fmt(row.get('posts_count')):>5} {_fmt(row.get('itemListLen')):>7} "
                f"{_fmt(row.get('xhr_item_list_seen')):>3} {_fmt(row.get('fetch_error')):>8} "
                f"{_fmt(row.get('ssr_universal_len')):>8} {_fmt(row.get('bandwidth_used_mb')):>7} "
                f"{_fmt(row.get('classification')):<22} {_fmt(row.get('timestamp'))}"
            )
        # Modes inattendus
        for mode, row in sorted(latest_by_mode.items()):
            if mode in ("baseline", "assets_off", "logged_in", "mobile_ua"):
                continue
            print(
                f"{mode:<12} {_fmt(row.get('country')):<4} "
                f"{_fmt(row.get('posts_count')):>5} {_fmt(row.get('itemListLen')):>7}"
            )

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
