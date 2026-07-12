# Dockerfile for the personal assistant Telegram bot (python main.py assistant).
# Separate from the root Dockerfile, which builds the tg-manager service.
#
# On Railway: create a NEW service from this repo and set
#   Settings → Build → Dockerfile Path = assistant.Dockerfile
# then add the env vars ASSISTANT_BOT_TOKEN and ANTHROPIC_API_KEY.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ASSISTANT_STATE_DIR=/data/assistant

WORKDIR /app

# Only the pure-Python stack is needed (anthropic, supabase, httpx, dotenv) —
# no build toolchain required, so the image stays small and builds fast.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Just the code the assistant actually imports.
COPY main.py orchestrator.py ./
COPY assistant/ ./assistant/
COPY agents/ ./agents/
COPY tools/ ./tools/
COPY database/ ./database/

# Persistent chat history / admin list. Mount a Railway volume at /data to
# keep context across redeploys; without a volume it simply resets on deploy.
RUN mkdir -p /data/assistant
VOLUME ["/data"]

CMD ["python", "main.py", "assistant"]
