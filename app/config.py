"""Application settings. Every secret comes from the environment.

Nothing in this file may carry a real credential as a default — `.env` is
gitignored and `.env.example` holds placeholders only.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# Объявления заказчика, не относящиеся к комплексу. Отдельной константой, а
# не литералом в Field, чтобы валидатор ниже мог вернуть ИМЕННО их на пустое
# значение переменной, и чтобы список был виден в одном месте.
DEFAULT_BLOCKED_ITEMS: tuple[str, ...] = (
    "8204183112",   # вакансия менеджера
    "8076244626",   # продажа глэмпинга
    "8076019723",   # арендный бизнес
    "7980739861",   # продажа банного комплекса
    "8172444564",   # квартира-студия
    # Второе объявление о продаже ВСЕГО комплекса — найдено живым прогоном
    # scripts/sync_item_scope.py 2026-08-28 против прода: заголовок «Банный
    # комплекс "Чайка" инвестиционная возможность» матчит allow-слово
    # «банный», и чистая классификация по заголовку (app/channels/
    # item_scope.py:classify_title) ошибочно открыла бы его — тот же класс
    # ошибки, что и у 7980739861 выше, просто под другим id.
    "7948732527",   # продажа банного комплекса «Чайка» (инвестиционная возможность)
    # Пять объявлений ниже — жёсткий deny по id БЕЗ новых ключевых слов
    # (решение заказчика 2026-08-29): расширять deny-список словами вроде
    # "комната"/"продажа помещения"/"доходный" рискует ложно денить будущие
    # объявления комплекса, которые случайно упомянут те же слова, а сами
    # эти пять — конкретные посторонние объявления, найденные живым прогоном
    # scripts/sync_item_scope.py 2026-08-28 (no_keyword_match, ошибочно
    # получали allow).
    "7980333044",   # продажа 2-этажных корпусов
    "8236197068",   # продажа доходной недвижимости
    "7980615746",   # продажа помещения под ресторан/клуб
    "8076853804",   # комната в квартире
    "7948469179",   # имущественный комплекс бывшего детского лагеря
)

# Явное «фильтра нет»: только этим словом, не пустой строкой (см. коммент
# у avito_blocked_items — пустая строка означает «дефолты»).
BLOCKLIST_DISABLED = "none"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Avito -----------------------------------------------------------
    avito_client_id: SecretStr = SecretStr("")
    avito_client_secret: SecretStr = SecretStr("")
    avito_user_id: str = ""
    # Заменяет проверку подписи вебхука, алгоритм которой Авито не публикует.
    # Секрет живёт в самом URL: /webhook/avito/{secret}. Минимум 32 символа —
    # см. комментарий в app/channels/avito_endpoints.py.
    avito_webhook_secret: SecretStr = SecretStr("")
    avito_webhook_secret_min_length: int = 32
    # Объявления, по которым агенту РАЗРЕШЕНО отвечать. В аккаунте заказчика
    # есть объявления, не относящиеся к комплексу: вакансия менеджера,
    # продажа глэмпинга, арендный бизнес, продажа банного комплекса,
    # квартира-студия. Без этого списка человек, спросивший про вакансию,
    # получал прайс на бани.
    #
    # ПУСТОЙ СПИСОК = РАЗРЕШЕНО ВСЁ. Это сознательный выбор в пользу
    # «не сломать при незаданной переменной»: на всех стендах, где
    # AVITO_ALLOWED_ITEMS не выставлена, поведение остаётся прежним.
    # Обратная трактовка (пусто = запретить всё) превратила бы забытую
    # переменную в молчащего агента, что заметно далеко не сразу.
    #
    # NoDecode — по той же причине, что и у telegram_allowed_users ниже:
    # без него pydantic-settings пытается разобрать значение как JSON ещё
    # до валидатора и падает на пустой строке.
    avito_allowed_items: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Чёрный список — основной способ фильтрации. В отличие от белого,
    # новое объявление КОМПЛЕКСА начинает работать сразу, без правки
    # переменной: под запретом только перечисленное здесь.
    #
    # Id из DEFAULT_BLOCKED_ITEMS зашиты значением по умолчанию сознательно. Это не про
    # «конфигурация в коде»: если переменную забудут выставить на новом
    # стенде, посторонние объявления снова начнут получать прайс на бани —
    # ровно тот баг, ради которого фильтр и появился.
    #
    # ПУСТОЕ ЗНАЧЕНИЕ = ДЕЙСТВУЮТ ДЕФОЛТЫ, а не «не блокировать ничего».
    # Раньше было наоборот, и это молча ломало фильтр: `.env.example` несёт
    # строку `AVITO_BLOCKED_ITEMS=` (её естественно скопировать в Railway
    # целиком), пустая строка превращалась в пустой список, дефолты не
    # применялись — и агент снова отвечал по вакансии и квартире-студии.
    # Ошибка настройки должна оставлять фильтр включённым, а не выключать
    # его беззвучно.
    #
    # Отключить фильтр целиком, если это правда нужно: `AVITO_BLOCKED_ITEMS=none`.
    avito_blocked_items: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_BLOCKED_ITEMS)
    )
    # Обращения из профиля продавца (chat_type u2u/a2u) приходят без
    # item_id — по спеку Авито объявления у таких чатов нет в принципе
    # (см. app/channels/avito_payloads.py:extract_chat_type). Это живые
    # клиенты, и молчать в ответ хуже, чем ответить: агент отвечает как
    # обычно, только первым сообщением уточняет направление.
    avito_allow_chats_without_item: bool = True
    # Concurrent in-flight requests to Avito. A semaphore, not a token
    # bucket — see AvitoClient.
    avito_max_concurrency: int = 5
    avito_timeout_seconds: float = 20.0
    avito_max_retries: int = 3

    # --- Поллинг Авито ---------------------------------------------------
    # Вебхуки по объявлениям комплекса не доставляются. Проверено живьём
    # (scripts/poll_once.py): за всю историю базы 49 входящих, и ВСЕ
    # u2i-чаты среди них — по объявлениям из чёрного списка (квартира-студия,
    # продажа комплекса). По разрешённым объявлениям бань и гриль-домиков —
    # ни одного события. Поллер поэтому основной канал, вебхук — второй.
    #
    # ДЕФОЛТ ВЫКЛЮЧЕН после инцидента 2026-08-28: первый боевой проход
    # поллера разослал 65 сообщений в старые чаты живых клиентов (найдено
    # 21 — ранняя ошибочная прикидка). Причина и разбор — см.
    # AGENT_MIN_INBOUND_TS ниже. Включать обратно — только вручную, отдельным
    # решением, не флагом на этом же деплое.
    poller_enabled: bool = False
    poller_interval_seconds: int = 60
    poller_chats_page_size: int = 100
    # ПОТОЛОК API, А НЕ НАШ ВЫБОР. GET /messenger/v2/.../chats отдаёт 400 на
    # offset=1100 — проверено живьём. Дальше 1000 пагинация не идёт, и это
    # ограничивает работу прохода сверху: 11 запросов, что бы ни творилось в
    # аккаунте. Чаты за этой границей поллеру не видны; список сортируется
    # по убыванию времени последнего сообщения (8 нарушений порядка на 1098
    # чатов, худшее — 4 суток), поэтому новая активность всплывает наверх
    # сама. Увеличивать бессмысленно: Авито ответит 400.
    poller_max_offset: int = 1000
    # Сколько сообщений тянуть за раз из одного чата и сколько таких страниц
    # разрешено. Упёрлись в потолок — в лог уходит warning: молча потерять
    # хвост переписки нельзя, это ровно сценарий «вернулись после суток
    # простоя».
    poller_messages_page_size: int = 20
    poller_max_message_pages: int = 10
    # Как часто обновляется список объявлений аккаунта для гуарда «свой ли
    # это чат» (см. app/avito/poller.py). Час — объявления не появляются
    # чаще, а лимит GET /core/v1/items всего 25 запросов в минуту.
    poller_items_refresh_seconds: int = 3600
    # Статусы объявлений, которые считаются своими. Снятое объявление —
    # по-прежнему наше, и клиент в таком чате по-прежнему живой.
    poller_items_statuses: str = "active,old,removed"
    # Перепривязка вебхука по расписанию. Пусто в public_base_url —
    # перепривязка не запускается (и говорит об этом в лог при старте).
    webhook_resubscribe_interval_hours: int = 24
    public_base_url: str = ""

    # --- Порог возраста входящих ------------------------------------------
    # ЕДИНСТВЕННАЯ защита от «ответили в чат месячной давности» (инцидент
    # 2026-08-28: холодный старт поллера — четыре состояния, которые должны
    # были сойтись (cold_start_decision, POLLER_BACKFILL_HOURS,
    # cold_start_skipped, seen_ids), не сошлись, и 65 клиентов получили
    # ответ в чат, где последнее слово клиента было от нескольких дней до
    # нескольких месяцев назад). Тот механизм убран целиком — см. историю
    # app/avito/poller.py и app/avito/cursors.py.
    #
    # Правило простое и одно: входящее с created < AGENT_MIN_INBOUND_TS не
    # порождает исходящего НИКОГДА. Проверяется в app/pipeline.py, сразу
    # после дедупа, до всякой бизнес-логики — независимо от курсора, флагов,
    # номера прохода поллера и от канала (поллер и вебхук идут через одну и
    # ту же проверку). Курсор поллера после этого отвечает только за «что
    # читать», а не за «кому отвечать» — его баги перестают быть опасными
    # по построению, а не по тщательности.
    #
    # ДЕФОЛТ 0 = АГЕНТ НЕ ОТВЕЧАЕТ НИ НА ЧТО. Сознательно наоборот тому, как
    # выглядела бы «защита выключена» (0 в качестве нижней границы, которую
    # created не может нарушить) — тот дефолт означал бы, что забытая на
    # деплое переменная тихо снимает защиту, ровно та же ошибка, что уже
    # стоила 65 сообщений (см. историю AVITO_BLOCKED_ITEMS в этом же файле:
    # пустое значение там тоже когда-то читалось как «фильтра нет», а не
    # как «действуют дефолты»). Здесь то же решение: незаданное значение
    # обязано быть БЕЗОПАСНЫМ, а не удобным. app/main.py пишет в лог
    # WARNING при старте, если это значение <= 0 и POLLER_ENABLED=true —
    # молчание агента после включения поллера должно быть замечено быстро,
    # а не через жалобы клиентов на то, что бот перестал отвечать.
    #
    # Включить ответы: выставить unix-секунды, раньше которых агент
    # отвечать не должен, — например, момент, когда поллер включают
    # обратно. При включённом пороге отсутствие `created` у входящего тоже
    # трактуется как «слишком старое» (fail closed, тот же принцип, что у
    # OutboundGate.is_allowed и у OwnItemIds.__call__): свежести сообщения,
    # о которой нечего сказать, доверять нельзя.
    agent_min_inbound_ts: int = 0

    # --- Anthropic -------------------------------------------------------
    # Только для харнесса качества: судья по тону (app/quality/judge.py) и
    # scripts/replay.py --provider anthropic. К диалогам с клиентами
    # отношения не имеет — их ведёт DeepSeek, см. llm_provider ниже.
    anthropic_api_key: SecretStr = SecretStr("")

    # --- LLM-провайдер (промт №12) ---------------------------------------
    # Диалоги ведёт DeepSeek — ключ заказчика, их токены, их расходы.
    #
    # ANTHROPIC КАК ПРОВАЙДЕР ДИАЛОГА УБРАН (2026-08-30). Он числился
    # основным по умолчанию и резервным на случай отказа DeepSeek, но
    # ANTHROPIC_API_KEY в проде всё это время был заглушкой
    # `sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx` из .env.example — то есть резерв не
    # сработал бы ни разу, и узнали бы мы об этом в момент падения DeepSeek.
    # Провайдер, который не может работать, хуже отсутствующего: он создаёт
    # уверенность, что запасной путь есть.
    #
    # Класс AnthropicProvider при этом ЖИВ и удалён не будет: это адаптер
    # формы сообщений Anthropic API, которой говорит и сам DeepSeek
    # (api.deepseek.com/anthropic), и через него же в AgentLoop заходят
    # тестовые клиенты. Оценка тона (app/quality/judge.py, claude-opus-5) и
    # scripts/compare_providers.py тоже остаются — они собирают клиента
    # сами и к рантайму агента отношения не имеют.
    llm_provider: Literal["deepseek"] = "deepseek"
    # Резервный провайдер: включается автоматически после
    # llm_fallback_after_errors подряд идущих ошибок основного
    # (см. app/agent/providers/failover.py). Пусто — резерва нет, ошибка
    # основного провайдера уходит наверх как есть.
    #
    # Сейчас провайдер ровно один, поэтому резерв настроить НЕЧЕМ: переход
    # deepseek -> deepseek не спасает ни от чего (тот же адрес, тот же
    # ключ). Механизм отказоустойчивости (FailoverProvider) оставлен рабочим
    # и покрытым тестами — он понадобится, когда появится второй настоящий
    # провайдер. Резерв ПО МОДЕЛИ (например, deepseek-v4-flash вместо
    # deepseek-v4-pro) этой настройкой не делается: модель выбирается в
    # factory.resolve_models на уровне хода, а не провайдера.
    llm_fallback_provider: Optional[Literal["deepseek"]] = None
    llm_fallback_after_errors: int = 3
    # Пусто — имя модели берётся по умолчанию для llm_provider
    # (app.agent.providers.factory.default_models_for).
    llm_dialog_model: str = ""
    llm_classifier_model: str = ""
    # Переопределение base_url — для локальных прокси/тестовых стендов.
    # Пусто — берётся адрес провайдера по умолчанию.
    llm_base_url: str = ""
    deepseek_api_key: SecretStr = SecretStr("")
    # DeepSeek v4 по умолчанию думает (thinking-блок) перед ответом — при
    # маленьком max_tokens это может съесть весь лимит и вернуть пустой
    # ответ (воспроизведено на классификаторе, см. deepseek_provider.py).
    # Выключено по умолчанию — не только защита от этого, но и честное
    # сравнение стоимости/задержки с Anthropic в Части 4 промта №12.
    deepseek_enable_thinking: bool = False

    # --- Админка ---------------------------------------------------------
    # Незаданные значения ОТКЛЮЧАЮТ админку (503), а не открывают её всем.
    admin_user: str = ""
    admin_password: SecretStr = SecretStr("")

    # --- YCLIENTS --------------------------------------------------------
    yclients_partner_token: SecretStr = SecretStr("")
    yclients_user_token: SecretStr = SecretStr("")
    yclients_company_id: str = ""
    # Агент сам ставит бронь в YCLIENTS, когда клиент подтвердил зону, дату,
    # время и оставил контакты.
    #
    # СТАРШЕ ЭТОГО ФЛАГА — `payment.handoff_on_payment_step` из
    # app/kb/payment.yaml. Пока он true (боевая настройка), значение
    # AUTO_BOOKING_ENABLED не влияет ни на что: агент не ставит бронь ни
    # при каких условиях, на этапе оплаты диалог уходит оператору с готовой
    # карточкой. Это решение заказчика, а не временная мера, и живёт оно в
    # базе знаний, а не в переменных окружения.
    #
    # ДЕФОЛТ ВЫКЛЮЧЕН после аудита 2026-08-28: `create_booking` ставит
    # РЕАЛЬНУЮ запись в YCLIENTS без единой проверки оплаты — только
    # занятости. Включать обратно только после того, как перед постановкой
    # появится гейт на факт оплаты/предоплаты — и только вместе с
    # осознанным выключением handoff, иначе флаг просто ничего не делает.
    #
    # Что при этом уже проверяется, даже когда флаг включён (см.
    # app/agent/tools.py:_tool_create_booking): занятость перепроверяется
    # заново непосредственно перед постановкой, блокируются ЧАСЫ ЗАНЯТОСТИ
    # (при акции «6-й час в подарок» их 6, а не оплаченные 5), бронь
    # пишется в нашу таблицу `bookings`, оператор получает уведомление.
    # Агенту по-прежнему запрещено говорить «забронировал» и «бронь
    # подтверждена» — только «придержала время».
    auto_booking_enabled: bool = False

    # --- Перехват чата живым менеджером ----------------------------------
    #
    # Что делает агент, когда в чате появился человек. Инцидент 2026-08-27/28
    # (см. комментарий у Chat.manual_hold): менеджер писал клиенту сам, а
    # агент продолжал отвечать поверх него.
    #
    #   off        — перехват ни на что не влияет, агент отвечает всегда.
    #                Режим для отладки: включать в бою значит вернуть тот же
    #                инцидент.
    #   cooldown   — ДЕФОЛТ. Менеджер написал — агент молчит
    #                `takeover_cooldown_minutes` минут с его последнего
    #                сообщения, потом продолжает сам. Каждое новое сообщение
    #                менеджера продлевает окно.
    #   permanent  — молчит, пока чат не вернут кнопкой «Вернуть ИИ» (и
    #                суточный авто-возврат, см. app/ops/state.py).
    #
    # Решение по режиму принимается в ЕДИНСТВЕННОЙ точке фильтрации
    # исходящих (app/channels/outbound_gate.py:takeover_blocks) — там же, где
    # ручной hold, аварийный рубильник и суточный лимит. `should_agent_reply`
    # зовёт ту же функцию, чтобы не тратить токены на ход, который всё равно
    # не уйдёт, — но правило одно, а не два похожих.
    #
    # НЕ ПУТАТЬ с `Chat.manual_hold`: тот к режимам отношения не имеет,
    # никаким кулдауном не снимается и живёт до ручного /unhold.
    takeover_mode: Literal["off", "cooldown", "permanent"] = "cooldown"
    takeover_cooldown_minutes: int = 15

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_ops_chat_id: str = ""
    # NoDecode: без него pydantic-settings пытается сам разобрать это поле
    # как JSON ДО того, как отработает _split_user_ids ниже — и падает
    # SettingsError на пустой строке (`TELEGRAM_ALLOWED_USERS=` без
    # значения), а не на кривом списке. Пустая строка — совершенно рабочее
    # состояние на середине настройки .env, ронять здесь весь Settings()
    # нельзя.
    telegram_allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Infrastructure --------------------------------------------------
    database_url: str = "postgresql+asyncpg://parmangal:parmangal@localhost:5432/parmangal"
    redis_url: str = "redis://localhost:6379/0"

    env: Literal["local", "staging", "prod"] = "local"

    # Аварийный рубильник (/pause). Не в .env — переключается из Telegram.
    agent_paused: bool = False
    # После этого числа ответов чат уходит оператору насовсем.
    #
    # Читается из ДВУХ переменных: `AGENT_MAX_REPLIES_PER_CHAT` (основная) и
    # `MAX_AGENT_REPLIES_PER_CHAT` (как было раньше — она уже описана в
    # docs/RAILWAY_SETUP.md и может быть выставлена на стендах, молча
    # перестать её слушать значит незаметно вернуть лимит к 25 там, где его
    # осознанно меняли). Первое совпадение выигрывает, порядок — как в
    # AliasChoices.
    #
    # Сбросить счётчик конкретного чата, не трогая лимит: /reset <chat_id>
    # в операторском боте (app/ops/handlers.py).
    max_agent_replies_per_chat: int = Field(
        default=25,
        validation_alias=AliasChoices(
            "AGENT_MAX_REPLIES_PER_CHAT", "MAX_AGENT_REPLIES_PER_CHAT"
        ),
    )
    debounce_window_seconds: float = 10.0
    # Дневной потолок расхода на модели, рубли. Считается и срабатывает в
    # app/metrics.py:DailyCostGuard, подключён к единственной точке, через
    # которую проходит каждый платный вызов модели
    # (app/agent/loop.py:run_turn). При превышении агент уходит на паузу —
    # той же самой, что ставит оператор командой /pause, и снимается она
    # только вручную через /resume: полночь обнуляет счётчик, но не паузу.
    #
    # Дефолт НЕНУЛЕВОЙ намеренно, по тому же принципу, что у
    # OUTBOUND_DAILY_LIMIT: незаданная переменная не должна означать «лимита
    # нет». Ноль — это осознанно снятый потолок, и он пишется в стартовый
    # лог как WARNING.
    daily_cost_limit_rub: Decimal = Decimal("3000")

    # Суточный лимит СООБЩЕНИЙ (не путать с daily_cost_limit_rub — тот в
    # рублях). Считается и срабатывает в
    # app/channels/outbound_gate.py — единственной точке, через которую
    # проходят все четыре пути отправки, — а не в конвейере, чтобы
    # отложенные касания и ответы оператора не обходили его молча (тот же
    # класс бага, что уже был у белого списка объявлений). Счётчик живёт в
    # Redis и сбрасывается в полночь по Москве; при достижении в Telegram
    # уходит алерт с числом отправленных и временем — см.
    # app/channels/daily_limit.py и app/ops/notifications.py.
    #
    # Дефолт 300, а НЕ 0: `0` в app/channels/daily_limit.py:check_and_increment
    # означает «лимита нет, в Redis за ним даже не ходим», то есть забытая на
    # деплое переменная тихо снимала потолок исходящих целиком. Ровно тот же
    # класс ошибки, что уже стоил 65 сообщений: пустой AVITO_BLOCKED_ITEMS и
    # незаданный AGENT_MIN_INBOUND_TS читались так же — «не настроено» как
    # «проверка не нужна». 0 остаётся допустимым значением, но теперь это
    # осознанное «выключено», о котором app/main.py пишет WARNING при каждом
    # старте, а не молчаливый дефолт.
    outbound_daily_limit: int = 300

    # Ships ON. Turning it off means the agent writes to real clients, so it
    # must be a deliberate, explicit act — never a default.
    dry_run: bool = True

    # --- Модерация -----------------------------------------------------
    # Не в .env — переключается из Telegram (/moderation), без передеплоя.
    # DRY_RUN остаётся отдельным аварийным рубильником: пока он включён,
    # ВСЁ уходит на одобрение независимо от этой настройки — она решает,
    # что происходит в живом режиме (dry_run=False).
    #   all             — держать на одобрении всё, как раньше (откат)
    #   concessions_only — одобрение только на ценовую уступку (по умолчанию)
    #   off             — полная автономия, включая ценовые уступки
    moderation_mode: Literal["all", "concessions_only", "off"] = "concessions_only"
    # Сколько ждать реакции оператора на запрос ценовой уступки, прежде чем
    # отправить клиенту версию ответа без скидки и продолжить диалог.
    concession_approval_timeout_minutes: int = 15
    # Как часто фоновый воркер проверяет просроченные запросы на скидку.
    concession_timeout_scheduler_interval_seconds: int = 60

    # Webhook message ids are remembered this long to drop duplicates.
    webhook_idempotency_ttl_seconds: int = 24 * 60 * 60
    # Access token is refreshed this many seconds before it actually expires.
    token_expiry_safety_margin_seconds: int = 60

    # --- Отложенные касания (регламент скидок Максима) -------------------
    # ЕДИНСТВЕННЫЙ путь наружу, который пишет клиенту БЕЗ свежего входящего:
    # касание по определению уходит в молчание, через
    # `touch_reminder_delay_minutes` после ответа агента. Все остальные
    # исходящие — ответ на сообщение клиента либо действие оператора.
    # Поэтому у него отдельный выключатель, а не «поставьте TOUCH_MAX_COUNT=0»:
    # то же значение читает app/agent/tools.py в логике уступок, и ноль там
    # меняет поведение скидок, а не только напоминания.
    #
    # False — воркер не запускается вовсе. Уже взведённые таймеры остаются в
    # БД нетронутыми: включат обратно — продолжат с того же места.
    touch_enabled: bool = True
    # Молчание клиента после названной цены — через столько минут ставится
    # второе касание (мягкое напоминание), потом третье (прямой вопрос).
    touch_reminder_delay_minutes: int = 30
    # Как часто фоновый воркер проверяет диалоги с истёкшим таймером.
    touch_scheduler_interval_seconds: int = 60
    # Максимум напоминаний на диалог — дальше молчим, а не долбим клиента.
    touch_max_count: int = 3

    @field_validator("avito_blocked_items", mode="before")
    @classmethod
    def _parse_blocked_items(cls, value: object) -> object:
        """Пустое значение -> DEFAULT_BLOCKED_ITEMS, а НЕ пустой список.

        Прямая причина живого бага: `.env.example` несёт строку
        `AVITO_BLOCKED_ITEMS=` — её естественно скопировать в переменные
        Railway целиком. Пустая строка превращалась в пустой список, дефолты
        не применялись, и агент снова отвечал по вакансии и квартире-студии,
        хотя в коде они «зашиты». Снаружи это выглядело как «фильтр не
        работает», хотя фильтр работал ровно так, как его настроили.

        Ошибка настройки теперь оставляет фильтр ВКЛЮЧЁННЫМ. Выключить
        осознанно: `AVITO_BLOCKED_ITEMS=none`.
        """
        if value is None:
            return list(DEFAULT_BLOCKED_ITEMS)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return list(DEFAULT_BLOCKED_ITEMS)
            if stripped.lower() == BLOCKLIST_DISABLED:
                return []
        elif isinstance(value, (list, tuple)) and not value:
            return list(DEFAULT_BLOCKED_ITEMS)
        # Разбор строки/списка — тот же, что у белого списка. Вызываем явно,
        # а не полагаемся на второй валидатор: порядок «before»-валидаторов
        # в pydantic не тот, о котором легко думать, и «none» успевал
        # превратиться в список из одного элемента ['none'] раньше, чем
        # доходил сюда.
        return cls._split_item_ids(value)

    @field_validator("avito_allowed_items", mode="before")
    @classmethod
    def _split_item_ids(cls, value: object) -> object:
        """`123,456` из переменной окружения, либо готовый JSON-список.

        Значения остаются СТРОКАМИ, хотя в API Авито item_id — число: в
        вебхуке он приходит числом, в нашем коде везде приводится к строке
        (`extract_item_id` -> `_first_scalar`), и в БД лежит строкой
        (`Chat.item_id`, `String(128)`). Сравнение строк со строками не
        зависит от того, записал ли оператор в переменную пробел после
        запятой, — а сравнение строки с числом молча не совпало бы никогда.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                # Разбираем JSON здесь сами, а НЕ возвращаем строку наверх в
                # надежде, что её декодирует pydantic: с NoDecode он этого уже
                # не сделает, и наверх уехала бы строка вместо списка. (В
                # соседнем _split_user_ids такая ветка так и осталась
                # нерабочей — там её просто никто не использует, все стенды
                # задают переменную через запятую.)
                import json

                return [str(part).strip() for part in json.loads(stripped)]
            return [part.strip() for part in stripped.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        return value

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def _split_user_ids(cls, value: object) -> object:
        """Accept `123,456` from the environment as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return stripped
            return [int(part) for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("llm_fallback_provider", mode="before")
    @classmethod
    def _empty_fallback_means_none(cls, value: object) -> object:
        """Пустая строка из .env (`LLM_FALLBACK_PROVIDER=`) — это «не задано»,
        а не невалидное значение литерала. Тот же класс проблемы, что и с
        telegram_allowed_users выше, только для Optional[Literal]."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_webhook_secret(self) -> str:
        """Секрет в пути — единственная защита вебхука, поэтому короткий или
        пустой секрет должен ронять запуск, а не молча ослаблять защиту."""
        secret = self.avito_webhook_secret.get_secret_value()
        if len(secret) < self.avito_webhook_secret_min_length:
            raise RuntimeError(
                f"AVITO_WEBHOOK_SECRET короче {self.avito_webhook_secret_min_length} символов. "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return secret

    def require_avito_credentials(self) -> tuple[str, str]:
        """Fail loudly rather than sending an empty client_id to Avito."""
        client_id = self.avito_client_id.get_secret_value()
        client_secret = self.avito_client_secret.get_secret_value()
        if not client_id or not client_secret:
            raise RuntimeError(
                "AVITO_CLIENT_ID / AVITO_CLIENT_SECRET are not set — "
                "see .env.example"
            )
        return client_id, client_secret

    def normalized_database_url(self) -> tuple[str, dict]:
        """(url для create_async_engine, connect_args) — см. normalize_database_url."""
        return normalize_database_url(self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def normalize_database_url(raw_url: str) -> tuple[str, dict]:
    """Railway (и Heroku-совместимые платформы) отдают DATABASE_URL со схемой
    `postgres://`/`postgresql://`, а SQLAlchemy с asyncpg требует
    `postgresql+asyncpg://` — без этого создание движка падает на схеме до
    единого запроса к базе.

    Платформа может дополнительно дописать `sslmode=require` в query —
    libpq-параметр, которого asyncpg не понимает как часть DSN: он падает
    с `TypeError`/`invalid connection option "sslmode"` при первом же
    подключении, а не при старте, так что без явной нормализации приложение
    поднимается и тут же перестаёт отвечать на первый запрос к БД. Здесь
    `sslmode` вырезается из строки подключения и переносится в
    `connect_args={"ssl": True}` — так, как ждёт asyncpg.

    Это самая частая причина, по которой приложение не поднимается на
    Railway с первого раза — см. docs/RAILWAY_SETUP.md, п.8.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(raw_url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    connect_args: dict = {}
    remaining_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            if value not in ("disable", ""):
                connect_args["ssl"] = True
        else:
            remaining_pairs.append((key, value))

    new_query = urlencode(remaining_pairs)
    normalized = urlunsplit((scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return normalized, connect_args
