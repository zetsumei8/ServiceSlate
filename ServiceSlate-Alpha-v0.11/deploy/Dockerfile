FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICESLATE_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-client clamav ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY deploy/entrypoint.sh /usr/local/bin/serviceslate-entrypoint
RUN chmod +x /usr/local/bin/serviceslate-entrypoint && pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 serviceslate && mkdir -p /data /var/lib/clamav && chown -R serviceslate:serviceslate /data /app /var/lib/clamav
USER serviceslate

EXPOSE 8080
CMD ["/usr/local/bin/serviceslate-entrypoint"]
