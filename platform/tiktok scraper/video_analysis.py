import json
import os
import sys
from datetime import datetime, timezone
import importlib
from pathlib import Path
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logging_setup import get_logger


LOGGER = get_logger(__name__, platform="tiktok", service="video_analysis")


def _load_env_file():
    """Charge un fichier .env local dans os.environ (sans ecraser l'existant).

    Cette fonction est appelee au chargement du module pour garantir que les
    variables de configuration (Gemini, chemins, options yt-dlp, etc.) sont
    disponibles meme si le script est lance hors du dossier racine.
    """
    # On teste plusieurs emplacements possibles pour .env.
    candidates = [
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path.cwd() / ".env",
    ]

    # On prend le premier .env existant, sinon None.
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        # Aucun fichier trouve: on sort sans erreur.
        return

    # Lecture ligne par ligne du fichier .env.
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # Ignore lignes vides, commentaires, et lignes mal formees.
        if not line or line.startswith("#") or "=" not in line:
            continue
        # Coupe en cle=valeur sur le premier '='.
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        # Nettoie les guillemets eventuels autour de la valeur.
        cleaned_value = value.strip().strip('"').strip("'")
        # setdefault n'ecrase pas une variable deja exportee dans l'environnement.
        os.environ.setdefault(key, cleaned_value)


_load_env_file()


def _slugify(value: str) -> str:
    """Transforme un texte libre en identifiant de fichier propre.

    Exemple: "Mon Rapport 2026!" -> "mon-rapport-2026".
    """
    # Valeur par defaut si entree vide.
    raw = (value or "video").strip().lower()
    cleaned = []
    prev_dash = False
    # Parcours caractere par caractere.
    for ch in raw:
        if ch.isalnum():
            # Caractere alphanumerique: conserve tel quel.
            cleaned.append(ch)
            prev_dash = False
        else:
            # Remplace toute sequence de separateurs par un seul '-'.
            if not prev_dash:
                cleaned.append("-")
                prev_dash = True
    # Supprime les '-' en debut/fin et renvoie une valeur de secours si vide.
    out = "".join(cleaned).strip("-")
    return out or "video"


def _safe_text(value) -> str:
    """Convertit proprement une valeur quelconque en texte."""
    if value is None:
        return ""
    return str(value).strip()


def _extract_json_from_text(text: str):
    """Tente d'extraire un objet JSON depuis une reponse texte.

    Gemini peut parfois renvoyer:
    - un JSON pur
    - du texte avec JSON inclus
    Cette fonction gere les deux cas.
    """
    if not text:
        return None

    # Cas ideal: le texte entier est deja un JSON.
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass

    # Cas fallback: on cherche le premier '{' et le dernier '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def _resolve_gemini_model_name() -> str:
    """Resout le nom de modele Gemini a utiliser.

    Regles:
    - lit GEMINI_MODEL
    - accepte le prefixe "models/"
    - migre d'anciens noms vers gemini-2.5-flash
    """
    # Nettoie les espaces et guillemets eventuels.
    raw = (os.getenv("GEMINI_MODEL") or "").strip().strip('"').strip("'")
    if not raw:
        # Valeur recommandee par defaut.
        return "gemini-2.5-flash"

    # Some configs are provided as "models/<name>"; SDK expects only the model name.
    if raw.startswith("models/"):
        raw = raw.split("/", 1)[1].strip()

    # Legacy/retired values should gracefully move to the current default.
    if raw in {"gemini-1.5-flash", "gemini-1.5-pro"}:
        LOGGER.info("Gemini model upgraded to gemini-2.5-flash")
        return "gemini-2.5-flash"

    return raw


