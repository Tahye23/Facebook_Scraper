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
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from html import escape

import pika

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger, with_context
from chrome_watchdog import kill_process_tree, start_chrome_watchdog
from scraper import get_proxy_pool, pick_replacement_proxy, scrape_tiktok_page
from scraper import _is_blacklisted as is_proxy_blacklisted
from scraper import _proxy_identity as proxy_identity
from scraper import _blacklist_proxy as blacklist_proxy
from video_analysis import (
    analyze_tiktok_video,
    analyze_videos_json_with_gemini,
    build_session_json_report,
    build_small_video_report,
)


LOGGER = get_logger(__name__, platform="tiktok", service="worker")

_SCRAPE_RUNNER = Path(__file__).resolve().parent / "scrape_job_runner.py"


def _job_hard_timeout_s() -> float:
    """Budget dur worker: kill process scrape+Chrome apres N secondes.

    Doit couvrir le pire cas TikTokApi:
      TIKTOK_API_MAX_ATTEMPTS * (SESSION_TIMEOUT_S + marge_warmup)
    Ex: 4 * (60 + ~10) ≈ 280s. Env (priorite):
      WORKER_HARD_TIMEOUT_S puis TIKTOK_JOB_HARD_TIMEOUT_S (legacy).
    """
    raw = (
        os.getenv("WORKER_HARD_TIMEOUT_S")
        or os.getenv("TIKTOK_JOB_HARD_TIMEOUT_S")
        or "280"
    ).strip()
    try:
        return max(45.0, float(raw))
    except ValueError:
        return 280.0


