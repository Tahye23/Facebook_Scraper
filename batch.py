from datetime import datetime
from scraper import (
    load_urls_from_csv,
    scrape_facebook_page,
    scrape_facebook_page_with_dates,
)
from browser_manager import run_in_browser_thread


def scrape_batch_from_csv(csv_path: str, date_from: str, date_to: str) -> list:
    """
    Batch avec filtre de dates.
    date_from / date_to : chaînes ISO 'YYYY-MM-DD' ou 'YYYY-MM-DDTHH:MM:SS'
    """
    try:
        dt_from = datetime.fromisoformat(date_from)
        dt_to   = datetime.fromisoformat(date_to)
    except ValueError as e:
        return [{"error": f"Format de date invalide: {e}"}]

    urls = load_urls_from_csv(csv_path)
    if not urls:
        return [{"error": "Aucune URL trouvée dans le CSV"}]

    results = []
    for url in urls:
        print(f"\n[BATCH DATE] Scraping: {url}")
        try:
            result = run_in_browser_thread(
                scrape_facebook_page_with_dates, url, dt_from, dt_to,
                timeout=120
            )
        except Exception as e:
            print(f"[!] Erreur pour {url}: {e}")
            result = {"url": url, "posts": [], "error": str(e)}
        results.append(result)

    return results


def scrape_batch_urls_only(csv_path: str, max_posts: int = None) -> list:
    """
    Batch sans filtre de dates — scrape toutes les URLs du CSV.
    Chaque page a son propre timeout de 120s.
    """
    urls = load_urls_from_csv(csv_path)
    if not urls:
        return [{"error": "Aucune URL trouvée dans le CSV"}]

    results = []
    for url in urls:
        print(f"\n[BATCH URL] Scraping: {url}")
        try:
            result = run_in_browser_thread(
                scrape_facebook_page, url, max_posts=max_posts,
                timeout=120
            )
        except Exception as e:
            print(f"[!] Erreur pour {url}: {e}")
            result = {"url": url, "posts": [], "error": str(e)}
        results.append(result)

    return results