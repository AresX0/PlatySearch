FROM python:3.11-slim AS base

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir . && \
    python -c "import nltk; nltk.download('punkt_tab', quiet=True); nltk.download('stopwords', quiet=True)"

# Seed DB is NOT included in code-only builds.
# The persistent /home/data/ DB on Azure is reused across deploys.
# To include a seed for first-ever deploy, temporarily remove data/ from .dockerignore.

EXPOSE 8000

ENV PORT=8000
# DB path — matches pydantic default so the app works even without env-var.
ENV PLATY_DB_PATH=/app/data/platysearch.db

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
