FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-nld \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8901
ENV GROCERIES_DATA_DIR=/app/data
VOLUME ["/app/data"]

CMD ["uv", "run", "--no-dev", "groceries-assistant", "serve", "--host", "0.0.0.0", "--port", "8901"]
