from flask import Flask, jsonify, request
from flask_cors import CORS
from pathlib import Path
import os

from scraper import scrape_tiktok_page
from video_analysis import analyze_tiktok_video

app = Flask(__name__)
CORS(app)


def _load_env_file():
    root_env = Path(__file__).resolve().parents[2] / ".env"
    if not root_env.exists():
        return

    for raw_line in root_env.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value.strip()


_load_env_file()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tiktok-local"})


@app.route("/scrape/page", methods=["POST"])
def scrape_page():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    if "tiktok.com" not in url.lower():
        return jsonify({"error": "URL TikTok invalide"}), 400

    max_posts = data.get("max_posts", 20)
    try:
        max_posts = int(max_posts)
    except Exception:
        return jsonify({"error": "max_posts doit etre un entier"}), 400

    raw_max_age_hours = data.get("max_age_hours")
    if raw_max_age_hours in (None, ""):
        max_age_hours = None
    else:
        try:
            max_age_hours = int(raw_max_age_hours)
        except Exception:
            return jsonify({"error": "max_age_hours doit etre un entier"}), 400

    raw_headless = data.get("headless", False)
    if isinstance(raw_headless, bool):
        headless = raw_headless
    else:
        headless = str(raw_headless).strip().lower() in ("1", "true", "yes", "y", "on")

    try:
        result = scrape_tiktok_page(
            url=url,
            max_posts=max_posts,
            max_age_hours=max_age_hours,
            headless_override=headless,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/analyze/video", methods=["POST"])
def analyze_video():
    data = request.get_json(silent=True) or {}
    video_url = (data.get("video_url") or data.get("url") or "").strip()
    if not video_url:
        return jsonify({"error": "video_url manquante"}), 400
    if "tiktok.com" not in video_url.lower():
        return jsonify({"error": "video_url TikTok invalide"}), 400

    output_dir = (data.get("output_dir") or "").strip() or None
    try:
        report = analyze_tiktok_video(video_url=video_url, output_dir=output_dir)
        return jsonify(report)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
