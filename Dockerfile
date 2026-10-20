FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY eval ./eval
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1
CMD ["sn-agent", "serve"]
