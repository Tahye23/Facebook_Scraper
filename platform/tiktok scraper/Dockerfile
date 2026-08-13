# IMPORTANT: ce Dockerfile est construit avec un contexte de build = LA RACINE
# DU DEPOT (voir docker-compose.yml: build.context = "."), pas le dossier
# "platform/tiktok scraper". Pourquoi: worker.py / scraper.py / video_analysis.py
# importent tous "logging_setup" qui vit a la racine du repo
# (Path(__file__).resolve().parents[2] dans worker.py). Si on ne construisait
# qu'a partir de ce sous-dossier, logging_setup.py serait absent de l'image et
# le conteneur planterait au demarrage avec "ModuleNotFoundError: logging_setup".
FROM python:3.12-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONUNBUFFERED=1
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    xvfb xauth \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    fonts-liberation fonts-noto-color-emoji fonts-dejavu-core fonts-noto-core fonts-noto-extra \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# On copie d'abord uniquement requirements.txt pour profiter du cache Docker
# (tant que les dependances ne changent pas, ce layer n'est jamais reconstruit).
# Syntaxe JSON obligatoire ici: le nom du dossier source contient un espace
# ("tiktok scraper") et la forme "shell" de COPY ne le supporte pas bien.
COPY ["platform/tiktok scraper/requirements.txt", "./requirements.txt"]
RUN pip install --no-cache-dir -r requirements.txt

# On installe chromium + chrome + webkit (TikTokApi EmptyResponseException
# recommande d'essayer browser=webkit). `--with-deps` pour les libs systeme.
RUN python -m playwright install chromium webkit --with-deps \
    && chmod -R 755 /ms-playwright

# logging_setup.py (racine du repo) + le code du scraper TikTok. On renomme le
# dossier de destination sans espace ("tiktok-scraper", tiret) pour eviter tout
# probleme de citation dans les instructions Docker suivantes (WORKDIR, etc.):
# cela ne change rien pour worker.py, qui compte juste 2 niveaux de dossiers
# parents (Path(__file__).resolve().parents[2]) pour retrouver /app.
COPY logging_setup.py .
COPY ["platform/tiktok scraper", "platform/tiktok-scraper"]

WORKDIR /app/platform/tiktok-scraper

RUN useradd -m workeruser \
    && mkdir -p tiktok_profile video_reports tiktok_sessions \
    && chown -R workeruser /app /ms-playwright
USER workeruser

CMD ["python", "worker.py"]
