from playwright.sync_api import sync_playwright
import json
import time

def save_facebook_cookies():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # ← navigateur VISIBLE
            args=["--no-sandbox", "--start-maximized"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print("Ouverture de Facebook...")
        page.goto("https://www.facebook.com", timeout=60000, wait_until="domcontentloaded")

        print("=" * 50)
        print("Connectez-vous manuellement dans le navigateur.")
        print("Une fois sur votre fil d'actualité, appuyez sur ENTRÉE ici.")
        print("=" * 50)
        input(">>> Appuyez sur ENTRÉE après connexion : ")

        cookies = context.cookies()
        with open("fb_cookies.json", "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)

        print(f"[+] {len(cookies)} cookies sauvegardés dans fb_cookies.json")
        browser.close()

save_facebook_cookies()