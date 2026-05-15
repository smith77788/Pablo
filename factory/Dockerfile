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

RUN addgroup --gid 1001 factory && adduser --uid 1001 --gid 1001 --disabled-password --gecos "" factory
RUN chown -R factory:factory /app
USER factory

CMD ["python", "factory_main.py"]
