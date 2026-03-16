FROM python:3.12-slim

# Install OS deps needed by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 fonts-liberation wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only re-runs when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright \
    && playwright install chromium

# Copy project files (changes here don't invalidate the pip install layer)
COPY . .

# Persistent data lives in /data (mount a volume here)
RUN mkdir -p /data
ENV PMON_DATA_DIR=/data

# Health check so Docker/Watchtower knows if the app is alive
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8888/api/health || exit 1

EXPOSE 8888

# Use exec form + unbuffered output for proper signal handling and live logs
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "pmon.cli", "run", "--host", "0.0.0.0"]
