
#scraper_core.py

from playwright.sync_api import sync_playwright
import time, random, json, os
from urllib.parse import urlparse

playwright = None
browser = None
context = None

COOKIES_FILE = "fb_cookies.json"

def init_browser():
    global playwright, browser, context

    if browser:
        return

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )

    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"
    )

    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r") as f:
            context.add_cookies(json.load(f))

def get_page():
    init_browser()
    return context.new_page()

def human_delay(a=2, b=4):
    time.sleep(random.uniform(a, b))

def is_login_wall(page):
    return "login" in page.url.lower()

# ───────────── SCRAPE PAGE ─────────────

def scrape_facebook_page(url):
    page = get_page()

    try:
        page.goto(url, timeout=60000)
        human_delay()

        if is_login_wall(page):
            return {"error": "login_required", "posts": []}

        posts = page.evaluate("""
            () => {
                let posts = [];
                document.querySelectorAll('div[role="article"]').forEach(a => {
                    let txt = a.innerText;
                    if(txt && txt.length > 30){
                        posts.push({
                            message: txt.slice(0, 500),
                            post_url: window.location.href
                        });
                    }
                });
                return posts;
            }
        """)

        return {"posts": posts, "total": len(posts)}

    except Exception as e:
        return {"error": str(e), "posts": []}

    finally:
        page.close()

# ───────────── COMMENTS ─────────────

def scrape_post_comments(url):
    page = get_page()

    try:
        page.goto(url, timeout=60000)
        human_delay()

        comments = page.evaluate("""
            () => {
                let data = [];
                document.querySelectorAll('div[aria-label*="Comment"]').forEach(c => {
                    let txt = c.innerText;
                    if(txt.length > 2){
                        data.push({message: txt});
                    }
                });
                return data;
            }
        """)

        return comments

    except Exception as e:
        return []
    finally:
        page.close()