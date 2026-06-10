FROM python:3.12-slim-bookworm

# Node + npm for the Dynatrace MCP server (`npx -y @dynatrace-oss/...`).
# Use Debian's own packages — bookworm ships Node 18 LTS, sufficient for MCP.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

ENV APP_PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
