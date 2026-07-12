FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl procps \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Primary service: tg-manager customer bot (unchanged).
# ---------------------------------------------------------------------------
COPY tg-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg-manager/ .

# ---------------------------------------------------------------------------
# Additive: personal assistant bot (Pablo) — fully isolated under /app/pablo
# with its own virtualenv so it can't touch tg-manager's dependencies.
# ---------------------------------------------------------------------------
COPY requirements.txt /app/pablo/requirements.txt
RUN python -m venv /app/pablo/.venv \
    && /app/pablo/.venv/bin/pip install --no-cache-dir -r /app/pablo/requirements.txt
COPY orchestrator.py /app/pablo/
COPY assistant/ /app/pablo/assistant/
COPY agents/ /app/pablo/agents/
COPY tools/ /app/pablo/tools/
COPY database/ /app/pablo/database/

# Run tg-manager (foreground) + assistant (background, self-restarting).
COPY start-all.sh /app/start-all.sh
RUN chmod +x /app/start-all.sh

CMD ["bash", "/app/start-all.sh"]
