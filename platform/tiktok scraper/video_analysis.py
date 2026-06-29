import base64
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import requests


STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "you", "your", "are", "was", "were", "have", "has",
    "had", "about", "dans", "avec", "pour", "sur", "une", "des", "les", "est", "pas", "que", "qui", "par",
    "mais", "plus", "nous", "vous", "ils", "elles", "their", "them", "its", "it's", "de", "du", "la", "le",
}


def _slugify(value: str) -> str:
    raw = (value or "video").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-") or "video"


def _safe_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_keywords(text: str, top_k: int = 12) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_']{3,}", (text or "").lower())
    cleaned = [w for w in words if w not in STOPWORDS and not w.isdigit()]
    counts = Counter(cleaned)
    return [word for word, _ in counts.most_common(top_k)]


def _extract_themes(keywords: list[str], top_k: int = 5) -> list[str]:
    return keywords[:top_k]


def _load_face_cascade():
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if not os.path.exists(cascade_path):
            return None
        detector = cv2.CascadeClassifier(cascade_path)
        return detector if not detector.empty() else None
    except Exception:
        return None


def _compute_confidence(transcript: str, segments: list[dict]) -> dict:
    transcript_len = len((transcript or "").strip())
    avg_logprob_values = [s.get("avg_logprob") for s in segments if s.get("avg_logprob") is not None]
    avg_logprob = sum(avg_logprob_values) / len(avg_logprob_values) if avg_logprob_values else None

    score = 0.0
    if transcript_len > 120:
        score += 0.45
    elif transcript_len > 40:
        score += 0.25

    if avg_logprob is not None:
        # avg_logprob closer to 0 is better.
        if avg_logprob > -0.4:
            score += 0.35
        elif avg_logprob > -1.0:
            score += 0.2
        else:
            score += 0.08
    else:
        score += 0.12

    score = min(0.98, max(0.05, score))

    limits = []
    if transcript_len < 30:
        limits.append("audio_trop_court_ou_peu_parole")
    if avg_logprob is not None and avg_logprob <= -1.0:
        limits.append("qualite_audio_ou_langue_difficle")

    return {
        "score": round(score, 2),
        "level": "high" if score >= 0.7 else "medium" if score >= 0.4 else "low",
        "limits": limits,
    }


def _download_video(video_url: str, output_dir: Path) -> dict:
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise RuntimeError(f"yt-dlp indisponible: {exc}") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_stem = _slugify(video_url.split("/")[-1])
    outtmpl = str(output_dir / f"{file_stem}_{ts}.%(ext)s")

    opts = {
        "format": "mp4/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        downloaded_path = Path(ydl.prepare_filename(info))

    if not downloaded_path.exists():
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


def _transcribe_with_whisper(video_path: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return {
            "language": None,
            "language_probability": None,
            "transcript": "",
            "segments": [],
            "error": f"whisper_unavailable: {exc}",
        }

    model_name = os.getenv("WHISPER_MODEL", "small")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "5"))

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        raw_segments, info = model.transcribe(video_path, beam_size=beam_size, vad_filter=True)
    except Exception as exc:
        return {
            "language": None,
            "language_probability": None,
            "transcript": "",
            "segments": [],
            "error": f"whisper_runtime_error: {exc}",
        }

    segments = []
    transcript_parts = []
    for seg in raw_segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        transcript_parts.append(text)
        segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": text,
            "avg_logprob": getattr(seg, "avg_logprob", None),
        })

    transcript = " ".join(transcript_parts).strip()
    return {
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "transcript": transcript,
        "segments": segments,
        "error": None,
    }


def _extract_frame_samples(video_path: str, sample_count: int = 6) -> list[str]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_total <= 0:
        cap.release()
        return []

    indices = sorted(set(int((i + 1) * frame_total / (sample_count + 1)) for i in range(sample_count)))
    frames_b64 = []

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        frames_b64.append(base64.b64encode(buf.tobytes()).decode("ascii"))

    cap.release()
    return frames_b64


