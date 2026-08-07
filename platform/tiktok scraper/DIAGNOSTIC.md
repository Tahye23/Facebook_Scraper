# Diagnostic tooling (`itemListLen=0`)

Scripts **standalone** — ne passent **pas** par RabbitMQ / `worker.py`.
Les metriques prod ne sont pas polluees (`TIKTOK_DIAG_MODE=true`).

## Prerequisites

- `.env` a la racine avec credentials Webshare
- Chrome/Chromium disponible (meme stack que le worker)
- Pour `logged_in` : un cookie jar JSON Playwright (liste d'objets cookies)

```bash
# Fichier attendu (gitignored) :
platform/tiktok scraper/tiktok_session_cookies.json
# ou override :
export TIKTOK_DIAG_SESSION_COOKIES_FILE=/absolute/path/session.json
```

## Lancer un test

Depuis `platform/tiktok scraper/` :

```bash
# Baseline (config prod actuelle)
python diagnostic_runner.py --test-mode baseline --target-profile bellewarmedia --country us

# Sans blocage image/media/font
python diagnostic_runner.py --test-mode assets_off --target-profile bellewarmedia --country fr

# Session TikTok connectee (cookies manuels)
python diagnostic_runner.py --test-mode logged_in --target-profile bellewarmedia --country us

# Mobile coherent (m.tiktok.com + UA + viewport + sec-ch-ua)
python diagnostic_runner.py --test-mode mobile_ua --target-profile bellewarmedia --country us
```

Chaque run **append** une ligne dans `diagnostic_results.jsonl`.

## Comparer

```bash
python diagnostic_compare.py --profile bellewarmedia
```

## Flags utiles

| Env | Role |
|---|---|
| `TIKTOK_BLOCK_HEAVY_ASSETS` | `true`/`false` — toggle sans rebuild |
| `TIKTOK_DIAG_SESSION_COOKIES_FILE` | cookie jar logged_in |
| `TIKTOK_ALLOW_MOBILE_HOST` | autorise `m.tiktok.com` |
| `TIKTOK_QUARANTINE_COOLDOWN_HOURS` | cooldown soft-block (defaut 2) |

## Interpretation rapide

| Signal | Lecture |
|---|---|
| `baseline` itemListLen=0, `logged_in` >0 | mur anonymat / besoin session |
| `assets_off` debloque, `baseline` non | le blocker assets gene l'hydration |
| tous modes =0 sur 4 pays | soft-block plateforme probable |
