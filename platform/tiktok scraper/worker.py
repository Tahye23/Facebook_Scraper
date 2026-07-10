"""Worker de scraping TikTok.

Ce module consomme des taches de scraping depuis RabbitMQ, execute le
scraping TikTok, publie les posts en flux, puis envoie les evenements de fin
ou d'erreur.

Flux d'execution:
1) Charger les variables d'environnement (.env en fallback).
2) Se connecter a RabbitMQ avec retry.
3) Consommer les taches de scraping TikTok.
4) Pour chaque tache, lancer le scraper dans un thread et publier les posts au fil de l'eau.
5) Generer les rapports de session (JSON/HTML/PDF) si possible.
6) Publier un evenement COMPLETED ou ERROR.
"""

import json
import os
import queue
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from html import escape

import pika

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger, with_context
from scraper import scrape_tiktok_page
from video_analysis import (
    analyze_tiktok_video,
    analyze_videos_json_with_gemini,
    build_session_json_report,
    build_small_video_report,
)


LOGGER = get_logger(__name__, platform="tiktok", service="worker")


def _load_env_file():
    """Charge les variables .env sans ecraser les variables deja definies.

    Ordre de recherche:
    - .env a la racine du repo (deux niveaux au-dessus de ce fichier)
    - .env du dossier platform (un niveau au-dessus)
    - .env du repertoire de travail courant

    Notes:
    - Les variables deja presentes dans l'OS sont conservees.
    - Les lignes vides, commentees ou invalides sont ignorees.
    """
    candidates = [
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path.cwd() / ".env",
    ]

    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip())


_load_env_file()

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")

EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "scrape.exchange")
QUEUE_CONSUME = os.getenv("RABBITMQ_QUEUE", "scraping_queue_tiktok")
QUEUE_RESULT = os.getenv("RABBITMQ_RESULT_QUEUE", "scrape_result_queue")
ROUTING_RESULT = "scrape.result"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _load_html_template(template_name: str) -> str:
    """Charge un template HTML depuis le dossier templates du worker."""
    template_path = TEMPLATE_DIR / template_name
    return template_path.read_text(encoding="utf-8")


def _render_html_template(template_name: str, replacements: dict[str, str]) -> str:
    """Rend un template HTML par remplacement de placeholders simples."""
    html = _load_html_template(template_name)
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def _to_int(value) -> int:
    """Convertit en entier au mieux, sinon retourne 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_session_html_report(page_url: str, posts: list[dict], output_dir: Path) -> str:
    """Genere un rapport HTML lisible pour une session de scraping TikTok.

    Le rapport contient:
    - Metadonnees de session (page source, horodatage)
    - KPI agreges (posts, likes, commentaires, partages, vues)
    - Une ligne par post avec un court resume IA quand disponible

    Retourne:
        Le chemin du fichier HTML genere.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"session_report_{ts}.html"

    total_posts = len(posts)
    total_likes = sum(_to_int(p.get("likes") or p.get("metrics", {}).get("likes")) for p in posts)
    total_comments = sum(_to_int(p.get("comments_count") or p.get("metrics", {}).get("comments")) for p in posts)
    total_shares = sum(_to_int(p.get("shares") or p.get("metrics", {}).get("shares")) for p in posts)
    total_views = sum(_to_int(p.get("views") or p.get("metrics", {}).get("views")) for p in posts)

    rows = []
    for idx, post in enumerate(posts, start=1):
        post_url = post.get("post_url") or post.get("sourceUrl") or ""
        author = post.get("author") or ""
        report = post.get("video_report") if isinstance(post.get("video_report"), dict) else {}
        summary = report.get("executive_summary") or []
        summary_text = " ".join(str(x) for x in summary[:2])
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{escape(author)}</td>"
            f"<td><a href='{escape(post_url)}' target='_blank'>{escape(post_url)}</a></td>"
            f"<td>{_to_int(post.get('likes') or post.get('metrics', {}).get('likes'))}</td>"
            f"<td>{_to_int(post.get('comments_count') or post.get('metrics', {}).get('comments'))}</td>"
            f"<td>{_to_int(post.get('shares') or post.get('metrics', {}).get('shares'))}</td>"
            f"<td>{_to_int(post.get('views') or post.get('metrics', {}).get('views'))}</td>"
            f"<td>{escape(summary_text)}</td>"
            "</tr>"
        )

    html = _render_html_template(
        "session_report.html",
        {
            "__PAGE_URL__": escape(page_url),
            "__GENERATED_AT__": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "__TOTAL_POSTS__": str(total_posts),
            "__TOTAL_LIKES__": str(total_likes),
            "__TOTAL_COMMENTS__": str(total_comments),
            "__TOTAL_SHARES__": str(total_shares),
            "__TOTAL_VIEWS__": str(total_views),
            "__ROWS__": "".join(rows),
        },
    )

    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def _build_session_pdf_report(posts: list[dict], output_dir: Path) -> str | None:
    """Genere un PDF synthetique de la session de scraping.

    Retourne:
        Chemin du PDF genere, ou None si reportlab n'est pas installe.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"session_report_{ts}.pdf"

    pdf = canvas.Canvas(str(out_path), pagesize=A4)
    width, height = A4
    y = height - 40

    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(40, y, "Rapport Session TikTok")
    y -= 24

    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, y, f"Generated at: {datetime.now(tz=timezone.utc).isoformat()}")
    y -= 20

    for idx, post in enumerate(posts, start=1):
        if y < 70:
            pdf.showPage()
            y = height - 40
            pdf.setFont("Helvetica", 10)
        post_url = str(post.get("post_url") or post.get("sourceUrl") or "")
        author = str(post.get("author") or "")
        likes = _to_int(post.get("likes") or post.get("metrics", {}).get("likes"))
        comments = _to_int(post.get("comments_count") or post.get("metrics", {}).get("comments"))
        shares = _to_int(post.get("shares") or post.get("metrics", {}).get("shares"))
        views = _to_int(post.get("views") or post.get("metrics", {}).get("views"))

        pdf.drawString(40, y, f"{idx}. {author} | likes={likes}, comments={comments}, shares={shares}, views={views}")
        y -= 14
        pdf.drawString(56, y, post_url[:130])
        y -= 18

    pdf.save()
    return str(out_path)


def _safe_int(val) -> int | None:
    """Convertit une valeur en int, ou None si conversion impossible."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def normalize_post(scrape_id: str, url: str, post: dict) -> dict:
    """Normalise un post brut vers le format contractuel du gateway.

    Permet de conserver un schema stable entre plateformes pour le stockage
    et le traitement des evenements en aval.
    """
    text = post.get("message") or post.get("text") or ""
    hashtags = [w for w in text.split() if w.startswith("#")]

    return {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "postId": post.get("post_id") or post.get("id") or "",
        "author": post.get("author") or "",
        "textContent": text,
        "hashtags": hashtags,
        "metrics": {
            "likes": _safe_int(post.get("likes")),
            "comments": _safe_int(post.get("comments_count")),
            "shares": _safe_int(post.get("shares")),
            "views": _safe_int(post.get("views")),
        },
        "sourceUrl": post.get("post_url") or url,
        "sourceMediaUrl": post.get("source_media_url"),
        "mediaPath": post.get("media_path"),
        "videoReport": post.get("video_report"),
        "publishedAt": post.get("published_at"),
        "scrapedAt": post.get("scraped_at") or datetime.now(tz=timezone.utc).isoformat(),
        "success": True,
        "errorMessage": None,
    }


