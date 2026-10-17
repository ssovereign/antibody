# ANTIBODY server (and, by default, the web app) in one container.
#   docker build -t antibody .
#   docker run -p 8765:8765 -v antibody-data:/data -e ANTHROPIC_API_KEY=... antibody
# All state lives on the /data volume. Hosted mode signs people in with access tokens
# (`docker exec <id> python -m server.cli invite --project demo_stage --name alice`).

FROM node:20-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
ARG VITE_API_BASE=""
RUN VITE_API_BASE=$VITE_API_BASE npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    ANTIBODY_DATA_DIR=/data \
    API_BIND_HOST=0.0.0.0 \
    API_PORT=8765 \
    PROJECT_AUTH_MODE=token
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server/ server/
COPY scripts/ scripts/
COPY fixtures/ fixtures/
COPY --from=web /web/dist apps/web/dist
VOLUME /data
EXPOSE 8765
# setup is idempotent (migrations, suites, server-side tokens); PORT is honored for hosts like Render/Railway
# (the demo project is created once; restarts keep all data)
CMD ["sh", "-c", "python -m server.cli setup && (python -m server.cli demo-env --name stage --dir /data/demo-folders/stage >/dev/null 2>&1 || true) && API_PORT=${PORT:-8765} python -m server.cli serve"]
