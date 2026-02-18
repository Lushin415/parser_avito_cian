FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/Duff89/parser_avito

# Зависимости для Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
	libatk-bridge2.0-0t64 \
	libatk1.0-0t64 \
	libatspi2.0-0t64 \
	libcairo2 \
	libdbus-1-3 \
	libdrm2 \
	libgbm1 \
	libglib2.0-0t64 \
	libnspr4 \
	libnss3 \
	libpango-1.0-0 \
	libxcomposite1 \
	libxdamage1 \
	libxfixes3 \
	libxrandr2 \
	libxkbcommon0 \
	libasound2t64 \
	curl \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
	&& pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install chromium-headless-shell

COPY . .

# Директории для данных
RUN mkdir -p /app/logs /app/result

EXPOSE 8009

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
	CMD curl -sf http://localhost:8009/health || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8009"]
