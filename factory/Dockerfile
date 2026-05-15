FROM python:3.11-slim

WORKDIR /app

# System deps for SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Create required directories and non-root user
RUN mkdir -p /app/data /app/logs && \
    addgroup --gid 1001 factory && \
    adduser --uid 1001 --gid 1001 --disabled-password --gecos "" factory && \
    chown -R factory:factory /app

USER factory

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import os; exit(0 if os.path.isdir('/app') else 1)"

CMD ["python", "factory_main.py"]
