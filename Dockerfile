FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CBV_DATA_DIR=/var/data \
    CBV_BUNDLES_DIR=/var/data/bundles

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets
COPY static ./static
COPY templates ./templates

EXPOSE 8000

CMD ["sh", "-c", "if [ -z \"$CBV_USER\" ] || [ -z \"$CBV_PASS\" ] || [ -z \"$CBV_SESSION_SECRET\" ]; then echo 'CBV_USER, CBV_PASS, and CBV_SESSION_SECRET must be configured.' >&2; exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
