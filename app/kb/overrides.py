"""Слой правок поверх YAML: путь в документе -> новое значение.

ЗАЧЕМ ЭТО СУЩЕСТВУЕТ. Файловая система контейнера на Railway эфемерная:
запись в `app/kb/catalog.yaml` пережила бы ровно до следующего деплоя и
исчезла бы МОЛЧА — цена откатилась бы к старой, и узнали бы об этом от
клиента. Поэтому YAML остаётся базой (он в git, он ревьюится), а правки
живут отдельным слоем в БД и накладываются поверх при каждой загрузке.

Модуль намеренно без БД и без aiogram: здесь только разбор пути, наложение
и валидация. Хранилище — `app/kb/override_store.py`, меню — `app/ops/menu.py`.

ФОРМАТ ПУТИ — тот же, что уже печатает загрузчик в своих ошибках
(`iter_disputed_leaves`), плюс селектор по id:

    $.catalog.zones[id=dome_bags].pricing.weekend_per_hour
    $.catalog.constants.working_window.from
    $.catalog.zones[0].capacity

`[id=...]` предпочтительнее `[0]`: индекс молча начинает указывать на
другую зону, если кто-то поменяет порядок в catalog.yaml, а правка при
этом останется «валидной» и уедет не туда.

ЗНАЧЕНИЕ ЗАМЕНЯЕТ УЗЕЛ ЦЕЛИКОМ, а не дописывается в него. Для
DisputedValue-листа это принципиально: дописать `value` в узел, где уже
есть `disputed`, значит получить документ с обоими полями сразу — ровно то,
что `validate_no_orphan_disputed` считает ошибкой. Правка спорного поля
поэтому кладёт новый лист целиком: `{"value": 5000, "resolved_from": "..."}`.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Union

# $.a.b[0].c  |  $.a[id=zone_x].b
_STEP_RE = re.compile(r"""
    \.(?P<key>[A-Za-z_][A-Za-z0-9_]*)      # .key
  | \[(?P<index>\d+)\]                      # [0]
  | \[id=(?P<id>[^\]]+)\]                   # [id=dome_bags]
""", re.VERBOSE)


class OverrideError(ValueError):
    """Правка отклонена. Текст предназначен ОПЕРАТОРУ в Telegram, а не
    разработчику: он попадает в чат как есть."""


@dataclass(frozen=True)
class IdSelector:
    value: str


Step = Union[str, int, IdSelector]


def parse_path(path: str) -> list[Step]:
    if not path.startswith("$"):
        raise OverrideError(f"путь должен начинаться с '$': {path!r}")

    steps: list[Step] = []
    position = 1
    for match in _STEP_RE.finditer(path):
        if match.start() != position:
            raise OverrideError(f"непонятный фрагмент пути в позиции {position}: {path!r}")
        position = match.end()
        if match.group("key") is not None:
            steps.append(match.group("key"))
        elif match.group("index") is not None:
            steps.append(int(match.group("index")))
        else:
            steps.append(IdSelector(match.group("id")))

    if position != len(path):
        raise OverrideError(f"непонятный фрагмент пути в позиции {position}: {path!r}")
    if not steps:
        raise OverrideError(f"пустой путь: {path!r}")
    return steps


def _descend(node: Any, step: Step, path: str) -> Any:
    if isinstance(step, str):
        if not isinstance(node, dict) or step not in node:
            raise OverrideError(f"в документе нет пути {path} (не найден ключ {step!r})")
        return node[step]
    if isinstance(step, int):
        if not isinstance(node, list) or step >= len(node):
            raise OverrideError(f"в документе нет пути {path} (нет элемента [{step}])")
        return node[step]
    if not isinstance(node, list):
        raise OverrideError(f"в документе нет пути {path} (ожидался список для [id=...])")
    for item in node:
        if isinstance(item, dict) and item.get("id") == step.value:
            return item
    raise OverrideError(f"в документе нет пути {path} (нет элемента с id={step.value!r})")


def get_at(root: Any, path: str) -> Any:
    node = root
    for step in parse_path(path):
        node = _descend(node, step, path)
    return node


def set_at(root: Any, path: str, value: Any) -> Any:
    """Возвращает НОВЫЙ документ с заменённым узлом. Исходный не трогает:
    загрузчик валидирует результат и, если правка плохая, должен остаться с
    прежним рабочим каталогом, а не с полупримененным."""
    steps = parse_path(path)
    result = copy.deepcopy(root)

    node = result
    for step in steps[:-1]:
        node = _descend(node, step, path)

    last = steps[-1]
    # Последний шаг проверяем тем же `_descend` — он и убедится, что путь
    # существует. Правка «мимо документа» должна падать понятной ошибкой, а
    # не создавать новый ключ: опечатка в пути иначе выглядела бы как
    # успешное сохранение, которое ни на что не влияет.
    _descend(node, last, path)

    if isinstance(last, str):
        node[last] = value
    elif isinstance(last, int):
        node[last] = value
    else:
        for i, item in enumerate(node):
            if isinstance(item, dict) and item.get("id") == last.value:
                node[i] = value
                break
    return result


@dataclass(frozen=True)
class Override:
    """Одна правка. `value` — JSON-совместимое значение, которое ПОЛНОСТЬЮ
    заменяет узел по `path`."""

    path: str
    value: Any


def apply_overrides(raw: dict, overrides: list[Override]) -> dict:
    """Накладывает правки по порядку. Порядок = хронология: последняя
    правка того же пути побеждает, как и ожидает человек, который правил
    цену дважды."""
    result = raw
    for override in overrides:
        result = set_at(result, override.path, override.value)
    return result
