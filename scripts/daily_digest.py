"""Срез за сутки: что агент делал, чем это обосновывал и во что обошлось.

    python -m scripts.daily_digest              # последние 24 часа
    python -m scripts.daily_digest --hours 12
    python -m scripts.daily_digest --brief      # только сводка, без диалогов

ЗАПУСКАТЬ ВНУТРИ КОНТЕЙНЕРА (`railway ssh`): читает боевую базу, наружу она
не смотрит. Только чтение — ни одной записи.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ ЛОГ RAILWAY. В логе нет двух вещей, ради
которых срез и снимают: ТЕКСТА ответа (он пишется в базу, а не в лог) и
СВЯЗИ ответа с цепочкой вызовов инструментов. По логу видно «агент ответил»,
но не видно, на чём этот ответ стоял, — а весь проект про то, чтобы ответ
стоял на вызове инструмента, а не на догадке модели.

Три вопроса, на которые срез отвечает по построению:

  * СТРОИЛАСЬ ЛИ ПОДСКАЗКА ЗОНЫ. Считается по КАРТЕ НА МОМЕНТ ЗАПУСКА
    среза, а не на момент ответа: карта живёт в базе и меняется. Для ходов
    до её заполнения столбец показывает «какая подсказка была бы сейчас»,
    а не «была тогда» — на срезе, пересекающем выкатку, это легко прочитать
    неверно. У чата есть item_id; если он есть в
    item_zone_map с zone_id или category — подсказка была, и агент не должен
    был переспрашивать «какая зона». Заполнение карты 2026-09-06 закрыло
    ровно эту дыру, и проверять её надо на живых диалогах, а не на выкладке.
  * ПРЕДЛАГАЛ ЛИ АЛЬТЕРНАТИВЫ. При занятости check_availability возвращает
    поле alternatives; если оно непустое, а в тексте ответа нет ни одного
    названия оттуда — модель проигнорировала данные, и это видно.
  * СРАБАТЫВАЛИ ЛИ РУБЕЖИ. С задержанным текстом, иначе «рубеж сработал» не
    отличить от «рубеж сработал зря».
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone


async def collect(hours: int) -> dict:
    from sqlalchemy import func, select

    from app.admin.queries import SqlAlchemyAdminQueries
    from app.db.models import Chat, Direction, ItemZoneMap, Message
    from app.db.session import get_sessionmaker

    sm = get_sessionmaker()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    async with sm() as s:
        zmap = {r.item_id: r for r in (await s.execute(select(ItemZoneMap))).scalars()}
        out = (await s.execute(
            select(Message)
            .where(Message.direction == Direction.outgoing, Message.created_at >= since)
            .order_by(Message.created_at)
        )).scalars().all()
        inc = (await s.execute(
            select(Message)
            .where(Message.direction == Direction.incoming, Message.created_at >= since)
            .order_by(Message.created_at)
        )).scalars().all()
        chat_ids = {m.chat_id for m in out} | {m.chat_id for m in inc}
        chats = {c.chat_id: c for c in (await s.execute(
            select(Chat).where(Chat.chat_id.in_(chat_ids or {""}))
        )).scalars()}
        holds = (await s.execute(
            select(func.count()).select_from(Chat).where(Chat.manual_hold.is_(True))
        )).scalar()
        spent = await SqlAlchemyAdminQueries(sm).cost_spent_today()

    turns = []
    for m in out:
        meta = m.llm_meta or {}
        trace = meta.get("tool_trace") or []
        chat = chats.get(m.chat_id)
        item_id = getattr(chat, "item_id", None) or ""
        row = zmap.get(item_id)
        hint = None
        if row is not None:
            hint = row.zone_id or (f"категория {row.category}" if row.category else None)
        # Альтернативы: пришли ли они и назвал ли их агент клиенту.
        offered, named = [], []
        for call in trace:
            result = call.get("result")
            if isinstance(result, dict):
                for alt in result.get("alternatives") or []:
                    offered.append(alt.get("name", ""))
        for name in offered:
            head = (name or "").split()[0].strip("«»")
            if head and head.lower() in (m.text or "").lower():
                named.append(name)
        turns.append({
            "at": m.created_at,
            "chat_id": m.chat_id,
            "item_id": item_id,
            "zone_hint": hint,
            "text": " ".join((m.text or "").split()),
            "tools": [c.get("tool") for c in trace],
            "trace": trace,
            "guard_rail": meta.get("guard_rail"),
            "withheld": " ".join((meta.get("withheld_text") or "").split()),
            "cost": meta.get("cost_rub"),
            "alternatives_offered": offered,
            "alternatives_named": named,
        })

    return {
        "since": since, "now": now, "hours": hours,
        "turns": turns, "incoming": len(inc),
        "dialogs": len({t["chat_id"] for t in turns}),
        "holds": holds, "spent_today": spent,
    }


def render(data: dict, brief: bool) -> str:
    turns = data["turns"]
    with_tools = [t for t in turns if t["tools"]]
    with_hint = [t for t in turns if t["zone_hint"]]
    guards = [t for t in turns if t["guard_rail"]]
    with_alts = [t for t in turns if t["alternatives_offered"]]
    named_alts = [t for t in with_alts if t["alternatives_named"]]

    lines = [
        f"СРЕЗ ЗА {data['hours']} Ч  ({data['since']:%d.%m %H:%M} — {data['now']:%d.%m %H:%M} UTC)",
        "",
        f"  диалогов с ответом агента : {data['dialogs']}",
        f"  входящих                  : {data['incoming']}",
        f"  исходящих                 : {len(turns)}",
        f"  из них с вызовом инструмента: {len(with_tools)}"
        + (f" ({len(with_tools) * 100 // len(turns)}%)" if turns else ""),
        f"  подсказка зоны есть СЕЙЧАС: {len(with_hint)}"
        + (f" ({len(with_hint) * 100 // len(turns)}%)" if turns else ""),
        f"  занятость с альтернативами: {len(with_alts)}"
        f" (агент назвал их клиенту: {len(named_alts)})",
        f"  срабатываний рубежей      : {len(guards)}",
        f"  потрачено сегодня         : {data['spent_today']} руб",
        f"  чатов на manual_hold      : {data['holds']}",
    ]
    if guards:
        lines += ["", "РУБЕЖИ:"]
        for t in guards:
            lines += [f"  [{t['at']:%d.%m %H:%M}] {t['guard_rail']}",
                      f"     задержано: {t['withheld'][:180]}"]
    if brief:
        return "\n".join(lines)

    lines += ["", "=" * 72, "ДИАЛОГИ", "=" * 72]
    for chat_id in dict.fromkeys(t["chat_id"] for t in turns):
        chat_turns = [t for t in turns if t["chat_id"] == chat_id]
        head = chat_turns[0]
        lines += ["", f"### {chat_id}  | объявление {head['item_id'] or '—'} "
                      f"| зона по карте: {head['zone_hint'] or 'НЕТ'}"]
        for t in chat_turns:
            lines.append(f"  [{t['at']:%d.%m %H:%M}] {t['cost']} руб")
            for call in t["trace"]:
                args = json.dumps(call.get("arguments"), ensure_ascii=False)[:110]
                res = json.dumps(call.get("result"), ensure_ascii=False)[:190]
                lines.append(f"      -> {call.get('tool')}({args})")
                lines.append(f"         {res}")
            if not t["trace"]:
                lines.append("      -> инструменты не вызывались")
            if t["alternatives_offered"]:
                lines.append(f"      АЛЬТЕРНАТИВЫ: предложено "
                             f"{t['alternatives_offered']}, названо клиенту "
                             f"{t['alternatives_named'] or 'НИ ОДНОЙ'}")
            if t["guard_rail"]:
                lines.append(f"      РУБЕЖ: {t['guard_rail']}")
            lines.append(f"      ОТВЕТ: {t['text'][:400]}")
    return "\n".join(lines)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--brief", action="store_true")
    args = parser.parse_args()
    print(render(await collect(args.hours), args.brief))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
