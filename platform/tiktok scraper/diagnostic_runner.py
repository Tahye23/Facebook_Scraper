#!/usr/bin/env python3
"""CLI diagnostic itemListLen=0 — HORS RabbitMQ / hors metriques prod.

Exemples:
  python diagnostic_runner.py --test-mode baseline --target-profile bellewarmedia --country us
  python diagnostic_runner.py --test-mode assets_off --target-profile bellewarmedia --country fr
  python diagnostic_runner.py --test-mode logged_in --target-profile bellewarmedia --country us
  python diagnostic_runner.py --test-mode mobile_ua --target-profile bellewarmedia --country us

Resultats append dans diagnostic_results.jsonl (gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _load_env_file() -> None:
    for candidate in (REPO_ROOT / ".env", HERE / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip())
        break


def _apply_test_mode(mode: str, country: str) -> None:
    """Override env pour CETTE run uniquement (process courant)."""
    os.environ["TIKTOK_DIAG_MODE"] = "true"
    os.environ["TIKTOK_PROXY_COUNTRIES"] = country
    # Ne pas polluer le pool identity / metrics prod.
    os.environ.setdefault("TIKTOK_SCRAPE_METRICS_FILE", str(HERE / "diagnostic_metrics.json"))
    os.environ.setdefault("TIKTOK_IDENTITY_POOL_FILE", str(HERE / "diagnostic_identity_pool.json"))

    if mode == "baseline":
        # Comportement prod actuel — ne force rien d'autre.
        return

    if mode == "assets_off":
        os.environ["TIKTOK_BLOCK_HEAVY_ASSETS"] = "false"
        return

    if mode == "logged_in":
        cookies = (os.getenv("TIKTOK_DIAG_SESSION_COOKIES_FILE") or "").strip()
        if not cookies:
            default = HERE / "tiktok_session_cookies.json"
            os.environ["TIKTOK_DIAG_SESSION_COOKIES_FILE"] = str(default)
            cookies = str(default)
        if not Path(cookies).exists():
            raise SystemExit(
                f"logged_in mode requires cookie jar at {cookies}\n"
                "Export Playwright/Netscape JSON cookies to that path "
                "(gitignored). See DIAGNOSTIC.md."
            )
        os.environ["TIKTOK_FORCE_COOKIE_INJECTION"] = "true"
        return

    if mode == "mobile_ua":
        os.environ["TIKTOK_ALLOW_MOBILE_HOST"] = "true"
        os.environ["TIKTOK_FORCE_USER_AGENT"] = "true"
        os.environ["TIKTOK_IS_MOBILE"] = "true"
        os.environ["TIKTOK_HAS_TOUCH"] = "true"
        os.environ["TIKTOK_VIEWPORT_WIDTH"] = "390"
        os.environ["TIKTOK_VIEWPORT_HEIGHT"] = "844"
        os.environ["TIKTOK_USER_AGENT"] = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        )
        os.environ["TIKTOK_SEC_CH_UA"] = (
            '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
        )
        os.environ["TIKTOK_SEC_CH_UA_MOBILE"] = "?1"
        os.environ["TIKTOK_SEC_CH_UA_PLATFORM"] = '"iOS"'
        # Prefer chromium channel for consistent UA override.
        if not (os.getenv("TIKTOK_BROWSER_CHANNEL") or "").strip():
            os.environ["TIKTOK_BROWSER_CHANNEL"] = "chromium"
        return

    raise SystemExit(f"Unknown test mode: {mode}")


def _profile_url(username: str, mode: str) -> str:
    handle = username.lstrip("@").strip()
    if mode == "mobile_ua":
        return f"https://m.tiktok.com/@{handle}"
    return f"https://www.tiktok.com/@{handle}"


def _extract_diag_row(result: dict, *, mode: str, profile: str, country: str) -> dict:
    signals = result.get("classification_signals") or {}
    if not isinstance(signals, dict):
        signals = {}
    scope = signals.get("ssr_scope_keys") or result.get("ssr_scope_keys") or []
    item_len = int(
        signals.get("item_list_len")
        or signals.get("itemListLen")
        or 0
    )
    bw_bytes = int(signals.get("bandwidth_bytes") or 0)
    # Fallback: parse from classification / error
    fetch_failed = signals.get("fetch_failed")
    xhr_seen = signals.get("item_list_xhr_seen")
    return {
        "test_mode": mode,
        "target_profile": profile.lstrip("@"),
        "country": country,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "itemListLen": item_len,
        "ssr_keys_present": list(scope)[:30] if isinstance(scope, list) else [],
        "xhr_item_list_seen": bool(xhr_seen) if xhr_seen is not None else False,
        "fetch_error": bool(fetch_failed) if fetch_failed is not None else None,
        "bandwidth_used_mb": round(bw_bytes / (1024.0 * 1024.0), 4) if bw_bytes else None,
        "classification": result.get("classification"),
        "error": result.get("error"),
        "posts_count": len(result.get("posts") or []),
        "ssr_universal_len": signals.get("ssr_universal_len"),
        "ssr_has_user_info": signals.get("ssr_has_user_info"),
        "ssr_has_post_list": signals.get("ssr_has_post_list"),
    }


def main() -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(description="TikTok diagnostic runner (standalone)")
    parser.add_argument(
        "--test-mode",
        required=True,
        choices=("baseline", "assets_off", "logged_in", "mobile_ua"),
    )
    parser.add_argument("--target-profile", required=True, help="username without or with @")
    parser.add_argument(
        "--country",
        default="us",
        choices=("us", "fr", "de", "gb"),
    )
    parser.add_argument(
        "--results-file",
        default=str(HERE / "diagnostic_results.jsonl"),
        help="JSONL append path",
    )
    parser.add_argument("--max-posts", type=int, default=12)
    args = parser.parse_args()

    _apply_test_mode(args.test_mode, args.country)
    url = _profile_url(args.target_profile, args.test_mode)

    # Import APRES overrides env (scraper lit os.environ au runtime).
    import scraper as scraper_mod
    from scraper import (
        _assign_webshare_sticky_session,
        _build_rotating_proxy_config,
        _finalize_classified_result,
        _parse_proxy_url_env,
        _rotating_proxy_mode,
        scrape_tiktok_page,
    )

    proxy_override = None
    if _rotating_proxy_mode():
        base = _build_rotating_proxy_config()
        os.environ["TIKTOK_PROXY_COUNTRIES"] = args.country
        proxy_override = _assign_webshare_sticky_session(base)
    else:
        base = _parse_proxy_url_env()
        if base:
            os.environ["TIKTOK_PROXY_COUNTRIES"] = args.country
            proxy_override = _assign_webshare_sticky_session(base)

    print(
        f"[DIAG] mode={args.test_mode} profile={args.target_profile} "
        f"country={args.country} url={url}",
        flush=True,
    )
    result = scrape_tiktok_page(
        url=url,
        max_posts=args.max_posts,
        analyze_video_content=False,
        proxy_override=proxy_override,
    )
    # Ensure classification_signals present even if outer finalize skipped.
    result = _finalize_classified_result(
        result,
        country=args.country,
        identity="",
        record=False,
    )
    signals = dict(result.get("classification_signals") or {})
    bw = getattr(scraper_mod, "_LAST_ATTEMPT_BANDWIDTH", {}) or {}
    signals.setdefault("bandwidth_bytes", int(bw.get("bytes") or 0))
    result["classification_signals"] = signals
    row = _extract_diag_row(
        result,
        mode=args.test_mode,
        profile=args.target_profile,
        country=args.country,
    )

    out = Path(args.results_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(row, indent=2, ensure_ascii=False))
    print(f"[DIAG] appended → {out}", flush=True)
    return 0 if row.get("posts_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
