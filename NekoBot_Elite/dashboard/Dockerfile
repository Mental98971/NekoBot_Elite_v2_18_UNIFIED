# Build with the PROJECT ROOT as context (see docker-compose.yml's
# `context: .` / `dockerfile: dashboard/Dockerfile`) — needed so this can
# copy in the shared bot.config / bot.core.database modules below.
FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt ./dashboard-requirements.txt
RUN pip install --no-cache-dir -r dashboard-requirements.txt

# Only the shared config/models — NOT bot/core/bot.py or bot/plugins,
# which would drag in python-telegram-bot/pyrogram/pytgcalls for no
# reason. See dashboard/main.py's module docstring.
COPY bot/__init__.py bot/__init__.py
COPY bot/config.py bot/config.py
COPY bot/utils/__init__.py bot/utils/__init__.py
COPY bot/utils/logger.py bot/utils/logger.py
COPY bot/utils/retry.py bot/utils/retry.py
COPY bot/core/__init__.py bot/core/__init__.py
COPY bot/core/database.py bot/core/database.py
COPY dashboard/main.py dashboard/main.py

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8080"]
