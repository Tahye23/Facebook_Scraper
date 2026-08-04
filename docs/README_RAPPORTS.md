# Rapports du projet

Deux types de rapports, **isolés du scraping** :

## 1) Rapport métier Mauritanie 24h (après CSV)

- Déclenché uniquement à la fin d’un job `POST /scrape/csv-report-24h`
- Fichiers écrits dans `platform/tiktok scraper/video_reports/` :
  - `mauritanie_24h_<scrapeId>_*.html` (design teal Cairo)
  - `mauritanie_24h_<scrapeId>_*.pdf` (ReportLab, pas Playwright)
  - `mauritanie_24h_<scrapeId>.docx` (optionnel, lazy-import)
- Une erreur de rapport est **non fatale** : le scraping et le `COMPLETED` continuent

## 2) Rapport technique handoff développeurs

- Script autonome, **jamais importé par les workers** :
  ```bash
  python docs/generate_handoff_report.py
  python docs/generate_handoff_report.py --pdf
  ```
- Sorties dans `docs/output/`

Règle : aucun module de rapport ne doit être importé au top-level de `worker.py` / `scraper.py`.
