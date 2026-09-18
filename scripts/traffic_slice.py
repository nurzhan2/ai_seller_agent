"""Срез живого трафика: окно ПОСЛЕ выкатки против такого же окна ДО неё.

    python -m scripts.traffic_slice --deploy 2026-09-18T14:21:03Z
    python -m scripts.traffic_slice --deploy 2026-09-18T14:21:03Z --hours 24

ЗАПУСКАТЬ ВНУТРИ КОНТЕЙНЕРА (`railway ssh`): читает боевую базу. Только
чтение — ни одной записи.

ЗАЧЕМ ОТДЕЛЬНО ОТ МЕТРИКИ. `parmangal_guard_rails_total` живёт в памяти
процесса и обнуляется при каждой выкатке — а 2026-09-18 их было шесть за
вечер. База же хранит у каждого ответа след рубежа (`llm_meta.guard_rail`,
`llm_meta.question_guard`), и по ней окно «до» и «после» сравнимо при любом
числе перезапусков. От scripts/daily_digest.py отличается тем, что
сравнивает два окна, а не описывает одно.

Что считается — ровно то, что меняла серия правок 2026-09-18:
  * вопросы в ответе и срабатывания рубежа одного вопроса;
  * прочие рубежи — по видам, без хвоста подробностей;
  * «Передала вопрос менеджеру» — путь, на который метка human классификатора
    уводила подтверждения клиента (модель может написать ту же фразу сама,
    поэтому это верхняя граница, а не точный счёт);
  * приветствия на системные сообщения Авито;
  * отправленные фото (`messages.image_ids`);
  * ответы в чаты вакансии 8302335893 (закрыта в тот же вечер).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from app.agent.one_question import count_questions
from app.pipeline import SYSTEM_MESSAGE_GREETING

HANDED_PREFIX = "Передала вопрос менеджеру"
VACANCY_ITEM = "8302335893"


def _meta(row) -> dict:
    meta = row["llm_meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta or {}


def describe(rows: list, label: str) -> list[str]:
    incoming = [r for r in rows if r["direction"] == "incoming"]
    out = [r for r in rows if r["direction"] == "outgoing" and r["status"] in ("sent", "dry_run")]
    guards: collections.Counter = collections.Counter()
    trimmed = multi = handed = 0
    questions: list[int] = []
    for row in out:
        meta = _meta(row)
        if meta.get("guard_rail"):
            guards[meta["guard_rail"].split(":")[0].strip()[:70]] += 1
        if meta.get("question_guard"):
            trimmed += 1
        q = count_questions(row["text"] or "")
        questions.append(q)
        multi += q > 1
        handed += (row["text"] or "").startswith(HANDED_PREFIX)
    greetings = sum(1 for r in out if (r["text"] or "") == SYSTEM_MESSAGE_GREETING)
    photos = sum(len(r["image_ids"] or []) for r in out)

    lines = [f"== {label}: чатов {len({r['chat_id'] for r in rows})}, "
             f"входящих {len(incoming)}, ответов {len(out)}"]
    if out:
        lines.append(f"   вопросов в ответе в среднем {sum(questions) / len(questions):.2f}; "
                     f"ушло клиенту с >1 вопросом: {multi}/{len(out)}")
    lines += [
        f"   рубеж одного вопроса обрезал: {trimmed}",
        f"   прочие рубежи: {dict(guards) or 'ни одного'}",
        f"   «{HANDED_PREFIX}»: {handed}",
        f"   приветствий на системные сообщения: {greetings}",
        f"   фото отправлено: {photos}",
    ]
    return lines


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy", required=True,
                        help="момент выкатки, ISO в UTC: 2026-09-18T14:21:03Z")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    deploy = datetime.fromisoformat(args.deploy.replace("Z", "+00:00"))
    window = timedelta(hours=args.hours)
    now = datetime.now(timezone.utc)
    after_end = min(deploy + window, now)

    import asyncpg

    conn = await asyncpg.connect(
        os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        rows = await conn.fetch(
            """
            select chat_id, direction::text as direction, status::text as status,
                   text, llm_meta, image_ids, created_at
            from messages where created_at >= $1 and created_at < $2
            order by created_at
            """,
            deploy - window, after_end,
        )
        vacancy = await conn.fetchval(
            "select count(*) from messages m join chats c on c.chat_id = m.chat_id "
            "where c.item_id = $1 and m.direction = 'outgoing' and m.created_at >= $2",
            VACANCY_ITEM, deploy,
        )
    finally:
        await conn.close()

    covered = (after_end - deploy).total_seconds() / 3600
    print(f"выкатка {deploy:%Y-%m-%d %H:%M} UTC; окно по {args.hours:g} ч; "
          f"после выкатки покрыто {covered:.1f} ч")
    if covered < args.hours:
        print(f"⚠️ окно «после» неполное: {covered:.1f} из {args.hours:g} ч")
    for line in describe([r for r in rows if r["created_at"] < deploy], "ДО"):
        print(line)
    for line in describe([r for r in rows if r["created_at"] >= deploy], "ПОСЛЕ"):
        print(line)
    print(f"\nответов в чаты вакансии {VACANCY_ITEM} после выкатки: {vacancy}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
