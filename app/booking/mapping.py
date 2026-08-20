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

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class ZoneServiceMap(Base):
    __tablename__ = "zone_service_map"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
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
