FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV APPDATA=/cache GAME_DIR=/game TZ=Asia/Singapore
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-tk ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv --system-site-packages /opt/venv
RUN /opt/venv/bin/pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.30,<1" "pillow>=10,<13"
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY extractor /app/extractor
COPY radio /app/radio
COPY web /app/web
RUN mkdir -p /cache /game
EXPOSE 8088
CMD ["uvicorn","web.app:app","--host","0.0.0.0","--port","8088"]
