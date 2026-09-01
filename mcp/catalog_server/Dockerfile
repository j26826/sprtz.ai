FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY catalog_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY catalog_server/ ./catalog_server/

ENV PORT=8080
EXPOSE 8080

# Non-root: this service holds Firestore write credentials.
RUN useradd --create-home --uid 1001 sprtz && chown -R sprtz:sprtz /app
USER sprtz

CMD ["python", "-m", "catalog_server.server"]
