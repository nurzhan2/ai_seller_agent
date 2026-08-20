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

USER appuser
EXPOSE 8000

# Форма shell (не exec) — иначе $PORT не подставится. Railway всегда задаёт
# PORT и ждёт, что процесс слушает именно его; :-8000 — запасной порт для
# обычного `docker run` без Railway. Для Railway этот CMD переопределяется
# startCommand из railway.toml (там же миграции перед стартом) — здесь он
# остаётся корректным дефолтом для прямого docker run/docker-compose.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
