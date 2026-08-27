"""Разрешение зоны по объявлению, из которого пришло сообщение клиента.

Правило, которое это реализует (Часть 3 промта №11): при неоднозначном
объявлении агент не гадает и не вываливает весь каталог, а уточняет ОДНИМ
вопросом с конкретными вариантами — «Вы про какую баню, «Русский стиль»,
«Гараж» или «Рыцарскую»?». На зону, которой вообще нет в объявлениях,
агент даёт ссылку на сайт (если она известна — см. вопрос 14.7).

Три исхода:
  resolved  — объявление однозначно ведёт на одну зону, вопрос не нужен.
  ambiguous — объявление ведёт на категорию (например, «баня»), но не на
              конкретную зону внутри неё — нужен один уточняющий вопрос.
  unknown   — нет ни одной зацепки (нет маппинга, нет item_id вообще) —
              агент действует по тексту клиента как обычно, без подсказки.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from app.kb.loader import KnowledgeBase


@dataclass(frozen=True)
class ItemZoneRow:
    zone_id: Optional[str] = None
    category: Optional[str] = None


class ItemZoneLookup(Protocol):
    async def get(self, item_id: str) -> Optional[ItemZoneRow]: ...


@dataclass(frozen=True)
class ListingResolution:
    status: str  # "resolved" | "ambiguous" | "unknown"
    zone_id: Optional[str] = None
    candidate_zone_ids: tuple[str, ...] = ()
    category: Optional[str] = None


async def resolve_listing(
    item_id: Optional[str],
    lookup: Optional[ItemZoneLookup],
    kb: KnowledgeBase,
) -> ListingResolution:
    if item_id is None or lookup is None:
        return ListingResolution(status="unknown")

    row = await lookup.get(item_id)
    if row is None:
        return ListingResolution(status="unknown")

    if row.zone_id:
        return ListingResolution(status="resolved", zone_id=row.zone_id)

    if row.category:
        candidates = tuple(
            z.id for z in kb.catalog.zones if z.category.value == row.category
        )
        if len(candidates) == 1:
            return ListingResolution(status="resolved", zone_id=candidates[0])
        if candidates:
            return ListingResolution(
                status="ambiguous", candidate_zone_ids=candidates, category=row.category
            )

    return ListingResolution(status="unknown")


def build_listing_hint(resolution: ListingResolution, kb: KnowledgeBase) -> Optional[str]:
    """Служебная подсказка для модели — НЕ текст клиенту. Вызывающий код
    (AgentLoop.run_turn) добавляет её к содержимому хода, а не к системному
    промту: контекст меняется от сообщения к сообщению, а кешируемый блок
    системного промта (справочник зон) должен оставаться неизменным байт в
    байт, иначе кеш будет промахиваться на каждом ходу."""
    if resolution.status == "resolved" and resolution.zone_id:
        zone = next((z for z in kb.catalog.zones if z.id == resolution.zone_id), None)
        if zone is None:
            return None
        return (
            f"[Служебно: сообщение пришло с объявления зоны «{zone.name}» "
            f"({zone.id}) — не переспрашивай, о какой зоне речь, если клиент "
            "сам не уточнит другое.]"
        )

    if resolution.status == "ambiguous":
        names = [z.name for z in kb.catalog.zones if z.id in resolution.candidate_zone_ids]
        listed = ", ".join(f"«{n}»" for n in names)
        return (
            "[Служебно: объявление, с которого пришёл клиент, общее на несколько "
            f"зон. Кандидаты: {listed}. Задай ОДИН уточняющий вопрос с этими "
            "вариантами — не описывай весь каталог и не гадай.]"
        )

    return None   # unknown — агенту нечего подсказать, работает по тексту клиента


def no_listing_hint() -> str:
    """Подсказка для чата, пришедшего НЕ с объявления.

    Обращения из профиля продавца (chat_type u2u/a2u) приходят без item_id —
    объявления у такого чата нет по спеку Авито, значит нет и обычной
    зацепки «клиент пришёл вот с этой зоны». Раньше такие чаты просто
    блокировались; теперь агент отвечает, но первым сообщением выясняет
    направление, иначе он либо гадает, либо вываливает весь каталог.

    Формулировка задана заказчиком дословно — не переписывать «покрасивее»:
    четыре направления в ней перечислены ровно те, что он готов продавать
    из профиля.
    """
    return (
        "[Служебно: клиент написал не с объявления, а из профиля — какая "
        "зона его интересует, неизвестно. Это ПЕРВОЕ сообщение диалога: "
        "поздоровайся и уточни направление примерно так — «Здравствуйте! "
        "Подскажите, что вас интересует: баня, купол, гриль-домик или "
        "шатёр?». Не перечисляй весь каталог и не угадывай зону сам.]"
    )


def site_fallback_hint(kb: KnowledgeBase) -> str:
    """Что сказать модели про зоны, которых нет ни в одном объявлении."""
    site_url = kb.catalog.site_url
    if site_url is not None and site_url.is_resolved():
        return (
            "[Служебно: для зон, которых нет в объявлениях Авито, "
            f"давай клиенту ссылку на сайт: {site_url.value}]"
        )
    return (
        "[Служебно: ссылка на сайт комплекса не подтверждена (вопрос 14.7). "
        "Пока её нет, на просьбу прислать ссылку по зоне вне объявлений отвечай, "
        "что уточнишь у менеджера, и вызывай escalate_to_human — не придумывай адрес.]"
    )
