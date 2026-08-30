"""Источник фотографий зоны для инструмента агента `get_photos`.

Единственная реализация читает базу знаний: `catalog.yaml → zone.photos` —
список `image_id` Авито, который туда пишет `scripts/import_photos.py` после
загрузки файлов (см. app/media/photo_import.py). Своего хранилища у провайдера
нет и не должно быть: id выдаёт Авито, и второй список тех же id рядом с
каталогом разошёлся бы с ним на первой же перезагрузке фотографий.

ПУСТОЙ СПИСОК — ШТАТНОЕ СОСТОЯНИЕ, а не сбой. На 2026-08-30 у всех десяти зон
`photos: []`: файлы распакованы в `media/photos/<zone_id>/`, но
`scripts/import_photos.py` ни разу не запускался, поэтому в Авито они не
загружены и id у них нет. Инструмент на пустой список отвечает тем же
честным «фотографий нет», что и при полностью отсутствующем провайдере, —
агент не должен обещать клиенту то, чего не придёт.

Отправка изображений в чат — отдельная, ещё не подключённая история
(`AvitoClient.send_image` существует, но между ним и агентом ничего нет).
Провайдер отдаёт только id.
"""

from __future__ import annotations

from typing import Any, Callable

from app.kb.loader import KnowledgeBase


class KbPhotoProvider:
    """`get(zone_id) -> list[str]` поверх базы знаний.

    База берётся КОЛБЭКОМ, а не значением: оператор правит каталог из
    Telegram (app/ops/menu_service.py), после чего `app.state.kb` заменяется
    целиком. Провайдер, захвативший старый объект, продолжал бы отдавать
    фотографии из версии каталога на момент старта процесса.
    """

    def __init__(self, kb_getter: Callable[[], KnowledgeBase]):
        self._kb_getter = kb_getter

    async def get(self, zone_id: str) -> list[str]:
        kb: Any = self._kb_getter()
        for zone in kb.catalog.zones:
            if zone.id == zone_id:
                return list(zone.photos)
        return []
