FROM python:3.11-slim AS base

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir . && \
    python -c "import nltk; nltk.download('punkt_tab', quiet=True); nltk.download('stopwords', quiet=True)"

# Create data dir and initialise the database at build time.
RUN mkdir -p /app/data && platysearch init

EXPOSE 8000

ENV PORT=8000
ENV PLATY_DB_PATH=/app/data/platysearch.db

# Use exec form to avoid shell issues.
CMD ["uvicorn", "platysearch.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
