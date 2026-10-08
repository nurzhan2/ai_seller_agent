"""Ежедневная самопроверка — отчёт в Telegram раньше, чем заметит заказчик.

ЗАЧЕМ. Поломки сентября–октября 2026 были не багами логики, а тихими
изменениями снаружи: временные цены из бот-меню, забытые на три недели;
переименованные объявления (беседки-купола продавались как гриль-домик);
удалённый из YCLIENTS ресурс (юрта); удалённые Авито фото; упавшие
деплои. Каждую нашла Валерия, а не мы. Самопроверка ловит именно такие
изменения и раз в сутки шлёт оператору одну сводку: «всё в порядке» —
одной строкой, иначе — что именно и где.

УСТРОЙСТВО. Сбор фактов (`collect_facts`) отделён от их оценки
(`analyze`): оценка — чистая функция, тестируется без сети и базы. Сбой
одной проверки не роняет остальные — он сам становится строкой отчёта.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.kb.title_zone import zone_from_title

logger = logging.getLogger("parmangal.selfcheck")

MSK = timezone(timedelta(hours=3))
OVERRIDE_WARN_DAYS = 7
SILENT_AFTER_MINUTES = 15


@dataclass
class ListingFact:
    item_id: str
    title: str
    price: Optional[int]
    zone_id: Optional[str]          # из item_zone_map
    category: Optional[str]         # из item_zone_map
    denied: bool                    # item_scope решил «не наше»


@dataclass
class OverrideFact:
    id: int
    path: str
    value: Any
    created_at: Optional[datetime]


@dataclass
class Facts:
    now: datetime
    listings: Optional[list[ListingFact]] = None
    zone_staff: dict[str, str] = field(default_factory=dict)   # зона -> staff_id
    yclients_staff: Optional[set[str]] = None
    availability: dict[str, str] = field(default_factory=dict) # зона -> free/busy/unknown
    overrides: Optional[list[OverrideFact]] = None
    photo_ok: Optional[bool] = None
    stats: dict[str, int] = field(default_factory=dict)
    silent_chats: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)            # упавшие проверки


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)   # сломано, нужно чинить
    warnings: list[str] = field(default_factory=list)   # стоит посмотреть
    stats_line: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and not self.warnings

    def render(self) -> str:
        head = (
            "✅ Самопроверка ПарМангал: всё в порядке"
            if self.ok else
            ("🔴" if self.problems else "🟡") + " Самопроверка ПарМангал"
        )
        parts = [head]
        if self.problems:
            parts.append("\nСломано:\n" + "\n".join(f"• {p}" for p in self.problems))
        if self.warnings:
            parts.append("\nПроверить:\n" + "\n".join(f"• {w}" for w in self.warnings))
        if self.stats_line:
            parts.append("\n" + self.stats_line)
        return "\n".join(parts)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def zone_prices(zone: Any) -> Optional[list[float]]:
    """Все цены зоны из каталога (почасовые, суточные, пакет «весь день»).
    None — цену по объявлению сверять не с чем (шатёр: цена от гостей)."""
    pricing = getattr(zone, "pricing", None) or {}
    if pricing.get("rate_depends_on_guests"):
        return None
    prices = [
        p for k, v in pricing.items()
        if ("per_hour" in k or "per_day" in k) and (p := _num(v))
    ]
    package = _num((getattr(zone, "day_package", None) or {}).get("price"))
    if package:
        prices.append(package)
    return prices or None


def _zones_for(kb: Any, zone_id: Optional[str], category: Optional[str]) -> list[Any]:
    zones = list(kb.catalog.zones)
    if zone_id:
        return [z for z in zones if z.id == zone_id]
    if category:
        return [z for z in zones if z.category.value == category]
    return []


def _label(item: ListingFact) -> str:
    title = item.title if len(item.title) <= 60 else item.title[:57] + "…"
    return f"«{title}» ({item.item_id})"


def check_listings(facts: Facts, kb: Any, report: Report) -> None:
    for item in facts.listings or []:
        if item.denied:
            continue
        zone_id, category = item.zone_id, item.category
        if not zone_id and not category:
            zone_id, category = zone_from_title(item.title)
            if not zone_id and not category:
                report.warnings.append(
                    f"Объявление {_label(item)} ни с чем не сопоставлено — бот "
                    "отвечает по нему обзором всех зон. Нужна строка в item_zone_map."
                )
                continue
        zones = _zones_for(kb, zone_id, category)
        if not zones:
            report.problems.append(
                f"Объявление {_label(item)} привязано к «{zone_id or category}», "
                "а такой зоны нет в каталоге."
            )
            continue
        if item.price:
            all_prices = [zone_prices(z) for z in zones]
            if any(p is None for p in all_prices):
                continue
            low = min(min(p) for p in all_prices)
            if item.price < low * 0.9:
                report.warnings.append(
                    f"Объявление {_label(item)}: цена {item.price} ₽ ниже минимальной "
                    f"цены «{zone_id or category}» ({int(low)} ₽). Похоже, объявление "
                    "сменило объект — проверьте сопоставление."
                )


def check_yclients(facts: Facts, report: Report) -> None:
    if facts.yclients_staff is not None:
        for zone_id, staff_id in sorted(facts.zone_staff.items()):
            if str(staff_id) not in facts.yclients_staff:
                report.problems.append(
                    f"Зона {zone_id}: ресурс {staff_id} в YCLIENTS не найден — бот не "
                    "видит по ней занятость и отвечает «уточню у менеджера»."
                )
    for zone_id, status in sorted(facts.availability.items()):
        if status == "unknown" and (
            facts.yclients_staff is None
            or str(facts.zone_staff.get(zone_id)) in facts.yclients_staff
        ):
            report.problems.append(
                f"Зона {zone_id}: YCLIENTS не отдал расписание на завтра."
            )


def check_overrides(facts: Facts, report: Report) -> None:
    for o in facts.overrides or []:
        if o.created_at is None:
            continue
        age = (facts.now - o.created_at).days
        if age >= OVERRIDE_WARN_DAYS:
            report.warnings.append(
                f"Правка каталога #{o.id} из бот-меню действует {age} дн. "
                f"(с {o.created_at.astimezone(MSK):%d.%m}): {o.path} = {o.value}. "
                "Если цена временная — откатите её в /menu, если постоянная — "
                "напишите разработчику, внесём в каталог."
            )


def check_runtime(facts: Facts, report: Report) -> None:
    if facts.photo_ok is False:
        report.problems.append(
            "Тестовое фото не загрузилось в Авито или не открывается — "
            "клиенты могут видеть серые заглушки."
        )
    if facts.silent_chats:
        report.warnings.append(
            f"Бот не ответил клиенту ({len(facts.silent_chats)}): "
            + ", ".join(facts.silent_chats[:5])
        )
    failed = facts.stats.get("failed", 0)
    if failed >= 3:
        report.warnings.append(f"Не отправлено сообщений за сутки: {failed}.")
    for err in facts.errors:
        report.problems.append(f"Проверка не выполнилась: {err}")


def analyze(facts: Facts, kb: Any) -> Report:
    report = Report()
    check_listings(facts, kb, report)
    check_yclients(facts, report)
    check_overrides(facts, report)
    check_runtime(facts, report)
    s = facts.stats
    report.stats_line = (
        f"За сутки: входящих {s.get('incoming', 0)}, ответов бота {s.get('sent', 0)}, "
        f"не отправлено {s.get('failed', 0)}, новых чатов {s.get('new_chats', 0)}."
    )
    return report


# ---------------------------------------------------------------- сбор фактов

_STATS_SQL = """
select
  count(*) filter (where direction = 'incoming')                     as incoming,
  count(*) filter (where direction = 'outgoing' and status = 'sent')  as sent,
  count(*) filter (where direction = 'outgoing' and status = 'failed') as failed
