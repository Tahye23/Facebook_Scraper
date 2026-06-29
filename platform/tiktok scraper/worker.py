import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pika

from scraper import scrape_tiktok_page


def _load_env_file():
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


def _safe_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def normalize_post(scrape_id: str, url: str, post: dict) -> dict:
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
    post_id = str(post.get("post_id") or post.get("id") or "").strip()
    if post_id:
        return f"id:{post_id}"
    post_url = str(post.get("post_url") or post.get("sourceUrl") or "").strip()
    text = str(post.get("message") or post.get("text") or post.get("textContent") or "").strip()
    return f"u:{post_url}|t:{text[:120]}"


def publish_result(channel, result: dict):
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(result, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )
    print(f"[->] TikTok result published scrapeId={result.get('scrapeId')} postId={result.get('postId')}")


def publish_error(channel, scrape_id: str, error_msg: str):
    payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "ERROR",
        "success": False,
        "errorMessage": error_msg,
    }
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )
    print(f"[!] TikTok error published for scrape_id={scrape_id}: {error_msg}")


def publish_completion(channel, scrape_id: str):
    payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "COMPLETED",
        "success": True,
        "errorMessage": None,
    }
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_RESULT,
        body=json.dumps(payload, ensure_ascii=False),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )
    print(f"[+] TikTok completion published for scrape_id={scrape_id}")


def on_message(channel, method, properties, body):
    scrape_id = "unknown"
    try:
        task = json.loads(body)
        scrape_id = task.get("scrape_id") or task.get("scrapeId", "unknown")
        url = task.get("url", "")
        raw_max_posts = task.get("max_posts", task.get("maxPosts", 20))
        try:
            max_posts = int(raw_max_posts)
        except (TypeError, ValueError):
            max_posts = 20
        if max_posts <= 0:
            max_posts = 20

        print(f"[<-] TikTok task: scrape_id={scrape_id} url={url} max_posts={max_posts}")

        if not url:
            publish_error(channel, scrape_id, "URL missing in message")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        post_queue = queue.Queue()
        done_event = threading.Event()
        worker_result = {"value": None, "error": None}

        def on_post(post: dict):
            post_queue.put(post)

        def run_scrape():
            try:
                worker_result["value"] = scrape_tiktok_page(url=url, max_posts=max_posts, on_post=on_post)
            except Exception as exc:
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
            publish_error(channel, scrape_id, result["error"])
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        posts = result.get("posts", [])
        if not posts:
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

        publish_completion(channel, scrape_id)
        print(f"[+] TikTok published {published_count} posts for scrape_id={scrape_id}")
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as exc:
        traceback.print_exc()
        publish_error(channel, scrape_id, str(exc))
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


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
            print(f"[+] Connected to RabbitMQ ({RABBITMQ_HOST}:{RABBITMQ_PORT})")
            return conn
        except Exception as exc:
            print(f"[!] Attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(delay)

    raise RuntimeError("Unable to connect to RabbitMQ")


def main():
    print("[*] Starting TikTok worker...")

    connection = connect_with_retry()
    channel = connection.channel()

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

    print(f"[*] Waiting for TikTok messages on '{QUEUE_CONSUME}'...")
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        print("[*] Stop requested")
    finally:
        try:
            channel.stop_consuming()
            connection.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
