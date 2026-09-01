FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is the whole point of this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY media_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY media_server/ ./media_server/

# Scratch holds only small artefacts (playlists in flight, thumbnails,
# rendered clips). Big sources are streamed over HTTPS, never stored.
ENV PORT=8080 \
    SCRATCH_DIR=/tmp/scratch
EXPOSE 8080

RUN useradd --create-home --uid 1001 sprtz \
    && chown -R sprtz:sprtz /app
USER sprtz

CMD ["python", "-m", "media_server.server"]
