from playwright.sync_api import sync_playwright
import time
import json
import os
import re
import random
from urllib.parse import urlparse, urlunparse, parse_qs
import csv
from datetime import datetime

COOKIES_FILE = "fb_cookies.json"

# ─── BROWSER GLOBAL ───────────────────────────────────────────────────────────

_playwright = None
_browser = None
_context = None


def init_browser():
    global _playwright, _browser, _context
    if _browser:
        return
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--lang=fr-FR",
        ]
    )
    _context = _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="fr-FR",
        timezone_id="Europe/Paris",
        extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8"}
    )
    _context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR', 'fr', 'en-US'] });
        window.chrome = { runtime: {} };
    """)
    cookies = load_cookies()
    if cookies:
        _context.add_cookies(cookies)
        print(f"[+] {len(cookies)} cookies injectés")


def get_page():
    init_browser()
    return _context.new_page()


def close_browser():
    global _playwright, _browser, _context
    try:
        if _browser:
            _browser.close()
        if _playwright:
            _playwright.stop()
    except Exception as e:
        print(f"[!] close_browser: {e}")
    finally:
        _browser = None
        _context = None
        _playwright = None


# ─── COOKIES ──────────────────────────────────────────────────────────────────

def load_cookies():
    if not os.path.exists(COOKIES_FILE):
        print("[!] Fichier fb_cookies.json introuvable")
        return []
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cookies = []
    for c in raw:
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": "None",
        }
        if "expirationDate" in c:
            cookie["expires"] = int(c["expirationDate"])
        if cookie["name"] and cookie["value"]:
            cookies.append(cookie)
    print(f"[+] {len(cookies)} cookies chargés")
    return cookies


# ─── URL HELPERS ──────────────────────────────────────────────────────────────

def extract_page_base_url(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if path_parts and path_parts[0] == "share":
        return url
    if path_parts and path_parts[0] == "profile.php":
        query = next((p for p in parsed.query.split("&") if p.startswith("id=")), "")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))
    if path_parts:
        return urlunparse((parsed.scheme, parsed.netloc, "/" + path_parts[0], "", "", ""))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def is_share_url(url: str) -> bool:
    path_parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    return bool(path_parts and path_parts[0] == "share")


def clean_post_url(url: str) -> str:
    if not url:
        return url
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def extract_page_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if path_parts and path_parts[0] not in ("permalink.php", "profile.php", "share", "posts"):
        return path_parts[0]
    return ""


def build_post_url_candidates(post_url: str, post_id: str = "", page_id: str = "",
                               page_name: str = "") -> list:
    candidates = []
    if post_id and page_id:
        candidates.append(
            f"https://www.facebook.com/permalink.php?story_fbid={post_id}&id={page_id}"
        )
    if post_url:
        candidates.append(post_url)
    return list(dict.fromkeys(candidates))


def load_urls_from_csv(csv_path: str) -> list:
    urls = []
    with open(csv_path, encoding="utf-8-sig") as f:
        raw = f.read()
        f.seek(0)
        delimiter = ";" if ";" in raw[:1024] else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            for key in row:
                if key.strip().lower() == "url":
                    val = row[key].strip()
                    if val:
                        urls.append(val)
                    break
    print(f"[+] {len(urls)} URLs chargées depuis {csv_path}")
    return urls


def filter_posts_by_date(posts, date_from, date_to):
    result = []
    skipped_no_date = 0
    for p in posts:
        raw_date = p.get("date", "")
        try:
            ts = int(str(raw_date).strip())
            if ts > 1_000_000_000:
                dt = datetime.fromtimestamp(ts)
                if date_from <= dt <= date_to:
                    result.append(p)
                continue
        except (ValueError, TypeError, OSError):
            pass
        try:
            dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            dt_naive = dt.replace(tzinfo=None)
            if date_from <= dt_naive <= date_to:
                result.append(p)
            continue
        except (ValueError, TypeError):
            pass
        if raw_date:
            print(f"[!] Date non parsable: '{raw_date}' — post inclus sans filtre")
            result.append(p)
        else:
            skipped_no_date += 1
    if skipped_no_date:
        print(f"[!] {skipped_no_date} posts ignorés (date vide)")
    return result


# ─── BROWSER HELPERS ──────────────────────────────────────────────────────────

def _human_delay(min_s=1.5, max_s=3.5):
    time.sleep(random.uniform(min_s, max_s))


def _is_login_wall(page) -> bool:
    url = page.url
    content = page.content()
    return (
        "login" in url or
        "Log in to Facebook" in content or
        "Connectez-vous à Facebook" in content or
        "login_form" in content or
        page.query_selector("input[name='email']") is not None
    )


def _is_redirected_to_home(page, expected_url: str) -> bool:
    current = page.url
    current_path = urlparse(current).path.rstrip("/")
    expected_path = urlparse(expected_url).path.rstrip("/")
    redirected = current_path in ("", "/") and expected_path not in ("", "/")
    if redirected:
        print(f"[!] Redirection home: {current}")
    return redirected


def _close_login_popup(page):
    try:
        for selector in [
            "[aria-label='Close']",
            "[aria-label='Fermer']",
            "div[role='dialog'] div[aria-label='Close']",
            "div[role='dialog'] [aria-label='Fermer']",
        ]:
            btn = page.query_selector(selector)
            if btn:
                btn.click()
                time.sleep(1)
                break
    except:
        pass


def _warmup_session(page) -> bool:
    print("[*] Warmup session...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector("body", timeout=10000)
    except:
        pass
    _human_delay(3, 5)
    _close_login_popup(page)
    try:
        page.mouse.move(random.randint(200, 800), random.randint(200, 600))
        _human_delay(0.5, 1.5)
    except:
        pass
    if _is_login_wall(page):
        print("[!] Login wall — cookies expirés ou invalides")
        return False
    print(f"[*] Session OK: {page.url}")
    return True


def _resolve_share_url(page, share_url: str) -> str:
    page.goto(share_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    _human_delay(4, 6)
    clean = extract_page_base_url(page.url)
    print(f"[*] URL page résolue: {clean}")
    return clean


# ─── DATE EXTRACTOR RÉCURSIF ──────────────────────────────────────────────────

DATE_KEYS = (
    "publish_time", "creation_time", "created_time",
    "story_create_time", "story_publish_time",
    "created_at", "published_at", "time",
)


def _extract_timestamp_recursive(data, depth=0) -> str:
    if depth > 12 or not isinstance(data, dict):
        return ""
    for key in DATE_KEYS:
        val = data.get(key)
        try:
            ts = int(val)
            if ts > 1_000_000_000:
                return str(ts)
        except (TypeError, ValueError):
            pass
    for key, value in data.items():
        if isinstance(value, dict):
            result = _extract_timestamp_recursive(value, depth + 1)
            if result:
                return result
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = _extract_timestamp_recursive(item, depth + 1)
                    if result:
                        return result
    return ""


# ─── ENGAGEMENT EXTRACTOR (REACTIONS / COMMENTS / SHARES) ────────────────────

def _extract_reactions_count(data: dict) -> str:
    """
    Cherche le nombre total de réactions dans un nœud feedback GraphQL.
    Essaie plusieurs chemins connus dans la structure JSON de Facebook.
    """
    try:
        # Chemin 1 : feedback.reaction_count.count
        rc = (data.get("feedback") or {}).get("reaction_count") or {}
        if isinstance(rc, dict):
            val = rc.get("count")
            if val is not None:
                return str(int(val))

        # Chemin 2 : feedback.reactors.count
        reactors = (data.get("feedback") or {}).get("reactors") or {}
        if isinstance(reactors, dict):
            val = reactors.get("count")
            if val is not None:
                return str(int(val))

        # Chemin 3 : feedback.i_like_count / reaction_count direct
        fb = data.get("feedback") or {}
        for key in ("reaction_count", "like_count", "reactions_count"):
            val = fb.get(key)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, dict):
                c = val.get("count")
                if c is not None:
                    return str(int(c))

        # Chemin 4 : direct dans le nœud
        for key in ("reaction_count", "like_count", "reactions_count"):
            val = data.get(key)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, dict):
                c = val.get("count")
                if c is not None:
                    return str(int(c))

    except (TypeError, ValueError, AttributeError):
        pass
    return "0"

def _extract_comments_count(data: dict) -> str:
    try:
        fb = data.get("feedback") or {}
        
        # Chemin 1 : feedback.comment_count.total_count / count
        cc = fb.get("comment_count") or fb.get("comments_count") or {}
        if isinstance(cc, dict):
            for key in ("total_count", "count"):
                val = cc.get(key)
                if val is not None:
                    return str(int(val))
        if isinstance(cc, int):
            return str(cc)

        # Chemin 2 : feedback.comments.total_count
        comments_node = fb.get("comments") or {}
        if isinstance(comments_node, dict):
            val = comments_node.get("total_count")
            if val is not None:
                return str(int(val))

        # ✅ Chemin 3 : feedback.total_comment_count (Facebook le met parfois ici)
        for key in ("total_comment_count", "comment_count", "comments_count"):
            val = fb.get(key)
            if isinstance(val, int):
                return str(val)

        # ✅ Chemin 4 : feedback.unified_reactors (certaines versions GraphQL)
        unified = fb.get("unified_reactors") or {}
        if isinstance(unified, dict):
            val = unified.get("count")
            if val is not None:
                return str(int(val))

        # Chemin 5 : direct dans le nœud
        for key in ("comment_count", "comments_count", "total_comment_count"):
            val = data.get(key)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, dict):
                c = val.get("total_count") or val.get("count")
                if c is not None:
                    return str(int(c))

        # Chemin 6 : comet_sections → feedback
        comet_fb = ((data.get("comet_sections") or {}).get("feedback", {}))
        if isinstance(comet_fb, dict):
            story = comet_fb.get("story") or {}
            fb2 = story.get("feedback") or {}
            for key in ("comment_count", "total_comment_count"):
                cc2 = fb2.get(key) or {}
                if isinstance(cc2, dict):
                    val = cc2.get("total_count") or cc2.get("count")
                    if val is not None:
                        return str(int(val))
                if isinstance(cc2, int):
                    return str(cc2)

    except (TypeError, ValueError, AttributeError):
        pass
    return "0"

def _extract_shares_count(data: dict) -> str:
    try:
        fb = data.get("feedback") or {}

        # Chemin 1 : feedback.share_count.count
        sc = fb.get("share_count") or fb.get("shares_count") or {}
        if isinstance(sc, dict):
            for key in ("count", "total_count"):
                val = sc.get(key)
                if val is not None:
                    return str(int(val))
        if isinstance(sc, int):
            return str(sc)

        # Chemin 2 : feedback.reshare_count
        rc = fb.get("reshare_count") or {}
        if isinstance(rc, dict):
            val = rc.get("count")
            if val is not None:
                return str(int(val))
        if isinstance(rc, int):
            return str(rc)

        # ✅ Chemin 3 : feedback.comet_ufi_summary_and_actions_renderer
        ufi = fb.get("comet_ufi_summary_and_actions_renderer") or {}
        if isinstance(ufi, dict):
            ufi_fb = ufi.get("feedback") or {}
            sc2 = ufi_fb.get("share_count") or {}
            if isinstance(sc2, dict):
                val = sc2.get("count")
                if val is not None:
                    return str(int(val))

        # Chemin 4 : direct dans le nœud
        for key in ("share_count", "shares_count", "reshare_count"):
            val = data.get(key)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, dict):
                c = val.get("count") or val.get("total_count")
                if c is not None:
                    return str(int(c))

        # Chemin 5 : comet_sections → feedback
        comet_fb = ((data.get("comet_sections") or {}).get("feedback", {}))
        if isinstance(comet_fb, dict):
            story = comet_fb.get("story") or {}
            fb2 = story.get("feedback") or {}
            sc3 = fb2.get("share_count") or {}
            if isinstance(sc3, dict):
                val = sc3.get("count") or sc3.get("total_count")
                if val is not None:
                    return str(int(val))

    except (TypeError, ValueError, AttributeError):
        pass
    return "0"

def _extract_engagement_from_adaptive_renderers(renderers: list) -> dict:
    """
    Extrait reactions/comments/shares depuis adaptive_ufi_action_renderers.
    Structure :
    [
      {"__typename": "UFIStoryReactActionRenderer",   "feedback": {"reaction_count": {"count": 143}}},
      {"__typename": "UFICommentActionRenderer",       "feedback": {"comment_rendering_instance": {"comments": {"total_count": 19}}}},
      {"__typename": "XFBUFIAdaptiveShareActionRenderer", "feedback": {"share_count": {"count": 11}}}
    ]
    """
    reactions = "0"
    comments  = "0"
    shares    = "0"

    for renderer in renderers:
        if not isinstance(renderer, dict):
            continue
        typename = renderer.get("__typename", "")
        fb = renderer.get("feedback") or {}

        if "React" in typename:
            rc = fb.get("reaction_count") or {}
            if isinstance(rc, dict):
                val = rc.get("count")
                if val is not None:
                    reactions = str(int(val))
            elif isinstance(rc, int):
                reactions = str(rc)

        elif "Comment" in typename:
            cri = fb.get("comment_rendering_instance") or {}
            if isinstance(cri, dict):
                comments_node = cri.get("comments") or {}
                val = comments_node.get("total_count")
                if val is not None:
                    comments = str(int(val))
            # fallback direct
            if comments == "0":
                cc = fb.get("comment_count") or {}
                if isinstance(cc, dict):
                    val = cc.get("total_count") or cc.get("count")
                    if val is not None:
                        comments = str(int(val))

        elif "Share" in typename:
            sc = fb.get("share_count") or {}
            if isinstance(sc, dict):
                val = sc.get("count")
                if val is not None:
                    shares = str(int(val))
            elif isinstance(sc, int):
                shares = str(sc)

    return {"reactions": reactions, "comments_count": comments, "shares": shares}


def _extract_engagement_recursive(data: dict, depth: int = 0) -> dict:
    empty = {"reactions": "0", "comments_count": "0", "shares": "0"}
    if depth > 15 or not isinstance(data, dict):
        return empty

    # ✅ CHEMIN PRIORITAIRE : adaptive_ufi_action_renderers (structure confirmée par debug)
    # Chemin complet :
    # story_ufi_container → story → feedback_context
    #   → feedback_target_with_context → comet_ufi_summary_and_actions_renderer
    #   → feedback → adaptive_ufi_action_renderers
    try:
        ufi_container = data.get("story_ufi_container") or {}
        story = ufi_container.get("story") or {}
        fb_ctx = story.get("feedback_context") or {}
        target = fb_ctx.get("feedback_target_with_context") or {}
        renderer = target.get("comet_ufi_summary_and_actions_renderer") or {}
        fb = renderer.get("feedback") or {}
        renderers = fb.get("adaptive_ufi_action_renderers")
        if renderers and isinstance(renderers, list):
            result = _extract_engagement_from_adaptive_renderers(renderers)
            if result != empty:
                return result
    except Exception:
        pass

    # ✅ CHEMIN 2 : via comet_sections → feedback → story → story_ufi_container
    try:
        comet = data.get("comet_sections") or {}
        fb_section = comet.get("feedback") or {}
        story2 = fb_section.get("story") or {}
        ufi2 = story2.get("story_ufi_container") or {}
        story2b = ufi2.get("story") or {}
        fb_ctx2 = story2b.get("feedback_context") or {}
        target2 = fb_ctx2.get("feedback_target_with_context") or {}
        renderer2 = target2.get("comet_ufi_summary_and_actions_renderer") or {}
        fb2 = renderer2.get("feedback") or {}
        renderers2 = fb2.get("adaptive_ufi_action_renderers")
        if renderers2 and isinstance(renderers2, list):
            result = _extract_engagement_from_adaptive_renderers(renderers2)
            if result != empty:
                return result
    except Exception:
        pass

    # Chemin classique feedback direct
    reactions = _extract_reactions_count(data)
    comments  = _extract_comments_count(data)
    shares    = _extract_shares_count(data)
    if reactions != "0" or comments != "0" or shares != "0":
        return {"reactions": reactions, "comments_count": comments, "shares": shares}

    # Récursion générale
    for key, value in data.items():
        if isinstance(value, dict):
            result = _extract_engagement_recursive(value, depth + 1)
            if result != empty:
                return result
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = _extract_engagement_recursive(item, depth + 1)
                    if result != empty:
                        return result

    return empty
# ─── GRAPHQL PARSER POSTS ─────────────────────────────────────────────────────

def _parse_graphql_responses_posts(responses: list) -> list:
    posts = []
    seen_messages = set()
    for url, body in responses:
        for line in body.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                posts.extend(_find_posts_recursive(data, seen_messages))
            except:
                pass
    return posts


def _find_posts_recursive(data, seen_messages: set, depth=0) -> list:
    if depth > 15 or not isinstance(data, dict):
        return []
    posts = []
    typename = data.get("__typename", "")
    if typename in ("Story", "XFBPost", "Post", "CometFeedStoryNode"):
        post = _extract_post_from_story_node(data)
        if post and post["message"] not in seen_messages and len(post["message"]) > 15:
            seen_messages.add(post["message"])
            posts.append(post)
            return posts
    if "message" in data and isinstance(data.get("message"), dict):
        text = data["message"].get("text", "")
        if text and len(text) > 20 and text not in seen_messages:
            url_val = data.get("url", "") or data.get("permalink_url", "") or data.get("wwwURL", "")
            if isinstance(url_val, str) and "facebook.com" in url_val:
                seen_messages.add(text)
                timestamp = ""
                for key in DATE_KEYS:
                    val = data.get(key)
                    try:
                        ts = int(val)
                        if ts > 1_000_000_000:
                            timestamp = str(ts)
                            break
                    except (TypeError, ValueError):
                        pass
                if not timestamp:
                    timestamp = _extract_timestamp_recursive(data)

                # ── Engagement ──
                engagement = _extract_engagement_recursive(data)

                posts.append({
                    "message": text.strip(),
                    "post_url": clean_post_url(url_val),
                    "date": timestamp,
                    "reactions": engagement["reactions"],
                    "comments_count": engagement["comments_count"],
                    "shares": engagement["shares"],
                })
                return posts
    for key, value in data.items():
        if isinstance(value, dict):
            posts.extend(_find_posts_recursive(value, seen_messages, depth + 1))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    posts.extend(_find_posts_recursive(item, seen_messages, depth + 1))
    return posts


def _debug_dump_feedback(data: dict, post_id: str = "", depth: int = 0):
    if depth > 6:
        return
    fb = data.get("feedback")
    if fb and isinstance(fb, dict):
        print(f"[DEBUG feedback keys post={post_id} depth={depth}]: {list(fb.keys())}")
        for k in fb:
            if any(x in k.lower() for x in ("comment", "share", "count", "react")):
                print(f"  └─ {k}: {fb[k]}")
        # ✅ Creuser dans story si présent
        story = fb.get("story")
        if story and isinstance(story, dict):
            print(f"  [story keys]: {list(story.keys())}")
            story_fb = story.get("feedback")
            if story_fb and isinstance(story_fb, dict):
                print(f"  [story.feedback keys]: {list(story_fb.keys())}")
                for k in story_fb:
                    if any(x in k.lower() for x in ("comment","share","count","react")):
                        print(f"    └─ {k}: {story_fb[k]}")

    # Creuser dans comet_sections
    comet = data.get("comet_sections")
    if comet and isinstance(comet, dict):
        print(f"[DEBUG comet_sections keys post={post_id}]: {list(comet.keys())}")
        fb2 = comet.get("feedback")
        if fb2 and isinstance(fb2, dict):
            print(f"  [comet.feedback keys]: {list(fb2.keys())}")
            story2 = fb2.get("story")
            if story2 and isinstance(story2, dict):
                print(f"  [comet.feedback.story keys]: {list(story2.keys())}")
                story2_fb = story2.get("feedback")
                if story2_fb and isinstance(story2_fb, dict):
                    print(f"  [comet.feedback.story.feedback keys]: {list(story2_fb.keys())}")
                    for k in story2_fb:
                        print(f"    └─ {k}: {story2_fb[k]}")

    for key, value in data.items():
        if key in ("feedback", "comet_sections"):
            continue
        if isinstance(value, dict):
            _debug_dump_feedback(value, post_id, depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _debug_dump_feedback(item, post_id, depth + 1)

def _extract_post_from_story_node(node: dict) -> dict | None:
    message = ""
    msg = node.get("message")
    if isinstance(msg, dict):
        message = msg.get("text", "")
    if not message:
        try:
            message = (node.get("comet_sections", {})
                           .get("content", {})
                           .get("story", {})
                           .get("message", {})
                           .get("text", ""))
        except:
            pass
    if not message:
        body = node.get("body")
        if isinstance(body, dict):
            message = body.get("text", "")
    if not message or len(message) < 10:
        return None

    post_id = str(node.get("post_id", ""))
    page_id = ""
    page_name = ""
    try:
        owning = node.get("feedback", {}).get("owning_profile", {})
        page_id = str(owning.get("id", ""))
        profile_url = owning.get("url", "") or owning.get("profile_url", "")
        if profile_url and "facebook.com/" in profile_url:
            page_name = extract_page_name_from_url(profile_url)
    except:
        pass

    post_url = ""
    if post_id and page_id:
        post_url = f"https://www.facebook.com/permalink.php?story_fbid={post_id}&id={page_id}"

    timestamp = ""
    for key in DATE_KEYS:
        val = node.get(key)
        try:
            ts = int(val)
            if ts > 1_000_000_000:
                timestamp = str(ts)
                break
        except (TypeError, ValueError):
            pass
    if not timestamp:
        fb = node.get("feedback", {}) or {}
        for key in DATE_KEYS:
            val = fb.get(key)
            try:
                ts = int(val)
                if ts > 1_000_000_000:
                    timestamp = str(ts)
                    break
            except (TypeError, ValueError):
                pass
    if not timestamp:
        comet = node.get("comet_sections", {}) or {}
        timestamp = _extract_timestamp_recursive(comet)
    if not timestamp:
        timestamp = _extract_timestamp_recursive(node)
    try:
        if int(timestamp) < 1_000_000_000:
            timestamp = ""
    except (TypeError, ValueError):
        timestamp = ""
    if not timestamp:
        print(f"[!] Date introuvable pour post_id={post_id}")

    # ── Engagement : on passe le nœud complet pour chercher dans feedback ──
    engagement = _extract_engagement_recursive(node)
    #_debug_dump_feedback(node, post_id=post_id)
    

    return {
        "message": message.strip(),
        "post_url": post_url,
        "post_id": post_id,
        "page_id": page_id,
        "page_name": page_name,
        "date": timestamp,
        "reactions": engagement["reactions"],
        "comments_count": engagement["comments_count"],
        "shares": engagement["shares"],
    }


# ─── GRAPHQL PARSER COMMENTS ──────────────────────────────────────────────────

def _parse_graphql_responses_comments(responses: list) -> list:
    comments = []
    seen = set()
    for body in responses:
        for line in body.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                found = _extract_comments_from_feedback(data, seen)
                if not found:
                    found = _find_comments_recursive(data, seen)
                comments.extend(found)
            except:
                pass
    return comments


def _extract_comments_from_feedback(data, seen: set, depth=0) -> list:
    if depth > 20 or not isinstance(data, dict):
        return []
    comments = []
    for path_key in ("comment_list_renderer", "comment_rendering_instance"):
        if path_key in data:
            sub = data[path_key]
            if not isinstance(sub, dict):
                continue
            for target in [sub.get("feedback", {}), sub]:
                if not isinstance(target, dict):
                    continue
                edges = (target.get("comments", {}) or {}).get("edges", [])
                for edge in edges:
                    node = edge.get("node", {}) if isinstance(edge, dict) else {}
                    if isinstance(node, dict):
                        c = _extract_comment_node(node)
                        if c and c["message"] not in seen:
                            seen.add(c["message"])
                            comments.append(c)
                        comments.extend(_extract_nested_replies(node, seen))
    if "comments" in data and isinstance(data["comments"], dict):
        edges = data["comments"].get("edges", [])
        for edge in edges:
            node = edge.get("node", {}) if isinstance(edge, dict) else {}
            if isinstance(node, dict) and node.get("__typename") == "Comment":
                c = _extract_comment_node(node)
                if c and c["message"] not in seen:
                    seen.add(c["message"])
                    comments.append(c)
                comments.extend(_extract_nested_replies(node, seen))
    for key, value in data.items():
        if key == "comments":
            continue
        if isinstance(value, dict):
            comments.extend(_extract_comments_from_feedback(value, seen, depth + 1))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    comments.extend(_extract_comments_from_feedback(item, seen, depth + 1))
    return comments


def _extract_nested_replies(comment_node: dict, seen: set) -> list:
    replies = []
    for replies_path in [
        comment_node.get("feedback", {}).get("replies", {}),
        comment_node.get("replies", {}),
    ]:
        if not isinstance(replies_path, dict):
            continue
        for edge in replies_path.get("edges", []):
            node = edge.get("node", {}) if isinstance(edge, dict) else {}
            if isinstance(node, dict):
                c = _extract_comment_node(node)
                if c and c["message"] not in seen:
                    seen.add(c["message"])
                    replies.append(c)
    return replies


def _find_comments_recursive(data, seen: set, depth=0) -> list:
    if depth > 15 or not isinstance(data, dict):
        return []
    comments = []
    typename = data.get("__typename", "")
    if typename == "Comment":
        comment = _extract_comment_node(data)
        if comment and comment["message"] not in seen:
            seen.add(comment["message"])
            comments.append(comment)
            comments.extend(_extract_nested_replies(data, seen))
        return comments
    for key, value in data.items():
        if isinstance(value, dict):
            comments.extend(_find_comments_recursive(value, seen, depth + 1))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    comments.extend(_find_comments_recursive(item, seen, depth + 1))
    return comments


def _extract_comment_node(node: dict) -> dict | None:
    message = ""
    body = node.get("body")
    if isinstance(body, dict):
        message = body.get("text", "")
    if not message:
        msg = node.get("message")
        if isinstance(msg, dict):
            message = msg.get("text", "")
    if not message or len(message.strip()) < 2:
        return None
    author = ""
    for key in ("author", "commenter"):
        a = node.get(key, {})
        if isinstance(a, dict):
            author = a.get("name", "")
            if author:
                break
    return {"message": message.strip(), "author": author}


# ─── SCROLL + INTERCEPTION ────────────────────────────────────────────────────

def _scroll_and_collect(page, scrolls=8, max_posts=None, label="") -> list:
    collected = []
    stopped = False

    def handle_response(response):
        if stopped:
            return
        if "graphql" in response.url or "api/graphql" in response.url:
            try:
                body = response.text()
                if body and len(body) > 100:
                    collected.append((response.url, body))
                    print(f"[*] GraphQL {label}: {len(body)} chars")
            except:
                pass

    page.on("response", handle_response)

    try:
        for i in range(scrolls):
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                _human_delay(2.5, 3.5)
                print(f"[*] Scroll {i+1}/{scrolls}")
                if max_posts and len(collected) > 0:
                    current_posts = _parse_graphql_responses_posts(collected)
                    if len(current_posts) >= max_posts:
                        print(f"[*] Limite {max_posts} posts atteinte, arrêt scroll")
                        break
            except:
                time.sleep(2)
    finally:
        stopped = True
        try:
            page.remove_listener("response", handle_response)
        except:
            pass
        print(f"[*] Total GraphQL {label}: {len(collected)}")

    return collected


# ─── DOM FALLBACK ─────────────────────────────────────────────────────────────

def _extract_posts_dom(page) -> list:
    try:
        result = page.evaluate("""
            () => {
                const posts = [], seen = new Set();
                const articles = Array.from(document.querySelectorAll('div[role="article"]'))
                    .filter(a => {
                        let p = a.parentElement;
                        while (p) {
                            if (p.getAttribute?.('role') === 'article') return false;
                            p = p.parentElement;
                        }
                        return true;
                    });
                articles.forEach(article => {
                    const clone = article.cloneNode(true);
                    clone.querySelectorAll('div[role="article"], ul, ol, form').forEach(e => e.remove());
                    let message = '';
                    const msgSelectors = [
                        'div[data-ad-comet-preview="message"]',
                        'div[data-ad-preview="message"]',
                        '[data-testid="post_message"]',
                    ];
                    for (const sel of msgSelectors) {
                        const el = clone.querySelector(sel);
                        const txt = (el?.innerText || '').trim();
                        if (txt.length > 20) { message = txt; break; }
                    }
                    if (!message) {
                        let longest = '';
                        clone.querySelectorAll('div[dir="auto"], span[dir="auto"]').forEach(d => {
                            const t = (d.innerText || d.textContent || '').trim();
                            if (t.length > longest.length && t.length > 20) longest = t;
                        });
                        message = longest;
                    }
                    if (!message || seen.has(message)) return;
                    seen.add(message);
                    let postUrl = '';
                    for (const sel of ['a[href*="/posts/"]','a[href*="story_fbid"]','a[href*="pfbid"]']) {
                        const l = article.querySelector(sel);
                        if (l?.href) { postUrl = l.href.split('?')[0]; break; }
                    }
                    let dateTimestamp = '';
                    const timeEl = article.querySelector('time[datetime]');
                    if (timeEl) {
                        const dt = timeEl.getAttribute('datetime');
                        if (dt) {
                            const ms = new Date(dt).getTime();
                            if (!isNaN(ms) && ms > 0) {
                                dateTimestamp = Math.floor(ms / 1000).toString();
                            }
                        }
                    }
                    if (!dateTimestamp) {
                        const abbrUtime = article.querySelector('abbr[data-utime]');
                        if (abbrUtime) dateTimestamp = abbrUtime.getAttribute('data-utime') || '';
                    }
                    if (!dateTimestamp) {
                        const dateSelectors = ['a[role="link"] > span[aria-label]','span[aria-label]','a[aria-label]'];
                        for (const sel of dateSelectors) {
                            const elements = article.querySelectorAll(sel);
                            for (const el of elements) {
                                const label = el.getAttribute('aria-label') || '';
                                if (/\\b(20\\d{2})\\b/.test(label) ||
                                    /janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre/i.test(label) ||
                                    /january|february|march|april|june|july|august|september|october|november|december/i.test(label)) {
                                    dateTimestamp = label; break;
                                }
                            }
                            if (dateTimestamp) break;
                        }
                    }
                    if (!dateTimestamp && postUrl) {
                        const m = postUrl.match(/\\/posts\\/(\\d{10,})/);
                        if (m) dateTimestamp = m[1];
                    }
                    if (!dateTimestamp) {
                        const relativePatterns = [
                            /il y a \\d+ (minute|heure|jour|semaine|mois|an)/i,
                            /\\d+ (minute|hour|day|week|month|year)s? ago/i,
                            /yesterday|hier/i,
                            /just now|à l'instant/i,
                        ];
                        const allText = article.innerText || '';
                        for (const pattern of relativePatterns) {
                            const match = allText.match(pattern);
                            if (match) { dateTimestamp = match[0]; break; }
                        }
                    }

                    // ── Engagement DOM ──────────────────────────────────────
                    let reactions = '0';
                    let commentsCount = '0';
                    let shares = '0';

                    // Réactions : cherche le span avec aria-label contenant "reaction"
                    const reactionSelectors = [
                        'span[aria-label*="reaction"]',
                        'span[aria-label*="réaction"]',
                        'span[aria-label*="J\'aime"]',
                        'div[aria-label*="reaction"]',
                    ];
                    for (const sel of reactionSelectors) {
                        const el = article.querySelector(sel);
                        if (el) {
                            const label = el.getAttribute('aria-label') || '';
                            const nums = label.match(/[\\d\\s,\\.]+/g);
                            if (nums) {
                                const n = parseInt(nums[0].replace(/[^\\d]/g, ''), 10);
                                if (!isNaN(n)) { reactions = String(n); break; }
                            }
                            // Sinon essaie le texte interne (ex: "1,2K" ou "1 200")
                            const txt = (el.innerText || el.textContent || '').trim();
                            if (txt) { reactions = txt; break; }
                        }
                    }

                    // Commentaires : bouton ou lien "X commentaires"
                    const commentSelectors = [
                        'a[href*="comment"]',
                        'span[aria-label*="comment"]',
                        'div[aria-label*="comment"]',
                    ];
                    for (const sel of commentSelectors) {
                        const els = article.querySelectorAll(sel);
                        for (const el of els) {
                            const label = el.getAttribute('aria-label') || el.innerText || '';
                            const m2 = label.match(/(\\d[\\d\\s,.]*)/);
                            if (m2) {
                                const n = parseInt(m2[1].replace(/[^\\d]/g, ''), 10);
                                if (!isNaN(n) && n > 0) { commentsCount = String(n); break; }
                            }
                        }
                        if (commentsCount !== '0') break;
                    }

                    // Partages : bouton ou texte "X partages" / "X shares"
                    const shareSelectors = [
                        'span[aria-label*="share"]',
                        'span[aria-label*="partage"]',
                        'div[aria-label*="share"]',
                    ];
                    for (const sel of shareSelectors) {
                        const el = article.querySelector(sel);
                        if (el) {
                            const label = el.getAttribute('aria-label') || el.innerText || '';
                            const m3 = label.match(/(\\d[\\d\\s,.]*)/);
                            if (m3) {
                                const n = parseInt(m3[1].replace(/[^\\d]/g, ''), 10);
                                if (!isNaN(n)) { shares = String(n); break; }
                            }
                        }
                    }
                    // Fallback texte brut : "1 234 partages" / "1,234 shares"
                    if (shares === '0') {
                        const rawText = article.innerText || '';
                        const mShares = rawText.match(/(\\d[\\d\\s,.]*)\\s*(partages?|shares?)/i);
                        if (mShares) {
                            const n = parseInt(mShares[1].replace(/[^\\d]/g, ''), 10);
                            if (!isNaN(n)) shares = String(n);
                        }
                    }

                    posts.push({
                        message: message.substring(0, 2000),
                        post_url: postUrl,
                        date: dateTimestamp,
                        reactions: reactions,
                        comments_count: commentsCount,
                        shares: shares,
                        source: 'dom'
                    });
                });
                return posts.filter(p => p.message.length > 15);
            }
        """)
        print(f"[*] DOM fallback: {len(result)} posts trouvés")
        no_date = [p for p in result if not p.get("date")]
        if no_date:
            print(f"[!] {len(no_date)} posts sans date détectée (DOM)")
        return result
    except Exception as e:
        print(f"[!] DOM posts error: {e}")
        return []


def _extract_comments_dom(page) -> list:
    try:
        return page.evaluate("""
            () => {
                const comments = [], seen = new Set();
                document.querySelectorAll(
                    'div[aria-label^="Comment by"], div[aria-label^="Commentaire de"]'
                ).forEach(el => {
                    el.querySelectorAll('div[dir="auto"]').forEach(div => {
                        const text = (div.innerText || div.textContent || '').trim();
                        if (text.length > 1 && !seen.has(text)) {
                            seen.add(text);
                            const a = el.querySelector('a[role="link"] span');
                            comments.push({ message: text, author: a?.innerText?.trim() || '' });
                        }
                    });
                });
                if (comments.length > 0) return comments;
                document.querySelectorAll('ul > li').forEach(el => {
                    el.querySelectorAll('div[dir="auto"]').forEach(div => {
                        const text = (div.innerText || div.textContent || '').trim();
                        if (text.length > 1 && !seen.has(text)) {
                            seen.add(text);
                            const a = el.querySelector('a[role="link"] span');
                            comments.push({ message: text, author: a?.innerText?.trim() || '' });
                        }
                    });
                });
                return comments;
            }
        """)
    except Exception as e:
        print(f"[!] DOM comments error: {e}")
        return []


# ─── SCRAPING PAGE ─────────────────────────────────────────────────────────────

def scrape_facebook_page(url: str, max_posts: int = None) -> dict:
    page = get_page()
    try:
        if not _warmup_session(page):
            return {"posts": [], "total": 0, "error": "login_required"}

        if is_share_url(url):
            page_url = _resolve_share_url(page, url)
        else:
            page_url = extract_page_base_url(url)

        print(f"[*] Navigation vers: {page_url}")
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("body", timeout=15000)
        _human_delay(3, 5)
        _close_login_popup(page)

        if _is_login_wall(page):
            return {"posts": [], "error": "login_required"}

        if _is_redirected_to_home(page, page_url):
            return {"posts": [], "error": "page_not_found_or_private"}

        scrolls = 6
        if max_posts:
            scrolls = max(3, (max_posts // 3) + 2)

        graphql_responses = _scroll_and_collect(
            page, scrolls=scrolls, max_posts=max_posts, label="posts"
        )
        posts = _parse_graphql_responses_posts(graphql_responses)

        if not posts:
            print("[*] Aucun résultat GraphQL → fallback DOM")
            try:
                page.wait_for_selector("time[datetime]", timeout=5000)
            except:
                pass
            posts = _extract_posts_dom(page)

        if max_posts and len(posts) > max_posts:
            posts = posts[:max_posts]

        print(f"[+] {len(posts)} posts collectés pour {url}")
        return {"posts": posts, "total": len(posts), "url": url}

    except Exception as e:
        print(f"[!] Erreur scrape_facebook_page: {e}")
        return {"posts": [], "error": str(e), "url": url}

    finally:
        try:
            page.remove_all_listeners()
        except:
            pass
        try:
            page.close()
        except:
            pass
        print(f"[*] Page fermée pour {url}")


# ─── SCRAPING PAGE AVEC FILTRE DATE ───────────────────────────────────────────

def scrape_facebook_page_with_dates(
    url: str,
    date_from: datetime,
    date_to: datetime,
    max_scrolls: int = 15
) -> dict:
    page = get_page()
    try:
        if not _warmup_session(page):
            return {"posts": [], "total": 0, "error": "login_required"}

        if is_share_url(url):
            page_url = _resolve_share_url(page, url)
        else:
            page_url = extract_page_base_url(url)

        print(f"[*] Navigation vers: {page_url}")
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("body", timeout=15000)
        _human_delay(3, 5)
        _close_login_popup(page)

        if _is_login_wall(page):
            return {"posts": [], "error": "login_required"}

        if _is_redirected_to_home(page, page_url):
            return {"posts": [], "error": "page_not_found_or_private"}

        graphql_responses = _scroll_and_collect(page, scrolls=max_scrolls, label="posts_dates")
        all_posts = _parse_graphql_responses_posts(graphql_responses)

        if not all_posts:
            print("[*] Aucun résultat GraphQL → fallback DOM")
            try:
                page.wait_for_selector("time[datetime]", timeout=5000)
            except:
                pass
            all_posts = _extract_posts_dom(page)

        filtered = filter_posts_by_date(all_posts, date_from, date_to)

        if not filtered and all_posts:
            print("[!] Pas de timestamps disponibles, retour de tous les posts")
            filtered = all_posts

        print(f"[+] {len(filtered)}/{len(all_posts)} posts dans la plage de dates pour {url}")
        return {
            "posts": filtered,
            "total": len(filtered),
            "total_scraped": len(all_posts),
            "url": url,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }

    except Exception as e:
        print(f"[!] Erreur scrape_facebook_page_with_dates: {e}")
        return {"posts": [], "error": str(e), "url": url}

    finally:
        try:
            page.remove_all_listeners()
        except:
            pass
        try:
            page.close()
        except:
            pass
        print(f"[*] Page fermée pour {url}")


# ─── SCRAPING COMMENTAIRES ────────────────────────────────────────────────────

def scrape_post_comments(post_url: str, page_name: str = "") -> list:
    print(f"[*] Scraping commentaires: {post_url}")

    parsed = urlparse(post_url)
    params = parse_qs(parsed.query)
    post_id = params.get("story_fbid", [""])[0]
    page_id_from_url = params.get("id", [""])[0]

    if not post_id:
        m = re.search(r'/posts/(\d+)', post_url)
        if m:
            post_id = m.group(1)

    if not page_name:
        page_name = extract_page_name_from_url(post_url)

    candidates = build_post_url_candidates(
        post_url=post_url,
        post_id=post_id,
        page_id=page_id_from_url,
        page_name=page_name,
    )
    print(f"[*] URLs candidates: {candidates}")

    comments = []
    page = get_page()
    collected_bodies = []
    stopped = False

    def handle_response(response):
        if stopped:
            return
        if "graphql" in response.url:
            try:
                body = response.text()
                if body and len(body) > 50:
                    collected_bodies.append(body)
                    print(f"[*] GraphQL commentaires: {len(body)} chars")
            except:
                pass

    try:
        if not _warmup_session(page):
            return []

        page.on("response", handle_response)

        for candidate in candidates:
            print(f"[*] Tentative: {candidate}")
            try:
                page.goto(candidate, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("body", timeout=15000)
                _human_delay(3, 5)
                _close_login_popup(page)
                if _is_login_wall(page):
                    return []
                if not _is_redirected_to_home(page, candidate):
                    print(f"[+] OK: {candidate}")
                    break
            except:
                continue

        try:
            for selector in [
                'div[aria-label*="comment"]',
                'span[aria-label*="comment"]',
                'a[href*="comment"]',
            ]:
                el = page.query_selector(selector)
                if el:
                    el.click()
                    print("[+] Ouverture commentaires")
                    _human_delay(2, 3)
                    break
        except:
            pass

        try:
            for text in ["All comments", "Tous les commentaires"]:
                btn = page.get_by_role("button", name=text)
                if btn.count() > 0:
                    btn.first.click()
                    print(f"[+] Tri: {text}")
                    _human_delay(2, 3)
                    break
        except:
            pass

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        for i in range(10):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _human_delay(2, 3)
            print(f"[*] Scroll {i+1}/10")
            for text in [
                "View more comments", "Voir plus de commentaires",
                "View more replies", "Voir plus de réponses"
            ]:
                try:
                    btn = page.get_by_role("button", name=text)
                    if btn.count() > 0:
                        btn.first.click()
                        print(f"[+] {text}")
                        _human_delay(1.5, 2)
                except:
                    pass
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except:
                pass

        print(f"[*] Total GraphQL capturé: {len(collected_bodies)}")

        if collected_bodies:
            comments = _parse_graphql_responses_comments(collected_bodies)

        print(f"[*] Commentaires GraphQL: {len(comments)}")

        if not comments:
            print("[*] Fallback DOM...")
            comments = _extract_comments_dom(page)

        for i, c in enumerate(comments[:5]):
            print(f"[*] {i+1}. {c.get('author','?')}: {c.get('message','')[:80]}")

    except Exception as e:
        print(f"[!] Erreur: {e}")

    finally:
        stopped = True
        try:
            page.remove_all_listeners()
        except:
            pass
        try:
            page.remove_listener("response", handle_response)
        except:
            pass
        try:
            page.close()
        except:
            pass
        print(f"[*] Page commentaires fermée pour {post_url}")

    return comments