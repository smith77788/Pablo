FROM python:3.12-slim

WORKDIR /app

# libc6-dev нужен здесь, а не только «на всякий случай»: без заголовков и
# библиотек libc компоновщик gcc не соберёт ни одно расширение, которое pip
# решит собрать из исходников, и сборка падает. Railway строит ИМЕННО этот файл
# (railway.json → dockerfilePath: "Dockerfile"), и фикс от Railway-бота
# (PR #7) в своё время попал только в tg-manager/Dockerfile — то есть в файл,
# которым деплой не собирается.
# Шрифты нужны продукту, а не «для красоты»: services/avatar_factory.py рисует
# аватары и ищет ровно эти каталоги (_FONT_PATHS). В python:3.12-slim их нет, и
# Pillow молча уходит на ImageFont.load_default() — а тот рисует 7 пикселей
# вместо запрошенных 87 и не имеет отдельных кириллических глифов (А, Ж, Щ
# выходят одной и той же заглушкой 5x7). То есть аватар получался пустым
# квадратом с крошечной кляксой, без единой ошибки в логах.
# В tg-manager/Dockerfile шрифты стояли, но Railway собирает ЭТОТ файл.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libpq-dev curl procps \
    fonts-dejavu-core fonts-liberation \
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

# ---------------------------------------------------------------------------
# Additive: модуль blackbird (OSINT-поиск аккаунтов по username/email).
# Свой каталог /app/blackbird и свой venv — его пины (aiohttp, pillow,
# reportlab, rich) не пересекаются с tg-manager. Это не сервис: ничего не
# запускается при старте, только по вызову:
#   /app/blackbird/.venv/bin/python -m pablo_blackbird -u <username>
# Пишет только в BLACKBIRD_WORKDIR (по умолчанию /tmp/blackbird).
# ---------------------------------------------------------------------------
COPY blackbird/requirements.txt /app/blackbird/requirements.txt
RUN python -m venv /app/blackbird/.venv \
    && /app/blackbird/.venv/bin/pip install --no-cache-dir -r /app/blackbird/requirements.txt
COPY blackbird/upstream/ /app/blackbird/upstream/
COPY blackbird/pablo_blackbird/ /app/blackbird/pablo_blackbird/

# Run tg-manager (foreground) + assistant (background, self-restarting).
COPY start-all.sh /app/start-all.sh
RUN chmod +x /app/start-all.sh

# Не-root: процессы работают от app. Код остаётся root-овым и app на запись
# недоступен; app пишет только в /tmp и в каталоги, которые точка входа отдаёт
# ему перед сбросом прав (тома Railway монтируются от root — см.
# deploy-entrypoint.sh). PYTHONDONTWRITEBYTECODE: app не может класть
# __pycache__ рядом с кодом, и пытаться незачем.
RUN useradd --system --create-home --uid 10001 --user-group app
ENV PYTHONDONTWRITEBYTECODE=1
COPY deploy-entrypoint.sh /app/deploy-entrypoint.sh
RUN chmod 0755 /app/deploy-entrypoint.sh

CMD ["bash", "/app/deploy-entrypoint.sh"]