def _env_bool_local(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _scrape_with_hard_timeout(**kwargs) -> dict:
    """Execute scrape dans un subprocess isole; kill tree au timeout (1.1).

    Fallback in-process (thread) si TIKTOK_SCRAPE_SUBPROCESS=false.
    `on_post` n'est pas propage au subprocess — les posts sont publies
    depuis le resultat final (comportement deja present cote worker).
    """
    timeout_s = _job_hard_timeout_s()
    if not _env_bool_local("TIKTOK_SCRAPE_SUBPROCESS", True):
        return _scrape_with_thread_timeout(timeout_s, **kwargs)

    # on_post non serialisable — ignore volontairement en subprocess.
    job = {
        "url": kwargs.get("url"),
        "max_posts": kwargs.get("max_posts", 20),
        "max_age_hours": kwargs.get("max_age_hours"),
        "analyze_video_content": bool(kwargs.get("analyze_video_content") or False),
        "headless_override": kwargs.get("headless_override"),
        "proxy_override": kwargs.get("proxy_override"),
        "force_refresh": bool(kwargs.get("force_refresh") or False),
    }

    tmp_dir = Path(tempfile.mkdtemp(prefix="tiktok_scrape_"))
    in_path = tmp_dir / "job.json"
    out_path = tmp_dir / "result.json"
    in_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

    cmd = [
        sys.executable,
        str(_SCRAPE_RUNNER),
        "--input",
        str(in_path),
        "--output",
        str(out_path),
    ]
    # Capturer stderr pour diagnostiquer TikTokApi/Playwright (sinon DEVNULL = aveugle).
    # TIKTOK_SCRAPE_SUBPROCESS_LOG_STDERR=false pour revenir au silence.
    capture_stderr = (os.getenv("TIKTOK_SCRAPE_SUBPROCESS_LOG_STDERR") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )
    err_path = tmp_dir / "stderr.log"
    err_fh = None
    popen_kwargs: dict = {
        "cwd": str(Path(__file__).resolve().parent),
        "env": os.environ.copy(),
        "stdout": subprocess.DEVNULL,
    }
    if capture_stderr:
        err_fh = open(err_path, "w", encoding="utf-8", errors="replace")
        popen_kwargs["stderr"] = err_fh
    else:
        popen_kwargs["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        # Nouveau process group Windows pour taskkill /T.
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    LOGGER.info(
        "[PERF] scrape subprocess pid=%s timeout=%.0fs url=%s",
        proc.pid,
        timeout_s,
        str(kwargs.get("url") or "")[:80],
    )
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        LOGGER.error(
            "[PERF] HARD TIMEOUT %.0fs — killing scrape process tree pid=%s",
            timeout_s,
            proc.pid,
            extra={"url": kwargs.get("url")},
        )
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        if err_fh is not None:
            try:
                err_fh.flush()
                err_fh.close()
            except Exception:
                pass
        if capture_stderr and err_path.exists():
            try:
                err_tail = err_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                if err_tail.strip():
                    LOGGER.warning(
                        "[PERF] scrape subprocess stderr tail:\n%s",
                        err_tail,
                    )
            except Exception:
                LOGGER.debug("Failed to read scrape stderr log", exc_info=True)

    if timed_out:
        return {
            "posts": [],
            "total": 0,
            "error": "proxy_blocked:attempt_timeout",
            "error_detail": f"worker_hard_timeout_kill_{int(timeout_s)}s",
            "classification": "attempt_timeout",
            "classification_action": "rotate_identity",
            "classification_reason": "worker_hard_timeout_process_kill",
            "url": kwargs.get("url"),
        }

    if out_path.exists():
        try:
            result = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(result, dict):
                return result
        except Exception:
            LOGGER.warning("Failed to parse scrape subprocess result", exc_info=True)

    return {
        "posts": [],
        "total": 0,
        "error": f"scrape_subprocess_exit_{proc.returncode}",
        "url": kwargs.get("url"),
    }


def _scrape_with_thread_timeout(timeout_s: float, **kwargs) -> dict:
    """Fallback legacy: thread + abandon (pas de kill Chrome)."""
    # Retirer on_post si present pour homogeniser avec subprocess.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(scrape_tiktok_page, **kwargs)
        try:
            result = future.result(timeout=timeout_s)
            return result if isinstance(result, dict) else {"posts": [], "error": "invalid_result"}
        except FuturesTimeoutError:
            LOGGER.error(
                "[PERF] HARD TIMEOUT %.0fs — abandoning scrape thread (no process kill)",
                timeout_s,
                extra={"url": kwargs.get("url")},
            )
            return {
                "posts": [],
                "total": 0,
                "error": "proxy_blocked:attempt_timeout",
                "error_detail": f"worker_hard_timeout_thread_{int(timeout_s)}s",
                "classification": "attempt_timeout",
                "url": kwargs.get("url"),
            }
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)


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


def normalize_post(scrape_id: str, url: str, post: dict, *, refresh_mode: str | None = None) -> dict:
    """Normalise un post brut vers le format contractuel du gateway.

    Permet de conserver un schema stable entre plateformes pour le stockage
    et le traitement des evenements en aval.
    """
    text = post.get("message") or post.get("text") or ""
    hashtags = [w for w in text.split() if w.startswith("#")]
    mode = (refresh_mode or "").strip().upper() or None

    payload = {
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
    if mode:
        payload["refreshMode"] = mode
        payload["refresh_mode"] = mode
    return payload


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


def publish_error(channel, scrape_id: str, error_msg: str, error_reason: str | None = None):
    """Publie un evenement de cycle de vie ERROR pour un job de scraping."""
    scoped_logger = with_context(LOGGER, scrape_id=scrape_id)
    payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "ERROR",
        "success": False,
        "errorMessage": error_msg,
        "errorReason": error_reason,
    }
    scoped_logger.error(
        "Publishing ERROR event reason=%s",
        error_reason or "",
        extra={"url": None},
    )
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


def _fetch_cached_video_reports(post_ids: list[str]) -> dict:
    """Interroge le gateway pour savoir quels post_id ont deja une analyse IA.

    Retourne un dict {post_id: video_report}. Sert le "cache de re-scraping":
    pour ces posts, le worker saute l'appel Gemini (etape la plus couteuse) et
    reutilise l'analyse existante. En cas d'erreur (gateway injoignable, etc.),
    retourne un dict vide -> on retombe simplement sur le comportement normal
    (on analyse), donc jamais bloquant.
    """
    ids = [str(pid).strip() for pid in post_ids if str(pid or "").strip()]
    if not ids:
        return {}

    base_url = (os.getenv("GATEWAY_INTERNAL_URL") or "http://gateway:8080").rstrip("/")
    endpoint = f"{base_url}/internal/results/reports"
    token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    payload = json.dumps({"platform": "tiktok", "postIds": ids}).encode("utf-8")

    request = urllib.request.Request(endpoint, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Internal-Token", token)

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        reports = data.get("reports") or {}
        return reports if isinstance(reports, dict) else {}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        LOGGER.warning(
            "Cache lookup failed; will analyze all posts",
            extra={"error": str(exc)},
        )
        return {}


def _fetch_fresh_posts_from_gateway(
    author: str,
    *,
    max_posts: int = 20,
    max_age_hours: int | None = None,
) -> dict | None:
    """Posts avec metrics fraiches (TTL gateway) — skip Apify / pas de quota.

    Retourne {posts, enough} ou None si gateway injoignable.
    """
    handle = (author or "").strip().lstrip("@")
    if not handle:
        return None
    base_url = (os.getenv("GATEWAY_INTERNAL_URL") or "http://gateway:8080").rstrip("/")
    endpoint = f"{base_url}/internal/results/fresh"
    token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    body = {
        "platform": "tiktok",
        "author": handle,
        "max_posts": max_posts,
    }
    if max_age_hours:
        body["max_age_hours"] = int(max_age_hours)
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Internal-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        LOGGER.warning("Fresh metrics lookup failed: %s", exc)
        return None


def _classify_error_reason(error_msg: str, error_code: str | None = None) -> str | None:
    code = (error_code or "").strip().lower()
    low = (error_msg or "").lower()
    if code == "apify_quota_exceeded" or ("quota" in low and "apify" in low):
        return "QUOTA_EXCEEDED"
    return None


def _enrich_post_video(post: dict, output_dir: str) -> dict:
    post_copy = dict(post)
    post_url = str(post_copy.get("post_url") or "").strip()
    post_description = str(post_copy.get("message") or post_copy.get("text") or "").strip()
    if not post_url or "/video/" not in post_url:
        return post_copy

    metrics = {
        "likes": post_copy.get("likes"),
        "comments": post_copy.get("comments_count") or post_copy.get("comments"),
        "shares": post_copy.get("shares"),
        "views": post_copy.get("views"),
    }
    # Hashtags: preferer ceux deja parses, sinon extraire du texte.
    hashtags = post_copy.get("hashtags")
    if not isinstance(hashtags, list):
        hashtags = [w for w in post_description.split() if w.startswith("#")]

    report = analyze_tiktok_video(
        video_url=post_url,
        output_dir=output_dir,
        save_json_report=True,
        description_text=post_description,
        metrics=metrics,
        hashtags=hashtags,
        author=str(post_copy.get("author") or ""),
    )
    post_copy["source_media_url"] = report.get("video_metadata", {}).get("media_url")
    post_copy["media_path"] = report.get("artifacts", {}).get("video_path")
    post_copy["video_report"] = build_small_video_report(report)
    post_copy["message"] = post_copy.get("message") or report.get("transcript_excerpt") or ""
    return post_copy


def _video_id_from_url(url: str) -> str:
    raw = str(url or "")
    marker = "/video/"
    idx = raw.find(marker)
    if idx < 0:
        return ""
    rest = raw[idx + len(marker) :]
    for sep in ("?", "/", "&"):
        cut = rest.find(sep)
        if cut >= 0:
            rest = rest[:cut]
    return rest.strip()


def _publish_batch_gemini_enrichments(
    channel,
    scrape_id: str,
    gemini_report: dict,
    videos_payload: list[dict],
) -> None:
    """Apres le rapport 24h, publie un video_report par post pour l'UI / Mongo."""
    items = gemini_report.get("videos") if isinstance(gemini_report, dict) else None
    if not isinstance(items, list) or not items:
        return
    by_url = {
        str(v.get("post_url") or "").strip(): v
        for v in (videos_payload or [])
        if isinstance(v, dict) and str(v.get("post_url") or "").strip()
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        post_url = str(item.get("post_url") or "").strip()
        if not post_url:
            continue
        base = by_url.get(post_url) or {}
        pid = _video_id_from_url(post_url) or str(base.get("post_id") or "").strip()
        desc = str(item.get("description_ar") or "").strip()
        sentiment = str(item.get("sentiment_ar") or "").strip()
        topic = str(item.get("topic_ar") or "").strip()
        post = {
            "post_id": pid,
            "author": base.get("author") or "",
            "text": base.get("description") or "",
            "post_url": post_url,
            "likes": base.get("likes"),
            "comments_count": base.get("comments"),
            "shares": base.get("shares"),
            "views": base.get("views"),
            "published_at": base.get("published_at"),
            "video_report": {
                "executive_summary": [desc] if desc else [],
                "sentiment": sentiment or None,
                "themes": [topic] if topic else [],
                "confidence_and_limits": {"level": "batch_24h"},
            },
        }
        publish_enrichment_update(
            channel=channel,
            scrape_id=scrape_id,
            url=str(base.get("source") or post_url),
            post=post,
            event_type="POST_ENRICHED",
            success=True,
            error_message=None,
        )


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


def _split_round_robin(items: list, num_buckets: int) -> list[list]:
    """Repartit `items` en `num_buckets` sous-listes, en alternance (round-robin).

    Exemple avec 7 urls et 3 buckets -> [u0,u3,u6], [u1,u4], [u2,u5].
    Chaque bucket est ensuite traite par un thread dedie (une "lane"), qui
    reste attache a UN SEUL proxy pendant toute sa lane: c'est ce qui permet
    de "diviser le travail" sur N proxies en parallele plutot que de les
    utiliser en rotation sequentielle sur une seule page a la fois.
    """
    num_buckets = max(1, num_buckets)
    buckets = [[] for _ in range(num_buckets)]
    for index, item in enumerate(items):
        buckets[index % num_buckets].append(item)
    return [bucket for bucket in buckets if bucket]


def _resolve_batch_concurrency(proxy_pool_size: int) -> int:
    """Determine le nombre de "lanes" paralleles pour un batch CSV.

    Par defaut: une lane par proxy configure dans TIKTOK_PROXY_LIST (ex: 10
    proxies Webshare = 10 profils TikTok traites en parallele, chacun avec sa
    propre adresse IP source). Peut etre force via TIKTOK_BATCH_CONCURRENCY
    (utile pour limiter la charge meme avec plus de proxies disponibles, ou
    pour paralleliser aussi en mode direct sans proxy).
    """
    override = (os.getenv("TIKTOK_BATCH_CONCURRENCY") or "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            pass
    return proxy_pool_size if proxy_pool_size > 0 else 1


def _proxy_label(proxy_cfg: dict | None) -> str:
    """Description courte (sans mot de passe) du proxy utilise, pour les logs."""
    if not proxy_cfg:
        return "direct"
    return proxy_cfg.get("server") or "proxy"


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

    # Repartition du travail sur plusieurs proxies (voir _split_round_robin /
    # _resolve_batch_concurrency). Avec 0 proxy configure, on garde une seule
    # lane sequentielle: comportement strictement identique a avant.
    proxy_pool = get_proxy_pool()
    batch_concurrency = _resolve_batch_concurrency(len(proxy_pool))
    scoped_logger.info(
        "Batch concurrency resolved for CSV task",
        extra={"proxy_pool_size": len(proxy_pool), "concurrency": batch_concurrency, "page_count": len(urls)},
    )

    source_rows = []
    failed_pages = []
    videos_payload = []
    published_count = 0
    published_snapshots = {}
    state_lock = threading.Lock()

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

    def _publish_page_posts(page_url: str, posts: list[dict]) -> None:
        # Appele depuis plusieurs threads/lanes en parallele: toute mutation
        # d'etat partage (listes/dict/compteur) doit rester sous `state_lock`.
        nonlocal published_count
        likes = sum(_to_int(p.get("likes")) for p in posts)
        comments = sum(_to_int(p.get("comments_count")) for p in posts)
        shares = sum(_to_int(p.get("shares")) for p in posts)
        views = sum(_to_int(p.get("views")) for p in posts)

        with state_lock:
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

    def _attempt_page(page_url: str, attempts: int, page_logger, proxy_cfg: dict | None = None) -> dict:
        """Scrape une page avec retries internes; retourne toujours le dernier resultat.

        Le resultat conserve les posts deja collectes meme en cas d'erreur
        finale (challenge/exception), grace au fix de scraper.py qui ne jette
        plus les posts partiels.

        `proxy_cfg`: proxy dedie de la lane courante (voir `get_proxy_pool` /
        `_split_round_robin`). Passe tel quel a `scrape_tiktok_page` via
        `proxy_override`, pour que cette page utilise TOUJOURS ce proxy (pas
        de rotation croisee avec les autres lanes).
        """
        result = None
        for attempt in range(1, attempts + 1):
            result = _scrape_with_hard_timeout(
                url=page_url,
                max_posts=max_posts_per_page,
                max_age_hours=time_window_hours,
                analyze_video_content=False,
                proxy_override=proxy_cfg,
            )

            if result.get("posts"):
                # Des posts ont ete recuperes: on arrete les retries internes
                # (meme si une erreur/warning accompagne le resultat), pour
                # eviter de "jeter" un succes partiel en retentant a vide.
                break

            error_code = str(result.get("error_code") or result.get("error") or "").strip().lower()
            # Quota: aucun interet a retenter — la limite ne changera pas avant demain.
            if "quota" in error_code or "apify_quota" in error_code:
                break
            if error_code not in {
                "challenge_detected",
                "no_posts_found",
                "empty_feed_or_softblock",
            }:
                break

            if attempt < attempts:
                backoff_multiplier = attempt
                retry_pause = (retry_backoff_seconds * backoff_multiplier) + random.uniform(0.0, page_delay_jitter)
                page_logger.warning("Page scrape retry scheduled after soft failure")
                time.sleep(retry_pause)

        return result or {"posts": [], "error": "unknown_error", "url": page_url}

    # --- Distribution DYNAMIQUE (work-queue) + releve de proxy ----------------
    # Au lieu de pre-decouper les URLs en lots fixes (round-robin statique), on
    # place toutes les pages dans une FILE partagee `pending_urls`. On lance
    # `batch_concurrency` lanes en parallele; chaque lane, des qu'elle est libre,
    # pioche la page suivante dans la file (auto-equilibrage: une lane lente ne
    # bloque plus les autres).
    #
    # RELEVE DE PROXY: si l'IP d'une lane echoue -> blacklist 24h + bascule sur
    # une IP de reserve. La reserve est ensuite recompletee depuis le pool 20k
    # (nouvelle IP hors blacklist / hors IP deja en service).
    pending_urls = deque(urls)
    active_proxies = (proxy_pool[:batch_concurrency] if proxy_pool else [None]) or [None]
    spare_proxies = deque(proxy_pool[batch_concurrency:]) if proxy_pool else deque()
    # Identites des IP actuellement assignees (lanes + reserve) pour ne pas
    # retirer la meme session sticky en remplacement.
    in_use_ids = {
        proxy_identity(p)
        for p in list(active_proxies) + list(spare_proxies)
        if p is not None
    }
    quota_exhausted = False
    quota_error_message = ""

    def _mark_quota_exhausted(msg: str) -> None:
        nonlocal quota_exhausted, quota_error_message
        with state_lock:
            if not quota_exhausted:
                quota_exhausted = True
                quota_error_message = msg or "Quota Apify journalier atteint."
                # Stopper le batch: vider la file pour que les autres lanes sortent.
                pending_urls.clear()

    def _next_pending_url() -> str | None:
        with state_lock:
            if quota_exhausted:
                return None
            return pending_urls.popleft() if pending_urls else None

    def _take_spare_proxy() -> dict | None:
        # 1) reserve locale (hors blacklist)  2) sinon tirage frais dans le pool 20k
        with state_lock:
            while spare_proxies:
                candidate = spare_proxies.popleft()
                if candidate is not None and is_proxy_blacklisted(candidate):
                    in_use_ids.discard(proxy_identity(candidate))
                    continue
                return candidate
            fresh = pick_replacement_proxy(exclude=in_use_ids)
            if fresh is not None:
                in_use_ids.add(proxy_identity(fresh))
            return fresh

    def _retire_and_replace(dead_proxy: dict | None, reason: str) -> dict | None:
        """Blacklist l'IP morte, retire une IP de remplacement, recomplete la reserve."""
        if dead_proxy is not None and not is_proxy_blacklisted(dead_proxy):
            blacklist_proxy(dead_proxy, reason or "no_posts_found")
        if dead_proxy is not None:
            with state_lock:
                in_use_ids.discard(proxy_identity(dead_proxy))

        replacement = _take_spare_proxy()
        # Recompleter la reserve: tirer une IP supplementaire du pool 20k pour
        # garder une marge de releve pour les prochaines pages.
        with state_lock:
            refill = pick_replacement_proxy(exclude=in_use_ids)
            if refill is not None:
                in_use_ids.add(proxy_identity(refill))
                spare_proxies.append(refill)
        return replacement

    def _clip_posts(result: dict) -> list[dict]:
        posts = result.get("posts") or []
        if not time_window_hours:
            posts = posts[:max_posts_per_page]
        return posts

    def _lane_worker(initial_proxy: dict | None) -> None:
        # Une lane = un thread qui garde un proxy "courant" et enchaine les
        # pages de la file jusqu'a epuisement, avec releve de proxy en cas d'echec.
        current_proxy = initial_proxy
        while True:
            page_url = _next_pending_url()
            if page_url is None:
                return
            page_logger = with_context(scoped_logger, url=page_url)
            try:
                page_logger.info("Processing page from CSV batch", extra={"proxy": _proxy_label(current_proxy)})
                result = _attempt_page(page_url, page_attempts, page_logger, proxy_cfg=current_proxy)
                posts = _clip_posts(result)

                # Releve: IP courante KO -> blacklist 24h + remplacement (reserve
                # ou nouvelle IP du pool 20k), puis on retente la meme page.
                while not posts:
                    reason = str(result.get("error_code") or result.get("error") or "no_posts_found")
                    if _classify_error_reason(str(result.get("error") or ""), reason) == "QUOTA_EXCEEDED":
                        _mark_quota_exhausted(str(result.get("error") or reason))
                        break
                    spare = _retire_and_replace(current_proxy, reason)
                    if spare is None:
                        break
                    page_logger.warning(
                        "Page failed on current proxy; blacklisted and replaced",
                        extra={"old_proxy": _proxy_label(current_proxy), "new_proxy": _proxy_label(spare)},
                    )
                    current_proxy = spare
                    result = _attempt_page(page_url, page_attempts, page_logger, proxy_cfg=current_proxy)
                    posts = _clip_posts(result)

                if not posts:
                    err_txt = str(result.get("error") or "no_posts_found")
                    err_code = str(result.get("error_code") or "")
                    if _classify_error_reason(err_txt, err_code) == "QUOTA_EXCEEDED":
                        _mark_quota_exhausted(err_txt)
                    with state_lock:
                        failed_pages.append({"url": page_url, "error": err_txt})
                    page_logger.warning("Page scrape failed")
                    if quota_exhausted:
                        return
                    _page_cooldown(multiplier=1.3)
                    continue

                if result.get("error"):
                    # Succes partiel: des posts collectes malgre une erreur
                    # (challenge tardif, exception apres coup...). On les garde.
                    page_logger.warning(
                        "Page scrape ended with error but posts were recovered; keeping partial result",
                        extra={"error": str(result.get("error"))},
                    )

                _publish_page_posts(page_url, posts)
                _page_cooldown()
            except Exception:
                page_logger.exception("Unhandled error while processing page in batch lane")
                with state_lock:
                    failed_pages.append({"url": page_url, "error": "unhandled_exception"})

    lane_count = max(1, len(active_proxies))
    with ThreadPoolExecutor(max_workers=lane_count) as executor:
        futures = [executor.submit(_lane_worker, active_proxies[i]) for i in range(lane_count)]
        for future in as_completed(futures):
            future.result()

    # Round de retry final: apres avoir traite toutes les pages, on laisse la
    # session "refroidir" puis on retente une derniere fois les pages
    # marquees en echec pour cause de challenge/absence de posts. Objectif:
    # que le plus de profils possible reviennent avec une reponse au lieu de
    # rester marques en echec definitif dans le rapport.
    final_retry_enabled = _env_bool("TIKTOK_BATCH_FINAL_RETRY_ENABLED", True) and not quota_exhausted
    recoverable_failed = [
        row for row in failed_pages
        if str(row.get("error") or "").strip().lower() in {"challenge_detected", "no_posts_found"}
    ]
    if final_retry_enabled and recoverable_failed:
        final_retry_delay = max(0.0, float((os.getenv("TIKTOK_BATCH_FINAL_RETRY_DELAY_SECONDS") or "20").strip()))
        scoped_logger.info(
            "Starting final retry round for recoverable failed pages",
            extra={"failed_count": len(recoverable_failed), "cooldown_seconds": final_retry_delay},
        )
        if final_retry_delay > 0:
            time.sleep(final_retry_delay)

        recoverable_urls = {row["url"] for row in recoverable_failed}
        still_failed_urls = set(recoverable_urls)

        def _retry_one_page(page_url: str, proxy_cfg: dict | None) -> None:
            page_logger = with_context(scoped_logger, url=page_url)
            page_logger.info("Final retry attempt for previously failed page", extra={"proxy": _proxy_label(proxy_cfg)})

            result = _attempt_page(page_url, 1, page_logger, proxy_cfg=proxy_cfg)
            posts = result.get("posts") or []
            if not time_window_hours:
                posts = posts[:max_posts_per_page]

            if posts:
                page_logger.info("Final retry recovered posts for previously failed page", extra={"post_count": len(posts)})
                _publish_page_posts(page_url, posts)
                with state_lock:
                    still_failed_urls.discard(page_url)
            else:
                page_logger.warning("Final retry still failed for page")

            _page_cooldown(multiplier=1.3)

        def _run_retry_lane(lane_urls: list[str], proxy_cfg: dict | None) -> None:
            for page_url in lane_urls:
                try:
                    _retry_one_page(page_url, proxy_cfg)
                except Exception:
                    with_context(scoped_logger, url=page_url).exception("Unhandled error during final retry lane")

        retry_lanes = _split_round_robin(sorted(recoverable_urls), batch_concurrency)
        with ThreadPoolExecutor(max_workers=max(1, len(retry_lanes))) as executor:
            retry_futures = [
                executor.submit(
                    _run_retry_lane,
                    lane_urls,
                    proxy_pool[lane_index % len(proxy_pool)] if proxy_pool else None,
                )
                for lane_index, lane_urls in enumerate(retry_lanes)
            ]
            for future in as_completed(retry_futures):
                future.result()

        failed_pages = [row for row in failed_pages if row["url"] not in recoverable_urls or row["url"] in still_failed_urls]

    report_json_path = _build_batch_pages_report(scrape_id, source_rows, failed_pages, output_dir)
    videos_json_path = _save_batch_videos_json(scrape_id=scrape_id, videos=videos_payload, output_dir=output_dir)

    # Rapports 24h: STRICTEMENT apres le scraping. Toute erreur ici est logguee
    # mais ne doit JAMAIS empecher le COMPLETED ni casser le worker.
    gemini_report_path = None
    mauritanie_html_path = None
    mauritanie_pdf_path = None
    mauritanie_docx_path = None
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
        scoped_logger.exception("Failed to build Gemini batch report (non-fatal)")

    if videos_payload and not gemini_report_obj:
        try:
            gemini_report_obj = _build_fallback_ar_report_from_videos(videos_payload)
        except Exception:
            scoped_logger.exception("Failed to build fallback Arabic report (non-fatal)")
            gemini_report_obj = None

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
        except Exception:
            scoped_logger.exception("Failed to generate Mauritanie 24h HTML (non-fatal)")

        try:
            mauritanie_pdf_path = _build_mauritanie_24h_pdf(
                scrape_id=scrape_id,
                source_rows=source_rows,
                gemini_report=gemini_report_obj,
                videos_payload=videos_payload,
                failed_pages=failed_pages,
                output_dir=output_dir,
            )
        except Exception:
            scoped_logger.exception("Failed to generate Mauritanie 24h PDF (non-fatal)")

        # Word optionnel: lazy-import pour ne jamais casser le demarrage du worker.
        try:
            from mauritanie_24h_docx import build_mauritanie_24h_docx

            mauritanie_docx_path = build_mauritanie_24h_docx(
                scrape_id=scrape_id,
                gemini_report=gemini_report_obj,
                videos_payload=videos_payload,
                output_dir=output_dir,
            )
        except Exception:
            scoped_logger.exception("Failed to generate Mauritanie 24h DOCX (non-fatal)")

        # Rattache un video_report par post (pour la colonne Gemini de l'UI).
        try:
            _publish_batch_gemini_enrichments(
                channel=channel,
                scrape_id=scrape_id,
                gemini_report=gemini_report_obj,
                videos_payload=videos_payload,
            )
        except Exception:
            scoped_logger.exception("Failed to publish per-video Gemini enrichments (non-fatal)")

    has_results = published_count > 0
    if not has_results:
        completion_status = "FAILED"
    elif failed_pages:
        completion_status = "PARTIAL_SUCCESS"
    else:
        completion_status = "SUCCESS"

    err_msg = None
    err_reason = None
    if quota_exhausted and not has_results:
        err_msg = quota_error_message or (
            "Quota Apify journalier atteint. Réessayez demain ou "
            "augmentez APIFY_DAILY_VIDEO_LIMIT."
        )
        err_reason = "QUOTA_EXCEEDED"
    elif not has_results:
        err_msg = "No posts extracted from CSV pages (TikTok challenge/no_posts_found)"
    elif quota_exhausted:
        # Succes partiel: des posts avant le plafond, puis stop.
        err_msg = quota_error_message or "Quota Apify journalier atteint (batch interrompu)."
        err_reason = "QUOTA_EXCEEDED"

    completion_payload = {
        "scrapeId": scrape_id,
        "platform": "tiktok",
        "eventType": "COMPLETED",
        "success": has_results,
        "status": completion_status,
        "errorMessage": err_msg,
        "errorReason": err_reason,
        "sessionReports": {
            "jsonPath": report_json_path,
            "htmlPath": mauritanie_html_path,
            "pdfPath": mauritanie_pdf_path,
            "docxPath": mauritanie_docx_path,
            "videosJsonPath": videos_json_path,
            "geminiJsonPath": gemini_report_path,
        },
        "batchSummary": {
            "pagesRequested": len(urls),
            "pagesSucceeded": len(source_rows),
            "pagesFailed": len(failed_pages),
            "failedPages": failed_pages,
            "timeWindowHours": time_window_hours,
            "quotaExceeded": bool(quota_exhausted),
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

        force_refresh = bool(
            task.get("force_refresh")
            if task.get("force_refresh") is not None
            else task.get("forceRefresh")
        )
        refresh_mode = str(
            task.get("refresh_mode")
            if task.get("refresh_mode") is not None
            else task.get("refreshMode")
            or ""
        ).strip().upper() or None
        if force_refresh:
            refresh_mode = "FULL"
        # METRICS_ONLY: toujours appeler Apify (skip cache TTL worker) pour metrics fraiches.
        apify_force = force_refresh or refresh_mode == "METRICS_ONLY"

        scoped_logger.info(
            "Task parsed max_posts=%s force_refresh=%s refresh_mode=%s (raw_max=%s keys=%s)",
            max_posts,
            force_refresh,
            refresh_mode,
            raw_max_posts,
            sorted(str(k) for k in task.keys()),
        )

        if report_mode and report_type in ("csv", "csv_24h") and urls:
            # CSV = fenetre temporelle (APIFY_CSV_REPORT_WINDOW_HOURS).
            # FIX G: plus de fetch large APIFY_CSV_FETCH_LIMIT — filtre date natif.
            window = time_window_hours
            if window is None or window <= 0:
                try:
                    window = int((os.getenv("APIFY_CSV_REPORT_WINDOW_HOURS") or "24").strip())
                except ValueError:
                    window = 24
            fetch_limit = max_posts
            scoped_logger.info(
                "CSV batch task received window_hours=%s fetch_limit=%s urls=%s",
                window,
                fetch_limit,
                len(urls),
            )
            _process_csv_batch_task(
                channel=channel,
                scrape_id=scrape_id,
                urls=urls,
                max_posts_per_page=fetch_limit,
                time_window_hours=window,
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

        # Resilience (job unique): si une tentative echoue (challenge TikTok,
        # exception navigateur, etc.) SANS avoir publie le moindre post, on
        # retente automatiquement avec un backoff au lieu d'abandonner tout
        # le job immediatement. Si des posts ont deja ete publies avant que
        # l'erreur survienne, on les conserve et on cloture en succes partiel
        # plutot que de tout marquer FAILED.
        job_max_attempts = max(1, int((os.getenv("TIKTOK_JOB_MAX_ATTEMPTS") or "1").strip()))
        job_retry_backoff_seconds = max(1.0, float((os.getenv("TIKTOK_JOB_RETRY_BACKOFF_SECONDS") or "10").strip()))

        published_snapshots: dict[str, tuple] = {}
        published_count = 0
        collected_posts_by_sig: dict[str, dict] = {}

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

        def _publish_post_if_new(post: dict) -> None:
            nonlocal published_count
            sig = _post_signature(post)
            payload = normalize_post(scrape_id, url, post, refresh_mode=refresh_mode)
            snapshot = _payload_snapshot(payload)
            collected_posts_by_sig[sig] = post
            previous = published_snapshots.get(sig)
            if previous is None or snapshot != previous:
                published_snapshots[sig] = snapshot
                publish_result(channel, payload)
                published_count += 1

        result: dict = {}
        for attempt in range(1, job_max_attempts + 1):
            post_queue = queue.Queue()

            def on_post(post: dict):
                post_queue.put(post)

            scoped_logger.info(
                "Starting scrape with hard timeout=%.0fs",
                _job_hard_timeout_s(),
                extra={"attempt": attempt},
            )
            # Phase 2: ThreadPoolExecutor + timeout dur (plus de hang infini).
            # Les posts streamés via on_post sont drainés apres le retour.
            try:
                result = _scrape_with_hard_timeout(
                    url=url,
                    max_posts=max_posts,
                    on_post=on_post,
                    analyze_video_content=False,
                    force_refresh=apify_force,
                )
            except Exception:
                scoped_logger.exception("Scrape failed")
                raise

            # Vider la file des posts publies pendant le scrape.
            while True:
                try:
                    post = post_queue.get_nowait()
                    _publish_post_if_new(post)
                except queue.Empty:
                    break

            result = result or {}
            for post in result.get("posts") or []:
                _publish_post_if_new(post)

            attempt_error = str(result.get("error") or "").strip()
            if not attempt_error:
                break

            # Quota: pas de retry (ne changera pas avant demain).
            if _classify_error_reason(attempt_error, str(result.get("error_code") or "")) == "QUOTA_EXCEEDED":
                scoped_logger.error("Apify quota exceeded — aborting retries")
                break

            if published_count > 0:
                scoped_logger.warning(
                    "Attempt failed but posts already published; keeping them as partial success",
                    extra={"attempt": attempt, "error": attempt_error, "published_count": published_count},
                )
                break

            if attempt < job_max_attempts:
                backoff = job_retry_backoff_seconds * attempt + random.uniform(0.0, 2.0)
                scoped_logger.warning(
                    "Attempt failed with 0 posts; retrying after backoff",
                    extra={"attempt": attempt, "error": attempt_error, "backoff_seconds": round(backoff, 1)},
                )
                time.sleep(backoff)
            else:
                scoped_logger.error(
                    "All attempts failed with 0 posts — error=%s class=%s detail=%s",
                    (attempt_error or "")[:240],
                    str(result.get("classification") or ""),
                    str(result.get("error_detail") or result.get("classification_reason") or "")[:160],
                    extra={"attempts": job_max_attempts, "error": attempt_error},
                )

        attempt_error = str(result.get("error") or "").strip()

        if published_count == 0:
            # Echec sec: malgre les tentatives, aucun post n'a pu etre recupere.
            scoped_logger.error("No posts found for task after retries", extra={"post_id": None})
            err_code = str(result.get("error_code") or "").strip()
            reason = _classify_error_reason(attempt_error, err_code)
            if reason == "QUOTA_EXCEEDED":
                err_msg = attempt_error or (
                    "Quota Apify journalier atteint. Réessayez demain ou "
                    "augmentez APIFY_DAILY_VIDEO_LIMIT."
                )
            elif attempt_error and (
                "Quota Apify" in attempt_error
                or "TikTok a bloque" in attempt_error
                or attempt_error.startswith("apify_")
            ):
                err_msg = attempt_error
            else:
                err_msg = (
                    "TikTok a bloque les proxies testes (0 video recuperee). "
                    "Les proxies en echec sont mis en pause 24h. "
                    "Reessaie dans quelques minutes."
                )
            publish_error(channel, scrape_id, err_msg, error_reason=reason)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        posts = list(collected_posts_by_sig.values())

        enriched_posts = []
        # FIX H: METRICS_ONLY → aucun appel Gemini (economie reseau + cout).
        enrichment_enabled = (
            _env_bool("TIKTOK_ASYNC_ENRICHMENT_ENABLED", True)
            and refresh_mode != "METRICS_ONLY"
        )
        enrichment_workers = max(1, int((os.getenv("TIKTOK_ENRICHMENT_WORKERS") or "2").strip()))
        output_dir = os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR") or "video_reports"

        if refresh_mode == "METRICS_ONLY":
            scoped_logger.info(
                "METRICS_ONLY: skipping Gemini enrichment entirely",
                extra={"post_id": None, "posts": len(posts)},
            )

        if enrichment_enabled and posts:
            # Cache de re-scraping (Option B): avant d'appeler Gemini, on demande
            # au gateway quels post_id possedent DEJA une analyse IA en base. Pour
            # ceux-la on reutilise l'analyse existante et on saute Gemini (etape la
            # plus couteuse). Les metriques, elles, sont gerees cote gateway (regle
            # TTL: pas de rafraichissement si scrape < SCRAPE_METRICS_TTL_HOURS).
            cache_reuse = _env_bool("TIKTOK_CACHE_REUSE_ENABLED", True)
            cached_reports = {}
            if cache_reuse:
                post_ids = [str(p.get("post_id") or p.get("id") or "").strip() for p in posts]
                cached_reports = _fetch_cached_video_reports(post_ids)

            posts_to_analyze = []
            for post in posts:
                pid = str(post.get("post_id") or post.get("id") or "").strip()
                cached = cached_reports.get(pid) if pid else None
                if cached:
                    # Reutilisation: on attache l'analyse existante (pour que les
                    # rapports de session soient complets) et on NE rappelle PAS
                    # Gemini. Le document Mongo conserve deja cette analyse (upsert
                    # par platform+postId cote gateway), inutile de republier.
                    post["video_report"] = cached
                    enriched_posts.append(post)
                else:
                    posts_to_analyze.append(post)

            scoped_logger.info(
                "Async enrichment: reusing cached analyses, analyzing the rest",
                extra={"post_id": None, "reused": len(enriched_posts), "to_analyze": len(posts_to_analyze)},
            )

            if posts_to_analyze:
                with ThreadPoolExecutor(max_workers=enrichment_workers) as executor:
                    futures = {executor.submit(_enrich_post_video, post, output_dir): post for post in posts_to_analyze}
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
            raw_posts = enriched_posts if enriched_posts else posts
            session_json = build_session_json_report(page_url=url, posts=raw_posts, output_dir=str(session_output_dir))
            session_json_path = session_json.get("json_path")
            session_html_path = _build_session_html_report(page_url=url, posts=raw_posts, output_dir=session_output_dir)
            session_pdf_path = _build_session_pdf_report(posts=raw_posts, output_dir=session_output_dir)
        except Exception:
            scoped_logger.exception("Failed to generate session reports")

        # PARTIAL_SUCCESS: une erreur/challenge est survenu en cours de route
        # (ou apres coup) mais des posts ont ete recuperes et publies malgre
        # tout. SUCCESS: tout s'est deroule sans accroc.
        is_partial = bool(attempt_error) or result.get("warning") in (
            "challenge_detected_partial",
            "exception_with_partial_posts",
        )
        completion_status = "PARTIAL_SUCCESS" if is_partial else "SUCCESS"

        completion_payload = {
            "scrapeId": scrape_id,
            "platform": "tiktok",
            "eventType": "COMPLETED",
            "success": True,
            "status": completion_status,
            "errorMessage": attempt_error or None,
            "count": published_count,
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
    # Garde-fou invariant #8: WEBSHARE_API_KEY ne doit pas etre utilise en mode
    # rotate (plan Residential Rotating). Aucun code n'appelle proxyproviders.Webshare,
    # mais une cle renseignee induit en erreur — on log un warning clair.
    proxy_mode = (os.getenv("TIKTOK_PROXY_MODE") or "rotate").strip().lower()
    webshare_key = (os.getenv("WEBSHARE_API_KEY") or "").strip()
    if webshare_key and proxy_mode in ("rotate", "rotating", "endpoint"):
        LOGGER.warning(
            "[PROXY] WEBSHARE_API_KEY is set but TIKTOK_PROXY_MODE=%s — "
            "ignored (proxyproviders.Webshare /proxy/list incompatible with "
            "Residential Rotating). Use TIKTOK_PROXY_USERNAME/PASSWORD sticky only.",
            proxy_mode,
        )

    # Filet de securite: tue Chrome orphelins (parent mort / PPID=1).
    try:
        start_chrome_watchdog()
    except Exception:
        LOGGER.debug("chrome watchdog failed to start", exc_info=True)

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