def _post_signature(post: dict) -> str:
    """Construit une signature deterministe pour dedupliquer les posts publies.

    Priorite:
    1) id du post quand disponible
    2) fallback URL du post + prefixe du texte
    """
    post_id = str(post.get("post_id") or post.get("id") or "").strip()
    if post_id:
        return f"id:{post_id}"
    post_url = str(post.get("post_url") or post.get("sourceUrl") or "").strip()
    text = str(post.get("message") or post.get("text") or post.get("textContent") or "").strip()
    return f"u:{post_url}|t:{text[:120]}"


def publish_result(channel, result: dict):
    """Publie un evenement resultat (post TikTok normalise) vers RabbitMQ."""
    LOGGER.debug(
        "Publishing post result",
        extra={"scrape_id": result.get("scrapeId"), "post_id": result.get("postId")},
    )
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(result, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )


def publish_error(channel, scrape_id: str, error_msg: str):
    """Publie un evenement de cycle de vie ERROR pour un job de scraping."""
    scoped_logger = with_context(LOGGER, scrape_id=scrape_id)
    payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "ERROR",
        "success": False,
        "errorMessage": error_msg,
    }
    scoped_logger.error("Publishing ERROR event", extra={"url": None})
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )


def publish_completion(channel, scrape_id: str):
    """Publie un evenement de cycle de vie COMPLETED minimal pour un job."""
    scoped_logger = with_context(LOGGER, scrape_id=scrape_id)
    payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "COMPLETED",
        "success": True,
        "errorMessage": None,
    }
    scoped_logger.info("Publishing COMPLETED event")
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )


def publish_enrichment_update(
    channel,
    scrape_id: str,
    url: str,
    post: dict,
    event_type: str,
    success: bool,
    error_message: str | None = None,
):
    payload = normalize_post(scrape_id, url, post)
    post_id = str(payload.get("postId") or "").strip()
    payload.update(
        {
            "eventType": event_type,
            "status": "PARTIAL",
            "success": success,
            "errorMessage": error_message,
            "scrapedAt": datetime.now(tz=timezone.utc).isoformat(),
            "enrichment": {
                "state": "DONE" if success else "FAILED",
            },
        }
    )
    with_context(LOGGER, scrape_id=scrape_id, url=url, post_id=post_id).info(
        "Publishing enrichment update",
        extra={"service": "worker"},
    )
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _enrich_post_video(post: dict, output_dir: str) -> dict:
    post_copy = dict(post)
    post_url = str(post_copy.get("post_url") or "").strip()
    post_description = str(post_copy.get("message") or "").strip()
    if not post_url or "/video/" not in post_url:
        return post_copy

    report = analyze_tiktok_video(
        video_url=post_url,
        output_dir=output_dir,
        save_json_report=True,
        description_text=post_description,
    )
    post_copy["source_media_url"] = report.get("video_metadata", {}).get("media_url")
    post_copy["media_path"] = report.get("artifacts", {}).get("video_path")
    post_copy["video_report"] = build_small_video_report(report)
    post_copy["message"] = post_copy.get("message") or report.get("transcript_excerpt") or ""
    return post_copy


