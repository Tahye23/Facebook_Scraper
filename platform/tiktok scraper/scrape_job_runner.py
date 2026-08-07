#!/usr/bin/env python3
"""Sous-processus isole pour un scrape TikTok (harden-mvp 1.1).

Usage interne (appele par worker.py):
  python scrape_job_runner.py --input job.json --output result.json

Le parent peut tuer ce process (+ enfants Chrome) via kill_process_tree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated TikTok scrape job runner")
    parser.add_argument("--input", required=True, help="JSON job args path")
    parser.add_argument("--output", required=True, help="JSON result path")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    try:
        payload = json.loads(in_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out_path.write_text(
            json.dumps({"posts": [], "error": f"bad_job_input:{exc}", "total": 0}),
            encoding="utf-8",
        )
        return 2

    # Env overrides for this process only (dict already applied by parent via env=).
    from scraper import scrape_tiktok_page

    kwargs = {
        "url": payload.get("url"),
        "max_posts": int(payload.get("max_posts") or 20),
        "max_age_hours": payload.get("max_age_hours"),
        "analyze_video_content": bool(payload.get("analyze_video_content") or False),
        "headless_override": payload.get("headless_override"),
        "proxy_override": payload.get("proxy_override"),
    }
    # Drop None optional keys that scrape_tiktok_page treats via defaults.
    if kwargs["max_age_hours"] is None:
        kwargs.pop("max_age_hours")
    if kwargs["headless_override"] is None:
        kwargs.pop("headless_override")
    if kwargs.get("proxy_override") is None:
        kwargs.pop("proxy_override", None)

    try:
        result = scrape_tiktok_page(**kwargs)
        if not isinstance(result, dict):
            result = {"posts": [], "error": "invalid_result", "total": 0}
    except Exception as exc:
        result = {
            "posts": [],
            "total": 0,
            "error": f"scrape_exception:{exc}",
            "url": payload.get("url"),
        }

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        print(json.dumps(result), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
