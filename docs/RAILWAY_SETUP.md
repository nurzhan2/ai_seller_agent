# Деплой на Railway — пошагово

HTTPS, домен и роутинг здесь даёт сама платформа — настраивать Caddy не
нужно (это для варианта с VPS, см. `docker-compose.prod.yml` и
`docs/RUNBOOK.md`). Здесь — только то, что специфично для Railway.

## 1. Создать проект, добавить Postgres и Redis

В Railway: **New Project → Deploy from GitHub repo** (репозиторий
`ai_seller_agent`) → в этом же проекте **+ New → Database → PostgreSQL** и
**+ New → Database → Redis**. Оба поднимутся как отдельные сервисы внутри
проекта — прокидывать их URL руками не нужно, см. шаг 3.

## 2. Подключить репозиторий, собрать из Dockerfile

При создании сервиса из GitHub-репозитория Railway сам находит `Dockerfile`
в корне и использует его — переключать builder вручную не требуется, но
стоит один раз проверить в **Settings → Build**, что выбран **Dockerfile**,
а не Nixpacks (Railway иногда предлагает оба). `railway.toml` в корне уже
фиксирует это явно (`builder = "dockerfile"`).

Как стартует контейнер — не через `startCommand` в `railway.toml` (его там
специально нет) и не через `CMD` в `Dockerfile`, а через `ENTRYPOINT
["./entrypoint.sh"]`. Причина: Railway не гарантированно выполняет
`startCommand` через шелл — воспроизведено на логах, когда `$PORT` в нём не
подставлялся и uvicorn падал ещё до первой строчки лога. `entrypoint.sh`
(шебанг `/bin/sh`) гарантированно исполняется через шелл всегда, независимо
от того, как Railway решит запустить контейнер, и делает и миграцию
(`alembic upgrade head`, с явной проверкой кода возврата — сбой печатается,
а не тонет в оборванной цепочке), и `exec uvicorn ... --port $PORT`. Трогать
не нужно, если не меняете инфраструктуру осознанно.

## 3. Переменные окружения

Заполняются в **Settings → Variables** сервиса приложения (не БД и не
Redis — им переменные не нужны, Railway настраивает их сам).

### Обязательные для старта

| переменная | значение |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — ссылка на сервис, а не скопированная строка. Railway отдаёт её в схеме `postgresql://` и может дописать `sslmode=require` — оба случая нормализует `app/config.py:normalize_database_url` сам, руками ничего не чинить |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` — та же логика; если Railway выдаст `rediss://` (TLS), сработает само — `redis.asyncio.from_url` распознаёт схему без доп. кода |
| `ENV` | `prod` |
| `DRY_RUN` | `true` — **заглавными в голове, не только в .env**: см. пункт 4 ниже, это не техническая мелочь |
| `AVITO_WEBHOOK_SECRET` | сгенерировать (см. пункт 5), минимум 32 символа |
| `ADMIN_USER` / `ADMIN_PASSWORD` | свои — без них `/admin/*` отвечает 503, а не открыт всем |

### Для Авито

| переменная | откуда |
|---|---|
| `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` | Личный кабинет продавца → Настройки → Avito API |
| `AVITO_USER_ID` | там же |

### Для модели

| переменная | значение |
|---|---|
| `ANTHROPIC_API_KEY` | обязателен — это провайдер по умолчанию (`LLM_PROVIDER=anthropic`) и всегда нужен судье в харнессе, даже если основной провайдер — DeepSeek |
| `LLM_PROVIDER` | `anthropic` по умолчанию, `deepseek` — если заказчик подключил свой ключ (см. `docs/PROVIDER_COMPARISON.md`) |
| `DEEPSEEK_API_KEY` | только если `LLM_PROVIDER=deepseek` или настроен `LLM_FALLBACK_PROVIDER=deepseek` |
| `TELEGRAM_BOT_TOKEN` | от @BotFather |
| `TELEGRAM_OPS_CHAT_ID` | id чата/канала операторов |
| `TELEGRAM_ALLOWED_USERS` | id операторов через запятую — пустое значение выключает бота для всех, не открывает его всем |

### Опциональные

`AVITO_MAX_CONCURRENCY`, `AVITO_TIMEOUT_SECONDS`, `AVITO_MAX_RETRIES`,
`LLM_FALLBACK_PROVIDER`, `LLM_FALLBACK_AFTER_ERRORS`, `LLM_DIALOG_MODEL`,
`LLM_CLASSIFIER_MODEL`, `LLM_BASE_URL`, `DEEPSEEK_ENABLE_THINKING`,
`YCLIENTS_*`, `AUTO_BOOKING_ENABLED`, `MAX_AGENT_REPLIES_PER_CHAT`,
`DEBOUNCE_WINDOW_SECONDS`, `DAILY_COST_LIMIT_RUB` — есть безопасные значения
по умолчанию в `app/config.py`, трогать только осознанно. Полный список с
комментариями — `.env.example`.

## 4. DRY_RUN=true на первом деплое

**Не переключайте на `false`, пока не прогоните хотя бы сутки в режиме
наблюдения.** При `DRY_RUN=true` агент отвечает в БД и дублирует ответ
оператору в Telegram, но клиенту в Авито ничего не уходит сам — весь трафик
идёт через ручное «Одобрить». Это единственный способ поймать неверную цену
или сорвавшийся тон до того, как их увидит живой клиент. Порядок выключения
модерации по зонам — `docs/GO_LIVE.md`.

