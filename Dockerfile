FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl procps \
    && rm -rf /var/lib/apt/lists/*

COPY tg-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg-manager/ .

CMD ["python", "main.py"]
