FROM python:3.12-slim

WORKDIR /app

# fonts-dejavu-core / fonts-liberation нужны генератору аватаров: базовый
# python:3.12-slim идёт вообще без TTF, и Pillow откатывается на встроенный
# растровый шрифт — на аватаре 512×512 он нечитаем. DejaVu выбран за покрытие
# кириллицы (инициалы городов).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libpq-dev curl procps \
    fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
