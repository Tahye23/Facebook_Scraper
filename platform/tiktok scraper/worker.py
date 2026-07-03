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
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from html import escape

import pika

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger, with_context
from scraper import scrape_tiktok_page
from video_analysis import build_session_json_report


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

    template_path = Path(__file__).resolve().parent / "templates" / "session_report.html"
    template_html = template_path.read_text(encoding="utf-8")

    html = (
        template_html
        .replace("__PAGE_URL__", escape(page_url))
        .replace("__GENERATED_AT__", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__TOTAL_POSTS__", str(total_posts))
        .replace("__TOTAL_LIKES__", str(total_likes))
        .replace("__TOTAL_COMMENTS__", str(total_comments))
        .replace("__TOTAL_SHARES__", str(total_shares))
        .replace("__TOTAL_VIEWS__", str(total_views))
        .replace("__ROWS__", "".join(rows))
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
        scoped_logger = with_context(LOGGER, scrape_id=scrape_id, url=url)
        raw_max_posts = task.get("max_posts", task.get("maxPosts", 20))
        try:
            max_posts = int(raw_max_posts)
        except (TypeError, ValueError):
            max_posts = 20
        if max_posts <= 0:
            max_posts = 20

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
                worker_result["value"] = scrape_tiktok_page(url=url, max_posts=max_posts, on_post=on_post)
            except Exception as exc:
                scoped_logger.exception("Scrape thread failed")
                worker_result["error"] = exc
            finally:
                done_event.set()

        threading.Thread(target=run_scrape, daemon=True).start()

        published_sigs = set()
        published_count = 0

        while True:
            try:
                post = post_queue.get(timeout=0.5)
                sig = _post_signature(post)
                if sig in published_sigs:
                    continue
                published_sigs.add(sig)
                publish_result(channel, normalize_post(scrape_id, url, post))
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
            if sig in published_sigs:
                continue
            published_sigs.add(sig)
            publish_result(channel, normalize_post(scrape_id, url, post))
            published_count += 1

        session_json_path = None
        session_html_path = None
        session_pdf_path = None

        try:
            output_dir = Path(os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports")
            raw_posts = result.get("posts", [])
            session_json = build_session_json_report(page_url=url, posts=raw_posts, output_dir=str(output_dir))
            session_json_path = session_json.get("json_path")
            session_html_path = _build_session_html_report(page_url=url, posts=raw_posts, output_dir=output_dir)
            session_pdf_path = _build_session_pdf_report(posts=raw_posts, output_dir=output_dir)
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
