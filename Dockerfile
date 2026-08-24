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