def _analyze_visual_local(video_path: str, sample_count: int = 6) -> dict:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {
            "visual_elements": [],
            "scene_summary": "Impossible d'ouvrir la video pour analyse visuelle locale.",
            "limits": ["local_visual_open_failed"],
        }

    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_total <= 0:
        cap.release()
        return {
            "visual_elements": [],
            "scene_summary": "Aucune frame exploitable pour analyse visuelle locale.",
            "limits": ["local_visual_no_frames"],
        }

    indices = sorted(set(int((i + 1) * frame_total / (sample_count + 1)) for i in range(sample_count)))
    face_detector = _load_face_cascade()

    face_hits = 0
    bright_sum = 0.0
    saturation_sum = 0.0
    sampled = 0

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue

        sampled += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        bright_sum += float(gray.mean())

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        saturation_sum += float(hsv[:, :, 1].mean())

        if face_detector is not None:
            faces = face_detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            if len(faces) > 0:
                face_hits += 1

    cap.release()

    if sampled == 0:
        return {
            "visual_elements": [],
            "scene_summary": "Aucune frame lisible apres echantillonnage.",
            "limits": ["local_visual_sampling_failed"],
        }

    avg_brightness = bright_sum / sampled
    avg_saturation = saturation_sum / sampled
    face_ratio = face_hits / sampled

    visual_elements = []

    if face_ratio >= 0.5:
        visual_elements.append("presence_humaine_dominante")
    elif face_ratio > 0:
        visual_elements.append("presence_humaine_ponctuelle")
    else:
        visual_elements.append("pas_de_visage_detecte")

    if avg_brightness < 70:
        visual_elements.append("scene_sombre")
    elif avg_brightness > 170:
        visual_elements.append("scene_tres_lumineuse")
    else:
        visual_elements.append("luminosite_moyenne")

    if avg_saturation > 90:
        visual_elements.append("couleurs_vives")
    elif avg_saturation < 45:
        visual_elements.append("couleurs_peu_saturees")
    else:
        visual_elements.append("couleurs_moderees")

    scene_summary = (
        f"Analyse locale sur {sampled} frames: luminosite moyenne {avg_brightness:.1f}, "
        f"saturation moyenne {avg_saturation:.1f}, presence visage sur {face_hits}/{sampled} frames."
    )

    return {
        "visual_elements": visual_elements,
        "scene_summary": scene_summary,
        "limits": [],
    }