def _normalize_file_state(state) -> str:
    """Normalise l'etat d'un fichier Gemini en texte majuscule."""
    if state is None:
        return "UNKNOWN"

    # Handles enum-like objects from SDK (e.g., state.name), strings, or unknown types.
    name = getattr(state, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip().upper()

    text = str(state).strip().upper()
    if "." in text:
        text = text.split(".")[-1]
    return text or "UNKNOWN"


def _wait_for_gemini_file_active(client, file_name: str):
    """Attend qu'un fichier uploade a Gemini passe a l'etat ACTIVE.

    Si l'etat devient terminal (FAILED/CANCELLED/DELETED), leve une erreur.
    Si le timeout est depasse, leve une erreur explicite.
    """
    # Timeout total et intervalle de polling configurables via env.
    timeout_s = int((os.getenv("GEMINI_FILE_ACTIVE_TIMEOUT_SECONDS") or "180").strip())
    poll_s = float((os.getenv("GEMINI_FILE_ACTIVE_POLL_SECONDS") or "2").strip())
    deadline = time.time() + max(15, timeout_s)
    last_state = "UNKNOWN"

    # Boucle de polling jusqu'au deadline.
    while time.time() < deadline:
        current = client.files.get(name=file_name)
        state_value = getattr(current, "state", None)
        state = _normalize_file_state(state_value)
        last_state = state

        if state == "ACTIVE":
            # Le fichier est pret a etre utilise dans generate_content.
            return current

        if state in {"FAILED", "CANCELLED", "DELETED"}:
            raise RuntimeError(f"Fichier Gemini dans un etat terminal non exploitable: {state}")

        time.sleep(max(0.5, poll_s))

    raise RuntimeError(f"Timeout en attente de l'etat ACTIVE du fichier Gemini (dernier etat: {last_state})")


def _module_dir() -> Path:
    """Retourne le dossier du module courant."""
    return Path(__file__).resolve().parent


def _cookies_json_candidates() -> list[Path]:
    """Retourne les emplacements candidats du fichier cookies JSON TikTok."""
    # Priorite au chemin configure explicitement par variable d'environnement.
    env_cookie_path = (os.getenv("TIKTOK_YTDLP_COOKIES_JSON") or "").strip()
    candidates = []
    if env_cookie_path:
        candidates.append(Path(env_cookie_path))

    candidates.extend(
        [
            _module_dir() / "tiktok_cookies.json",
            Path.cwd() / "tiktok_cookies.json",
        ]
    )
    return candidates


def _build_cookiefile(output_dir: Path) -> str | None:
    """Construit un cookiefile Netscape pour yt-dlp a partir d'un JSON cookies.

    Retourne le chemin du fichier texte genere, ou None si indisponible.
    """
    # Cherche le premier fichier cookies JSON existant.
    source_path = next((path for path in _cookies_json_candidates() if path.exists()), None)
    if source_path is None:
        return None

    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Impossible de lire les cookies TikTok JSON", exc_info=True)
        return None

    if not isinstance(raw, list):
        return None

    # Format attendu par yt-dlp: Netscape HTTP Cookie File.
    cookiefile_path = output_dir / "yt_dlp_tiktok_cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]

    for cookie in raw:
        if not isinstance(cookie, dict):
            continue

        domain = str(cookie.get("domain") or "").strip()
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        path = str(cookie.get("path") or "/")

        if not domain or not name:
            continue

        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expirationDate", cookie.get("expires", 0))
        try:
            expires = str(int(float(expires or 0)))
        except (TypeError, ValueError):
            expires = "0"

        lines.append("\t".join([domain, include_subdomains, path, secure, expires, name, value]))

    if len(lines) == 1:
        return None

    cookiefile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cookiefile_path)


