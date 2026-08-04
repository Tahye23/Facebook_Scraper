#!/usr/bin/env python3
"""Génère un rapport handoff développeurs (HTML + PDF optionnel).

Usage:
  python docs/generate_handoff_report.py
  python docs/generate_handoff_report.py --out docs/output
  python docs/generate_handoff_report.py --pdf

Sorties:
  - handoff_developers_<timestamp>.html  (rapport principal, joli, imprimable)
  - handoff_developers_<timestamp>.pdf   (si --pdf et reportlab installé)
"""

from __future__ import annotations

import argparse
import html
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path(__file__).resolve().parent / "output"


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def md_inline(text: str) -> str:
    """Mini markdown inline: **bold**, `code`."""
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def p(text: str) -> str:
    return f"<p>{md_inline(text)}</p>"


def ul(items: list[str]) -> str:
    lis = "".join(f"<li>{md_inline(i)}</li>" for i in items)
    return f"<ul>{lis}</ul>"


def ol(items: list[str]) -> str:
    lis = "".join(f"<li>{md_inline(i)}</li>" for i in items)
    return f"<ol>{lis}</ol>"


def table(headers: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        tds = "".join(f"<td>{md_inline(c)}</td>" for c in row)
        body.append(f"<tr>{tds}</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def pre(title: str, content: str) -> str:
    return (
        f"<figure class='diagram'>"
        f"<figcaption>{esc(title)}</figcaption>"
        f"<pre>{esc(content)}</pre>"
        f"</figure>"
    )


def card(title: str, body_html: str, tag: str = "") -> str:
    badge = f"<span class='badge'>{esc(tag)}</span>" if tag else ""
    return f"<article class='card'><h3>{esc(title)}{badge}</h3>{body_html}</article>"


def adr(title: str, context: str, decision: str, why: str, consequences: str, rejected: str = "") -> str:
    parts = [
        f"<div class='adr-grid'>"
        f"<div><h4>Contexte</h4>{p(context)}</div>"
        f"<div><h4>Décision</h4>{p(decision)}</div>"
        f"<div><h4>Pourquoi</h4>{p(why)}</div>"
        f"<div><h4>Conséquences</h4>{p(consequences)}</div>"
    ]
    if rejected:
        parts.append(f"<div class='full'><h4>Alternatives rejetées</h4>{p(rejected)}</div>")
    parts.append("</div>")
    return card(title, "".join(parts), tag="ADR")


def section(anchor: str, title: str, body_html: str) -> str:
    return f"<section id='{esc(anchor)}'><h2>{esc(title)}</h2>{body_html}</section>"


def build_sections() -> list[tuple[str, str, str]]:
    """Retourne [(anchor, title, html_body), ...]."""
    sections: list[tuple[str, str, str]] = []

    # 1 Overview
    sections.append((
        "overview",
        "1. Vue d’ensemble",
        "".join([
            p("Système de scraping multi-plateforme (Facebook + TikTok) orienté **métadonnées de posts** "
              "+ analyse IA Gemini, exposé via une API Gateway Spring Boot, orchestré par RabbitMQ, "
              "persisté dans MongoDB, packagé entièrement en **Docker Compose**."),
            p("Objectif TikTok : scraper un profil ou un CSV, streamer vite les résultats à l’utilisateur, "
              "enrichir ensuite avec Gemini, et gérer le re-scrape sans historique inutile."),
            table(
                ["Couche", "Techno"],
                [
                    ["API / orchestration", "Spring Boot (gateway)"],
                    ["Messaging", "RabbitMQ"],
                    ["Persistence", "MongoDB"],
                    ["Workers", "Python + Playwright / Chrome"],
                    ["IA", "Google Gemini"],
                    ["Déploiement", "Docker Compose"],
                ],
            ),
        ]),
    ))

    # 2 Architecture
    arch_ascii = r"""
┌─────────────┐     HTTP      ┌──────────────┐
│ Postman /   │──────────────▶│   Gateway    │
│ Frontend    │◀──────────────│ Spring Boot  │
└─────────────┘   poll GET    └──────┬───────┘
                                     │ publish job
                                     ▼
                              ┌──────────────┐
                              │   RabbitMQ   │
                              └──────┬───────┘
                     ┌───────────────┼───────────────┐
                     ▼               ▼               ▼
              scraping_queue   scraping_queue   scrape_result
                 _tiktok          _facebook         _queue
                     │               │               ▲
                     ▼               ▼               │
              ┌────────────┐  ┌────────────┐        │
              │worker-tiktok│  │worker-fb   │────────┘
              │Playwright   │  └────────────┘
              │Chrome+Xvfb  │
              └──────┬──────┘
                     │ sticky IP
                     ▼
              ┌────────────┐     ┌─────────┐
              │  Webshare  │────▶│ TikTok  │
              └────────────┘     └─────────┘
                     │
                     ▼
              ┌────────────┐
              │   Gemini   │  (video_report)
              └────────────┘

Gateway upsert ──▶ MongoDB (1 doc / platform+postId)
"""
    sections.append((
        "architecture",
        "2. Architecture globale",
        "".join([
            pre("Schéma composants", arch_ascii),
            table(
                ["Service", "Fait", "Ne fait pas"],
                [
                    ["gateway", "API, jobs, Rabbit, upsert Mongo, TTL, cache reports", "Ne scrape pas TikTok"],
                    ["worker-tiktok", "Browser scrape, proxies, Gemini, rapports batch", "N’est pas la source de vérité Mongo"],
                    ["worker-facebook", "Scrape Facebook via sa queue", "—"],
                    ["rabbitmq", "Transport async, découplage", "—"],
                    ["mongodb", "Jobs + résultats", "Pas d’historique de versions"],
                ],
            ),
        ]),
    ))

    # 3 ADR
    sections.append((
        "decisions",
        "3. Décisions d’architecture (pourquoi)",
        "".join([
            adr(
                "Gateway + workers (pas monolithe)",
                "Le scrape Playwright est long, fragile, CPU/RAM lourd.",
                "API Spring séparée des workers Python.",
                "API responsive ; scale workers indépendamment ; isolation des crashes browser.",
                "Évolutions résultats = souvent gateway + worker.",
                "Scrape synchrone dans l’API = timeouts HTTP et blocage.",
            ),
            adr(
                "RabbitMQ (pas HTTP synchrone worker)",
                "Besoin de buffer, retry, multi-plateformes.",
                "Exchange `scrape.exchange` + queues dédiées + result queue + DLQ.",
                "Découplage, résilience, plusieurs consumers possibles.",
                "Observer les files (UI :15672) en debug.",
                "Appels HTTP gateway→worker : couplage fort, pas de buffer.",
            ),
            adr(
                "Mongo 1 doc / (platform, postId)",
                "Besoin de l’état actuel d’une vidéo, pas d’un journal.",
                "Upsert unique ; `scrapeId` rattache au dernier job.",
                "Simplifie cache Gemini et GET results.",
                "Pas de timeline metrics historique (sauf évolution future).",
                "Historique complet = complexité et coût stockage.",
            ),
            adr(
                "Streaming métadonnées puis Gemini",
                "Gemini est lent/coûteux ; l’utilisateur veut voir tôt.",
                "Publish RESULT immédiat, puis ENRICHMENT async.",
                "UX streaming + économie IA via cache `video_report`.",
                "Deux messages Rabbit par post enrichi.",
                "Attendre Gemini avant tout affichage = UX mauvaise.",
            ),
            adr(
                "Sticky proxy = sticky Chrome profile",
                "Cookie global sur IP aléatoires → soft-block TikTok (grille vide).",
                "`sdwopfmy-N` → `tiktok_sessions/sdwopfmy-N/` persistent context.",
                "Cookies/fingerprint nés sous la même IP sticky.",
                "Volume Docker obligatoire pour survivre aux recreate.",
                "Un seul profil Chrome partagé = détection / locks.",
            ),
            adr(
                "Pool 20k + sample + blacklist",
                "Beaucoup d’IP meurent (tunnel, auth, soft-block).",
                "Sample N IP/job, blacklist ciblée, rotation fail-fast.",
                "Résilience sans dépendre d’une seule IP.",
                "Surveiller `proxy_blacklist.json` pour éviter pool brûlé.",
                "IP unique fixe = SPOF.",
            ),
            adr(
                "Chrome headed + Xvfb",
                "Headless pur plus souvent détecté.",
                "`TIKTOK_HEADLESS=false` + `DISPLAY=:99` sous Xvfb.",
                "Fingerprint plus proche d’un navigateur réel en Docker.",
                "Image plus lourde ; besoin Xvfb dans le container.",
                "Headless seul = plus de soft-blocks observés.",
            ),
            adr(
                "TTL metrics 12h + exception null",
                "Re-scrape fréquent ne doit pas écraser metrics fraîches, ni bloquer un 1er scrape sans likes.",
                "Si metrics présentes et <12h → garder ; si null → accepter incoming.",
                "Équilibre fraîcheur / coût scrape.",
                "Bug historique corrigé : TTL bloquait likes même quand null.",
                "Toujours écraser = charge inutile ; jamais écraser = metrics figées null.",
            ),
        ]),
    ))

    # 4 Docker
    sections.append((
        "docker",
        "4. Déploiement Docker",
        "".join([
            p("Commande : `docker compose up --build`"),
            ul([
                "`scraper_gateway` :8080",
                "`scraper_worker_tiktok` / `scraper_worker_facebook`",
                "`scraper_rabbitmq` :5672 + UI :15672",
                "`scraper_mongodb` :27017",
            ]),
            table(
                ["Volume host", "Rôle"],
                [
                    ["tiktok_sessions/", "Profils Chrome sticky persistants"],
                    ["webshare_residential_proxies.txt", "Pool ~20k IPs"],
                    ["proxy_blacklist.json", "Cooldownoldown IP"],
                    ["video_reports/", "Artefacts Gemini / PDF"],
                ],
            ),
            p("**Piège** : sans volume `tiktok_sessions`, chaque recreate perd les profils chauffés."),
        ]),
    ))

    # 5 Flows
    seq_single = r"""
User → Gateway: POST /scrape
Gateway → Mongo: job QUEUED
Gateway → Rabbit: task tiktok
Gateway → User: scrape_id

Rabbit → Worker: consume
Worker → TikTok: profile via sticky proxy
loop chaque vidéo
  Worker → Rabbit: RESULT (text + metrics)
  Rabbit → Gateway → Mongo: upsert
User → Gateway: GET results (déjà visibles)

loop enrichissement
  Worker → Gateway: cache video_report?
  alt hit: reuse
  else: Gemini(description)
  Worker → Rabbit: ENRICHMENT
  Gateway → Mongo: merge video_report

Worker → Rabbit: COMPLETED
Gateway → Mongo: job SUCCESS
"""
    csv_flow = r"""
CSV URLs
   │
   ├─ Lane 1 (IP A) ── page ── page ── page
   ├─ Lane 2 (IP B) ── page ── page
   └─ Lane N (IP N) ── page ── …
         │
         ├─ IP morte → blacklist + replacement proxy
         └─ posts streamés comme profil unique
   │
   └─ fin → rapport Gemini HTML/PDF → COMPLETED
"""
    sections.append((
        "flows",
        "5. Flux métier : profil unique vs CSV",
        "".join([
            h3("5.1 Profil unique"),
            pre("Séquence", seq_single),
            h3("5.2 CSV batch"),
            pre("Lanes parallèles", csv_flow),
            table(
                ["", "Profil unique", "CSV"],
                [
                    ["Proxies", "Sample + rotation séquentielle", "1 IP dédiée / lane"],
                    ["Parallelisme", "1 scrape (+ enrich async)", "N lanes"],
                    ["Rapport", "API + reports/post", "+ rapport agrégé batch"],
                ],
            ),
        ]),
    ))

    # 6 Rabbit
    sections.append((
        "rabbit",
        "6. Messaging RabbitMQ",
        "".join([
            ul([
                "Exchange : `scrape.exchange`",
                "Queues jobs : `scraping_queue_tiktok`, `scraping_queue_facebook`",
                "Résultats : `scrape_result_queue`",
                "DLQ : `scraping_queue_dlq`",
            ]),
            p("Le gateway publie selon `platform`. Les workers publient RESULT / ENRICHMENT / COMPLETED / ERROR."),
        ]),
    ))

    # 7 Streaming
    sections.append((
        "streaming",
        "7. Streaming & Gemini",
        "".join([
            ol([
                "Dès qu’une vidéo est trouvée → publish métadonnées + metrics (si dispo)",
                "Après scrape → pour chaque post : cache DB `video_report` ou appel Gemini sur la **description texte**",
                "Publish enrichment update → le client voit le report apparaître",
            ]),
            p("Gemini est surtout text-only ici → `confidence.level = low/medium` est **normal** sans audio/visuel."),
        ]),
    ))

    # 8 Mongo
    mongo_ascii = r"""
Incoming result
    │
    ├─ pas de doc (platform,postId) → INSERT
    └─ doc existe → UPDATE même _id
           │
           ├─ metrics présentes ET scrapedAt < 12h → garder metrics
           ├─ sinon (ou metrics null) → mergeMetrics(incoming)
           └─ videoReport incoming ? merge : garder existant
"""
    sections.append((
        "mongo",
        "8. MongoDB — modèle & cache",
        "".join([
            p("**ScrapeResult** : 1 document par `(platform, postId)`. Champs clés : author, textContent, hashtags, metrics, videoReport, publishedAt, scrapedAt, scrapeId."),
            pre("Règles upsert", mongo_ascii),
            p("Avant Gemini, le worker appelle l’API interne gateway pour récupérer les reports déjà connus et **sauter** l’analyse IA."),
        ]),
    ))

    # 9 Proxies
    select_ascii = r"""
pool 20k
  → drop blacklist
  → prioriser STICKY_POOL_SIZE (compléter si trop peu)
  → drop .in_use
  → prefer warmed > cold
  → shuffle + take PROXY_MAX_PER_JOB
  → pour chaque IP:
       soft-block/auth → blacklist courte + next IP (pas de retry inutile)
       tunnel/timeout → retry flaky puis blacklist longue
  → toutes échouent → FAILED count=0
"""
    sections.append((
        "proxies",
        "9. Pool proxies, sticky, blacklist, 0 posts",
        "".join([
            h3("Modèle mental sticky"),
            pre("1 sticky = 1 IP = 1 Chrome", "sdwopfmy-42 → IP Webshare sticky → tiktok_sessions/sdwopfmy-42/"),
            h3("Sélection d’IP"),
            pre("Algorithme", select_ascii),
            h3("Blacklist"),
            table(
                ["Situation", "Blacklist ?", "Durée défaut"],
                [
                    ["no_posts_found", "Oui", "2h (soft)"],
                    ["challenge/captcha", "Oui", "2h (soft)"],
                    ["403 / HTTP failure", "Oui", "24h"],
                    ["timeout / tunnel", "Oui", "24h"],
                    ["INVALID_AUTH_CREDENTIALS", "Oui", "6h"],
                    ["SingletonLock / profile in use", "Non", "rotation soft"],
                    ["scrape réussi", "Non", "—"],
                    ["pause normale entre pages", "Non", "—"],
                ],
            ),
            p("Fichier : `platform/tiktok scraper/proxy_blacklist.json`"),
            p("**0 posts** : toutes les IP du sample ont échoué → `status=FAILED`, message friendly, `count=0`."),
        ]),
    ))

    # 10 Pipeline
    sections.append((
        "pipeline",
        "10. Pipeline scrape TikTok",
        "".join([
            ol([
                "`launch_persistent_context` sur profil sticky",
                "Warmup homepage (+ auto-warm si session froide)",
                "Open profil + wait ancres `/video/`",
                "Extract DOM + SIGI/universal data + réseau (hors For You)",
                "Scroll jusqu’à max_posts / empty scrolls",
                "Enrich : ouvrir pages `/video/` pour stats / statsV2 / DOM counts",
                "Return posts → worker stream + Gemini",
            ]),
            p("Fail-fast : si aucune ancre `/video/` après wait → soft-block → rotate (pas 2×25s inutiles)."),
        ]),
    ))

    # 11 Reports
    sections.append((
        "reports",
        "11. Rapports métier",
        ul([
            "Par vidéo : `video_report` Gemini (thèmes, résumé, sentiment, keywords, confidence…)",
            "Batch CSV : rapport agrégé Mauritanie 24h HTML + PDF (+ fallback si timeout Gemini)",
        ]),
    ))

    # 12 Logs
    sections.append((
        "logs",
        "12. Système de logs",
        "".join([
            p("Module `logging_setup.py` → JSON structuré : timestamp, level, logger, message, platform, service, scrape_id, url, post_id."),
            ul([
                "Soft proxy fail → WARNING (sans stack Playwright)",
                "Bug inattendu → ERROR + traceback",
                "Corréler via scrape_id",
            ]),
        ]),
    ))

    # 13 Incidents
    sections.append((
        "incidents",
        "13. Problèmes rencontrés & solutions",
        table(
            ["Symptôme", "Cause", "Solution"],
            [
                ["Chrome profile in use", "SingletonLock / concurrence", "clear locks + .in_use + launch dans try"],
                ["Profil OK, 0 vidéos", "Soft-block grille squelette", "wait /video/, parse HTML, sticky, fail-fast"],
                ["Likes null API alors logs OK", "TTL 12h bloquait metrics null", "si metrics missing → merge incoming"],
                ["Enrichissement partiel", "Tunnel pages vidéo", "retries + statsV2"],
                ["Soft-block massif", "cookie ≠ IP", "1 sticky = 1 Chrome profile"],
                ["Build/perte sessions", "sockets / pas de volume", "dockerignore + volume sessions"],
                ["Logs bruyants", "stack Playwright", "soft errors WARNING"],
                ["Pool brûlé trop vite", "retries longs soft-block", "fail-fast + soft blacklist 2h"],
                ["Gemini redondant", "pas de cache", "API interne reports + reuse"],
            ],
        ),
    ))

    # 14 Code map
    sections.append((
        "code",
        "14. Carte du code",
        pre(
            "Arborescence",
            """Facebook_Scraper/
├── docker-compose.yml
├── .env
├── logging_setup.py
├── docs/
│   ├── HANDOFF_DEVELOPERS.md
│   └── generate_handoff_report.py   ← ce générateur
├── scraper/   # Gateway Spring Boot
│   └── .../messaging/listener/ScrapeResultListener.java
└── platform/
    ├── tiktok scraper/
    │   ├── worker.py
    │   ├── scraper.py
    │   ├── sticky_sessions.py
    │   ├── video_analysis.py
    │   ├── webshare_residential_proxies.txt
    │   ├── proxy_blacklist.json
    │   └── tiktok_sessions/
    └── facebook scraper/""",
        ),
    ))

    # 15 Debug
    sections.append((
        "debug",
        "15. Guide debug",
        ol([
            "`docker compose ps` — services healthy",
            "`docker compose logs -f worker-tiktok`",
            "Chercher : Selected proxy sample / no_posts_found / enrichment ok likes=",
            "Vérifier proxy_blacklist.json",
            "Vérifier volume tiktok_sessions",
            "Metrics null → logs enrich + TTL gateway",
            "RabbitMQ UI :15672",
        ]),
    ))

    # 16 Traps
    sections.append((
        "traps",
        "16. Pièges à ne pas casser",
        ol([
            "Ne pas réinjecter cookies globaux sur IP aléatoires en mode sticky",
            "Ne pas lancer 2 Chrome sur le même tiktok_sessions/<id>/",
            "Ne pas traiter no_posts_found comme succès vide sans rotation",
            "Ne pas relancer Gemini si video_report existe",
            "Ne pas oublier le volume Docker des sessions",
            "Ne pas logger les passwords proxies",
            "Ne pas retirer l’exception TTL « metrics null »",
        ]),
    ))

    # 17 Roadmap
    sections.append((
        "roadmap",
        "17. Évolutions prioritaires",
        ol([
            "Healthcheck proxy avant d’ouvrir Chrome",
            "Dashboard succès / blacklist / latence",
            "Mode metrics-only refresh sans Gemini",
            "Historique optionnel des metrics",
            "Téléchargement média seulement si confidence basse",
            "Tests intégration mock TikTok + TTL gateway",
            "Alerte quota Webshare (INVALID_AUTH en rafale)",
        ]),
    ))

    # 18 First day
    sections.append((
        "onboarding",
        "18. 10 tâches premier jour",
        ol([
            "Lire ce rapport + docker-compose + .env",
            "Monter la stack, scraper un profil via Postman",
            "Suivre un scrape_id dans les logs",
            "Voir l’upsert Mongo (platform, postId)",
            "Relancer <12h et observer reuse video_report",
            "Observer rotation sur soft-block",
            "Lire sticky_sessions.py + launch_persistent_context",
            "Lire ScrapeResultListener.upsertResult",
            "Lancer un petit CSV batch",
            "Proposer une PR mineure (log / test / doc)",
        ]),
    ))

    return sections


def h3(text: str) -> str:
    return f"<h3>{esc(text)}</h3>"


def build_html(sections: list[tuple[str, str, str]]) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    toc = "".join(
        f"<li><a href='#{esc(a)}'>{esc(t)}</a></li>" for a, t, _ in sections
    )
    body = "".join(section(a, t, html_body) for a, t, html_body in sections)

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Handoff Développeurs — Facebook / TikTok Scraper</title>
  <style>
    :root {{
      --bg: #f6f3ee;
      --surface: #fffdf9;
      --ink: #1c2430;
      --muted: #5b6573;
      --line: #d9d2c5;
      --accent: #0f6b5c;
      --accent-soft: #e4f3ef;
      --warn: #8a4b12;
      --warn-soft: #f8ead8;
      --code-bg: #eef2f4;
      --shadow: 0 10px 30px rgba(28, 36, 48, 0.06);
      --radius: 14px;
      --max: 980px;
      --font: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      --mono: "Cascadia Code", "Consolas", "Courier New", monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: var(--font);
      color: var(--ink);
      background:
        radial-gradient(1200px 500px at 10% -10%, #dff3ee 0%, transparent 55%),
        radial-gradient(900px 400px at 100% 0%, #f3e7d5 0%, transparent 50%),
        var(--bg);
      line-height: 1.55;
    }}
    .wrap {{ max-width: var(--max); margin: 0 auto; padding: 32px 20px 80px; }}
    header.hero {{
      background: linear-gradient(135deg, #123d36 0%, #0f6b5c 55%, #1f8f7a 100%);
      color: #f7fffc;
      border-radius: 22px;
      padding: 36px 32px;
      box-shadow: var(--shadow);
      margin-bottom: 28px;
    }}
    header.hero .eyebrow {{
      letter-spacing: .12em;
      text-transform: uppercase;
      font-size: 12px;
      opacity: .85;
      margin: 0 0 10px;
    }}
    header.hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(1.7rem, 3vw, 2.4rem);
      line-height: 1.15;
      font-weight: 700;
    }}
    header.hero p {{ margin: 0; max-width: 62ch; opacity: .95; }}
    header.hero .meta {{
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .pill {{
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,.14);
      border: 1px solid rgba(255,255,255,.2);
      font-size: 12px;
    }}
    nav.toc {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 20px 22px;
      margin-bottom: 28px;
      box-shadow: var(--shadow);
    }}
    nav.toc h2 {{ margin: 0 0 12px; font-size: 1.05rem; }}
    nav.toc ol {{ margin: 0; padding-left: 1.2rem; columns: 1; }}
    @media (min-width: 720px) {{
      nav.toc ol {{ columns: 2; column-gap: 28px; }}
    }}
    nav.toc a {{ color: var(--accent); text-decoration: none; }}
    nav.toc a:hover {{ text-decoration: underline; }}
    section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 26px 28px;
      margin-bottom: 18px;
      box-shadow: var(--shadow);
    }}
    section h2 {{
      margin: 0 0 14px;
      padding-bottom: 10px;
      border-bottom: 2px solid var(--accent-soft);
      font-size: 1.35rem;
    }}
    h3 {{ margin: 18px 0 8px; font-size: 1.05rem; color: #244038; }}
    p {{ margin: 0 0 12px; color: var(--ink); }}
    ul, ol {{ margin: 0 0 12px; padding-left: 1.25rem; }}
    li {{ margin: 4px 0; }}
    code {{
      font-family: var(--mono);
      font-size: .92em;
      background: var(--code-bg);
      padding: 1px 6px;
      border-radius: 6px;
    }}
    .table-wrap {{ overflow-x: auto; margin: 12px 0 4px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: .95rem;
      min-width: 560px;
    }}
    th, td {{
      border: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: var(--accent-soft);
      color: #163f37;
      font-weight: 650;
    }}
    tr:nth-child(even) td {{ background: #fcfaf6; }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px 16px 8px;
      margin: 14px 0;
      background: #fffcf7;
    }}
    .card h3 {{
      margin: 0 0 10px;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .badge {{
      font-size: 11px;
      letter-spacing: .06em;
      text-transform: uppercase;
      background: var(--warn-soft);
      color: var(--warn);
      border: 1px solid #ebc894;
      border-radius: 999px;
      padding: 3px 8px;
      font-weight: 700;
    }}
    .adr-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }}
    @media (min-width: 800px) {{
      .adr-grid {{ grid-template-columns: 1fr 1fr; }}
      .adr-grid .full {{ grid-column: 1 / -1; }}
    }}
    .adr-grid > div {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
    }}
    .adr-grid h4 {{
      margin: 0 0 6px;
      font-size: 12px;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    figure.diagram {{
      margin: 14px 0;
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      background: #111827;
    }}
    figure.diagram figcaption {{
      background: #1f2937;
      color: #d1d5db;
      padding: 8px 12px;
      font-size: 12px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    figure.diagram pre {{
      margin: 0;
      padding: 14px 16px 18px;
      overflow-x: auto;
      color: #e5f6f1;
      font-family: var(--mono);
      font-size: 12.5px;
      line-height: 1.45;
      white-space: pre;
    }}
    footer {{
      margin-top: 28px;
      color: var(--muted);
      font-size: .92rem;
      text-align: center;
    }}
    @media print {{
      body {{ background: #fff; }}
      header.hero, section, nav.toc {{
        box-shadow: none;
        break-inside: avoid;
      }}
      nav.toc {{ break-after: page; }}
      a {{ color: inherit; text-decoration: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <p class="eyebrow">Documentation technique · Handoff</p>
      <h1>Facebook / TikTok Scraper</h1>
      <p>
        Rapport explicatif pour les développeurs qui feront évoluer le projet :
        architecture, flux, choix techniques, incidents résolus, proxies sticky,
        streaming Gemini, et règles Mongo.
      </p>
      <div class="meta">
        <span class="pill">Généré {esc(now)}</span>
        <span class="pill">Docker Compose</span>
        <span class="pill">Gateway · RabbitMQ · MongoDB · Workers</span>
      </div>
    </header>

    <nav class="toc">
      <h2>Sommaire</h2>
      <ol>{toc}</ol>
    </nav>

    {body}

    <footer>
      Généré par <code>docs/generate_handoff_report.py</code> —
      source aussi disponible dans <code>docs/HANDOFF_DEVELOPERS.md</code>.
      Pour imprimer en PDF : ouvrir le HTML → Ctrl+P → Enregistrer en PDF.
    </footer>
  </div>
</body>
</html>
"""


def build_pdf(path: Path, sections: list[tuple[str, str, str]]) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
        title="Handoff Développeurs — Facebook / TikTok Scraper",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleFR",
        parent=styles["Title"],
        fontSize=18,
        textColor=colors.HexColor("#123d36"),
        spaceAfter=8,
    )
    h_style = ParagraphStyle(
        "HFR",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=colors.HexColor("#0f6b5c"),
        spaceBefore=12,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "BodyFR",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=13,
        alignment=TA_LEFT,
        spaceAfter=4,
    )

    story = [
        Paragraph("Handoff Développeurs — Facebook / TikTok Scraper", title_style),
        Paragraph(
            "Version PDF condensée. Préférer le HTML pour diagrammes et mise en page complète.",
            body_style,
        ),
        Spacer(1, 8),
    ]

    for _, title, html_body in sections:
        story.append(Paragraph(esc(title), h_style))
        # Strip tags roughly for PDF text
        text = re.sub(r"<br\s*/?>", "\n", html_body)
        text = re.sub(r"</p>|</li>|</h3>|</h4>|</tr>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        for block in text.split("\n"):
            line = " ".join(block.split())
            if not line:
                continue
            if len(line) > 1200:
                line = line[:1200] + "…"
            story.append(Paragraph(esc(line), body_style))

    doc.build(story)


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère le rapport handoff développeurs")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Dossier de sortie")
    parser.add_argument("--pdf", action="store_true", help="Générer aussi un PDF (reportlab)")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    sections = build_sections()

    html_path = args.out / f"handoff_developers_{ts}.html"
    html_path.write_text(build_html(sections), encoding="utf-8")
    print(f"HTML écrit : {html_path}")

    # Copie stable "latest"
    latest = args.out / "handoff_developers_latest.html"
    latest.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"HTML latest : {latest}")

    if args.pdf:
        try:
            pdf_path = args.out / f"handoff_developers_{ts}.pdf"
            build_pdf(pdf_path, sections)
            print(f"PDF écrit  : {pdf_path}")
        except ImportError:
            print("reportlab non installé — PDF ignoré. Installe: pip install reportlab")
            return 1

    print("\nOuvre le HTML dans le navigateur. Pour un PDF joli : Ctrl+P -> Enregistrer en PDF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
