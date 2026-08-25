"""Правка каталога: провалидировать, сохранить, перезагрузить, показать эффект.

Здесь сходятся все части: белый список полей (`app/kb/editable.py`), слой
правок (`app/kb/overrides.py`), хранилище (`app/kb/override_store.py`) и
загрузчик. Telegram-хендлеры (`app/ops/handlers.py`) вызывают только
методы отсюда — в них не должно быть ни валидации, ни SQL.

ПОРЯДОК ОПЕРАЦИЙ ВАЖЕН: сначала собираем НОВЫЙ документ целиком и гоняем
его через ту же валидацию, что проходит YAML при старте, и только потом
пишем в БД. Обратный порядок означал бы, что невалидная правка успевает
сохраниться и уронить приложение на следующем рестарте — когда чинить её
будет уже некому и нечем (бот к тому моменту не поднимется).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as DateType, time as TimeType, timedelta
from typing import Any, Optional

from app.kb.editable import EditableField, build_override_value, field_by_key
from app.kb.loader import KnowledgeBase, read_raw_kb, validate_raw
from app.kb.override_store import OverrideRecord, OverrideStore, to_overrides
from app.kb.overrides import Override, OverrideError, apply_overrides, get_at

logger = logging.getLogger("parmangal.kb.editor")


@dataclass(frozen=True)
class EditPreview:
    """«Было X, станет Y» — то, что показывается оператору ДО сохранения."""

    field: EditableField
    zone_id: Optional[str]
    path: str
    previous_value: Any
    new_value: Any
    previous_human: str
    new_human: str


@dataclass(frozen=True)
class EditResult:
    record: OverrideRecord
    kb: KnowledgeBase
    # Пересчитанный пример цены с новым значением (пункт 4 задачи) — или
    # None, если для этой зоны пример построить не удалось.
    price_example: Optional[str]


def human_value(value: Any) -> str:
    """Значение из документа в вид, понятный человеку в чате."""
    if isinstance(value, dict):
        if value.get("value") is not None:
            return human_value(value["value"])
        if value.get("disputed") is not None:
            return "не задано (спорное поле)"
        return "не задано"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "пусто"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


class CatalogEditor:
    def __init__(self, store: OverrideStore, kb_dir=None):
        self.store = store
        self._kb_dir = kb_dir

    # -- чтение -------------------------------------------------------------

    async def current_kb(self) -> KnowledgeBase:
        records = await self.store.list_active()
        return self._build(to_overrides(records))

    def _build(self, overrides: list[Override]) -> KnowledgeBase:
        raw = apply_overrides(read_raw_kb(self._kb_dir), overrides)
        return validate_raw(raw)

    def _raw_with_active(self, records: list[OverrideRecord]) -> dict:
        return apply_overrides(read_raw_kb(self._kb_dir), to_overrides(records))

    async def current_value(self, field: EditableField, zone_id: Optional[str] = None) -> Any:
        raw = self._raw_with_active(await self.store.list_active())
        return get_at(raw, self._path_for(field, zone_id))

    @staticmethod
    def _path_for(field: EditableField, zone_id: Optional[str]) -> str:
        if "{zone}" in field.path_template:
            if not zone_id:
                raise OverrideError(f"Поле «{field.label}» требует указания зоны.")
            return field.path_template.format(zone=zone_id)
        return field.path_template

    # -- правка -------------------------------------------------------------

    async def preview(
        self, field_key: str, raw_input: str, user_id: int, zone_id: Optional[str] = None
    ) -> EditPreview:
        """Разобрать ввод, проверить И ПРИМЕНИТЬ на копии документа — но не
        сохранять. Валидация здесь, а не при подтверждении: оператор должен
        увидеть отказ сразу после ввода, а не после лишнего нажатия."""
        field = field_by_key(field_key)
        parsed = field.parse(raw_input)          # доменные проверки
        path = self._path_for(field, zone_id)
        new_value = build_override_value(field, parsed, user_id)

        records = await self.store.list_active()
        raw = self._raw_with_active(records)
        previous = get_at(raw, path)

        # Полная проверка НОВОГО документа — та же, что при старте.
        candidate = apply_overrides(raw, [Override(path=path, value=new_value)])
        try:
            validate_raw(candidate)
        except Exception as exc:
            raise OverrideError(
                f"Значение не прошло проверку базы знаний и не сохранено.\n\n{exc}"
            ) from exc

        return EditPreview(
            field=field, zone_id=zone_id, path=path,
            previous_value=previous, new_value=new_value,
            previous_human=human_value(previous), new_human=human_value(new_value),
        )

    async def apply(self, preview: EditPreview, user_id: int, comment: Optional[str] = None) -> EditResult:
        """Сохранить уже проверенную правку.

        Валидируем ВТОРОЙ раз, уже после записи: между `preview` и
        подтверждением могла проехать чужая правка (второй оператор,
        соседний чат), и вместе они могут дать документ, невалидный целиком,
        хотя каждая по отдельности была в порядке. Если так — откатываем
        свою же строку и честно говорим об этом, а не оставляем базу
        сломанной до следующего рестарта.
        """
        record = await self.store.add(
            path=preview.path, value=preview.new_value,
            previous_value=preview.previous_value,
            field_key=preview.field.key, zone_id=preview.zone_id,
            changed_by=user_id, comment=comment,
        )
        try:
            kb = await self.current_kb()
        except Exception as exc:
            await self.store.revert(record.id, reverted_by=user_id)
            raise OverrideError(
                "Правка не сохранена: вместе с другими изменениями каталог "
                f"перестал быть валидным.\n\n{exc}"
            ) from exc

        logger.info(
            "catalog override applied",
            extra={"path": preview.path, "changed_by": user_id, "override_id": record.id},
        )
        return EditResult(record=record, kb=kb, price_example=price_example(kb, preview.zone_id))

    async def revert_last(self, user_id: int) -> Optional[EditResult]:
        """Откат последней действующей правки. Строка не удаляется —
        помечается `reverted_at`: журнал обязан помнить и саму правку, и то,
        что её отменили."""
        last = await self.store.last_active()
        if last is None:
            return None
        reverted = await self.store.revert(last.id, reverted_by=user_id)
        if reverted is None:
            return None
        kb = await self.current_kb()
        logger.info(
            "catalog override reverted",
            extra={"override_id": last.id, "reverted_by": user_id},
        )
        return EditResult(record=reverted, kb=kb, price_example=price_example(kb, last.zone_id))


# --------------------------------------------------------------------------
# Пересчёт примера цены (пункт 4)
# --------------------------------------------------------------------------

def _next_weekend(today: Optional[DateType] = None) -> DateType:
    day = today or DateType.today()
    # 5 = суббота. Ищем ближайшую будущую субботу, чтобы пример не
    # приходился на сегодня — цена «на сегодня» может быть заблокирована
    # праздником и увести пример в blocked без всякой связи с правкой.
    ahead = (5 - day.weekday()) % 7 or 7
    return day + timedelta(days=ahead)


def price_example(kb: KnowledgeBase, zone_id: Optional[str], today: Optional[DateType] = None) -> Optional[str]:
    """«Купол, суббота, 4 часа = 6000 ₽» — чтобы оператор увидел последствия
    правки сразу, а не узнал о них от клиента.

    Возвращает None, если пример построить нельзя (нет зоны, расчёт
    заблокирован спорным полем) — молчание честнее выдуманного числа.
    """
    from app.pricing.engine import PriceRequest, quote

    if not zone_id:
        return None
    zone = next((z for z in kb.catalog.zones if z.id == zone_id), None)
    if zone is None:
        return None

    saturday = _next_weekend(today)
    hours = 4
    guests = zone.capacity.value if zone.capacity.is_resolved() else 4
    request = PriceRequest(
        zone_id=zone_id, date=saturday, start_time=TimeType(14, 0),
        hours=hours, guests=guests,
    )
    result = quote(request, kb)
    if result.status != "ok" or result.total is None:
        return (
            f"{zone.name}, суббота, {hours} ч — рассчитать не удалось "
            f"(статус «{result.status}»). Это не обязательно ошибка правки: "
            "у зоны может быть незаполненное поле."
        )
    return f"{zone.name}, суббота {saturday.strftime('%d.%m')}, {hours} ч = {result.total} ₽"
