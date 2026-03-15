FROM python:3.12-slim

# Install OS deps needed by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 fonts-liberation wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy everything needed
COPY pyproject.toml requirements.txt ./
COPY pmon/ pmon/
COPY config/ config/

# Install Python deps + Playwright Chromium
RUN pip install --no-cache-dir . \
    && playwright install chromium

# Persistent data lives on Railway volume at /data
ENV PMON_DATA_DIR=/data

EXPOSE 8888

CMD python -m pmon.cli run --host 0.0.0.0 --port ${PORT:-8888}
