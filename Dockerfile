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
#
# ==============================================================================
# [OPTIONAL - VNC/WEB SCREEN]:
# To enable viewing the container screen in your browser or via a VNC client,
# uncomment the 4 packages below before building:
# ==============================================================================
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

RUN mkdir -p /app/downloads \
             /root/.spotiflac/extensions \
             /root/.cache/spotiflac \
             /root/.spotiflac/signed_sessions

VOLUME ["/app/downloads", "/root/.spotiflac", "/root/.cache/spotiflac"]

# ==============================================================================
# [OPTIONAL - VNC/WEB SCREEN]:
# Expose ports only if you want to view the container screen:
# - 6080: Web Browser access (noVNC) -> http://localhost:6080/vnc.html
# - 5900: Classic VNC client access (e.g., RealVNC, TigerVNC)
# ==============================================================================

EXPOSE 6080 5900

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]