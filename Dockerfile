FROM python:3.12-slim

WORKDIR /app

# Set Python environment variables and default virtual screen for Xvfb
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

# Install system dependencies:
# - ffmpeg and flac: for audio processing
# - nodejs: for SpotiFLAC extensions
# - xvfb: to create the virtual display (MANDATORY for Chromium even without VNC)
# - chromium and fonts-liberation: browser for Pydoll and web fonts

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        flac \
        nodejs \
        xvfb \
        fluxbox \
        x11vnc \
        novnc \
        websockify \
        chromium \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./

RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python3 -m pip install --no-cache-dir .

# Runs as a normal user, not root. This image installs and then executes
# third-party extensions (Node and Python) fetched from whatever registry the
# operator configured, and — in --web mode — does so behind a server that is
# unauthenticated unless a token is set. Root inside the container is a much
# larger blast radius than that combination deserves, and it is also what
# makes bind-mounted downloads come out owned by root on the host.
#
# UID 1000 matches the first non-system user on most Linux hosts, so mounted
# volumes line up without a chown. Override at build time if yours differs:
#   docker build --build-arg APP_UID=1234 --build-arg APP_GID=1234 .
ARG APP_UID=1000
ARG APP_GID=1000
ENV HOME=/home/spotiflac

RUN groupadd --gid "${APP_GID}" spotiflac \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
        --create-home --home-dir "${HOME}" spotiflac \
    && mkdir -p /app/downloads \
                "${HOME}/.spotiflac/extensions" \
                "${HOME}/.spotiflac/signed_sessions" \
                "${HOME}/.cache/spotiflac" \
    && chown -R spotiflac:spotiflac /app "${HOME}"

VOLUME ["/app/downloads", "/home/spotiflac/.spotiflac", "/home/spotiflac/.cache/spotiflac"]

# ==============================================================================
# [VNC/WEB SCREEN] — desktop GUI over VNC (default `spotiflac --gui` path):
# - 6080: Web Browser access (noVNC) -> http://localhost:6080/vnc.html
# - 5900: Classic VNC client access (e.g., RealVNC, TigerVNC)
#
# [WEB MODE] — `spotiflac --web` (see docker-entrypoint.sh): no VNC needed,
# uses a lightweight local web server instead.
# - 8000: web GUI -> http://localhost:8000
# ==============================================================================
EXPOSE 6080 5900 8000

# No blanket HEALTHCHECK here on purpose: this same image runs three very
# different things depending on the CMD it's given — a one-shot CLI download
# that's *supposed* to exit, a VNC-backed --gui session, and a long-running
# --web server — and a check written for one of those would be meaningless
# (or actively misleading) for the other two. docker-compose.example.yml
# commits to --web specifically and defines a real HTTP healthcheck there.

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Last, and after the chmod above: /usr/local/bin is root-owned, so dropping
# privileges any earlier makes that RUN fail and the image fail to build.
USER spotiflac

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]