# Brief Claude — Debug TikTokApi / sticky residential (maj)

## Correction importante vs analyse precedente
Dans `proxyproviders` **0.2.2 installé**, la signature réelle est `search_params=` ;
la doc README montre encore `params=` (exemple obsolète) → TypeError si on suit la doc.
**Mais ce point est secondaire** : `proxyproviders.Webshare` appelle `/api/v2/proxy/list/`
(produit Proxy List statique) et **ne fonctionne pas** avec un plan Residential Rotating
(`p.webshare.io` + `base-country-sessionId`).

## Architecture cible (appliquee)
1. **Ne plus utiliser** `proxy_provider=Webshare(...)`.
2. Construire sticky via `TIKTOK_PROXY_*` + `identity_pool` / `_assign_webshare_sticky_session`.
3. Injecter via `browser_context_factory` (defaut) ou `proxies=[sticky]`.
4. `TIKTOK_API_SESSION_TIMEOUT_MS=60000`, hard timeout worker `120s`.
5. Subprocess stderr capturé (`TIKTOK_SCRAPE_SUBPROCESS_LOG_STDERR=true`).

## Fichiers
- `platform/tiktok scraper/tiktok_extractor.py`
- `platform/tiktok scraper/scraper.py` (`_scrape_tiktok_page_via_api`)
- `platform/tiktok scraper/worker.py` (stderr log)
- `docker-compose.yml`

## Test
```bash
docker compose build worker-tiktok && docker compose up -d worker-tiktok
# puis meme job Postman @bellewarmedia
docker logs -f scraper_worker_tiktok
```
Chercher: `sticky_context_factory`, `browser_context_factory ready`, stderr tail.
