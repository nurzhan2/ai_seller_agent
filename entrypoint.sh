#!/bin/sh
# Единая точка входа контейнера: миграция, потом uvicorn.
#
# Раньше это жило в railway.toml → [deploy] startCommand. Оказалось, что
# Railway не гарантированно исполняет startCommand через шелл — на логах
# было видно, что `echo PORT=$PORT` печатал буквально "PORT=$PORT", без
# подстановки, а значит и `--port $PORT` доезжал до uvicorn как есть,
# строкой, и тот падал ещё до первой строчки собственного лога.
#
# У ENTRYPOINT-скрипта с шебангом этой проблемы нет по конструкции: ядро
# всегда запускает файл через интерпретатор, указанный в шебанге (здесь —
# /bin/sh), независимо от того, как именно Railway/Docker решают вызвать
# сам ENTRYPOINT. Подстановка переменных окружения происходит внутри
# настоящего шелла, а не зависит от чужого решения.
#
# Явная проверка кода возврата вместо `alembic upgrade head && uvicorn...` —
# чтобы сбой миграции всегда был виден в логах, а не тонул в оборванной
# цепочке `&&`.
alembic upgrade head
ALEMBIC_STATUS=$?
if [ "$ALEMBIC_STATUS" -ne 0 ]; then
    echo "ALEMBIC MIGRATION FAILED — exit code $ALEMBIC_STATUS, смотри traceback выше" >&2
    exit "$ALEMBIC_STATUS"
fi
echo "ALEMBIC: migration ok"

# exec, а не просто запуск — uvicorn заменяет собой этот шелл-процесс и
# получает сигналы остановки от Railway/Docker напрямую, а не через
# промежуточный шелл. ${PORT:-8000} — Railway всегда задаёт PORT сам; :-8000
# только запасной порт для обычного docker run/docker-compose без Railway.
exec python -u -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
