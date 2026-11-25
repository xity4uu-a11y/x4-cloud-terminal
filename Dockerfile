FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8501 \
    X4_DATA_DIR=/app/data

WORKDIR /app

RUN useradd --create-home --shell /bin/bash x4user \
    && mkdir -p /app/data \
    && chown -R x4user:x4user /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY policies ./policies
COPY x4_cloud ./x4_cloud
COPY run.py .

RUN chown -R x4user:x4user /app
USER x4user

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8501\")}/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "python run.py --host 0.0.0.0 --port ${PORT:-8501}"]
