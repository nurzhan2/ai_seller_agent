"""Спецификация Avito Business API.

Источник: официальный OpenAPI 3.0 Авито (238 путей, 245 операций),
опубликован в https://github.com/MissiaL/avito-api
→ references/avito-api-openapi.json

Все пути и схемы ниже взяты из спека дословно. SPEC_VERIFIED=True.

Если понадобится эндпоинт, которого здесь нет:
    git clone --depth 1 https://github.com/MissiaL/avito-api.git
    python -c "import json;s=json.load(open('avito-api/references/avito-api-openapi.json'));print('\n'.join(s['paths']))"
"""

SPEC_VERIFIED = True

BASE_URL = "https://api.avito.ru"

# ── Авторизация ───────────────────────────────────────────────────────────
# POST, параметры идут в QUERY STRING, а не в теле и не в форме.
# grant_type=client_credentials, client_id, client_secret
TOKEN = ("POST", "/token")
TOKEN_GRANT_TYPE = "client_credentials"

# Токен передаётся в каждом запросе: Authorization: Bearer <access_token>
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

# Для отправки и изменения сообщений нужен scope messenger:write
REQUIRED_SCOPE = "messenger:write"

# ── Чаты ──────────────────────────────────────────────────────────────────
LIST_CHATS = ("GET", "/messenger/v2/accounts/{user_id}/chats")
GET_CHAT = ("GET", "/messenger/v2/accounts/{user_id}/chats/{chat_id}")

# ВНИМАНИЕ: у этого пути в спеке ЗАВЕРШАЮЩИЙ СЛЕШ. Без него будет 404.
GET_MESSAGES = ("GET", "/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/")

MARK_CHAT_READ = ("POST", "/messenger/v1/accounts/{user_id}/chats/{chat_id}/read")

# ── Отправка сообщений ────────────────────────────────────────────────────
SEND_MESSAGE = ("POST", "/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages")
# Тело: {"message": {"text": "..."}, "type": "text"}
# ЖЁСТКИЙ ЛИМИТ: text не длиннее 1000 символов. Проверять ДО отправки —
# агент режет до 700, но обрезка должна быть и здесь, на границе.
MESSAGE_TEXT_MAX_LENGTH = 1000

DELETE_MESSAGE = ("POST", "/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/{message_id}")

# ── Изображения ───────────────────────────────────────────────────────────
UPLOAD_IMAGES = ("POST", "/messenger/v1/accounts/{user_id}/uploadImages")
# multipart/form-data, имя поля ровно такое, вместе со скобками:
UPLOAD_IMAGES_FIELD = "uploadfile[]"

SEND_IMAGE = ("POST", "/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/image")
# Тело: {"image_id": "<id из ответа uploadImages>"}

# ── Объявления ────────────────────────────────────────────────────────────
# Проверено тем же способом, что описан в шапке файла: чистый клон спека
# (238 путей) и точечный поиск. Тот же BASE_URL, тот же Authorization: Bearer,
# тот же OAuth client_credentials — транспорт не отличается от messenger,
# отдельного скоупа для client_credentials в спеке не заявлено.
LIST_ITEMS = ("GET", "/core/v1/items")
# Query-параметры: per_page (по умолчанию 25, макс. 100), page, status
# (active,removed,old,blocked,rejected — по умолчанию active), updatedAtFrom,
# category. Лимит — 25 запросов в минуту (в спеке явно указано).
LIST_ITEMS_RATE_LIMIT_PER_MINUTE = 25
LIST_ITEMS_MAX_PER_PAGE = 100
# Ответ: {"meta": {"page", "per_page"}, "resources": [{"id", "title", "url",
# "status", "price", "address", "category": {"id","name"}}, ...]}

# ── Вебхуки ───────────────────────────────────────────────────────────────
WEBHOOK_SUBSCRIBE = ("POST", "/messenger/v3/webhook")
# Тело: {"url": "https://.../webhook/avito/<secret>"}

LIST_SUBSCRIPTIONS = ("POST", "/messenger/v1/subscriptions")   # именно POST
WEBHOOK_UNSUBSCRIBE = ("POST", "/messenger/v1/webhook/unsubscribe")

# ── Ограничение частоты ───────────────────────────────────────────────────
RATE_LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining")


# ── Подпись вебхука: НЕ РЕАЛИЗОВАНА, И ЭТО ОСОЗНАННО ──────────────────────
#
# Авито присылает заголовок x-avito-messenger-signature (64 hex-символа,
# похоже на sha256), но алгоритм не описан ни в OpenAPI-спеке, ни в
# документации. Разработчики публично сообщают, что поддержка Авито не
# смогла ответить на этот вопрос в течение месяца.
#
# Правдоподобно угаданная схема подписи опаснее отсутствующей: неверный
# путь падает громко, а неверная проверка подписи МОЛЧА ПРИНИМАЕТ подделки.
# Поэтому проверяем не подпись, а секрет в самом URL.
#
# Схема защиты вебхука:
#   1. AVITO_WEBHOOK_SECRET — случайные 32+ символа в .env
#   2. вебхук регистрируется по адресу /webhook/avito/{AVITO_WEBHOOK_SECRET}
#   3. запрос на любой другой путь → 404
#   4. сравнение секрета через secrets.compare_digest, не через ==
#   5. дополнительно: ограничение по IP, если Авито опубликует диапазоны
#
# Секретный путь знают только мы и Авито, этого достаточно поверх HTTPS.
# Если алгоритм подписи станет известен — добавить проверку вторым слоем,
# не убирая секретный путь.

WEBHOOK_SIGNATURE_HEADER = "x-avito-messenger-signature"
WEBHOOK_SIGNATURE_ALGORITHM_KNOWN = False