## 5. AVITO_WEBHOOK_SECRET

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Результат — в переменную `AVITO_WEBHOOK_SECRET` в Railway. Он же становится
частью пути вебхука (`/webhook/avito/{secret}`) — это осознанная замена
проверке подписи, алгоритм которой Авито не публикует (подробности —
`app/webhooks.py`).

## 6. Регистрация вебхука после первого деплоя

1. Взять публичный домен сервиса: **Settings → Networking → Public
   Networking → Generate Domain** (или подключить свой).
2. Зарегистрировать вебхук — из Railway shell (**сервис → три точки →
   Shell**, попадаете внутрь запущенного контейнера):

   ```bash
   python -m scripts.register_webhook https://<ваш-домен>.up.railway.app
   ```

   Секрет подставляется скриптом из `AVITO_WEBHOOK_SECRET` самим — в
   команду его вписывать не нужно, и в истории шелла он не остаётся.

3. Убедиться, что подписка встала:

   ```bash
   python -m scripts.register_webhook --list
   ```

   В ответе должен быть URL с вашим доменом и путём `/webhook/avito/...`.
   Если списка нет или URL другой — регистрация не прошла, см. пункт 8.

### Какие скрипты работают из Console, а какие нет

Папка `scripts/` есть в образе, но `docs/` и `media/` в него не копируются
(`.dockerignore`) — они нужны только при разработке. Поэтому:

| скрипт | из Console |
|---|---|
| `scripts.register_webhook` | да — основной сценарий, ради которого Console и нужен |
| `scripts.export_listings` | да, каталог для `--out` создаёт сам |
| `scripts.rotate_avito_keys` | да (`--check` — только проверка, ничего не меняет) |
| `scripts.import_photos` | технически да, но папку с фото сначала надо куда-то положить, а файловая система контейнера пропадает при редеплое |
| `scripts.replay`, `scripts.compare_providers` | нет — нужен `docs/analysis/dialogs.json`, запускать локально |
| `scripts.anonymize_dialogs`, `scripts.unpack_google_drive_photos` | нет — инструменты разработки, работают с `docs/` и `media/` |

## 7. Проверка живости

- `GET /health` на публичном домене → `{"status": "ok", ...}`. `"degraded"`
  с `"database": "error: ..."` внутри `checks` означает, что приложение
  поднялось, но не достучалось до Postgres — почти всегда это шаг 3
  (`DATABASE_URL` не подставлен ссылкой на сервис).
- `/admin/readiness` (Basic Auth — `ADMIN_USER`/`ADMIN_PASSWORD`) — все 10
  зон должны быть `yes` в обеих колонках.
- Логи сервиса (**Deployments → View Logs**): на старте должна быть строка
  `telegram operator bot: polling started` — если вместо неё
  `TELEGRAM_BOT_TOKEN не задан`, бот не запущен, проверьте переменную.
- Тестовое сообщение оператору: напишите что угодно любому боту-получателю
  из `TELEGRAM_ALLOWED_USERS` — `/stats` в чате с ботом должен ответить
  сводкой, а не молчанием.

## 8. Если не поднялось

Три самые вероятные причины, в порядке частоты:

1. **`DATABASE_URL` не подставлен ссылкой на сервис.** Симптом: `/health`
   отвечает `"degraded"`, в логах — ошибка подключения к Postgres на самом
   первом запросе (миграция при старте тоже падает первой). Проверить: в
   Variables значение должно быть `${{Postgres.DATABASE_URL}}` — Railway
   разворачивает эту ссылку сам при деплое; вписанная руками строка с
   `sslmode=require` тоже сработает (нормализуется автоматически), но
   ссылка надёжнее — переживает переезд БД на другой хост без правки.
2. **`AVITO_WEBHOOK_SECRET` короче 32 символов или не задан.**
   Симптом: приложение стартует, но в логах предупреждение «вебхук будет
   отклонять все запросы», а `register_webhook` не проходит. Проверить:
   `Settings.require_webhook_secret()` (`app/config.py`) рушит запуск при
   попытке реально отправить сообщение с таким секретом — сообщение об
   ошибке там же называет минимальную длину.
3. **Healthcheck считает деплой неуспешным, хотя приложение живо.**
   Симптом: Railway откатывает деплой или бесконечно перезапускает
   контейнер (`restartPolicyMaxRetries` в `railway.toml` ограничивает это
   пятью попытками, дальше контейнер остаётся упавшим). Проверить: `/health`
   возвращает HTTP 200 даже в состоянии `"degraded"` (это осознанно — база
   недоступна не должна валить весь под) — если Railway всё равно считает
   деплой неуспешным, смотрите `healthcheckTimeout` в `railway.toml`
   (300 секунд) и логи на предмет исключения ДО того, как FastAPI успел
   поднять `/health` вообще (например, `load_catalog()` упал на невалидной
   базе знаний — единственный случай, когда старт останавливается
   намеренно, см. `app/main.py`).

Если ни один из трёх пунктов не объясняет симптом — дальше по
`docs/RUNBOOK.md` → «Быстрая диагностика».
