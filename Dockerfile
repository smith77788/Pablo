FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY tg-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg-manager/ .

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "python.*main.py" || exit 1

CMD ["python", "main.py"]
