from playwright.sync_api import sync_playwright
import os
import time

PROFILE_DIR = os.path.abspath("fb_profile")

def save_facebook_session():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print(f"[*] Profil: {PROFILE_DIR}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
        )

        page = context.new_page()
        page.goto("https://www.facebook.com")

        print("\n" + "="*50)
        print("1. Connecte-toi à Facebook dans la fenêtre")
        print("2. Attends d'être sur le feed principal")
        print("3. Appuie sur ENTRÉE ici")
        print("="*50 + "\n")
        input(">> ENTRÉE quand connecté: ")

        # Attendre que la page soit stable après la connexion
        time.sleep(3)

        # Vérifier sans evaluate (juste vérifier l'URL)
        current_url = page.url
        print(f"[*] URL actuelle: {current_url}")

        if "facebook.com" in current_url and "login" not in current_url:
            print(f"[+] Session sauvegardée dans: {PROFILE_DIR}")
            print("[+] Tu peux maintenant utiliser scraper.py")
        else:
            print("[!] Pas connecté — vérifie que tu es sur facebook.com")

        context.close()

if __name__ == "__main__":
    save_facebook_session()