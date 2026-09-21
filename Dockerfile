FROM python:3.12-slim

WORKDIR /app

# libc6-dev нужен здесь, а не только «на всякий случай»: без заголовков и
# библиотек libc компоновщик gcc не соберёт ни одно расширение, которое pip
# решит собрать из исходников, и сборка падает. Railway строит ИМЕННО этот файл
# (railway.json → dockerfilePath: "Dockerfile"), и фикс от Railway-бота
# (PR #7) в своё время попал только в tg-manager/Dockerfile — то есть в файл,
# которым деплой не собирается.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libpq-dev curl procps \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Primary service: tg-manager customer bot (unchanged).
# ---------------------------------------------------------------------------
COPY tg-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg-manager/ .

# ---------------------------------------------------------------------------
# Additive: personal assistant bot — fully isolated under /app/pablo with its
# own virtualenv so it can't touch tg-manager's dependencies. It only needs
# httpx + python-dotenv (talks to OpenRouter over plain HTTP).
# ---------------------------------------------------------------------------
COPY assistant/requirements.txt /app/pablo/requirements.txt
RUN python -m venv /app/pablo/.venv \
    && /app/pablo/.venv/bin/pip install --no-cache-dir -r /app/pablo/requirements.txt
COPY assistant/ /app/pablo/assistant/

# Run tg-manager (foreground) + assistant (background, self-restarting).
COPY start-all.sh /app/start-all.sh
RUN chmod +x /app/start-all.sh

CMD ["bash", "/app/start-all.sh"]
