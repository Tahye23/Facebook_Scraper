"""Chauffe une session sticky TikTok: 1 proxy = 1 profil Chrome.

Usage (local, Chrome visible):
  python warm_sticky_session.py --sticky-id sdwopfmy-100
  python warm_sticky_session.py --proxy-line "p.webshare.io:80:sdwopfmy-100:PASSWORD"

Etapes:
  1) Ouvre Chrome headed VIA le proxy sticky choisi
  2) Charge le profil persistant `tiktok_sessions/<id>/`
  3) Va sur tiktok.com — tu te connectes / acceptes cookies manuellement
  4) Appuie Entree ici: on exporte les cookies dans le dossier sticky

Ensuite le worker scrapera avec CE proxy + CE profil (plus le cookies.json global).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from playwright.sync_api import sync_playwright

import sticky_sessions
from scraper import _parse_all_proxies, _parse_proxy_spec, _proxy_identity


def _find_proxy(sticky_id: str | None, proxy_line: str | None) -> dict:
    if proxy_line:
        proxy = _parse_proxy_spec(proxy_line)
        if not proxy:
            raise SystemExit(f"Ligne proxy invalide: {proxy_line}")
        return proxy

    if not sticky_id:
        raise SystemExit("Fournir --sticky-id ou --proxy-line")

    for proxy in _parse_all_proxies():
        if _proxy_identity(proxy) == sticky_id:
            return proxy
    raise SystemExit(
        f"Sticky '{sticky_id}' introuvable dans TIKTOK_PROXY_FILE / TIKTOK_PROXY_LIST"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm TikTok sticky session (proxy + profile)")
    parser.add_argument("--sticky-id", help="Ex: sdwopfmy-100")
    parser.add_argument("--proxy-line", help='Ex: "p.webshare.io:80:sdwopfmy-100:pass"')
    parser.add_argument(
        "--url",
        default="https://www.tiktok.com/",
        help="URL de demarrage (defaut: homepage TikTok)",
    )
    args = parser.parse_args()

    proxy = _find_proxy(args.sticky_id, args.proxy_line)
    identity = _proxy_identity(proxy)
    session_dir = sticky_sessions.prepare_session_dir(identity)
    if not sticky_sessions.acquire_session_lock(session_dir):
        print(f"ERROR: sticky '{identity}' deja en cours d'utilisation")
        return 1
    sticky_sessions.clear_chrome_profile_locks(session_dir)

    channel = (os.getenv("TIKTOK_BROWSER_CHANNEL") or "chrome").strip()
    print(f"Sticky identity : {identity}")
    print(f"Proxy server    : {proxy.get('server')}")
    print(f"Session dir     : {session_dir}")
    print(f"Browser channel : {channel or 'chromium'}")
    print()
    print("1) Connecte-toi / accepte les cookies dans la fenetre Chrome")
    print("2) Navigue un peu sur TikTok (profil, For You...)")
    print("3) Reviens ici et appuie Entree pour sauver les cookies")
    print()

    try:
        with sync_playwright() as p:
            launch_args = {
                "user_data_dir": str(session_dir),
                "headless": False,
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
            context = p.chromium.launch_persistent_context(**launch_args)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
            input("Appuie Entree quand la session est prete... ")
            cookies = context.cookies()
            out = sticky_sessions.save_sticky_cookies(identity, cookies)
            print(f"Cookies sauves: {out} ({len(cookies)} cookies)")
            context.close()
    finally:
        sticky_sessions.touch_session(session_dir)
        sticky_sessions.release_session_lock(session_dir)
        sticky_sessions.enforce_lru_limit()

    print("Warmup termine. Tu peux lancer un scrape avec cette sticky.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