def _download_video(video_url: str, output_dir: Path) -> dict:
    """Telecharge une video TikTok via yt-dlp et retourne ses metadonnees utiles.

    Inclut:
    - chemin du fichier local
    - URL media directe (si disponible)
    - metadonnees titre/uploader/duree/tags
    """
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise RuntimeError(f"yt-dlp indisponible: {exc}") from exc

    # Prepare le dossier et le schema de nom de fichier.
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_stem = _slugify(video_url.split("/")[-1])
    outtmpl = str(output_dir / f"{file_stem}_{ts}.%(ext)s")
    user_agent = (
        os.getenv("TIKTOK_YTDLP_USER_AGENT")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    cookiefile = _build_cookiefile(output_dir)

    # Options yt-dlp: format, retries, headers anti-blocage.
    opts = {
        "format": "mp4/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "http_headers": {
            "User-Agent": user_agent,
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        },
    }

    if cookiefile:
        # Fournit explicitement un cookiefile texte a yt-dlp.
        opts["cookiefile"] = cookiefile

    browser_name = (os.getenv("TIKTOK_YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    if browser_name:
        # Optionnel: demande a yt-dlp de lire les cookies depuis un navigateur local.
        opts["cookiesfrombrowser"] = (browser_name,)

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            downloaded_path = Path(ydl.prepare_filename(info))
    except Exception as exc:
        raise RuntimeError(
            "yt-dlp n'a pas pu telecharger la video TikTok. "
            f"Verifie GEMINI_API_KEY, les cookies TikTok et les headers de contournement. Erreur: {exc}"
        ) from exc

    if not downloaded_path.exists():
        # Certains extracteurs changent l'extension finale; tentative .mp4.
        alt = downloaded_path.with_suffix(".mp4")
        if alt.exists():
            downloaded_path = alt
        else:
            raise FileNotFoundError("Impossible de trouver la video telechargee")

    return {
        "video_path": str(downloaded_path),
        "media_url": _safe_text(info.get("url") or info.get("play_addr") or info.get("download_url")),
        "title": _safe_text(info.get("title")),
        "description": _safe_text(info.get("description")),
        "uploader": _safe_text(info.get("uploader") or info.get("channel")),
        "duration_seconds": info.get("duration"),
        "webpage_url": _safe_text(info.get("webpage_url") or video_url),
        "tags": info.get("tags") or [],
    }


def _analyze_video_with_gemini_sdk(video_path: str) -> dict:
    """Analyse une video locale avec Gemini et impose une sortie JSON structuree."""
    # Cle API et modele resolves depuis l'environnement.
    api_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    model_name = _resolve_gemini_model_name()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY manquante")

    # Import dynamique pour eviter une erreur immediate si le package manque.
    try:
        genai = importlib.import_module("google.genai")
        types = genai.types
    except Exception as exc:
        raise RuntimeError(f"google-genai indisponible: {exc}") from exc

    # Schema JSON strict demande au modele pour fiabiliser le parsing.
    schema = {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "array", "items": {"type": "string"}},
            "transcript_excerpt": {"type": "string"},
            "transcript_full": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
            "visual_elements_detected": {"type": "array", "items": {"type": "string"}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "confidence_and_limits": {
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                    "level": {"type": "string"},
                    "limits": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "level", "limits"],
            },
            "audio_language": {"type": "string"},
            "on_screen_text": {"type": "array", "items": {"type": "string"}},
            "sentiment": {"type": "string"},
            "safety_flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "executive_summary",
            "transcript_excerpt",
            "transcript_full",
            "themes",
            "visual_elements_detected",
            "keywords",
            "confidence_and_limits",
        ],
    }

    # Prompt systeme metier: impose factuel + format JSON.
    prompt = (
        "Tu es un analyste video TikTok. Analyse cette video et retourne UNIQUEMENT un JSON valide. "
        "Sois factuel et ne fabrique pas d'informations. "
        "Remplis toutes les cles requises du schema. "
        "Pour confidence_and_limits.level, utilise uniquement: low, medium, high."
    )

    # Client Gemini initialise avec la cle API.
    client = genai.Client(api_key=api_key)

    # Upload du fichier video vers Gemini Files API.
    upload_obj = client.files.upload(file=video_path)
    upload_name = getattr(upload_obj, "name", None)
    if not upload_name:
        raise RuntimeError("Upload Gemini reussi mais identifiant de fichier absent")

    # Attente active jusqu'a ce que le fichier soit exploitable.
    ready_file = _wait_for_gemini_file_active(client, upload_name)
    file_uri = getattr(ready_file, "uri", None) or getattr(upload_obj, "uri", None)
    mime_type = getattr(ready_file, "mime_type", None) or getattr(upload_obj, "mime_type", None)

    if not file_uri or not mime_type:
        raise RuntimeError("Fichier Gemini ACTIVE mais informations uri/mime_type manquantes")

    # Reference media transmise au modele (URI + mime type).
    file_ref = types.Part.from_uri(file_uri=file_uri, mime_type=mime_type)

    # Config de generation: JSON obligatoire + temperature basse.
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.2,
    )

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, file_ref],
            config=config,
        )
    except Exception as exc:
        # Fallback automatique si modele introuvable (404).
        msg = str(exc)
        if "404" in msg and "not found" in msg.lower() and model_name != "gemini-2.5-flash":
            fallback_model = "gemini-2.5-flash"
            LOGGER.warning("Gemini model indisponible, retry avec fallback")
            response = client.models.generate_content(
                model=fallback_model,
                contents=[prompt, file_ref],
                config=config,
            )
            model_name = fallback_model
        else:
            raise

    raw_text = getattr(response, "text", "") or ""
    # Parse du JSON (direct ou extrait du texte).
    parsed = _extract_json_from_text(raw_text)

    if not isinstance(parsed, dict):
        try:
            candidate = response.candidates[0].content.parts[0].text
            parsed = _extract_json_from_text(candidate)
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini n'a pas retourne un JSON exploitable")

    return {
        "analysis": parsed,
        "raw": raw_text,
        "model_name": model_name,
    }