def _build_batch_pages_report(scrape_id: str, source_rows: list[dict], failed_pages: list[dict], output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"csv_pages_report_{scrape_id}_{ts}.json"

    payload = {
        "scrape_id": scrape_id,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "pages_total": len(source_rows) + len(failed_pages),
        "pages_succeeded": len(source_rows),
        "pages_failed": len(failed_pages),
        "pages": source_rows,
        "failed_pages": failed_pages,
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out_path)


def _save_batch_videos_json(scrape_id: str, videos: list[dict], output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"batch_videos_{scrape_id}_{ts}.json"
    payload = {
        "scrape_id": scrape_id,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "videos_count": len(videos),
        "videos": videos,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out_path)


def _build_mauritanie_24h_html_report(
    scrape_id: str,
    source_rows: list[dict],
    gemini_report: dict,
    videos_payload: list[dict] | None,
    failed_pages: list[dict] | None,
    output_dir: Path,
) -> str | None:
    """Genere une version HTML stylisee du rapport Mauritanie 24h via template externe."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"mauritanie_24h_{scrape_id}_{ts}.html"

    def _int_value(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _ratio_color(ratio: float | None) -> str:
        if ratio is None:
            return "#9AA5B1"
        if ratio >= 0.08:
            return "#58A65C"
        if ratio >= 0.03:
            return "#F2C94C"
        return "#E35D5D"

    def _ratio_label(ratio: float | None) -> str:
        if ratio is None:
            return "غير متاح"
        if ratio >= 0.08:
            return "مرتفع"
        if ratio >= 0.03:
            return "متوسط"
        return "ضعيف"

    def _source_label(url: str, author: str | None = None) -> str:
        author_text = str(author or "").strip().lstrip("@")
        if author_text:
            return author_text

        raw = str(url or "").strip()
        if not raw:
            return "مصدر غير معروف"
        if "share" in raw.lower() and "/video/" not in raw.lower():
            return "منشورات مشتركة"
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw)
            path_parts = [part for part in parsed.path.split("/") if part]
            if path_parts:
                first = path_parts[0]
                if first.startswith("@"):
                    return first.lstrip("@")
            if parsed.netloc:
                return parsed.netloc.replace("www.", "")
        except Exception:
            pass
        return raw[:48]

    def _normalize_date(text: str) -> str:
        raw = str(text or "").strip()
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            month_names = {
                "01": "يناير",
                "02": "فبراير",
                "03": "مارس",
                "04": "أبريل",
                "05": "مايو",
                "06": "يونيو",
                "07": "يوليو",
                "08": "أغسطس",
                "09": "سبتمبر",
                "10": "أكتوبر",
                "11": "نوفمبر",
                "12": "ديسمبر",
            }
            return f"{int(raw[8:10])} {month_names.get(raw[5:7], raw[5:7])} {raw[:4]}"
        return raw or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    videos_payload = videos_payload or []
    source_rows = source_rows or []
    failed_pages = failed_pages or []
    video_items = gemini_report.get("videos") or []

    total_posts = sum(_int_value(row.get("posts")) for row in source_rows)
    total_likes = sum(_int_value(row.get("likes")) for row in source_rows)
    total_comments = sum(_int_value(row.get("comments")) for row in source_rows)
    total_shares = sum(_int_value(row.get("shares")) for row in source_rows)
    total_interactions = total_likes + total_comments + total_shares

    metrics_by_post = {}
    for item in videos_payload:
        if not isinstance(item, dict):
            continue
        key = str(item.get("post_url") or item.get("sourceUrl") or "").strip()
        if key:
            metrics_by_post[key] = item

    topic_rows = []
    grouped = {}
    for item in video_items:
        if not isinstance(item, dict):
            continue
        topic_name = str(item.get("topic_ar") or "محتوى عام").strip() or "محتوى عام"
        post_key = str(item.get("post_url") or "").strip()
        joined = metrics_by_post.get(post_key, {})
        likes = _int_value(joined.get("likes"))
        views = _int_value(joined.get("views"))
        comments = _int_value(joined.get("comments"))
        shares = _int_value(joined.get("shares"))

        bucket = grouped.setdefault(topic_name, {"posts": 0, "interactions": 0, "likes": 0, "views": 0})
        bucket["posts"] += 1
        bucket["interactions"] += likes + comments + shares
        bucket["likes"] += likes
        bucket["views"] += views

    for topic_name, stats in sorted(grouped.items(), key=lambda pair: pair[1]["interactions"], reverse=True):
        ratio = (stats["likes"] / stats["views"]) if stats["views"] > 0 else None
        ratio_text = f"{ratio * 100:.1f}%" if ratio is not None else "N/A"
        topic_rows.append(
            "<tr>"
            f"<td><span class='dot' style='background:{_ratio_color(ratio)}'></span></td>"
            f"<td>{escape(ratio_text)}</td>"
            f"<td>{escape(_ratio_label(ratio))}</td>"
            f"<td>{stats['interactions']:,}</td>"
            f"<td>{stats['posts']:,}</td>"
            f"<td>{escape(topic_name)}</td>"
            "</tr>"
        )

    top_posts_rows = []
    ranked_posts = []
    for item in videos_payload:
        if not isinstance(item, dict):
            continue
        likes = _int_value(item.get("likes"))
        comments = _int_value(item.get("comments"))
        shares = _int_value(item.get("shares"))
        views = _int_value(item.get("views"))
        ratio = (likes / views) if views > 0 else None
        ranked_posts.append(
            {
                "source": _source_label(item.get("source") or item.get("post_url") or "", item.get("author")),
                "description": str(item.get("description") or "").strip(),
                "interactions": likes + comments + shares,
                "comments": comments,
                "shares": shares,
                "ratio": ratio,
            }
        )

    ranked_posts.sort(key=lambda row: row["interactions"], reverse=True)
    for row in ranked_posts[:5]:
        ratio_text = f"{row['ratio'] * 100:.1f}%" if row["ratio"] is not None else "N/A"
        top_posts_rows.append(
            "<tr>"
            f"<td>{row['shares']:,}</td>"
            f"<td>{row['comments']:,}</td>"
            f"<td>{row['interactions']:,}</td>"
            f"<td class='description-cell'>{escape(row['description'] or '—')}</td>"
            f"<td>{escape(row['source'])}</td>"
            f"<td><span class='dot' style='background:{_ratio_color(row['ratio'])}'></span> {escape(ratio_text)}</td>"
            "</tr>"
        )

    source_rows_html = []
    for row in sorted(source_rows, key=lambda r: (_int_value(r.get("likes")) + _int_value(r.get("comments")) + _int_value(r.get("shares"))), reverse=True):
        interactions = _int_value(row.get("likes")) + _int_value(row.get("comments")) + _int_value(row.get("shares"))
        source_rows_html.append(
            "<tr>"
            f"<td>{_int_value(row.get('shares')):,}</td>"
            f"<td>{_int_value(row.get('comments')):,}</td>"
            f"<td>{interactions:,}</td>"
            f"<td>{_int_value(row.get('posts')):,}</td>"
            f"<td>{escape(_source_label(row.get('source')))}</td>"
            "</tr>"
        )

    failed_rows_html = []
    for row in failed_pages:
        failed_rows_html.append(
            "<tr>"
            f"<td>{escape(_source_label(row.get('url')))}</td>"
            f"<td>{escape(str(row.get('error') or 'unknown_error'))}</td>"
            "</tr>"
        )

    html = _render_html_template(
        "mauritanie_24h_report.html",
        {
            "__REPORT_TITLE__": escape(str(gemini_report.get("report_title_ar") or "موريتانيا في الـ 24 ساعة الماضية")),
            "__REPORT_DATE__": escape(_normalize_date(datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))),
            "__OVERVIEW__": escape(str(gemini_report.get("overview_ar") or "")),
            "__SUMMARY_LINE__": escape(
                f"رصد هذا التقرير {total_posts:,} منشورا من {len(source_rows)} مصادر، بإجمالي {total_interactions:,} تفاعلا و{total_comments:,} تعليقا و{total_shares:,} مشاركة."
            ),
            "__TOPIC_ROWS__": "".join(topic_rows) or "<tr><td colspan='6'>لا توجد بيانات كافية</td></tr>",
            "__TOP_POST_ROWS__": "".join(top_posts_rows) or "<tr><td colspan='6'>لا توجد بيانات كافية</td></tr>",
            "__CONCLUSION__": escape(str(gemini_report.get("conclusion_ar") or "")),
            "__SOURCE_ROWS__": "".join(source_rows_html) or "<tr><td colspan='5'>لا توجد بيانات كافية</td></tr>",
            "__TOPIC_APPENDIX_ROWS__": "".join(topic_rows) or "<tr><td colspan='6'>لا توجد بيانات كافية</td></tr>",
            "__ACTIVE_ROWS__": "".join(source_rows_html) or "<tr><td colspan='5'>لا توجد بيانات كافية</td></tr>",
            "__FAILED_PAGES_COUNT__": str(len(failed_pages)),
            "__FAILED_ROWS__": "".join(failed_rows_html) or "<tr><td colspan='2'>لا توجد صفحات فاشلة</td></tr>",
        },
    )

    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def _build_mauritanie_24h_pdf(
    scrape_id: str,
    source_rows: list[dict],
    gemini_report: dict,
    videos_payload: list[dict] | None,
    failed_pages: list[dict] | None,
    output_dir: Path,
) -> str | None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Flowable
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.platypus.flowables import HRFlowable
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"mauritanie_24h_{scrape_id}_{ts}.pdf"

    def _shape_ar(text: str) -> str:
        base = str(text or "")
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display

            return get_display(arabic_reshaper.reshape(base))
        except Exception:
            return base

    def _normalize_date(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return datetime.now(tz=timezone.utc).strftime("%d %B %Y")

        month_names = {
            "01": "يناير",
            "02": "فبراير",
            "03": "مارس",
            "04": "أبريل",
            "05": "مايو",
            "06": "يونيو",
            "07": "يوليو",
            "08": "أغسطس",
            "09": "سبتمبر",
            "10": "أكتوبر",
            "11": "نوفمبر",
            "12": "ديسمبر",
        }
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            try:
                day = str(int(raw[8:10]))
                month = month_names.get(raw[5:7], raw[5:7])
                year = raw[:4]
                return f"{day} {month} {year}"
            except Exception:
                return raw
        return raw

    def _int_value(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _source_label(url: str, author: str | None = None) -> str:
        author_text = str(author or "").strip().lstrip("@")
        if author_text:
            return author_text

        raw = str(url or "").strip()
        if not raw:
            return "مصدر غير معروف"
        if "share" in raw.lower() and "/video/" not in raw.lower():
            return "منشورات مشتركة"
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw)
            path_parts = [part for part in parsed.path.split("/") if part]
            if path_parts:
                first = path_parts[0]
                if first.startswith("@"):
                    return first.lstrip("@")
            if parsed.netloc:
                return parsed.netloc.replace("www.", "")
        except Exception:
            pass
        return raw[:42]

    def _make_para(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(_shape_ar(text), style)

    class RTLTextBlock(Flowable):
        def __init__(self, text: str, style: ParagraphStyle, width: float):
            super().__init__()
            self.text = str(text or "")
            self.style = style
            self.width = width
            self.leading = getattr(style, "leading", style.fontSize * 1.35)
            self.font_name = style.fontName
            self.font_size = style.fontSize
            self.text_color = style.textColor
            self.space_before = getattr(style, "spaceBefore", 0)
            self.space_after = getattr(style, "spaceAfter", 0)
            self.alignment = getattr(style, "alignment", TA_RIGHT)
            self.lines = []

        def wrap(self, availWidth, availHeight):
            usable_width = min(self.width, availWidth)
            words = self.text.split()
            if not words:
                self.lines = [""]
                return usable_width, self.leading + self.space_before + self.space_after

            lines = []
            current_words = []

            def line_width(line_text: str) -> float:
                shaped = _shape_ar(line_text)
                return pdfmetrics.stringWidth(shaped, self.font_name, self.font_size)

            for word in words:
                trial_words = current_words + [word]
                trial_text = " ".join(trial_words)
                if current_words and line_width(trial_text) > usable_width:
                    lines.append(" ".join(current_words))
                    current_words = [word]
                else:
                    current_words.append(word)

            if current_words:
                lines.append(" ".join(current_words))

            self.lines = lines or [self.text]
            height = self.space_before + self.space_after + len(self.lines) * self.leading
            return usable_width, height

        def draw(self):
            self.canv.saveState()
            self.canv.setFont(self.font_name, self.font_size)
            self.canv.setFillColor(self.text_color)
            width = self.width
            y = (len(self.lines) - 1) * self.leading
            for line in self.lines:
                shaped = _shape_ar(line)
                if self.alignment == TA_CENTER:
                    self.canv.drawCentredString(width / 2, y, shaped)
                else:
                    self.canv.drawRightString(width, y, shaped)
                y -= self.leading
            self.canv.restoreState()

    def _metric_total(row: dict) -> int:
        return _int_value(row.get("likes")) + _int_value(row.get("comments")) + _int_value(row.get("shares"))

    videos_payload = videos_payload or []
    source_rows = source_rows or []
    failed_pages = failed_pages or []

    font_name = "Helvetica"
    font_env = (os.getenv("TIKTOK_ARABIC_FONT_PATH") or "").strip()
    font_candidates = [
        Path(font_env) if font_env else None,
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in font_candidates:
        if candidate and candidate.exists():
            try:
                pdfmetrics.registerFont(TTFont("ArabicUI", str(candidate)))
                font_name = "ArabicUI"
                break
            except Exception:
                continue

    report_title = gemini_report.get("report_title_ar") or "موريتانيا في الـ 24 ساعة الماضية"
    report_date = _normalize_date(datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    overview = gemini_report.get("overview_ar") or ""
    top_topics = gemini_report.get("top_topics_ar") or []
    video_items = gemini_report.get("videos") or []
    conclusion = gemini_report.get("conclusion_ar") or ""

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=34,
        bottomMargin=28,
    )
    page_width = doc.width

    colors_map = {
        "navy": colors.HexColor("#173F67"),
        "navy_dark": colors.HexColor("#16304D"),
        "navy_mid": colors.HexColor("#2C5D8A"),
        "steel": colors.HexColor("#7A8798"),
        "line": colors.HexColor("#C7CED9"),
        "row_alt": colors.HexColor("#F5F7FB"),
        "row_alt2": colors.HexColor("#EEF3F9"),
        "green": colors.HexColor("#58A65C"),
        "yellow": colors.HexColor("#F2C94C"),
        "red": colors.HexColor("#E35D5D"),
        "text": colors.HexColor("#1E2430"),
    }

    styles = {
        "title": ParagraphStyle(
            "title",
            fontName=font_name,
            fontSize=21,
            leading=25,
            alignment=TA_CENTER,
            textColor=colors_map["navy_dark"],
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=font_name,
            fontSize=11,
            leading=14,
            alignment=TA_CENTER,
            textColor=colors_map["steel"],
            spaceAfter=1,
        ),
        "date": ParagraphStyle(
            "date",
            fontName=font_name,
            fontSize=10,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors_map["steel"],
            spaceAfter=6,
        ),
        "section": ParagraphStyle(
            "section",
            fontName=font_name,
            fontSize=13,
            leading=16,
            alignment=TA_RIGHT,
            textColor=colors_map["navy_dark"],
            spaceBefore=8,
            spaceAfter=5,
            bold=True,
        ),
        "subsection": ParagraphStyle(
            "subsection",
            fontName=font_name,
            fontSize=11.5,
            leading=14,
            alignment=TA_RIGHT,
            textColor=colors_map["navy_mid"],
            spaceBefore=5,
            spaceAfter=3,
            bold=True,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=font_name,
            fontSize=10.5,
            leading=16,
            alignment=TA_RIGHT,
            textColor=colors_map["text"],
            spaceAfter=6,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=font_name,
            fontSize=9,
            leading=12,
            alignment=TA_RIGHT,
            textColor=colors_map["text"],
        ),
        "table_header": ParagraphStyle(
            "table_header",
            fontName=font_name,
            fontSize=9,
            leading=11,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "table_cell",
            fontName=font_name,
            fontSize=8.6,
            leading=11,
            alignment=TA_RIGHT,
            textColor=colors_map["text"],
        ),
        "table_cell_center": ParagraphStyle(
            "table_cell_center",
            fontName=font_name,
            fontSize=8.6,
            leading=11,
            alignment=TA_CENTER,
            textColor=colors_map["text"],
        ),
    }

    total_posts = sum(_int_value(row.get("posts")) for row in source_rows)
    total_likes = sum(_int_value(row.get("likes")) for row in source_rows)
    total_comments = sum(_int_value(row.get("comments")) for row in source_rows)
    total_shares = sum(_int_value(row.get("shares")) for row in source_rows)
    total_interactions = total_likes + total_comments + total_shares
    total_sources = len(source_rows)

    video_join = {}
    for item in videos_payload:
        if not isinstance(item, dict):
            continue
        key = str(item.get("post_url") or item.get("sourceUrl") or "").strip()
        if key:
            video_join[key] = item

    topic_groups: dict[str, dict] = {}
    for item in video_items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("topic_ar") or "محتوى عام").strip() or "محتوى عام"
        joined = video_join.get(str(item.get("post_url") or "").strip(), {})
        bucket = topic_groups.setdefault(
            key,
            {
                "posts": 0,
                "interactions": 0,
                "likes": 0,
                "views": 0,
            },
        )
        likes = _int_value(joined.get("likes"))
        comments = _int_value(joined.get("comments"))
        shares = _int_value(joined.get("shares"))
        views = _int_value(joined.get("views"))
        bucket["posts"] += 1
        bucket["interactions"] += likes + comments + shares
        bucket["likes"] += likes
        bucket["views"] += views

    topic_rows = []
    for topic_name, stats in sorted(topic_groups.items(), key=lambda item: item[1]["interactions"], reverse=True):
        likes_views_ratio = (stats["likes"] / stats["views"]) if stats["views"] > 0 else None
        if likes_views_ratio is None:
            dot_color = colors_map["steel"]
            quality_label = "غير متاح"
        elif likes_views_ratio >= 0.08:
            dot_color = colors_map["green"]
            quality_label = "مرتفع"
        elif likes_views_ratio >= 0.03:
            dot_color = colors_map["yellow"]
            quality_label = "متوسط"
        else:
            dot_color = colors_map["red"]
            quality_label = "ضعيف"

        trend_text = f"{likes_views_ratio * 100:.1f}%" if likes_views_ratio is not None else "N/A"
        topic_rows.append(
            [
                _make_para(f'<font color="#{dot_color.hexval()[2:]}">●</font>', styles["table_cell_center"]),
                _make_para(trend_text, styles["table_cell_center"]),
                _make_para(quality_label, styles["table_cell_center"]),
                _make_para(f'{stats["interactions"]:,}', styles["table_cell_center"]),
                _make_para(f'{stats["posts"]:,}', styles["table_cell_center"]),
                _make_para(topic_name, styles["table_cell"]),
            ]
        )

    def _build_table(data: list[list], col_widths: list[float], header_fill=colors_map["navy_dark"], row_heights=None):
        table = Table(data, colWidths=col_widths, repeatRows=1, rowHeights=row_heights)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), header_fill),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8.6),
            ("LEADING", (0, 0), (-1, -1), 11),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#A9B5C6")),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#A9B5C6")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for row_index in range(1, len(data)):
            bg = colors_map["row_alt"] if row_index % 2 else colors.white
            style_cmds.append(("BACKGROUND", (0, row_index), (-1, row_index), bg))
        table.setStyle(TableStyle(style_cmds))
        return table

    def _source_interaction(row: dict) -> int:
        return _int_value(row.get("likes")) + _int_value(row.get("comments")) + _int_value(row.get("shares"))

    story = []
    story.append(Spacer(1, 3))
    story.append(_make_para(report_title, styles["title"]))
    story.append(_make_para("تقرير تحليلي لوسائل التواصل الاجتماعي", styles["subtitle"]))
    story.append(_make_para(report_date, styles["date"]))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1, color=colors_map["line"], spaceBefore=0, spaceAfter=0))
    story.append(Spacer(1, 10))

    story.append(_make_para("أولا: نظرة عامة على النشاط الرقمي", styles["section"]))
    story.append(RTLTextBlock(overview, styles["body"], doc.width))

    summary_line = (
        f"رصد هذا التقرير ما مجموعه {total_posts:,} منشورا خلال الـ 24 ساعة الماضية، "
        f"صدرت عن {total_sources} مصادر متنوعة، وسجلت {total_interactions:,} تفاعلا، "
        f"و{total_comments:,} تعليقا، و{total_shares:,} مشاركة."
    )
    story.append(RTLTextBlock(summary_line, styles["body"], doc.width))

    story.append(_make_para("ثانيا: أبرز المحاور الموضوعية وحجم التفاعل", styles["section"]))
    if topic_rows:
        topic_table_data = [[
            _make_para("", styles["table_header"]),
            _make_para("مؤشر الإعجاب/المشاهدة", styles["table_header"]),
            _make_para("مستوى الجودة", styles["table_header"]),
            _make_para("إجمالي التفاعلات", styles["table_header"]),
            _make_para("المنشورات", styles["table_header"]),
            _make_para("الموضوع", styles["table_header"]),
        ]]
        topic_table_data.extend(topic_rows)
        topic_widths = [16, 74, 76, 84, 58, max(160, page_width - 308)]
        story.append(_build_table(topic_table_data, topic_widths))
    else:
        story.append(RTLTextBlock("لا توجد بيانات كافية لتوليد جدول المحاور الموضوعية.", styles["body"], doc.width))

    story.append(_make_para("ثالثا: أبرز الأحداث والقضايا الساخنة", styles["section"]))
    for idx, item in enumerate(video_items[:3], start=1):
        topic_name = str(item.get("topic_ar") or "محتوى عام").strip() or "محتوى عام"
        heading = f"{idx}. {topic_name}"
        story.append(_make_para(heading, styles["subsection"]))
        story.append(RTLTextBlock(item.get("description_ar") or item.get("description") or "لا توجد تفاصيل كافية.", styles["body"], doc.width))

    top_posts = []
    for item in videos_payload:
        if not isinstance(item, dict):
            continue
        interactions = _metric_total(item)
        top_posts.append(
            {
                "source": _source_label(item.get("source") or item.get("post_url") or "", item.get("author")),
                "description": str(item.get("description") or item.get("description_ar") or "").strip(),
                "likes": _int_value(item.get("likes")),
                "comments": _int_value(item.get("comments")),
                "shares": _int_value(item.get("shares")),
                "interactions": interactions,
            }
        )
    top_posts.sort(key=lambda row: row["interactions"], reverse=True)

    story.append(_make_para("رابعا: أكثر المنشورات تفاعلا في الـ 24 ساعة الأخيرة", styles["section"]))
    if top_posts:
        top_post_data = [[
            _make_para("المشاركات", styles["table_header"]),
            _make_para("التعليقات", styles["table_header"]),
            _make_para("التفاعلات", styles["table_header"]),
            _make_para("الموضوع / الوصف", styles["table_header"]),
            _make_para("الصفحة / المصدر", styles["table_header"]),
        ]]
        for row in top_posts[:5]:
            top_post_data.append(
                [
                    _make_para(f"{row['shares']:,}", styles["table_cell_center"]),
                    _make_para(f"{row['comments']:,}", styles["table_cell_center"]),
                    _make_para(f"{row['interactions']:,}", styles["table_cell_center"]),
                    _make_para(row["description"] or "—", styles["table_cell"]),
                    _make_para(row["source"], styles["table_cell"]),
                ]
            )
        top_post_widths = [52, 58, 68, max(130, page_width - 318), 110]
        story.append(_build_table(top_post_data, top_post_widths, header_fill=colors_map["navy"]))

    if failed_pages:
        story.append(_make_para("خامسا: الصفحات التي فشل جمعها", styles["section"]))
        failed_table = [[
            _make_para("الصفحة / المصدر", styles["table_header"]),
            _make_para("سبب الفشل", styles["table_header"]),
        ]]
        for row in failed_pages:
            failed_table.append(
                [
                    _make_para(_source_label(row.get("url")), styles["table_cell"]),
                    _make_para(str(row.get("error") or "unknown_error"), styles["table_cell"]),
                ]
            )
        failed_widths = [max(180, page_width - 200), 180]
        story.append(_build_table(failed_table, failed_widths, header_fill=colors_map["navy"]))

    story.append(_make_para("سادسا: تحليل التفاعل والمؤشرات الرقمية", styles["section"]))
    avg_interactions = (total_interactions / total_posts) if total_posts else 0
    avg_comments = (total_comments / total_posts) if total_posts else 0
    avg_shares = (total_shares / total_posts) if total_posts else 0
    analysis_text = (
        f"متوسط التفاعل لكل منشور بلغ نحو {avg_interactions:,.0f}، مع متوسط {avg_comments:,.0f} تعليق لكل منشور، "
        f"و{avg_shares:,.0f} مشاركة. وتظهر البيانات أن التفاعل يتركز أساسا حول القضايا السياسية والاجتماعية ذات الأثر المباشر."
    )
    story.append(RTLTextBlock(analysis_text, styles["body"], doc.width))

    story.append(_make_para("سابعا: الخلاصة والاستنتاجات", styles["section"]))
    story.append(RTLTextBlock(conclusion, styles["body"], doc.width))

    story.append(PageBreak())
    story.append(_make_para("ملحق – جداول الأداء والرصد", styles["section"]))
    story.append(_make_para("جدول ملخص: أداء المصادر في الـ 24 ساعة الأخيرة", styles["subsection"]))
    if source_rows:
        sorted_sources = sorted(source_rows, key=_source_interaction, reverse=True)
        source_table = [[
            _make_para("المشاركات", styles["table_header"]),
            _make_para("التعليقات", styles["table_header"]),
            _make_para("التفاعلات", styles["table_header"]),
            _make_para("المنشورات", styles["table_header"]),
            _make_para("الصفحة / المصدر", styles["table_header"]),
        ]]
        for row in sorted_sources:
            source_table.append(
                [
                    _make_para(f"{_int_value(row.get('shares')):,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('comments')):,}", styles["table_cell_center"]),
                    _make_para(f"{_source_interaction(row):,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('posts')):,}", styles["table_cell_center"]),
                    _make_para(_source_label(row.get("source")), styles["table_cell"]),
                ]
            )
        source_table.append(
            [
                _make_para(f"{total_shares:,}", styles["table_cell_center"]),
                _make_para(f"{total_comments:,}", styles["table_cell_center"]),
                _make_para(f"{total_interactions:,}", styles["table_cell_center"]),
                _make_para(f"{total_posts:,}", styles["table_cell_center"]),
                _make_para("الإجمالي", styles["table_cell"]),
            ]
        )
        source_widths = [58, 62, 78, 62, max(150, page_width - 310)]
        story.append(_build_table(source_table, source_widths, header_fill=colors_map["navy_dark"]))

    story.append(Spacer(1, 12))
    story.append(_make_para("جدول المواضيع الأكثر تفاعلا – أبريل 2026", styles["subsection"]))
    if topic_rows:
        appendix_topic_table = [[
            _make_para("", styles["table_header"]),
            _make_para("مؤشر الإعجاب/المشاهدة", styles["table_header"]),
            _make_para("مستوى الجودة", styles["table_header"]),
            _make_para("إجمالي التفاعلات", styles["table_header"]),
            _make_para("المنشورات", styles["table_header"]),
            _make_para("الموضوع", styles["table_header"]),
        ]]
        for row in topic_rows[:8]:
            appendix_topic_table.append([row[0], row[1], row[2], row[3], row[4], row[5]])
        topic_widths = [16, 74, 76, 84, 58, max(170, page_width - 318)]
        story.append(_build_table(appendix_topic_table, topic_widths, header_fill=colors_map["navy"]))

    if source_rows:
        story.append(Spacer(1, 12))
        story.append(_make_para("أداء الصفحات الأكثر نشاطاً – أبريل 2026", styles["subsection"]))
        active_sources = sorted(source_rows, key=_source_interaction, reverse=True)
        active_table = [[
            _make_para("إجمالي التفاعل", styles["table_header"]),
            _make_para("المشاركات", styles["table_header"]),
            _make_para("التعليقات", styles["table_header"]),
            _make_para("التفاعلات", styles["table_header"]),
            _make_para("المنشورات", styles["table_header"]),
            _make_para("الصفحة", styles["table_header"]),
        ]]
        for row in active_sources:
            total_interaction = _source_interaction(row)
            active_table.append(
                [
                    _make_para(f"{total_interaction:,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('shares')):,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('comments')):,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('likes')):,}", styles["table_cell_center"]),
                    _make_para(f"{_int_value(row.get('posts')):,}", styles["table_cell_center"]),
                    _make_para(_source_label(row.get("source")), styles["table_cell"]),
                ]
            )
        active_widths = [72, 58, 66, 70, 58, max(164, page_width - 388)]
        story.append(_build_table(active_table, active_widths, header_fill=colors_map["navy_dark"]))

    def _decorate(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors_map["line"])
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, doc.pagesize[1] - 18, doc.pagesize[0] - doc.rightMargin, doc.pagesize[1] - 18)
        canvas.setFont(font_name, 8.5)
        canvas.setFillColor(colors_map["steel"])
        canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 14, f"{_shape_ar(report_title)}  |  {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return str(out_path)


def _build_fallback_ar_report_from_videos(videos_payload: list[dict]) -> dict:
    """Construit un rapport arabe minimal quand Gemini est indisponible."""
    top_topics = [
        "الشأن السياسي",
        "الاقتصاد والخدمات",
        "قضايا المجتمع",
        "الأمن والحوادث",
    ]
    videos = []
    for item in videos_payload[:80]:
        desc = str(item.get("description") or "").strip()
        videos.append(
            {
                "post_url": str(item.get("post_url") or ""),
                "source": str(item.get("source") or ""),
                "author": str(item.get("author") or ""),
                "description_ar": desc if desc else "لا توجد تفاصيل كافية في الوصف المتاح.",
                "sentiment_ar": "محايد",
                "topic_ar": "محتوى عام",
            }
        )

    return {
        "report_title_ar": "موريتانيا في الـ 24 ساعة الماضية",
        "report_date_ar": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "overview_ar": "تم إنشاء هذا التقرير بصيغة احتياطية بسبب تعذر الوصول إلى خدمة Gemini مؤقتا.",
        "top_topics_ar": top_topics,
        "videos": videos,
        "conclusion_ar": "يستند هذا الإصدار إلى أوصاف المنشورات الخام، وسيتم تحسينه تلقائيا عند توفر Gemini.",
    }


def _process_csv_batch_task(
    channel,
    scrape_id: str,
    urls: list[str],
    max_posts_per_page: int,
    time_window_hours: int | None = None,
):
    scoped_logger = with_context(LOGGER, scrape_id=scrape_id)
    output_dir = Path(os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports")
    page_attempts = max(1, int((os.getenv("TIKTOK_BATCH_PAGE_ATTEMPTS") or "2").strip()))
    page_delay_seconds = max(0.0, float((os.getenv("TIKTOK_BATCH_PAGE_DELAY_SECONDS") or "2.5").strip()))
    page_delay_jitter = max(0.0, float((os.getenv("TIKTOK_BATCH_PAGE_DELAY_JITTER_SECONDS") or "1.5").strip()))
    retry_backoff_seconds = max(0.5, float((os.getenv("TIKTOK_BATCH_RETRY_BACKOFF_SECONDS") or "5").strip()))

    source_rows = []
    failed_pages = []
    videos_payload = []
    published_count = 0
    published_snapshots = {}

    def _payload_snapshot(payload: dict):
        metrics = payload.get("metrics") or {}
        return (
            payload.get("author") or "",
            payload.get("textContent") or "",
            metrics.get("likes"),
            metrics.get("comments"),
            metrics.get("shares"),
            metrics.get("views"),
        )

    def _page_cooldown(multiplier: float = 1.0):
        pause = (page_delay_seconds * multiplier) + random.uniform(0.0, page_delay_jitter)
        if pause > 0:
            time.sleep(pause)

    for page_url in urls:
        page_logger = with_context(scoped_logger, url=page_url)
        page_logger.info("Processing page from CSV batch")

        result = None
        for attempt in range(1, page_attempts + 1):
            result = scrape_tiktok_page(
                url=page_url,
                max_posts=max_posts_per_page,
                max_age_hours=time_window_hours,
                analyze_video_content=False,
            )

            error_code = str(result.get("error") or "").strip().lower()
            if error_code not in {"challenge_detected", "no_posts_found"}:
                break

            if attempt < page_attempts:
                backoff_multiplier = attempt
                retry_pause = (retry_backoff_seconds * backoff_multiplier) + random.uniform(0.0, page_delay_jitter)
                page_logger.warning("Page scrape retry scheduled after soft failure")
                time.sleep(retry_pause)

        if result.get("error"):
            failed_pages.append({"url": page_url, "error": str(result.get("error"))})
            page_logger.warning("Page scrape failed")
            _page_cooldown(multiplier=1.3)
            continue

        posts = result.get("posts") or []
        if not time_window_hours:
            posts = posts[:max_posts_per_page]
        if not posts:
            failed_pages.append({"url": page_url, "error": "no_posts_found"})
            page_logger.warning("Page returned no posts")
            continue
        likes = sum(_to_int(p.get("likes")) for p in posts)
        comments = sum(_to_int(p.get("comments_count")) for p in posts)
        shares = sum(_to_int(p.get("shares")) for p in posts)
        views = sum(_to_int(p.get("views")) for p in posts)

        source_rows.append(
            {
                "source": page_url,
                "posts": len(posts),
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "views": views,
            }
        )

        for post in posts:
            sig = f"{page_url}|{_post_signature(post)}"
            payload = normalize_post(scrape_id, page_url, post)
            snapshot = _payload_snapshot(payload)
            previous = published_snapshots.get(sig)
            if previous is None or snapshot != previous:
                published_snapshots[sig] = snapshot
                publish_result(channel, payload)
                published_count += 1

            videos_payload.append(
                {
                    "source": page_url,
                    "post_url": payload.get("sourceUrl"),
                    "author": payload.get("author"),
                    "description": payload.get("textContent"),
                    "likes": (payload.get("metrics") or {}).get("likes"),
                    "comments": (payload.get("metrics") or {}).get("comments"),
                    "shares": (payload.get("metrics") or {}).get("shares"),
                    "views": (payload.get("metrics") or {}).get("views"),
                    "published_at": payload.get("publishedAt"),
                }
            )

        _page_cooldown()

    report_json_path = _build_batch_pages_report(scrape_id, source_rows, failed_pages, output_dir)
    videos_json_path = _save_batch_videos_json(scrape_id=scrape_id, videos=videos_payload, output_dir=output_dir)

    gemini_report_path = None
    mauritanie_html_path = None
    mauritanie_pdf_path = None
    gemini_report_obj = None
    try:
        if videos_payload:
            gemini_timeout_seconds = max(5, int((os.getenv("TIKTOK_GEMINI_BATCH_TIMEOUT_SECONDS") or "90").strip()))
            scoped_logger.info("Starting Gemini batch report generation", extra={"post_id": None})
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    analyze_videos_json_with_gemini,
                    videos_json_path=videos_json_path,
                    output_dir=str(output_dir),
                )
                gemini_result = future.result(timeout=gemini_timeout_seconds)

            gemini_report_path = gemini_result.get("report_path")
            gemini_report_obj = gemini_result.get("report") or {}
            scoped_logger.info("Gemini batch report generation completed", extra={"post_id": None})
    except FuturesTimeoutError:
        scoped_logger.warning("Gemini batch report generation timed out; using fallback report")
    except Exception:
        scoped_logger.exception("Failed to build Gemini/PDF Mauritanie 24h report")

    if videos_payload and not gemini_report_obj:
        gemini_report_obj = _build_fallback_ar_report_from_videos(videos_payload)

    if videos_payload and gemini_report_obj:
        try:
            mauritanie_html_path = _build_mauritanie_24h_html_report(
                scrape_id=scrape_id,
                source_rows=source_rows,
                gemini_report=gemini_report_obj,
                videos_payload=videos_payload,
                failed_pages=failed_pages,
                output_dir=output_dir,
            )
            mauritanie_pdf_path = _build_mauritanie_24h_pdf(
                scrape_id=scrape_id,
                source_rows=source_rows,
                gemini_report=gemini_report_obj,
                videos_payload=videos_payload,
                failed_pages=failed_pages,
                output_dir=output_dir,
            )
        except Exception:
            scoped_logger.exception("Failed to generate fallback Mauritanie 24h PDF")

    has_results = published_count > 0
    completion_payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "COMPLETED",
        "success": has_results,
        "errorMessage": None if has_results else "No posts extracted from CSV pages (TikTok challenge/no_posts_found)",
        "sessionReports": {
            "jsonPath": report_json_path,
            "htmlPath": mauritanie_html_path,
            "pdfPath": mauritanie_pdf_path,
            "videosJsonPath": videos_json_path,
            "geminiJsonPath": gemini_report_path,
        },
        "batchSummary": {
            "pagesRequested": len(urls),
            "pagesSucceeded": len(source_rows),
            "pagesFailed": len(failed_pages),
            "failedPages": failed_pages,
            "timeWindowHours": time_window_hours,
        },
        "count": published_count,
    }
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(completion_payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )


def on_message(channel, method, properties, body):
    """Callback RabbitMQ pour traiter un message de tache TikTok.

    Flux detaille:
    - Parser et valider le payload entrant.
    - Lancer le scraper dans un thread et diffuser les posts des qu'ils arrivent.
    - Dedupliquer les posts (streaming + final).
    - Generer les rapports de session.
    - Publier COMPLETED (ou ERROR) puis ACK/NACK du message.
    """
    scrape_id = "unknown"
    try:
        task = json.loads(body)
        scrape_id = task.get("scrape_id") or task.get("scrapeId", "unknown")
        url = task.get("url", "")
        urls = task.get("urls") if isinstance(task.get("urls"), list) else []
        report_mode = bool(task.get("report_mode") if task.get("report_mode") is not None else task.get("reportMode"))
        report_type = str(task.get("report_type") or task.get("reportType") or "").strip().lower()
        raw_time_window = task.get("time_window_hours", task.get("timeWindowHours"))
        try:
            time_window_hours = int(raw_time_window) if raw_time_window is not None else None
        except (TypeError, ValueError):
            time_window_hours = None
        scoped_logger = with_context(LOGGER, scrape_id=scrape_id, url=url)
        raw_max_posts = task.get("max_posts", task.get("maxPosts", 20))
        try:
            max_posts = int(raw_max_posts)
        except (TypeError, ValueError):
            max_posts = 20
        if max_posts <= 0:
            max_posts = 20

        if report_mode and report_type in ("csv", "csv_24h") and urls:
            scoped_logger.info("CSV batch task received")
            _process_csv_batch_task(
                channel=channel,
                scrape_id=scrape_id,
                urls=urls,
                max_posts_per_page=max_posts,
                time_window_hours=(24 if report_type == "csv_24h" else time_window_hours),
            )
            scoped_logger.info("CSV batch task completed")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        if not url:
            scoped_logger.error("Invalid task payload: URL missing")
            publish_error(channel, scrape_id, "URL missing in message")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        scoped_logger.info("Task received", extra={"post_id": None})

        post_queue = queue.Queue()
        done_event = threading.Event()
        worker_result = {"value": None, "error": None}

        def on_post(post: dict):
            post_queue.put(post)

        def run_scrape():
            try:
                scoped_logger.info("Starting scrape thread")
                worker_result["value"] = scrape_tiktok_page(
                    url=url,
                    max_posts=max_posts,
                    on_post=on_post,
                    analyze_video_content=False,
                )
            except Exception as exc:
                scoped_logger.exception("Scrape thread failed")
                worker_result["error"] = exc
            finally:
                done_event.set()

        threading.Thread(target=run_scrape, daemon=True).start()

        published_snapshots = {}
        published_count = 0

        def _payload_snapshot(payload: dict):
            metrics = payload.get("metrics") or {}
            return (
                payload.get("author") or "",
                payload.get("textContent") or "",
                metrics.get("likes"),
                metrics.get("comments"),
                metrics.get("shares"),
                metrics.get("views"),
                payload.get("sourceMediaUrl"),
                payload.get("mediaPath"),
                bool(payload.get("videoReport")),
            )

        while True:
            try:
                post = post_queue.get(timeout=0.5)
                sig = _post_signature(post)
                payload = normalize_post(scrape_id, url, post)
                snapshot = _payload_snapshot(payload)
                previous = published_snapshots.get(sig)
                if previous is None or snapshot != previous:
                    published_snapshots[sig] = snapshot
                    publish_result(channel, payload)
                    published_count += 1
            except queue.Empty:
                if done_event.is_set():
                    break

        if worker_result["error"] is not None:
            raise worker_result["error"]

        result = worker_result["value"] or {}
        if result.get("error"):
            scoped_logger.error("Scraper returned error", extra={"post_id": None})
            publish_error(channel, scrape_id, result["error"])
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        posts = result.get("posts", [])
        if not posts:
            scoped_logger.warning("No posts found for task")
            publish_error(channel, scrape_id, "No TikTok posts found")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        for post in posts:
            sig = _post_signature(post)
            payload = normalize_post(scrape_id, url, post)
            snapshot = _payload_snapshot(payload)
            previous = published_snapshots.get(sig)
            if previous is None or snapshot != previous:
                published_snapshots[sig] = snapshot
                publish_result(channel, payload)
                published_count += 1

        enriched_posts = []
        enrichment_enabled = _env_bool("TIKTOK_ASYNC_ENRICHMENT_ENABLED", True)
        enrichment_workers = max(1, int((os.getenv("TIKTOK_ENRICHMENT_WORKERS") or "2").strip()))
        output_dir = os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports"

        if enrichment_enabled and posts:
            scoped_logger.info("Starting async enrichment", extra={"post_id": None})
            with ThreadPoolExecutor(max_workers=enrichment_workers) as executor:
                futures = {executor.submit(_enrich_post_video, post, output_dir): post for post in posts}
                for future in as_completed(futures):
                    base_post = futures[future]
                    try:
                        enriched = future.result()
                        enriched_posts.append(enriched)
                        if enriched.get("video_report"):
                            publish_enrichment_update(
                                channel=channel,
                                scrape_id=scrape_id,
                                url=url,
                                post=enriched,
                                event_type="POST_ENRICHED",
                                success=True,
                                error_message=None,
                            )
                    except Exception as exc:
                        scoped_logger.warning("Post enrichment failed", exc_info=True)
                        publish_enrichment_update(
                            channel=channel,
                            scrape_id=scrape_id,
                            url=url,
                            post=base_post,
                            event_type="POST_ENRICHMENT_FAILED",
                            success=False,
                            error_message=str(exc),
                        )
        else:
            enriched_posts = posts

        session_json_path = None
        session_html_path = None
        session_pdf_path = None

        try:
            session_output_dir = Path(os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports")
            raw_posts = enriched_posts if enriched_posts else result.get("posts", [])
            session_json = build_session_json_report(page_url=url, posts=raw_posts, output_dir=str(session_output_dir))
            session_json_path = session_json.get("json_path")
            session_html_path = _build_session_html_report(page_url=url, posts=raw_posts, output_dir=session_output_dir)
            session_pdf_path = _build_session_pdf_report(posts=raw_posts, output_dir=session_output_dir)
        except Exception:
            scoped_logger.exception("Failed to generate session reports")

        completion_payload = {
            "scrapeId": scrape_id,
            "platform": "tiktok",
            "eventType": "COMPLETED",
            "success": True,
            "errorMessage": None,
            "sessionReports": {
                "jsonPath": session_json_path,
                "htmlPath": session_html_path,
                "pdfPath": session_pdf_path,
            },
        }

        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=ROUTING_RESULT,
            body=json.dumps(completion_payload, ensure_ascii=False),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        scoped_logger.info("Task completed", extra={"post_id": None})
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as exc:
        with_context(LOGGER, scrape_id=scrape_id).exception("Task failed")
        publish_error(channel, scrape_id, str(exc))
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def connect_with_retry(retries=10, delay=5) -> pika.BlockingConnection:
    """Se connecte a RabbitMQ avec un nombre limite de tentatives.

    Args:
        retries: Nombre maximal de tentatives de connexion.
        delay: Delai (secondes) entre deux tentatives.

    Raises:
        RuntimeError: Si toutes les tentatives echouent.
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )

    for attempt in range(1, retries + 1):
        try:
            conn = pika.BlockingConnection(params)
            LOGGER.info("Connected to RabbitMQ")
            return conn
        except Exception:
            LOGGER.warning("RabbitMQ connection failed", extra={"post_id": None, "url": None})
            if attempt < retries:
                time.sleep(delay)

    raise RuntimeError("Unable to connect to RabbitMQ")


def main():
    """Point d'entree du worker.

    Declare exchange/queues/bindings, configure la QoS, puis demarre la
    consommation des messages TikTok jusqu'a interruption.
    """
    connection = connect_with_retry()
    channel = connection.channel()

    LOGGER.info("Starting TikTok worker consumer")

    channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(
        queue=QUEUE_CONSUME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": EXCHANGE,
            "x-dead-letter-routing-key": "scrape.dlq",
        },
    )
    channel.queue_declare(queue=QUEUE_RESULT, durable=True)
    channel.queue_bind(queue=QUEUE_CONSUME, exchange=EXCHANGE, routing_key="scrape.tiktok")
    channel.queue_bind(queue=QUEUE_RESULT, exchange=EXCHANGE, routing_key=ROUTING_RESULT)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=QUEUE_CONSUME, on_message_callback=on_message)

    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        LOGGER.info("Worker interrupted by user")
    finally:
        try:
            channel.stop_consuming()
            connection.close()
        except Exception:
            LOGGER.exception("Error while shutting down worker")


if __name__ == "__main__":
    main()
