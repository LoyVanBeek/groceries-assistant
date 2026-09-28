FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-nld \
      xvfb chromium chromium-driver \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8901
ENV GROCERIES_DATA_DIR=/app/data
# playwright drives the system chromium, headed under xvfb (Akamai bypass)
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
ENV DISPLAY=:99
VOLUME ["/app/data"]

# xvfb (virtual display) runs alongside the app; the app talks to it via DISPLAY.
# The sh & trick dies with the parent: keep Xvfb alive with a supervising loop.
CMD ["/bin/sh", "-c", "Xvfb :99 -screen 0 1280x800x24 & xvfb_pid=$!; trap 'kill $xvfb_pid' EXIT; uv run --no-dev groceries-assistant serve --host 0.0.0.0 --port 8901"]
