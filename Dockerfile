FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TS_PROFILE_DIR=/tmp/ts_profile \
    DISPLAY=:99

ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        flac \
        nodejs \
        wget \
        gnupg \
        xvfb \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome / Chromium fallback
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
        && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
           > /etc/apt/sources.list.d/google-chrome.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends google-chrome-stable \
        && rm -rf /var/lib/apt/lists/*; \
    else \
        apt-get update \
        && apt-get install -y --no-install-recommends chromium \
        && rm -rf /var/lib/apt/lists/* \
        && ln -sf /usr/bin/chromium /usr/bin/google-chrome-stable; \
    fi

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