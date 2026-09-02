"""Мутационная проверка: ломаем правило по одному месту и ждём красного теста.

    python -m scripts.mutate                    # весь реестр
    python -m scripts.mutate --rule занятость   # только правила этой группы
    python -m scripts.mutate --list             # что вообще проверяется
    python -m scripts.mutate --jobs 4           # параллельно (осторожно, см. ниже)

ЗАЧЕМ ЭТО ЛЕЖИТ В РЕПОЗИТОРИИ. Зелёный прогон отвечает на вопрос «код делает
то, что делает». Он НЕ отвечает на вопрос «а если правило сломать, кто-нибудь
заметит?». В этом проекте ответом трижды оказывалось «никто»:

  * `test_a_past_visit_forces_nothing` проходил и с рубежом признака
    доступности, и без него — слова «вчера» не было ни в одном словаре,
    так что тест не касался проверяемой строки вовсе;
  * контрольное плечо живого замера падало с TypeError 43 раза из 45, а
    отчёт показывал «0 из 5» и читался как измерение;
  * `_CONDITION_WINDOW` можно было менять с 16 на 200, и ни один тест не
    менял цвета — константа не значила ничего.

Все три находки — мутационные. Поэтому стенд не «когда-нибудь потом», а
часть проверки наравне с pytest, и живёт он здесь, а не в чьей-то папке.

КАК ЧИТАТЬ РЕЗУЛЬТАТ. «поймана» — мутация внесена, тест покраснел, правило
под охраной. «ПРОПУЩЕНА» — правило сломано, а прогон зелёный: либо тест
проверяет не то, либо правило не нужно. Оба вывода полезны, и оба требуют
действия. «НЕ ПРИМЕНИЛАСЬ» — искомой строки в файле нет: код уехал вперёд
реестра, мутацию надо переписать (это не «всё хорошо»).

БЕЗОПАСНОСТЬ. Файл возвращается на место в `finally`, после чего содержимое
сверяется с исходным побайтно; при расхождении скрипт кричит и падает. Перед
началом проверяется чистота рабочего дерева — если прогон всё же оборвут на
полпути, `git status` покажет, что именно осталось мутированным.

ПРО --jobs. По умолчанию 1. Тесты этого проекта ходят в общую базу
`parmangal_test` (tests/test_sql_stores.py и соседи), и два pytest
одновременно роняют друг друга по внешним ключам — это уже наблюдалось.
Параллелить можно, только если в реестре остались мутации, чьи тесты в базу
не ходят.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    """Одна поломка одного правила.

    `rule` — группа, по которой удобно фильтровать и по которой видно, что
    правило вообще кем-то охраняется. `tests` — что гоняем: узкий файл, а не
    весь набор, иначе реестр не прогнать за разумное время.
    """

    rule: str
    path: str
    old: str
    new: str
    tests: str
    why: str


# --------------------------------------------------------------------------
# Реестр. Одна мутация на правило — минимум; там, где правило состоит из
# нескольких решений (порядок веток, форма глагола, ширина окна), мутаций
# столько же, сколько решений.
# --------------------------------------------------------------------------

FORCING = "принуждение"
GUARD = "рубеж-занятости"
AMOUNT = "рубеж-суммы"
LADDER = "лестница"
GARBAGE = "мусорный-tool_use"
WIRING = "проводка"
MORPH = "морфология"

MUTATIONS: tuple[Mutation, ...] = (
    # -- принуждение инструмента -------------------------------------------
    Mutation(FORCING, "app/agent/tool_forcing.py",
             '    if has_date and has_time:\n        return "check_availability"',
             '    if has_date:\n        return "check_availability"',
             "tests/test_tool_forcing.py",
             "дата без времени тоже дёргает календарь («завтра перезвоню»)"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             '    if PRICE_ASK.search(text) and price_slot_known(f"{text}\\n{context}"):',
             "    if False:",
             "tests/test_tool_forcing.py",
             "ценовая ветка выключена"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             "    if asks_nearest and not CONCRETE_DATE.search(text):",
             "    if asks_nearest:",
             "tests/test_tool_forcing.py",
             "«ближайшее» побеждает даже при названном числе"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             '        return "resolve_date"',
             '        return "check_availability"',
             "tests/test_tool_forcing.py",
             "относительная дата идёт мимо resolve_date"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             "    if not (asks_availability or asks_nearest):\n        return None",
             "    if False:\n        return None",
             "tests/test_tool_forcing.py",
             "признак доступности перестал быть обязательным"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             ' and price_slot_known(f"{text}\\n{context}")',
             '',
             "tests/test_tool_forcing.py",
             "сужение цены снято — голая «а цена какая?» снова принуждает"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             'price_slot_known(f"{text}\\n{context}")',
             'price_slot_known(text)',
             "tests/test_tool_forcing.py",
             "контекст разговора выброшен, смотрим только текущее сообщение"),
    Mutation(FORCING, "app/agent/tool_forcing.py",
             "        ZONE_WORDS.search(context)\n        or CONCRETE_DATE.search(context)",
             "        CONCRETE_DATE.search(context)",
             "tests/test_tool_forcing.py",
             "зона перестала считаться признаком, осталась только дата"),
    Mutation(FORCING, "app/agent/loop.py",
             '             if m.get("role") == "user"]',
             '             if m.get("role") in ("user", "assistant")]',
             "tests/test_agent.py",
             "в контекст попали реплики агента — приветствие перечисляет все зоны"),
    Mutation(FORCING, "app/agent/loop.py",
             "        forced_tool = forced_tool_for(user_text, forcing_context)",
             "        forced_tool = forced_tool_for(user_text)",
             "tests/test_agent.py",
             "контекст не доезжает до решения о принуждении"),
    Mutation(FORCING, "app/agent/loop.py",
             "            if forced_tool and iteration == 0:",
             "            if forced_tool:",
             "tests/test_agent.py",
             "принуждение на всех витках, а не только на первом"),

    # -- морфология: русская основа в регэкспе -----------------------------
    # Ловушка тут не в конкретном слове, а в том, как пишется основа: `\b`
    # съедается приставкой, а краткая форма не начинается на полную основу.
    Mutation(MORPH, "app/agent/loop.py",
             r"\bсвобод(?:ен|ны|на|но|н\w*)|\bосвобод\w*|\bосвобожд\w*|\bпосвободн\w*|",
             r"\bсвободн\w*|",
             "tests/test_agent.py",
             "краткая форма «свободен» снова мимо рубежа"),
    Mutation(MORPH, "app/agent/loop.py",
             r"\bокошк\w*|\bокно\b|есть\s+врем\w*|\bближайш\w*",
             r"\bокошк\w*|\bокн[оауе]\w*|\bокно\b|есть\s+врем\w*|\bближайш\w*",
             "tests/test_agent.py",
             "«окна» затащили в словарь рубежа — настоящие окна бани глушат ответ"),
    Mutation(MORPH, "app/agent/loop.py",
             r"|давайте|давай|хочу|надо|нужно)\b",
             r"|давайте|давай|хочу|надо|нужно|сейчас)\b",
             "tests/test_agent.py",
             "«сейчас» вернулось в маркеры цели — «сейчас проверила: занято» проходит"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"\b(?:за|пере)?брон(?:ь|и|ю|я|е|ир\w*)",
             r"\bброн(?:ь|и|ю|я|е|ир\w*)",
             "tests/test_tool_forcing.py",
             "приставка снова съедает границу — «забронировать» не опознаётся"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"\bсвобод(?:ен|ны|на|но|н\w*)|\bосвобод\w*|\bзанят\w*|",
             r"\bсвободн\w*|\bзанят\w*|",
             "tests/test_tool_forcing.py",
             "краткая форма «свободен» снова мимо принуждения"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"в\s+\w+|на\s+\w+)|",
             r"послезавтра)|",
             "tests/test_tool_forcing.py",
             "«можно на субботу» снова мимо — список хвостов после «можно» сузили"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"\b(?:за)?резерв(?!уар)\w*",
             r"\b(?:за)?резерв\w*",
             "tests/test_tool_forcing.py",
             "«резервуар для воды» снова считается бронью"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"\bближайш\w*\b(?!\s+врем)",
             r"\bближайш\w*",
             "tests/test_tool_forcing.py",
             "«в ближайшее время» снова включает поиск дат"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             "    if NOT_A_BOOKING.search(text):\n        return None",
             "    if False:\n        return None",
             "tests/test_tool_forcing.py",
             "вето снято — обещание перезвонить и прошедший визит идут в календарь"),
    Mutation(MORPH, "app/agent/tool_forcing.py",
             '    if PRICE_ASK.search(text) and price_slot_known(f"{text}\\n{context}"):\n        return "calculate_price"',
             '    if PRICE_ASK.search(text):\n        return "calculate_price" if price_slot_known(f"{text}\\n{context}") else None',
             "tests/test_tool_forcing.py",
             "ценовая ветка снова коротит и глотает вопрос о занятости"),

    Mutation(MORPH, "app/agent/tool_forcing.py",
             r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\b(?!\s*(?:чел|гост|персон|час|шт|мин))|",
             r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\b|",
             "tests/test_tool_forcing.py",
             "«нас будет 6-8 человек» снова считается датой"),

    # -- рубеж занятости: утверждение против обещания -----------------------
    Mutation(GUARD, "app/agent/loop.py",
             "        if _promises_to_check(left):\n            continue",
             "        if False:\n            continue",
             "tests/test_agent.py",
             "обещание проверить снова считается утверждением"),
    Mutation(GUARD, "app/agent/loop.py",
             "    if not _PAST_FORM.search(verb):\n        return True",
             "    return True",
             "tests/test_agent.py",
             "форма глагола не смотрится — «посмотрела» опять обещание"),
    Mutation(GUARD, "app/agent/loop.py",
             "    return bool(_PURPOSE.search(left))",
             "    return False",
             "tests/test_agent.py",
             "маркер цели не смотрится — «чтобы я проверила» снова утверждение"),
    Mutation(GUARD, "app/agent/loop.py",
             "    verb = matches[-1].group()",
             "    verb = matches[0].group()",
             "tests/test_agent.py",
             "берётся первый глагол слева вместо ближайшего к слову занятости"),
    Mutation(GUARD, "app/agent/loop.py",
             "_PROMISE_WINDOW = 30",
             "_PROMISE_WINDOW = 300",
             "tests/test_agent.py",
             "окно глагола растянуто на всю фразу"),
    Mutation(GUARD, "app/agent/loop.py",
             "        if _under_a_condition(text[max(0, m.start() - _CONDITION_WINDOW):m.start()]):\n            continue",
             "        if False:\n            continue",
             "tests/test_agent.py",
             "оговорка «если свободна» снова считается утверждением"),
    Mutation(GUARD, "app/agent/loop.py",
             "        if _QUESTION_AFTER.match(right):\n            continue",
             "        if False:\n            continue",
             "tests/test_agent.py",
             "вопрос «свободна ли» снова считается утверждением"),
    Mutation(GUARD, "app/agent/loop.py",
             "_CLAUSE_BREAK.split(left)[-1]",
             "left",
             "tests/test_agent.py",
             "оговорка перестала кончаться на знаке — индульгенция на всю фразу"),
    Mutation(GUARD, "app/agent/loop.py",
             "_CONDITION_WINDOW = 60",
             "_CONDITION_WINDOW = 6",
             "tests/test_agent.py",
             "окно оговорки сужено — «если желаемая дата свободна» снова рубится"),
    Mutation(MORPH, "app/agent/loop.py",
             '        if m.group().lower().startswith("свободно") and _FREELY_AS_MANNER.match(right):\n            continue',
             "        if False:\n            continue",
             "tests/test_agent.py",
             "«свободно размещаются 8 гостей» снова считается календарём"),
    Mutation(MORPH, "app/agent/loop.py",
             r"\bрасписан[оаы]\b|\bзабит[оаы]\b|\bзаброниров\w*|",
             "",
             "tests/test_agent.py",
             "«всё расписано» и «всё забито» снова мимо рубежа"),
    Mutation(MORPH, "app/agent/loop.py",
             r"\bнет\s+мест|\bмест\s+нет|\bмест\s+не\s+остал\w*|",
             "",
             "tests/test_agent.py",
             "«нет мест на 6 сентября» снова мимо рубежа"),
    Mutation(AMOUNT, "app/agent/loop.py",
             "        if _THOUSANDS.search(match.group()):",
             "        if False:",
             "tests/test_agent.py",
             "«14 тыс.» не приводится к тысячам — честная цена станет выдумкой"),
    Mutation(AMOUNT, "app/agent/loop.py",
             "(?:-|—|–|до)",
             "(?:НИКОГДАНЕТ)",
             "tests/test_agent.py",
             "нижняя граница вилки «от 8000 до 12000 ₽» снова не проверяется"),
    Mutation(GUARD, "app/agent/loop.py",
             '    return end is not None and end.group() == "?"',
             "    return end is not None",
             "tests/test_agent.py",
             "любой конец предложения оправдывает «ближайший», не только вопрос"),
    Mutation(GUARD, "app/agent/loop.py",
             '        if m.group().lower().startswith("ближайш") and _nearest_is_not_about_our_calendar(right):',
             "        if _nearest_is_not_about_our_calendar(right):",
             "tests/test_agent.py",
             "исключение для «ближайшего» распространено на весь словарь"),
    Mutation(GUARD, "app/agent/loop.py",
             "        if not near:\n            continue\n        _, _, day, month = min(near)",
             "        if not near:\n            continue\n        _, _, day, month = min(near)\n        return None",
             "tests/test_agent.py",
             "рубеж дат выключен целиком"),
    Mutation(GUARD, "app/agent/loop.py",
             "        _, _, day, month = min(near)",
             "        _, _, day, month = max(near)",
             "tests/test_agent.py",
             "берётся самая дальняя дата вместо ближайшей"),

    # -- рубеж суммы --------------------------------------------------------
    Mutation(AMOUNT, "app/agent/loop.py",
             "        if final_text and tool_calls:\n            invented = invented_amounts(final_text, allowed_amounts)",
             "        if False:\n            invented = invented_amounts(final_text, allowed_amounts)",
             "tests/test_agent.py",
             "рубеж суммы выключен целиком"),
    Mutation(AMOUNT, "app/agent/loop.py",
             "                allowed_amounts |= amounts_in_payload(payload)",
             "                pass",
             "tests/test_agent.py",
             "числа из ответов инструментов не копятся — честная цена станет выдумкой"),
    Mutation(AMOUNT, "app/agent/loop.py",
             '    return cleaned.lstrip("0") or "0"',
             "    return cleaned",
             "tests/test_agent.py",
             "формы числа не приводятся к одной"),
    Mutation(AMOUNT, "app/agent/loop.py",
             "    elif isinstance(value, str):\n        for match in _NUMBER_IN_TEXT.finditer(value):",
             "    elif isinstance(value, str) and False:\n        for match in _NUMBER_IN_TEXT.finditer(value):",
             "tests/test_agent.py",
             "числа внутри строк ответа не видны"),
    Mutation(AMOUNT, "app/quality/asserts.py",
             "        for amount in invented_amounts(text, set(turn.tool_amounts)):",
             "        for amount in []:",
             "tests/test_quality.py",
             "харнесс качества перестал проверять суммы"),

    # -- лестница безопасного ответа ----------------------------------------
    Mutation(LADDER, "app/agent/loop.py",
             "    if repeats == len(AVAILABILITY_GUARD_REPLIES):\n        return AVAILABILITY_GUARD_HANDOFF, True\n    return \"\", True",
             "    return AVAILABILITY_GUARD_HANDOFF, True",
             "tests/test_agent.py",
             "после карточки она же дословно, а не молчание"),
    Mutation(LADDER, "app/agent/loop.py",
             "        return AVAILABILITY_GUARD_REPLIES[repeats], False",
             "        return AVAILABILITY_GUARD_REPLIES[0], False",
             "tests/test_agent.py",
             "второй ответ дословно повторяет первый"),
    Mutation(LADDER, "app/agent/loop.py",
             "            n += 1\n        else:\n            break",
             "            n += 1\n        else:\n            continue",
             "tests/test_agent.py",
             "обычный ответ агента не обрывает счёт подстановок"),
    Mutation(LADDER, "app/agent/loop.py",
             "    | {AVAILABILITY_GUARD_HANDOFF, GUARD_RAIL_FALLBACK}",
             "    | {AVAILABILITY_GUARD_HANDOFF}",
             "tests/test_agent.py",
             "отбивка ценового рубежа снова рвёт серию"),
    Mutation(LADDER, "app/agent/loop.py",
             '        return "" if (msg.get("content") or "").strip() == text else text',
             "        return text",
             "tests/test_agent.py",
             "рубеж без лестницы снова повторяется дословно"),
    Mutation(LADDER, "app/pipeline.py",
             "            if result.escalated:\n                await self._notify_operator(chat, result, merged_text)",
             "            if False:\n                await self._notify_operator(chat, result, merged_text)",
             "tests/test_pipeline.py",
             "молчаливая эскалация не доходит до оператора"),

    # -- мусорный tool_use --------------------------------------------------
    Mutation(GARBAGE, "app/agent/loop.py",
             "        try:\n            parsed = json.loads(raw)\n        except (ValueError, TypeError):\n            parsed = None\n        arguments = dict(parsed) if isinstance(parsed, dict) else {}",
             "        arguments = {}",
             "tests/test_agent.py",
             "аргументы-строка молча теряются"),
    Mutation(GARBAGE, "app/agent/loop.py",
             '    name = name if isinstance(name, str) else ""',
             "    name = name",
             "tests/test_agent.py",
             "имя не приводится к строке — блок без имени роняет ход"),
    Mutation(GARBAGE, "app/agent/loop.py",
             '        block_id = f"unknown_tool_use_{index}"',
             '        block_id = "unknown_tool_use"',
             "tests/test_agent.py",
             "два блока без id схлопываются в один"),
    Mutation(GARBAGE, "app/agent/loop.py",
             '                "content": sanitized_assistant_content(response.content, calls),',
             '                "content": response.content,',
             "tests/test_agent.py",
             "сырой мусорный блок уезжает провайдеру, id не совпадает с tool_result"),
    Mutation(GARBAGE, "app/agent/loop.py",
             '            "name": name or WIRE_UNKNOWN_TOOL,',
             '            "name": name,',
             "tests/test_agent.py",
             "на провод уходит пустое имя инструмента"),

    # -- проводка: без неё всё остальное декоративно ------------------------
    Mutation(WIRING, "app/agent/providers/deepseek_provider.py",
             '            kwargs["tool_choice"] = tool_choice',
             "            pass",
             "tests/test_providers.py",
             "DeepSeek не получает tool_choice вовсе"),
    Mutation(WIRING, "app/agent/providers/failover.py",
             "                    max_tokens=max_tokens, tool_choice=tool_choice,\n                )\n                self._consecutive_errors = 0",
             "                    max_tokens=max_tokens,\n                )\n                self._consecutive_errors = 0",
             "tests/test_providers.py",
             "резервный провайдер теряет принуждение"),
    Mutation(WIRING, "app/agent/loop.py",
             "            tool_amounts=allowed_amounts,\n            granted_offer_templates",
             "            granted_offer_templates",
             "tests/test_agent.py",
             "числа инструментов не отдаются наружу — харнесс качества слепнет"),
    Mutation(WIRING, "app/config.py",
             "    touch_enabled: bool = False",
             "    touch_enabled: bool = True",
             "tests/test_config.py",
             "«не пишет первым» снова держится переменной, а не кодом"),
)


# --------------------------------------------------------------------------
# Прогон
# --------------------------------------------------------------------------

CAUGHT = "поймана"
SURVIVED = "ПРОПУЩЕНА"
STALE = "НЕ ПРИМЕНИЛАСЬ"


def _run_pytest(tests: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", tests, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def run_one(mutation: Mutation) -> tuple[str, str]:
    """Применить, прогнать, ВЕРНУТЬ ФАЙЛ НА МЕСТО. Возвращает (вердикт, деталь)."""
    target = ROOT / mutation.path
    original = target.read_text(encoding="utf-8")
    if mutation.old not in original:
        return STALE, f"строки нет в {mutation.path} — реестр отстал от кода"

    target.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
    try:
        proc = _run_pytest(mutation.tests)
    finally:
        target.write_text(original, encoding="utf-8")
        # Побайтная сверка: молча оставить мутацию в рабочем дереве — худшее,
        # что этот скрипт может сделать.
        if target.read_text(encoding="utf-8") != original:
            raise SystemExit(f"НЕ ВОССТАНОВЛЕН {mutation.path} — почините руками до коммита")

    if proc.returncode != 0:
        tail = [ln for ln in proc.stdout.splitlines() if ln.startswith("FAILED")]
        return CAUGHT, (tail[0][:120] if tail else "тест покраснел")
    return SURVIVED, f"{mutation.tests} зелёный при сломанном правиле"


def _working_tree_is_clean() -> bool:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                          capture_output=True, text=True)
    return proc.returncode == 0 and not proc.stdout.strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", help="прогнать только правила этой группы (подстрока)")
    parser.add_argument("--list", action="store_true", help="показать реестр и выйти")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="не требовать чистого рабочего дерева")
    args = parser.parse_args()

    chosen = [m for m in MUTATIONS if not args.rule or args.rule.lower() in m.rule.lower()]
    if not chosen:
        print(f"нет мутаций для группы {args.rule!r}; есть: "
              + ", ".join(sorted({m.rule for m in MUTATIONS})))
        return 2

    if args.list:
        for m in chosen:
            print(f"{m.rule:<18} {m.path:<32} {m.why}")
        print(f"\nвсего: {len(chosen)}")
        return 0

    if not args.allow_dirty and not _working_tree_is_clean():
        print("Рабочее дерево грязное. Мутации правят файлы на месте и возвращают "
              "их обратно; на грязном дереве оборванный прогон не отличить от "
              "своих правок. Закоммитьте или спрячьте изменения, либо --allow-dirty.")
        return 2

    survived: list[Mutation] = []
    stale: list[Mutation] = []
    for m in chosen:
        verdict, detail = run_one(m)
        print(f"{verdict:<14} [{m.rule}] {m.why}\n               {detail}", flush=True)
        if verdict == SURVIVED:
            survived.append(m)
        elif verdict == STALE:
            stale.append(m)

    print(f"\nвсего {len(chosen)}: поймано {len(chosen) - len(survived) - len(stale)}, "
          f"пропущено {len(survived)}, не применилось {len(stale)}")
    if survived:
        print("\nПРОПУЩЕННЫЕ — правило сломано, а прогон зелёный:")
        for m in survived:
            print(f"  [{m.rule}] {m.why} ({m.path}, ловить в {m.tests})")
    if stale:
        print("\nНЕ ПРИМЕНИВШИЕСЯ — код уехал вперёд реестра, мутацию переписать:")
        for m in stale:
            print(f"  [{m.rule}] {m.why} ({m.path})")
    return 1 if (survived or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
