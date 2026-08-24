"""Соответствие зон комплекса и услуг YCLIENTS.

Таблица правится через админку (/admin/catalog), а не в коде: каталог услуг у
заказчика ещё наполняется, и зоны будут появляться по одной.

Правило деградации: НЕТ МАППИНГА → UNKNOWN, а не падение и не догадка.
Отсутствующая строка означает ровно «мы не знаем, занято ли», и агент обязан
эскалировать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class ZoneServiceMap(Base):
    __tablename__ = "zone_service_map"

    # BigInteger().with_variant(Integer, "sqlite"): в Postgres — обычный
    # BIGSERIAL, без изменений. Под SQLite (только в тестах, см.
    # tests/test_zone_mapping.py) SQLAlchemy рендерит голый BigInteger как
    # `BIGINT`, а не `INTEGER` — SQLite включает автоинкремент через ROWID
    # только для колонки, объявленной ИМЕННО как `INTEGER PRIMARY KEY`, без
    # этой замены INSERT без явного id падает на NOT NULL. Влияет только на
    # то, как колонка компилируется под sqlite-диалект — на реальную схему
    # в Postgres/на миграции никак не сказывается.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    zone_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    service_id: Mapped[Optional[str]] = mapped_column(String(64))
    staff_id: Mapped[Optional[str]] = mapped_column(String(64))
    company_id: Mapped[Optional[str]] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(Text)


@dataclass
class InMemoryZoneMapping:
    """Реализация для тестов и для работы до заполнения каталога."""

    rows: dict[str, dict] = field(default_factory=dict)

    def get(self, zone_id: str) -> Optional[dict]:
        row = self.rows.get(zone_id)
        if row and row.get("enabled", True):
            return row
        return None

    def set(self, zone_id: str, **values) -> None:
        self.rows[zone_id] = {"enabled": True, **values}

    def mapped_zones(self) -> list[str]:
        return sorted(z for z, r in self.rows.items() if r.get("enabled", True))

    def unmapped_zones(self, all_zone_ids: list[str]) -> list[str]:
        mapped = set(self.mapped_zones())
        return sorted(z for z in all_zone_ids if z not in mapped)


class SqlAlchemyZoneMapping:
    """Тот же интерфейс, что и `InMemoryZoneMapping`, но поверх реальной
    таблицы `ZoneServiceMap` — правки через админку доезжают до живого
    `YClientsProvider`, а не теряются в отдельном процессе от БД.

    `get`/`mapped_zones`/`unmapped_zones` НАМЕРЕННО остаются синхронными
    (как у `InMemoryZoneMapping`) и читают только предзагруженный кеш в
    памяти — `check_availability` дёргается на каждом ходу диалога, и поход
    в базу на каждый вызов добавил бы сетевой round-trip в горячий путь
    живой переписки без всякой пользы (маппинг зона→услуга меняется руками
    и редко). Кеш заполняется явным `await load()` (см. app/main.py
    lifespan) и обновляется целиком заново при любой записи через `set()` —
    не по TTL: устаревание тут не грозит, а TTL добавил бы задержку без
    выгоды. Если `load()` ни разу не вызван, `get()` просто не находит
    ничего — то же безопасное вырождение в «не заведено», что и у пустого
    `InMemoryZoneMapping`, никогда не падение.

    `set()` — единственное отличие от `InMemoryZoneMapping`: там он
    синхронный (пишет в dict в памяти), здесь обязан быть `async` (пишет в
    БД). Сегодня у этого нет ни одного живого вызывающего кода — панель
    /admin/catalog пока только ЧИТАЕТ маппинг, формы редактирования нет
    (см. README → «Известные пробелы») — но когда она появится, это будет
    обычный async-обработчик FastAPI, которому `await` не в тягость.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._cache: dict[str, dict] = {}

    async def load(self) -> None:
        """Полная перезагрузка кеша из БД. Вызывать при старте процесса и
        после любой правки в обход `set()` (например, ручным SQL)."""
        from sqlalchemy import select

        async with self._session_factory() as session:
            rows = (await session.execute(select(ZoneServiceMap))).scalars().all()

        self._cache = {
            row.zone_id: {
                "service_id": row.service_id,
                "staff_id": row.staff_id,
                "company_id": row.company_id,
                "enabled": row.enabled,
                "note": row.note,
            }
            for row in rows
        }

    def invalidate(self) -> None:
        """Сбрасывает кеш до пустого — следующий `get()` увидит «не
        заведено», пока кто-нибудь не вызовет `load()` заново. `set()` сам
        обновляет кеш точечно и `invalidate()` не требует; этот метод — для
        внешних правок в обход адаптера."""
        self._cache = {}

    def get(self, zone_id: str) -> Optional[dict]:
        row = self._cache.get(zone_id)
        if row and row.get("enabled", True):
            return row
        return None

    async def set(self, zone_id: str, **values) -> None:
        """Upsert в БД + точечное обновление кеша (не полная перезагрузка —
        одна запись не обязана платить round-trip'ом за все остальные)."""
        from sqlalchemy import select

        async with self._session_factory() as session:
            existing = (
                await session.execute(select(ZoneServiceMap).where(ZoneServiceMap.zone_id == zone_id))
            ).scalar_one_or_none()
            if existing is None:
                existing = ZoneServiceMap(zone_id=zone_id, enabled=True)
                session.add(existing)
            for key, value in values.items():
                setattr(existing, key, value)
            if "enabled" not in values:
                existing.enabled = True
            await session.commit()
            row = {
                "service_id": existing.service_id,
                "staff_id": existing.staff_id,
                "company_id": existing.company_id,
                "enabled": existing.enabled,
                "note": existing.note,
            }

        self._cache[zone_id] = row

    def mapped_zones(self) -> list[str]:
        return sorted(z for z, r in self._cache.items() if r.get("enabled", True))

    def unmapped_zones(self, all_zone_ids: list[str]) -> list[str]:
        mapped = set(self.mapped_zones())
        return sorted(z for z in all_zone_ids if z not in mapped)


def coverage_report(mapping: InMemoryZoneMapping, all_zone_ids: list[str]) -> dict:
    """Что из каталога заказчика заведено в YCLIENTS, а что нет.

    Заказчик просил не «чинить» неполный каталог, а показывать его состояние —
    этот отчёт и есть ответ на вопрос «какие зоны там есть».
    """
    mapped = mapping.mapped_zones()
    unmapped = mapping.unmapped_zones(all_zone_ids)
    return {
        "total_zones": len(all_zone_ids),
        "mapped": mapped,
        "unmapped": unmapped,
        "coverage": round(len(mapped) / len(all_zone_ids), 3) if all_zone_ids else 0.0,
        "note": (
            "Зоны из «unmapped» агент не может проверить на занятость: "
            "по ним check_availability вернёт unknown и уйдёт к менеджеру."
        ),
    }
