"""Rechauffe en batch les sessions sticky existantes (auto-warm, sans login manuel).

Usage (dans le conteneur worker, CWD = platform/tiktok-scraper):
  python rewarm_all_sticky.py
  python rewarm_all_sticky.py --workers 2 --force

- Vide la blacklist proxy
- Pour chaque dossier tiktok_sessions/<id>/: ouvre Chrome via le proxy sticky,
  visite TikTok, sauve cookies + marque .warmed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sticky_sessions
from scraper import (
    _blacklist_path,
    _dismiss_cookie_banner,
    _human_pause,
    _parse_all_proxies,
    _proxy_identity,
)


def _clear_blacklist() -> None:
    path = _blacklist_path()
    path.write_text("{}\n", encoding="utf-8")
    print(f"[ok] blacklist reset: {path}")


def _proxy_index() -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for proxy in _parse_all_proxies():
        identity = _proxy_identity(proxy)
        if identity and identity not in mapping:
            mapping[identity] = proxy
    return mapping


def _force_cold(identity: str) -> None:
    session_dir = sticky_sessions.session_dir_for_identity(identity)
    for name in (".warmed", "cookies.json"):
        target = session_dir / name
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass


def _warm_one(identity: str, proxy: dict, force: bool) -> tuple[str, bool, str]:
    from playwright.sync_api import sync_playwright

    if force:
        _force_cold(identity)

    session_dir = sticky_sessions.prepare_session_dir(identity)
    if not sticky_sessions.acquire_session_lock(session_dir):
        return identity, False, "locked"
    sticky_sessions.clear_chrome_profile_locks(session_dir)

    channel = (os.getenv("TIKTOK_BROWSER_CHANNEL") or "chrome").strip()
    headless = (os.getenv("TIKTOK_HEADLESS") or "false").strip().lower() in {"1", "true", "yes"}

    try:
        with sync_playwright() as playwright:
            launch_args = {
                "user_data_dir": str(session_dir),
                "headless": headless,
                "proxy": proxy,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1365,768",
                ],
                "ignore_default_args": ["--enable-automation"],
                "viewport": {"width": 1365, "height": 768},
                "locale": "en-US",
                "timezone_id": "Europe/Paris",
            }
            if channel:
                launch_args["channel"] = channel
            context = playwright.chromium.launch_persistent_context(**launch_args)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
                _dismiss_cookie_banner(page)
                _human_pause(2.0, 1.0)
                try:
                    page.mouse.wheel(0, 1200)
                except Exception:
                    pass
                _human_pause(1.2, 0.6)
                try:
                    page.goto(
                        "https://www.tiktok.com/foryou",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    _dismiss_cookie_banner(page)
                    _human_pause(1.2, 0.6)
                except Exception as exc:
                    return identity, False, f"foryou_failed:{exc}"[:160]
                cookies = context.cookies()
                sticky_sessions.mark_session_warmed(identity, cookies)
                return identity, True, f"cookies={len(cookies)}"
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        return identity, False, str(exc)[:200]
    finally:
        sticky_sessions.release_session_lock(session_dir)
        sticky_sessions.touch_session(session_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewarm all sticky sessions + clear blacklist")
    parser.add_argument("--workers", type=int, default=2, help="Chrome paralleles (defaut 2)")
    parser.add_argument("--force", action="store_true", help="Rechauffe meme si deja .warmed")
    parser.add_argument(
        "--identities",
        default="",
        help="Liste CSV d'ids (sinon tous les dossiers tiktok_sessions/)",
    )
    parser.add_argument("--skip-blacklist-clear", action="store_true")
    args = parser.parse_args()

    if not args.skip_blacklist_clear:
        _clear_blacklist()

    proxies = _proxy_index()
    if args.identities.strip():
        identities = [x.strip() for x in args.identities.split(",") if x.strip()]
    else:
        identities = sorted(p.name for p in sticky_sessions.list_session_dirs())

    if not identities:
        print("Aucune sticky session a chauffer.")
        return 1

    jobs = []
    skipped = []
    for identity in identities:
        proxy = proxies.get(identity)
        if not proxy:
            skipped.append(identity)
            continue
        jobs.append((identity, proxy))

    print(f"A chauffer: {len(jobs)} | skip (proxy introuvable): {len(skipped)} | workers={args.workers}")
    if skipped[:10]:
        print("Skip exemples:", ", ".join(skipped[:10]))

    ok = 0
    ko = 0
    results_path = HERE / "video_reports" / f"rewarm_report_{int(time.time())}.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    report = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(_warm_one, identity, proxy, args.force): identity
            for identity, proxy in jobs
        }
        for future in as_completed(futures):
            identity, success, detail = future.result()
            status = "OK" if success else "FAIL"
            print(f"[{status}] {identity} -> {detail}")
            report.append({"identity": identity, "ok": success, "detail": detail})
            if success:
                ok += 1
            else:
                ko += 1

    results_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Termine: ok={ok} fail={ko} report={results_path}")
    return 0 if ko == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
