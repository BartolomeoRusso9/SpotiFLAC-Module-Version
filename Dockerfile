FROM python:3.12-slim

WORKDIR /app

# Imposta le variabili d'ambiente di Python e lo schermo virtuale di default per Xvfb
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

# Installa le dipendenze di sistema:
# - ffmpeg e flac: per l'elaborazione audio
# - nodejs: per le estensioni di SpotiFLAC
# - xvfb: per creare il display virtuale (risolve l'errore Errno 2)
# - chromium e fonts-liberation: il browser per Pydoll e i font web
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        flac \
        nodejs \
        xvfb \
        chromium \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./

RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python3 -m pip install --no-cache-dir .

RUN mkdir -p /app/downloads \
             /root/.spotiflac/extensions \
             /root/.cache/spotiflac \
             /root/.spotiflac/signed_sessions

VOLUME ["/app/downloads", "/root/.spotiflac", "/root/.cache/spotiflac"]

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]