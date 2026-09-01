FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PORT=8080
EXPOSE 8080

RUN useradd --create-home --uid 1001 sprtz && chown -R sprtz:sprtz /app
USER sprtz

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2
