FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for asyncpg + cryptography wheels (most are already manylinux,
# but build-essential covers the edge cases on slim).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

# Non-root for safety
RUN useradd -r -u 1000 freeai && \
    mkdir -p /app/backend/data && \
    chown -R freeai:freeai /app
USER freeai

EXPOSE 8000

# Healthcheck — /api/health is public
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