def analyze_tiktok_video(video_url: str, output_dir: str | None = None, save_json_report: bool = True) -> dict:
    """Pipeline complet: telecharger la video TikTok puis l'analyser avec Gemini.

    Retourne un rapport riche pret a etre exploite par worker/scraper.
    """
    if not video_url or "tiktok.com" not in video_url.lower():
        raise ValueError("video_url TikTok invalide")

    # Dossier de sortie des artefacts (video + rapports).
    base_output = Path(output_dir or os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR", "video_reports"))
    base_output.mkdir(parents=True, exist_ok=True)

    # 1) Telechargement local de la video.
    download_info = _download_video(video_url, base_output)
    video_path = download_info["video_path"]

    # 2) Analyse IA du fichier video.
    gemini_result = _analyze_video_with_gemini_sdk(video_path)
    analysis = gemini_result["analysis"]

    # 3) Construction d'un rapport normalise.
    report = {
        "executive_summary": analysis.get("executive_summary", []),
        "transcript_excerpt": analysis.get("transcript_excerpt", ""),
        "transcript_full": analysis.get("transcript_full", ""),
        "themes": analysis.get("themes", []),
        "visual_elements_detected": analysis.get("visual_elements_detected", []),
        "keywords": analysis.get("keywords", []),
        "confidence_and_limits": analysis.get(
            "confidence_and_limits",
            {"score": 0.0, "level": "low", "limits": ["missing_confidence_from_model"]},
        ),
        "video_metadata": {
            "title": download_info.get("title"),
            "uploader": download_info.get("uploader"),
            "duration_seconds": download_info.get("duration_seconds"),
            "webpage_url": download_info.get("webpage_url"),
            "media_url": download_info.get("media_url"),
            "tags": download_info.get("tags"),
        },
        "artifacts": {
            "video_path": video_path,
            "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
            "model_provider": "google-genai",
            "model_name": gemini_result.get("model_name") or _resolve_gemini_model_name(),
        },
        "gemini_analysis": analysis,
        "gemini_raw": gemini_result.get("raw"),
    }

    # Champs optionnels presents selon la sortie modele.
    extra_fields = ["audio_language", "on_screen_text", "sentiment", "safety_flags"]
    for field in extra_fields:
        if field in analysis:
            report[field] = analysis.get(field)

    if save_json_report:
        # Sauvegarde JSON du rapport sur disque avec nom horodate.
        report_name = _slugify(download_info.get("title") or video_url.split("/")[-1])
        report_path = base_output / f"report_{report_name}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        with report_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["artifacts"]["report_path"] = str(report_path)

    return report


def build_small_video_report(report: dict) -> dict:
    """Retourne une vue compacte du rapport video pour les payloads legers."""
    return {
        "executive_summary": report.get("executive_summary", []),
        "transcript_excerpt": report.get("transcript_excerpt", ""),
        "themes": report.get("themes", []),
        "visual_elements_detected": report.get("visual_elements_detected", []),
        "keywords": report.get("keywords", []),
        "confidence_and_limits": report.get("confidence_and_limits", {}),
        "audio_language": report.get("audio_language"),
        "on_screen_text": report.get("on_screen_text", []),
        "sentiment": report.get("sentiment"),
        "safety_flags": report.get("safety_flags", []),
    }


def build_session_json_report(page_url: str, posts: list[dict], output_dir: str | None = None) -> dict:
    """Construit un rapport JSON de session (niveau page/profil).

    Ce rapport regroupe l'URL source, la date de generation et tous les posts
    collectes/analyses pendant la session.
    """
    base_output = Path(output_dir or os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR", "video_reports"))
    base_output.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    page_name = _slugify(page_url.split("/")[-1] or "tiktok_page")
    out_path = base_output / f"session_{page_name}_{ts}.json"

    # Charge utile finale du fichier session_*.json.
    payload = {
        "page_url": page_url,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_posts": len(posts),
        "posts": posts,
    }

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return {
        "json_path": str(out_path),
    }
