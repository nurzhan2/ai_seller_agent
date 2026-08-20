"""Обезличивание реальных переписок перед публикацией репозитория.

    python -m scripts.anonymize_dialogs            # применить
    python -m scripts.anonymize_dialogs --check     # только проверить, что уже применено

Заказчик согласовал публикацию коммерческих условий (прайс, полы скидок,
механика дожима) как есть. Но в диалогах — персональные данные ТРЕТЬИХ ЛИЦ
(клиентов), на которые согласия нет и быть не может. Меняется:

    - имена клиентов → «Клиент 1», «Клиент 2», ... — стабильно, тот же
      человек в рамках диалога = тот же номер. Имена менеджеров/сотрудников
      заказчика НЕ трогаются — они публичные лица бизнеса, а не третьи лица.
    - телефоны в любом формате → +7 XXX XXX-XX-XX
    - номера карт (16 цифр) → вырезаны
    - ссылки на объявления Авито (несут utm/sharing-метки) → обрезаны до домена

docs/analysis/dialogs.json размечен по диалогам (`client_name` на диалог) —
оттуда и берётся список имён для вычёркивания. docs/source/real_dialogs.md —
исходный неструктурированный экспорт; выровнен с dialogs.json по разделителю
«=== КОНЕЦ ДИАЛОГА ===», который делит файл РОВНО на 27 сегментов в том же
порядке, что и 27 диалогов в JSON (проверено вручную перед тем, как строить
на этом логику: посчитаны сегменты, сверены с числом диалогов).
Имя из диалога N вычёркивается только внутри сегмента N, а не по всему файлу
— иначе одинаковое имя клиента в одном диалоге и менеджера в другом (в этих
переписках такое есть — «Мария») стёрлось бы не в том месте.

Оригиналы уходят в docs/source/real_dialogs_raw.md и
docs/analysis/dialogs_raw.json — не коммитятся (см. .gitignore, *_raw.md/
*_raw.json).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIALOGS_JSON = ROOT / "docs" / "analysis" / "dialogs.json"
DIALOGS_JSON_RAW = ROOT / "docs" / "analysis" / "dialogs_raw.json"
REAL_DIALOGS_MD = ROOT / "docs" / "source" / "real_dialogs.md"
REAL_DIALOGS_MD_RAW = ROOT / "docs" / "source" / "real_dialogs_raw.md"
DIALOG_SEPARATOR = "=== КОНЕЦ ДИАЛОГА ==="

PHONE = re.compile(r"(?:\+7|8)[\d\s\-\(\)]{9,14}\d")
CARD = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")
AVITO_LINK = re.compile(r"https?://link\.avito\.ru/go\?to=\S+")

_ALIAS_IN_PARENS = re.compile(r"[«\"]([^»\"]+)[»\"]")
_NAME_WORD = re.compile(r"[А-ЯЁ][а-яё]+")


def redact_universal(text: str) -> str:
    """Телефоны/карты/ссылки — применяется ко всему файлу без исключений."""
    text = AVITO_LINK.sub("https://link.avito.ru/go?to=[объявление]", text)
    text = CARD.sub("[номер карты]", text)
    text = PHONE.sub("+7 XXX XXX-XX-XX", text)
    return text


def name_tokens(client_name: str) -> list[str]:
    """Все узнаваемые части имени/ника клиента — самые длинные первыми,
    чтобы «Ирина Трушина» вычёркивалось раньше, чем одинокое «Ирина»."""
    tokens: set[str] = set()
    alias_match = _ALIAS_IN_PARENS.search(client_name)
    if alias_match:
        tokens.add(alias_match.group(1))
        client_name = client_name[: alias_match.start()]
    stripped = re.sub(r"[()«»\"]", " ", client_name).strip()
    if stripped:
        tokens.add(stripped)
    for word in _NAME_WORD.findall(stripped):
        tokens.add(word)
    return sorted(tokens, key=len, reverse=True)


def redact_client_name(text: str, client_name: str, label: str) -> str:
    for token in name_tokens(client_name):
        text = re.sub(rf"\b{re.escape(token)}\b", label, text)
    return text


def anonymize_dialogs_json(data: list[dict]) -> tuple[list[dict], dict[str, str]]:
    labels: dict[str, str] = {}   # оригинальное имя -> «Клиент N»
    counter = 0
    out = []
    for dialog in data:
        dialog = json.loads(json.dumps(dialog))   # глубокая копия
        client_name = dialog.get("client_name")
        if client_name:
            if client_name not in labels:
                counter += 1
                labels[client_name] = f"Клиент {counter}"
            label = labels[client_name]
            dialog["client_name"] = label
            for turn in dialog.get("turns", []):
                turn["text"] = redact_client_name(turn["text"], client_name, label)
                if "note" in turn:
                    turn["note"] = redact_client_name(turn["note"], client_name, label)
        for turn in dialog.get("turns", []):
            turn["text"] = redact_universal(turn["text"])
            if "note" in turn:
                turn["note"] = redact_universal(turn["note"])
        out.append(dialog)
    return out, labels


def anonymize_real_dialogs_md(text: str, dialogs: list[dict], labels: dict[str, str]) -> str:
    segments = text.split(DIALOG_SEPARATOR)
    if len(segments) != len(dialogs):
        raise ValueError(
            f"Сегментов в real_dialogs.md ({len(segments)}) не совпадает с "
            f"числом диалогов в dialogs.json ({len(dialogs)}) — разметка разошлась, "
            "автоматическая привязка имени к сегменту небезопасна. Остановлено."
        )

    out_segments = []
    for dialog, segment in zip(dialogs, segments):
        client_name = dialog.get("client_name")
        if client_name and client_name in labels:
            segment = redact_client_name(segment, client_name, labels[client_name])
        segment = redact_universal(segment)
        out_segments.append(segment)
    return DIALOG_SEPARATOR.join(out_segments)


def check_no_leftover_phones(*texts: str) -> list[str]:
    problems = []
    for text in texts:
        for m in PHONE.finditer(text):
            if m.group() != "+7 XXX XXX-XX-XX":
                problems.append(m.group())
    return problems


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не менять")
    args = parser.parse_args()

    if DIALOGS_JSON_RAW.exists() or REAL_DIALOGS_MD_RAW.exists():
        print(
            "Похоже, обезличивание уже применялось (найдены *_raw файлы). "
            "Повторный запуск без ручной проверки может задвоить замены — остановлено.",
            file=sys.stderr,
        )
        return 1

    original_json = json.loads(DIALOGS_JSON.read_text(encoding="utf-8"))
    anonymized_json, labels = anonymize_dialogs_json(original_json)

    original_md = REAL_DIALOGS_MD.read_text(encoding="utf-8")
    anonymized_md = anonymize_real_dialogs_md(original_md, original_json, labels)

    json_text = json.dumps(anonymized_json, ensure_ascii=False, indent=2) + "\n"
    problems = check_no_leftover_phones(json_text, anonymized_md)
    if problems:
        print("❌ Остались нераспознанные телефоны, ничего не записано:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(f"Клиентов обезличено: {len(labels)}")
    for original, label in labels.items():
        print(f"  {original!r} → {label}")

    if args.check:
        print("\n--check: файлы не изменены.")
        return 0

    DIALOGS_JSON.rename(DIALOGS_JSON_RAW)
    REAL_DIALOGS_MD.rename(REAL_DIALOGS_MD_RAW)
    DIALOGS_JSON.write_text(json_text, encoding="utf-8")
    REAL_DIALOGS_MD.write_text(anonymized_md, encoding="utf-8")

    print(f"\nОригиналы сохранены: {DIALOGS_JSON_RAW}, {REAL_DIALOGS_MD_RAW}")
    print("Обезличенные версии записаны на место исходных.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
