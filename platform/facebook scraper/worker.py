"""
worker.py — RabbitMQ consumer pour le scraper Facebook.

Ce worker :
  1. Consomme les messages de scraping_queue_facebook
  2. Appelle scrape_facebook_page() du scraper existant
  3. Normalise chaque post au format ScrapeResultMessage
  4. Publie le résultat dans scrape_result_queue via scrape.exchange
"""

import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timezone

import pika

from browser_manager import run_in_browser_thread, stop_browser_thread
from scraper import scrape_facebook_page

# ─── CONFIG RABBITMQ (depuis variables d'environnement) ───────────────────────

RABBITMQ_HOST     = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT     = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER     = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")

EXCHANGE          = os.getenv("RABBITMQ_EXCHANGE", "scrape.exchange")
QUEUE_CONSUME     = os.getenv("RABBITMQ_QUEUE", "scraping_queue_facebook")
QUEUE_RESULT      = os.getenv("RABBITMQ_RESULT_QUEUE", "scrape_result_queue")
ROUTING_RESULT    = "scrape.result"


# ─── NORMALISATION POST → ScrapeResultMessage ─────────────────────────────────

def _safe_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _ts_to_iso(ts_str: str) -> str | None:
    """Convertit un timestamp Unix (str) en ISO 8601 UTC."""
    try:
        ts = int(ts_str)
        if ts > 1_000_000_000:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    return None


def normalize_post(scrape_id: str, url: str, post: dict) -> dict:
    """
    Transforme un post brut du scraper en ScrapeResultMessage JSON
    compatible avec ScrapeResultListener.java.
    """
    post_id   = post.get("post_id") or post.get("id") or ""
    author    = post.get("page_name") or post.get("author") or ""
    text      = post.get("message") or post.get("text") or ""
    post_url  = post.get("post_url") or url

    # hashtags extraits du texte
    hashtags = [w for w in text.split() if w.startswith("#")]

    # métriques
    metrics = {
        "likes":    _safe_int(post.get("reactions")),
        "comments": _safe_int(post.get("comments_count")),
        "shares":   _safe_int(post.get("shares")),
        "views":    None,
    }

    published_at = _ts_to_iso(post.get("date", ""))

    return {
        "scrapeId":         scrape_id,
        "platform":         "facebook",
        "postId":           post_id,
        "author":           author,
        "textContent":      text,
        "hashtags":         hashtags,
        "metrics":          metrics,
        "sourceUrl":        post_url,
        "sourceMediaUrl":   None,
        "mediaPath":        None,
        "publishedAt":      published_at,
        "scrapedAt":        datetime.now(tz=timezone.utc).isoformat(),
        "success":          True,
        "errorMessage":     None,
    }


# ─── PUBLICATION DU RÉSULTAT ──────────────────────────────────────────────────

def publish_result(channel, result: dict):
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(result, ensure_ascii=False),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,   # persistant
        ),
    )
    print(f"[→] Résultat publié pour scrapeId={result.get('scrapeId')} postId={result.get('postId')}")


def _post_signature(post: dict) -> str:
    post_id = str(post.get("post_id") or post.get("id") or "").strip()
    if post_id:
        return f"id:{post_id}"
    post_url = str(post.get("post_url") or post.get("sourceUrl") or "").strip()
    text = str(post.get("message") or post.get("text") or post.get("textContent") or "").strip()
    return f"u:{post_url}|t:{text[:120]}"


def publish_error(channel, scrape_id: str, error_msg: str):
    payload = {
        "scrapeId":      scrape_id,
        "platform":      "facebook",
        "eventType":     "ERROR",
        "success":       False,
        "errorMessage":  error_msg,
    }
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
        ),
    )
    print(f"[!] Erreur publiée pour scrape_id={scrape_id}: {error_msg}")


def publish_completion(channel, scrape_id: str):
    payload = {
        "scrapeId":      scrape_id,
        "platform":      "facebook",
        "eventType":     "COMPLETED",
        "success":       True,
        "errorMessage":  None,
    }
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
        ),
    )
    print(f"[✓] Completion publiée pour scrape_id={scrape_id}")


