FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TS_PROFILE_DIR=/tmp/ts_profile

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        flac \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./

RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python3 -m pip install --no-cache-dir .

RUN mkdir -p /app/downloads \
             /root/.spotiflac/extensions \
             /root/.cache/spotiflac \
             /tmp/ts_profile

VOLUME ["/app/downloads", "/root/.spotiflac", "/root/.cache/spotiflac", "/tmp/ts_profile"]

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]