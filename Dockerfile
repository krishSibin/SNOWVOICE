# The web pages: a React app built with Node, then copied into the package.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY --from=frontend /src/sn_agent/web/ui ./src/sn_agent/web/ui
RUN pip install --no-cache-dir .

COPY eval ./eval
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1
CMD ["sn-agent", "serve"]