# ─── CALLBACK CONSOMMATEUR ────────────────────────────────────────────────────

def on_message(channel, method, properties, body):
    scrape_id = "unknown"
    try:
        task = json.loads(body)
        scrape_id = task.get("scrape_id") or task.get("scrapeId", "unknown")
        url       = task.get("url", "")
        print(f"[←] Tâche reçue: scrape_id={scrape_id} url={url}")

        if not url:
            publish_error(channel, scrape_id, "URL manquante dans le message")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Scraping progressif : le BrowserThread pousse les posts dans une queue locale,
        # et ce thread Rabbit les publie au fur et à mesure.
        post_queue = queue.Queue()
        done_event = threading.Event()
        worker_result = {"value": None, "error": None}

        def on_post(post: dict):
            post_queue.put(post)

        def run_scrape():
            try:
                worker_result["value"] = run_in_browser_thread(
                    scrape_facebook_page,
                    url,
                    timeout=300,
                    on_post=on_post,
                )
            except Exception as e:
                worker_result["error"] = e
            finally:
                done_event.set()

        scrape_thread = threading.Thread(target=run_scrape, daemon=True)
        scrape_thread.start()

        published_sigs = set()
        published_count = 0

        while True:
            try:
                post = post_queue.get(timeout=0.5)
                sig = _post_signature(post)
                if sig in published_sigs:
                    continue
                published_sigs.add(sig)
                msg = normalize_post(scrape_id, url, post)
                publish_result(channel, msg)
                published_count += 1
            except queue.Empty:
                if done_event.is_set():
                    break

        if worker_result["error"] is not None:
            raise worker_result["error"]

        result = worker_result["value"] or {}

        if result.get("error"):
            publish_error(channel, scrape_id, result["error"])
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        posts = result.get("posts", [])
        if not posts:
            publish_error(channel, scrape_id, "Aucun post trouvé")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Rattrapage final: publier uniquement les posts non déjà publiés en progressif.
        for post in posts:
            sig = _post_signature(post)
            if sig in published_sigs:
                continue
            msg = normalize_post(scrape_id, url, post)
            publish_result(channel, msg)
            published_count += 1

        publish_completion(channel, scrape_id)
        print(f"[✓] {published_count} posts publiés pour scrape_id={scrape_id}")
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        traceback.print_exc()
        publish_error(channel, scrape_id, str(e))
        # nack sans requeue pour envoyer en DLQ
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


# ─── CONNEXION RABBITMQ AVEC RETRY ────────────────────────────────────────────

def connect_with_retry(retries=10, delay=5) -> pika.BlockingConnection:
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
            print(f"[✓] Connecté à RabbitMQ ({RABBITMQ_HOST}:{RABBITMQ_PORT})")
            return conn
        except Exception as e:
            print(f"[!] Tentative {attempt}/{retries} échouée: {e}")
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError("Impossible de se connecter à RabbitMQ après plusieurs tentatives")


# ─── POINT D'ENTRÉE ───────────────────────────────────────────────────────────

def main():
    print("[*] Démarrage du worker Facebook...")

    connection = connect_with_retry()
    channel    = connection.channel()

    # S'assurer que l'exchange et les queues existent
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(
        queue=QUEUE_CONSUME,
        durable=True,
        arguments={
            "x-dead-letter-exchange":    EXCHANGE,
            "x-dead-letter-routing-key": "scrape.dlq",
        }
    )
    channel.queue_declare(queue=QUEUE_RESULT, durable=True)
    channel.queue_bind(queue=QUEUE_CONSUME, exchange=EXCHANGE, routing_key="scrape.facebook")
    channel.queue_bind(queue=QUEUE_RESULT,  exchange=EXCHANGE, routing_key=ROUTING_RESULT)

    # Un seul message à la fois (évite de surcharger Playwright)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=QUEUE_CONSUME, on_message_callback=on_message)

    print(f"[*] En attente de messages sur '{QUEUE_CONSUME}'... (Ctrl+C pour arrêter)")
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        print("[*] Arrêt demandé")
    finally:
        try:
            channel.stop_consuming()
            connection.close()
        except Exception:
            pass
        stop_browser_thread()


if __name__ == "__main__":
    main()