def _analyze_visual_with_gemini(frame_b64_images: list[str]) -> dict:
    enabled = os.getenv("VIDEO_ANALYSIS_ENABLE_GEMINI_VISUAL", "false").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    if not enabled:
        return {
            "visual_elements": [],
            "raw": None,
            "limits": ["gemini_visual_desactive"],
        }
    if not api_key:
        return {
            "visual_elements": [],
            "raw": None,
            "limits": ["gemini_api_key_absente"],
        }
    if not frame_b64_images:
        return {
            "visual_elements": [],
            "raw": None,
            "limits": ["aucune_frame_extraite"],
        }

    prompt = (
        "Analyse ces images extraites d'une video TikTok et retourne uniquement un JSON valide avec le schema: "
        "{\"visual_elements\": [\"...\"], \"on_screen_text\": [\"...\"], \"scene_summary\": \"...\"}. "
        "Sois factuel et bref."
    )

    parts = [{"text": prompt}]
    for b64 in frame_b64_images:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})

    payload = {"contents": [{"role": "user", "parts": parts}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {
            "visual_elements": [],
            "raw": None,
            "limits": [f"gemini_erreur: {exc}"],
        }

    text_out = ""
    try:
        candidates = data.get("candidates") or []
        content = candidates[0].get("content") if candidates else {}
        content_parts = (content or {}).get("parts") or []
        text_out = "\n".join(p.get("text", "") for p in content_parts if p.get("text"))
    except Exception:
        text_out = ""

    parsed = None
    if text_out:
        json_match = re.search(r"\{[\s\S]*\}", text_out)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
            except Exception:
                parsed = None

    visual_elements = []
    if isinstance(parsed, dict):
        ve = parsed.get("visual_elements")
        if isinstance(ve, list):
            visual_elements = [str(x).strip() for x in ve if str(x).strip()]

    limits = []
    if not parsed:
        limits.append("reponse_gemini_non_json")

    return {
        "visual_elements": visual_elements,
        "raw": parsed if isinstance(parsed, dict) else text_out,
        "limits": limits,
    }


def _build_executive_summary(transcript: str, keywords: list[str], visual_elements: list[str], title: str) -> list[str]:
    lines = []
    if title:
        lines.append(f"Video cible: {title}")

    if transcript:
        topic_hint = transcript[:220].replace("\n", " ").strip()
        lines.append("Sujet audio principal: " + topic_hint)
    else:
        lines.append("Aucun contenu audio exploitable n'a ete transcrit.")

    if keywords:
        lines.append("Mots dominants: " + ", ".join(keywords[:6]))

    if visual_elements:
        lines.append("Indices visuels detectes: " + ", ".join(visual_elements[:5]))
    else:
        lines.append("Analyse visuelle locale limitee disponible (sans modele vision externe).")

    return lines[:5]


def analyze_tiktok_video(video_url: str, output_dir: str | None = None) -> dict:
    if not video_url or "tiktok.com" not in video_url.lower():
        raise ValueError("video_url TikTok invalide")

    base_output = Path(output_dir or os.getenv("VIDEO_ANALYSIS_OUTPUT_DIR", "video_reports"))
    base_output.mkdir(parents=True, exist_ok=True)

    download_info = _download_video(video_url, base_output)
    video_path = download_info["video_path"]

    whisper_info = _transcribe_with_whisper(video_path)
    transcript = whisper_info["transcript"]
    segments = whisper_info["segments"]

    keywords = _extract_keywords(transcript)
    themes = _extract_themes(keywords)

    frame_samples = _extract_frame_samples(video_path)
    visual_info = _analyze_visual_with_gemini(frame_samples)

    local_visual = _analyze_visual_local(video_path)
    local_elements = local_visual.get("visual_elements", [])
    gemini_elements = visual_info.get("visual_elements", [])
    visual_elements = gemini_elements if gemini_elements else local_elements

    confidence = _compute_confidence(transcript, segments)
    confidence["limits"].extend(visual_info.get("limits", []))
    confidence["limits"].extend(local_visual.get("limits", []))
    if whisper_info.get("error"):
        confidence["limits"].append(whisper_info["error"])

    excerpt = ""
    if transcript:
        excerpt = transcript[:600]

    report = {
        "executive_summary": _build_executive_summary(
            transcript=transcript,
            keywords=keywords,
            visual_elements=visual_elements,
            title=download_info.get("title", ""),
        ),
        "transcript_excerpt": excerpt,
        "transcript_full": transcript,
        "themes": themes,
        "visual_elements_detected": visual_elements,
        "keywords": keywords,
        "confidence_and_limits": confidence,
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
            "frame_sample_count": len(frame_samples),
            "whisper_language": whisper_info.get("language"),
            "whisper_language_probability": whisper_info.get("language_probability"),
            "whisper_error": whisper_info.get("error"),
        },
        "visual_raw": visual_info.get("raw"),
        "visual_local_summary": local_visual.get("scene_summary"),
    }

    report_name = _slugify(download_info.get("title") or video_url.split("/")[-1])
    report_path = base_output / f"report_{report_name}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    report["artifacts"]["report_path"] = str(report_path)
    return report


def build_small_video_report(report: dict) -> dict:
    return {
        "executive_summary": report.get("executive_summary", []),
        "transcript_excerpt": report.get("transcript_excerpt", ""),
        "themes": report.get("themes", []),
        "visual_elements_detected": report.get("visual_elements_detected", []),
        "keywords": report.get("keywords", []),
        "confidence_and_limits": report.get("confidence_and_limits", {}),
    }
