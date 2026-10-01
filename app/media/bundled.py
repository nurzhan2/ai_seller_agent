"""Фото зон, вшитые в образ: app/media/bundled/<image_id>.jpg.

Живой случай 2026-09-30 (жалоба заказчика «фото не открываются»): image_id,
загруженные в Авито один раз 18.09 (photo_import), через ~10 дней Авито
удалил — URL картинок в чате отдают 404, клиент видит серую заглушку.
Повторно использовать загруженный id нельзя: каждое фото перед отправкой
загружается заново. image_id из каталога остаётся ключом — по нему
считается «уже показано» и ищется файл.

Файлы — сжатые (1280px, JPEG q82) копии media/photos/<зона>/*, имя = id
из .photos_manifest.json. Пересобрать: scripts/build_bundled_photos.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

BUNDLED_DIR = Path(__file__).resolve().parent / "bundled"
_SAFE_ID = re.compile(r"^[0-9A-Za-z._-]+$")


def bundled_photo_path(image_id: str) -> Optional[Path]:
    if not image_id or not _SAFE_ID.match(image_id) or ".." in image_id:
        return None
    path = BUNDLED_DIR / f"{image_id}.jpg"
    return path if path.is_file() else None
