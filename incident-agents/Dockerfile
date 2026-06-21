FROM python:3.12-slim

# Run as non-root for AKS.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

USER appuser

# Health/probe port (overridable via HEALTH_PORT).
EXPOSE 8080

# Endpoints are injected via env vars (PROMETHEUS_URL, LOKI_URL, TEMPO_URL, ...).
ENTRYPOINT ["obs-agents"]
