FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Media inspection/rendering, PDF utilities, CJK fonts, and WeasyPrint runtime libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fontconfig \
        fonts-noto-cjk \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz-subset0 \
        libjpeg62-turbo \
        libopenjp2-7 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        poppler-utils \
        shared-mime-info \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install deno JS runtime for yt-dlp YouTube extraction
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /tmp \
    && mv /tmp/deno /usr/local/bin/deno \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip

 WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && python -m pip install --upgrade "yt-dlp[default]"

COPY index.html ./index.html

RUN groupadd --gid 10001 framepage \
    && useradd --uid 10001 --gid framepage --create-home --shell /usr/sbin/nologin framepage \
    && mkdir -p /data \
    && chown -R framepage:framepage /data

USER framepage

ENV FRAMEPAGE_DATA_DIR=/data
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
