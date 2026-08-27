"""Единственная граница, через которую уходит сообщение живому клиенту.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ СЛОЙ, А НЕ ПРОВЕРКА В КАЖДОЙ ВЕТКЕ. Белый список
объявлений (`AVITO_ALLOWED_ITEMS`) сначала жил только в конвейере — и
этого оказалось недостаточно: в 09:00 воркер отложенных касаний отправил
третье касание в чат `u2u-…`, а в 12:09 конвейер тот же чат заблокировал.
Проверка стояла на одном входе, а выходов у системы четыре:

  * автономный ответ агента            (app/pipeline.py:_deliver)
  * запасной ответ по таймауту уступки (app/pipeline.py:check_concession_timeouts)
  * отложенное касание                 (app/main.py:build_touch_sender)
  * ответ, одобренный оператором       (app/ops/bot.py:OpsService.approve/edit)

Тот же приём, что и `app/pricing/quote_gate.py` для цен: инвариант,
который обязан выполняться ВЕЗДЕ, проверяется на границе, а не
переписывается в каждом вызывающем месте — иначе следующий новый путь
отправки снова про него забудет, и узнаем мы об этом опять из логов.

Гейт СОЗНАТЕЛЬНО обёртка над `AvitoClient`, а не правка внутри него:
`AvitoClient` — транспорт (токены, ретраи, лимиты), а «кому мы вообще
имеем право писать» — правило бизнеса. Снаружи гейт неотличим от клиента
(те же имена методов), поэтому вызывающий код не знает о его
существовании и не может его обойти по невнимательности — ему просто
передают гейт вместо клиента.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("parmangal.outbound")

# chat_id -> item_id объявления (или None, если чат не по объявлению).
ItemIdLookup = Callable[[str], Awaitable[Optional[str]]]


def is_listing_allowed(item_id: Optional[str], settings: Any) -> bool:
    """Единственное определение правила «по этому объявлению можно писать».

    Одна функция на оба места, где правило применяется: на входе в конвейер
    (чтобы не заводить диалог по чужому объявлению) и на границе отправки
    (`OutboundGate`, чтобы ни один путь не написал клиенту мимо проверки).
    Раньше это была скопированная логика в двух местах — так и разъезжаются
    инварианты.

    Порядок разбора:

    1. Нет item_id (обращение из профиля, chat_type u2u/a2u — объявления у
       такого чата нет по спеку Авито). Решает
       `AVITO_ALLOW_CHATS_WITHOUT_ITEM`; по умолчанию отвечаем — это живой
       клиент, и молчание хуже ответа.
    2. Задан белый список `AVITO_ALLOWED_ITEMS` — он В ПРИОРИТЕТЕ и работает
       как раньше: разрешено только перечисленное. Оставлен ради стендов,
       где он уже выставлен, и как аварийный режим «пускать только вот эти».
    3. Иначе — чёрный список `AVITO_BLOCKED_ITEMS`: запрещено только
       перечисленное, всё остальное (включая новые объявления комплекса)
       работает сразу.
    """
    if item_id is None:
        return bool(getattr(settings, "avito_allow_chats_without_item", True))

    # item_id приводится к строке ЗДЕСЬ, а не только у вызывающих. В API
    # Авито это число (спек: "item_id": {"type": "integer"}), у нас везде
    # строка — и сравнение строки с числом не совпадает молча, то есть
    # заблокированное объявление тихо становится разрешённым. Сейчас все
    # вызывающие передают строку (`extract_item_id` -> `_first_scalar` и
    # колонка `Chat.item_id`), но полагаться на это по всей цепочке —
    # ровно тот вид допущения, который однажды перестаёт выполняться.
    item_id = str(item_id)
    allowed = [str(i) for i in (getattr(settings, "avito_allowed_items", None) or [])]
    if allowed:
        return item_id in allowed

    blocked = [str(i) for i in (getattr(settings, "avito_blocked_items", None) or [])]
    return item_id not in blocked


class ListingNotAllowed(RuntimeError):
    """Попытка написать в чат по объявлению вне белого списка.

    Исключение, а не тихий `{"blocked": True}` в ответе, СОЗНАТЕЛЬНО.
    Вызывающий код (app/pipeline.py:_deliver) после успешной отправки
    пишет `SendStatus.sent` — то есть на молчаливый отказ он записал бы в
    базу «отправлено» для сообщения, которого клиент никогда не получал, и
    заметить это стало бы нечем. Исключение попадает в уже существующие
    `except` вокруг отправки: сообщение честно помечается недоставленным,
    а в логе видно, почему.

    Штатные пути (конвейер на входе, воркер касаний через `can_send`)
    отсекают такие чаты РАНЬШЕ, поэтому сюда долетает только то, что
    просочилось мимо них — то есть ошибка в коде, и она должна быть
    шумной, а не тихой.
    """



class OutboundGate:
    """Пропускает наружу только сообщения по разрешённым объявлениям."""

    def __init__(
        self,
        client: Any,
        settings: Any,
        item_id_lookup: Optional[ItemIdLookup] = None,
    ):
        self._client = client
        self._settings = settings
        self._item_id_lookup = item_id_lookup

    # -- решение -----------------------------------------------------------

    def _filter_is_off(self) -> bool:
        """Ни чёрного списка, ни белого, и чаты без объявления разрешены —
        фильтровать нечего, и ходить в базу за item_id незачем."""
        return (
            not (getattr(self._settings, "avito_allowed_items", None) or [])
            and not (getattr(self._settings, "avito_blocked_items", None) or [])
            and bool(getattr(self._settings, "avito_allow_chats_without_item", True))
        )

    async def is_allowed(self, chat_id: str) -> bool:
        """Можно ли писать в этот чат — по правилу `is_listing_allowed`.

        Ошибка поиска item_id — это ЗАПРЕТ, а не разрешение. Чат без
        объявления и чат, про который мы НЕ СМОГЛИ УЗНАТЬ, есть ли у него
        объявление, — разные вещи: первое штатно разрешено
        (`AVITO_ALLOW_CHATS_WITHOUT_ITEM`), второе означает, что проверка не
        отработала, и подменять её результат догадкой нельзя.
        """
        if self._filter_is_off():
            return True

        if self._item_id_lookup is None:
            logger.warning(
                "outbound: фильтр объявлений включён, но искать item_id нечем — "
                "отправка заблокирована",
                extra={"chat_id": chat_id},
            )
            return False

        try:
            item_id = await self._item_id_lookup(chat_id)
        except Exception:
            logger.exception(
                "outbound: не удалось определить item_id чата — отправка заблокирована",
                extra={"chat_id": chat_id},
            )
            return False

        if is_listing_allowed(item_id, self._settings):
            return True

        logger.info(
            "outbound: заблокировано — объявление %s под запретом",
            item_id if item_id is not None else "(чат без объявления)",
            extra={"chat_id": chat_id, "item_id": item_id},
        )
        return False

    # -- отправка ----------------------------------------------------------

    async def _require_allowed(self, chat_id: str) -> None:
        if not await self.is_allowed(chat_id):
            raise ListingNotAllowed(
                f"чат {chat_id} не проходит белый список объявлений — отправка отменена"
            )

    async def send_message(self, chat_id: str, text: str) -> dict:
        await self._require_allowed(chat_id)
        return await self._client.send_message(chat_id, text)

    async def send_image(self, chat_id: str, image_id: str) -> dict:
        await self._require_allowed(chat_id)
        return await self._client.send_image(chat_id, image_id)

    async def upload_and_send_image(self, chat_id: str, image_bytes: bytes, **kwargs) -> dict:
        # Проверка ДО загрузки картинки: не только не отправить, но и не
        # тратить трафик и лимиты Авито на чат, куда всё равно нельзя.
        await self._require_allowed(chat_id)
        return await self._client.upload_and_send_image(chat_id, image_bytes, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Всё остальное (чтение чатов, токены, aclose) — как у клиента.

        Намеренно только для того, что НЕ пишет клиенту: методы отправки
        перечислены выше явно и сюда не попадают. Если в `AvitoClient`
        когда-нибудь появится новый метод отправки, он проскочит через
        `__getattr__` без проверки — поэтому в tests/test_outbound_gate.py
        есть тест, который следит за списком методов клиента и падает,
        когда появляется незакрытый.
        """
        return getattr(self._client, name)