from messages where created_at > :since
"""

_SILENT_SQL = """
select c.chat_id
from chats c
where c.ai_enabled and not c.is_human_takeover and not coalesce(c.manual_hold, false)
  and (c.item_id is null or c.item_id not in
       (select item_id from item_scope where decision = 'deny'))
  and exists (
    select 1 from messages i where i.chat_id = c.chat_id and i.direction = 'incoming'
      and i.created_at between :since and :quiet
      and not exists (select 1 from messages o where o.chat_id = c.chat_id
                      and o.direction = 'outgoing' and o.created_at >= i.created_at))
order by c.chat_id
"""


async def _guard(facts: Facts, name: str, coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — сбой проверки = строка отчёта
        logger.exception("selfcheck: %s failed", name)
        facts.errors.append(f"{name} ({type(exc).__name__}: {str(exc)[:120]})")
        return None


async def _listings(session_factory, items_client, settings) -> list[ListingFact]:
    from sqlalchemy import select

    from app.db.models import ItemScope, ItemZoneMap

    listings = await items_client.list_all_items(status="active")
    async with session_factory() as session:
        zmap = {
            r.item_id: r for r in (await session.execute(select(ItemZoneMap))).scalars()
        }
        scope = {
            r.item_id: r.decision
            for r in (await session.execute(select(ItemScope))).scalars()
        }
    raw = getattr(settings, "avito_blocked_items", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    blocked = {str(x).strip() for x in raw if str(x).strip()}
    out = []
    for item in listings:
        row = zmap.get(str(item.item_id))
        out.append(ListingFact(
            item_id=str(item.item_id), title=item.title or "", price=item.price,
            zone_id=getattr(row, "zone_id", None), category=getattr(row, "category", None),
            denied=scope.get(str(item.item_id)) == "deny" or str(item.item_id) in blocked,
        ))
    return out


async def _yclients_staff(booking_provider, company_id) -> set[str]:
    data = await booking_provider._request(("GET", ""), f"/book_staff/{company_id}")
    rows = data if isinstance(data, list) else (data or {}).get("data", [])
    return {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}


async def _photo_canary(avito_client) -> bool:
    import httpx

    from app.media.bundled import BUNDLED_DIR

    path = next(iter(sorted(BUNDLED_DIR.glob("*.jpg"))), None)
    if path is None:
        return False
    uploaded = await avito_client.upload_image(path.read_bytes(), path.name)
    urls = [
        u for v in (uploaded or {}).values() if isinstance(v, dict)
        for u in v.values() if isinstance(u, str) and u.startswith("http")
    ]
    if not urls:
        return False
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(urls[0])
    return resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image")


async def _stats(session_factory, now: datetime) -> tuple[dict[str, int], list[str]]:
    from sqlalchemy import text

    since = now - timedelta(hours=24)
    quiet = now - timedelta(minutes=SILENT_AFTER_MINUTES)
    async with session_factory() as session:
        row = (await session.execute(text(_STATS_SQL), {"since": since})).mappings().one()
        new_chats = (await session.execute(
            text("select count(*) from chats where created_at > :since"), {"since": since}
        )).scalar_one()
        silent = [r[0] for r in (await session.execute(
            text(_SILENT_SQL), {"since": since, "quiet": quiet}
        )).all()]
    stats = {k: int(v or 0) for k, v in dict(row).items()}
    stats["new_chats"] = int(new_chats or 0)
    return stats, silent


async def collect_facts(
    *, session_factory, items_client, avito_client, booking_provider, zone_mapping,
    settings, now: Optional[datetime] = None,
) -> Facts:
    from app.kb.override_store import SqlAlchemyOverrideStore

    facts = Facts(now=now or datetime.now(timezone.utc))

    facts.listings = await _guard(
        facts, "объявления Авито", _listings(session_factory, items_client, settings)
    )

    for zone_id in zone_mapping.mapped_zones():
        row = zone_mapping.get(zone_id) or {}
        if row.get("enabled", True) and row.get("staff_id"):
            facts.zone_staff[zone_id] = str(row["staff_id"])
    facts.yclients_staff = await _guard(
        facts, "ресурсы YCLIENTS",
        _yclients_staff(booking_provider, settings.yclients_company_id),
    )
    tomorrow = (facts.now.astimezone(MSK) + timedelta(days=1)).date()
    for zone_id in facts.zone_staff:
        avail = await _guard(
            facts, f"занятость {zone_id}", booking_provider.get_free_slots(zone_id, tomorrow)
        )
        if avail is not None:
            facts.availability[zone_id] = getattr(avail.status, "value", str(avail.status))

    records = await _guard(
        facts, "правки каталога", SqlAlchemyOverrideStore(session_factory).list_active()
    )
    if records is not None:
        facts.overrides = [
            OverrideFact(id=r.id, path=r.path, value=r.value, created_at=r.created_at)
            for r in records
        ]

    facts.photo_ok = await _guard(facts, "фото в Авито", _photo_canary(avito_client))

    got = await _guard(facts, "статистика", _stats(session_factory, facts.now))
    if got is not None:
        facts.stats, facts.silent_chats = got
    return facts


# ---------------------------------------------------------------- расписание

def seconds_until(hour_msk: int, now: datetime) -> float:
    local = now.astimezone(MSK)
    target = datetime.combine(local.date(), time(hour_msk), tzinfo=MSK)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


async def supervised_selfcheck(
    run: Callable[[], Awaitable[str]],
    send: Callable[[str], Awaitable[Any]],
    *,
    hour_msk: int,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Раз в сутки в hour_msk по Москве. Сбой прохода изолирован, как у
    остальных supervised_* воркеров: следующий день всё равно наступит."""
    while True:
        await sleep(seconds_until(hour_msk, now_fn()))
        try:
            await send(await run())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("selfcheck: daily pass failed")
            try:
                await send("🔴 Самопроверка ПарМангал не выполнилась — смотрите логи Railway.")
            except Exception:
                logger.exception("selfcheck: could not report the failure")
