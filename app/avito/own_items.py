"""Идентификаторы объявлений аккаунта — гуард поллера «свой ли это чат».

ЗАЧЕМ. Вебхук приходил только по тем чатам, которые Авито считал нашими.
Поллер видит ВЕСЬ ящик, а в ящике заказчика лежит и то, где он сам
покупатель: «Репетитор по физике ЕГЭ», «Покос травы триммером», «Подставка
для чайника Tefal KI270», «Авитолог за % со сделок». Живьём — 33 таких чата
на 1100. Ни одного из этих объявлений нет в чёрном списке, потому что чёрный
список составлялся под ЧУЖИЕ ДЛЯ КОМПЛЕКСА объявления ЗАКАЗЧИКА, а не под
чужие вовсе. Без гуарда агент ответил бы репетитору прайсом на баню.

Плюс 109 чатов, у которых контекст объявления есть, а `value.id` равен нулю:
`extract_item_id_from_chat` отдаёт для них строку "0" — не None, — и общий
фильтр видит обычное разрешённое объявление. Их закрывает та же проверка:
нуля среди объявлений аккаунта нет.

ОБА КЛАССА ТЕПЕРЬ ЗАКРЫТЫ И В `is_listing_allowed` — item_scope
(app/channels/item_scope.py:ItemScopeResolver) денит и item_id == "0", и
объявления не нашего аккаунта, той же логикой, что и здесь. Этот гуард
остаётся в поллере не как единственная защита, а как оптимизация чтения:
без него поллер продолжал бы вычитывать полную историю сообщений для всех
33+109 чужих/нулевых чатов на каждом проходе (item_scope денит ответ, но
не экономит сам запрос за сообщениями) — гуард отсекает их ДО этого,
пометив курсор `not_our_listing`, и не стоит ни одного лишнего запроса к
Авито после первого прохода.

Снимок обновляется раз в час и ПЕРЕЖИВАЕТ СБОЙ ОБНОВЛЕНИЯ: у GET /core/v1/items
лимит 25 запросов в минуту, и разовая неудача не должна оборачиваться
проходом, который либо молчит целиком, либо (что хуже) пропускает всех.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("parmangal.poller.items")


class OwnItemIds:
    """Кеш идентификаторов объявлений аккаунта с мягкой деградацией."""

    def __init__(
        self,
        items_client: Any,
        settings: Any,
        monotonic=time.monotonic,
    ):
        self._client = items_client
        self._settings = settings
        self._monotonic = monotonic
        self._snapshot: Optional[set[str]] = None
        self._loaded_at: float = 0.0

    async def __call__(self) -> set[str]:
        """Актуальный набор. Бросает только если снимка нет ВООБЩЕ.

        Исключение здесь означает «проверить принадлежность нечем», и поллер
        обязан свернуть проход, а не пропустить всех. Пока есть хоть какой-то
        прошлый снимок, устаревший на час-другой, он лучше отсутствия:
        объявления не появляются ежеминутно.
        """
        fresh_enough = (
            self._snapshot is not None
            and self._monotonic() - self._loaded_at
            < self._settings.poller_items_refresh_seconds
        )
        if fresh_enough:
            return self._snapshot  # type: ignore[return-value]

        try:
            listings = await self._client.list_all_items(
                status=self._settings.poller_items_statuses
            )
        except Exception:
            if self._snapshot is not None:
                logger.warning(
                    "poller: список объявлений не обновился, работаем по снимку "
                    "%d-часовой давности (%d объявлений)",
                    int((self._monotonic() - self._loaded_at) // 3600),
                    len(self._snapshot),
                )
                return self._snapshot
            raise

        # str() на границе: в API Авито item_id — число, у нас везде строка,
        # и сравнение строки с числом молча не совпадает никогда.
        self._snapshot = {str(listing.item_id) for listing in listings}
        self._loaded_at = self._monotonic()
        logger.info(
            "poller: объявлений аккаунта в гуарде: %d (статусы: %s)",
            len(self._snapshot), self._settings.poller_items_statuses,
        )
        return self._snapshot
