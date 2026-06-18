FROM python:3.12-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    xvfb xauth \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install chromium --with-deps \
    && chmod -R 755 /ms-playwright

COPY . .

RUN useradd -m workeruser && chown -R workeruser /app /ms-playwright
USER workeruser

CMD ["python", "worker.py"]
