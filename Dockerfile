FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# Non-root: the process handles untrusted input from the public internet.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser migrations ./migrations
# Эксплуатационные скрипты (`python -m scripts.register_webhook` и т.п.) —
# без них Railway Console бесполезен: перерегистрировать вебхук после смены
# AVITO_WEBHOOK_SECRET можно только оттуда. Новых зависимостей не тянет:
# единственные сторонние импорты во всей папке — httpx, anthropic и PIL,
# и все три уже нужны самому app/ (проверено разбором AST по scripts/*.py).
# Вес — 80 КБ. Часть скриптов рассчитана на docs/ и media/, которых в образе
# нет по .dockerignore (replay.py, anonymize_dialogs.py,
# unpack_google_drive_photos.py) — это заведомо инструменты разработки, а не
# Console; из Console работают register_webhook, export_listings (сам создаёт
# каталог для --out) и rotate_avito_keys.
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser entrypoint.sh ./
# chmod, пока ещё root — appuser ниже уже не сможет.
RUN chmod +x ./entrypoint.sh

USER appuser
EXPOSE 8000

# ENTRYPOINT (не CMD, не startCommand в railway.toml) — единственное место,
# где решается, как стартует контейнер что на Railway, что в обычном
# docker run/docker-compose. entrypoint.sh сам делает alembic upgrade head
# и exec uvicorn с $PORT — см. комментарии в самом файле, почему это
# надёжнее, чем полагаться на то, как Railway решит выполнить startCommand.
ENTRYPOINT ["./entrypoint.sh"]
