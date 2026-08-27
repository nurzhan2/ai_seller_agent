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

    async def is_allowed(self, chat_id: str) -> bool:
        """Можно ли писать в этот чат. Пустой список — можно всё.

        Ошибка поиска item_id — это ЗАПРЕТ, а не разрешение: список задан,
        значит оператор явно перечислил, по каким объявлениям агент имеет
        право писать, и «мы не смогли проверить» не должно превращаться в
        «значит, отправляем». Молчание дешевле ответа не тому человеку.
        """
        allowed = getattr(self._settings, "avito_allowed_items", None) or []
        if not allowed:
            return True

        if self._item_id_lookup is None:
            logger.warning(
                "outbound: список объявлений задан, но искать item_id нечем — "
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

        if item_id is None:
            logger.info(
                "outbound: заблокировано — item_id чата неизвестен, а список "
                "разрешённых объявлений задан (%d шт.)",
                len(allowed), extra={"chat_id": chat_id},
            )
            return False

        if item_id not in allowed:
            logger.info(
                "outbound: заблокировано — объявление %s не в списке разрешённых (%d шт.)",
                item_id, len(allowed), extra={"chat_id": chat_id, "item_id": item_id},
            )
            return False

        return True

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
