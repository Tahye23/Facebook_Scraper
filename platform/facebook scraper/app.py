from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import atexit

from browser_manager import run_in_browser_thread, stop_browser_thread
from scraper import scrape_facebook_page, scrape_post_comments, close_browser
from batch import scrape_batch_from_csv, scrape_batch_urls_only
from job_manager import create_job, set_running, set_done, set_error, get_job

app = Flask(__name__)
CORS(app)

batch_lock = threading.Lock()
atexit.register(stop_browser_thread)
atexit.register(close_browser)


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ─── SCRAPE PAGE UNIQUE ───────────────────────────────────────────────────────

@app.route("/scrape/page", methods=["POST"])
def scrape_page():
    """
    Scrape une page Facebook (sans filtre date).

    Body JSON:
    {
        "url": "https://www.facebook.com/pagename",
        "max_posts": 5          ← optionnel
    }
    """
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "URL manquante"}), 400

    url = data["url"]
    if "facebook.com" not in url and "fb.com" not in url:
        return jsonify({"error": "URL Facebook invalide"}), 400

    max_posts = data.get("max_posts", None)
    if max_posts is not None:
        try:
            max_posts = int(max_posts)
        except Exception:
            return jsonify({"error": "max_posts doit être un entier"}), 400

    try:
        result = run_in_browser_thread(scrape_facebook_page, url, max_posts=max_posts)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── SCRAPE COMMENTAIRES ──────────────────────────────────────────────────────

@app.route("/scrape/comments", methods=["POST"])
def scrape_comments():
    """
    Scrape les commentaires d'un post.

    Body JSON:
    {
        "post_url": "https://www.facebook.com/permalink.php?story_fbid=...&id=..."
    }
    """
    data = request.get_json()
    if not data or "post_url" not in data:
        return jsonify({"error": "post_url manquant"}), 400

    post_url = data["post_url"]
    try:
        comments = run_in_browser_thread(scrape_post_comments, post_url)
        return jsonify({"comments": comments, "total": len(comments)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── BATCH AVEC FILTRE DATE ───────────────────────────────────────────────────

@app.route("/scrape/batch", methods=["POST"])
def scrape_batch():
    """
    Batch scraping avec filtre de dates.

    Body JSON:
    {
        "csv_path": "pages.csv",
        "date_from": "2024-01-01",
        "date_to":   "2024-12-31"
    }
    """
    if not batch_lock.acquire(blocking=False):
        return jsonify({"error": "Un batch est déjà en cours"}), 429

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Body JSON manquant"}), 400

        csv_path  = data.get("csv_path")
        date_from = data.get("date_from")
        date_to   = data.get("date_to")

        if not csv_path or not date_from or not date_to:
            return jsonify({
                "error": "Paramètres manquants: csv_path, date_from, date_to obligatoires"
            }), 400

        print(f"\n{'='*60}")
        print(f"[BATCH DATE] csv_path  = {csv_path}")
        print(f"[BATCH DATE] date_from = {date_from}")
        print(f"[BATCH DATE] date_to   = {date_to}")
        print(f"{'='*60}")

        result = scrape_batch_from_csv(csv_path, date_from, date_to)

        print(f"\n[BATCH DATE] Résultat: {len(result)} page(s)")
        for r in result:
            print(f"  → url={r.get('url')} | posts={r.get('total', 0)} | error={r.get('error', '')}")

        return jsonify({"results": result, "pages_count": len(result)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        batch_lock.release()


# ─── BATCH URLS SEULEMENT (async, sans date) ──────────────────────────────────

@app.route("/scrape/batch/urls", methods=["POST"])
def scrape_batch_urls_async():
    """
    Lance un batch asynchrone sans filtre de dates.

    Body JSON:
    {
        "csv_path":  "pages.csv",
        "max_posts": 5          ← optionnel
    }

    Retourne immédiatement un job_id.
    Interroger GET /scrape/job/<job_id> pour suivre l'avancement.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON manquant"}), 400

    csv_path  = data.get("csv_path")
    max_posts = data.get("max_posts", None)

    if not csv_path:
        return jsonify({"error": "csv_path obligatoire"}), 400

    if max_posts is not None:
        try:
            max_posts = int(max_posts)
        except Exception:
            return jsonify({"error": "max_posts doit être un entier"}), 400

    job_id = create_job()

    def worker():
        try:
            set_running(job_id)
            # scrape_batch_urls_only appelle run_in_browser_thread page par page
            # → Playwright reste dans son BrowserThread dédié
            result = scrape_batch_urls_only(csv_path, max_posts)
            set_done(job_id, result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            set_error(job_id, e)

    threading.Thread(target=worker, daemon=True).start()

    return jsonify({"job_id": job_id, "status": "started"})


# ─── STATUT JOB ───────────────────────────────────────────────────────────────

@app.route("/scrape/job/<job_id>", methods=["GET"])
def get_job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job introuvable"}), 404
    return jsonify(job)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)