"""Каждый scripts/*.py обязан реально импортироваться.

Повод: scripts/sync_yclients_services.py ссылался на app.media.photo_import,
который существовал на диске у разработчика, но не был закоммичен в git
(".gitignore"/".dockerignore" ловили "media/" без якоря на корень — заодно с
реальными фото зон попал и app/media/, исходный код). Все 28 тестов скрипта
были зелёными, потому что тестировали функции в обход точки входа и
запускались в том же окружении, где файл физически лежит. На Railway (сборка
из git) это ModuleNotFoundError на первой строке.

Просто "импортировать модуль" здесь недостаточно — импорт в ЭТОМ окружении
пройдёт для той же самой дыры (файл ведь на диске есть), поэтому вторая
проверка сверяет каждый локальный импорт (app./scripts./migrations.) с
`git ls-files`: если файл существует на диске, но не отслеживается git — это
и есть тот самый сценарий "падает в чистом контейнере, но не локально".
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL_PACKAGES = ("app", "scripts", "migrations")

SCRIPT_MODULES = [
    f"scripts.{p.stem}"
    for p in sorted((REPO_ROOT / "scripts").glob("*.py"))
    if p.stem != "__init__"
]


@pytest.mark.parametrize("module_name", SCRIPT_MODULES)
def test_script_module_imports(module_name):
    importlib.import_module(module_name)


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return {line.replace("\\", "/") for line in out.splitlines() if line}


def _local_module_to_path(module: str) -> pathlib.Path | None:
    """app.media.photo_import -> app/media/photo_import.py, либо .../__init__.py."""
    rel = pathlib.Path(*module.split("."))
    as_file = rel.with_suffix(".py")
    if (REPO_ROOT / as_file).is_file():
        return as_file
    as_package = rel / "__init__.py"
    if (REPO_ROOT / as_package).is_file():
        return as_package
    return None


def _local_imports_in(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            candidates = [node.module] if node.module else []
        else:
            continue
        for module in candidates:
            if module.split(".")[0] in LOCAL_PACKAGES:
                modules.add(module)
    return modules


def test_every_local_import_from_scripts_is_tracked_by_git():
    """Файл на диске — не гарантия, что он доедет до Railway. Только git."""
    tracked = _tracked_files()
    untracked: dict[str, str] = {}

    for py_file in sorted((REPO_ROOT / "scripts").glob("*.py")):
        for module in _local_imports_in(py_file):
            resolved = _local_module_to_path(module)
            if resolved is None:
                continue  # не файл/пакет (например, атрибут внутри модуля) — не наша забота здесь
            if resolved.as_posix() not in tracked:
                untracked[module] = f"{py_file.relative_to(REPO_ROOT)} -> {resolved.as_posix()}"

    assert not untracked, (
        "Локальный модуль импортируется, но не отслеживается git — на Railway "
        f"(сборка из git) это ModuleNotFoundError: {untracked}"
    )


def test_the_probe_stub_accepts_what_the_loop_passes():
    """Заглушку зовут с тем же числом аргументов, что и настоящую функцию.

    Повод живой: заглушка была `lambda _text: None`, в `forced_tool_for`
    добавился второй аргумент, и контрольное плечо замера стало падать на
    КАЖДОЙ попытке — 43 TypeError из 45. Отчёт при этом показывал «0 из 5»,
    то есть выглядел как измерение, а был обломками. Сигнатуры расходятся
    молча; пусть расходятся громко.
    """
    import inspect

    from app.agent.tool_forcing import forced_tool_for
    from scripts.probe_tool_forcing import no_forcing

    signature = inspect.signature(forced_tool_for)
    # Настоящую функцию петля зовёт с текстом и контекстом.
    bound = signature.bind("на сегодня есть окошко?", "интересует баня")
    assert no_forcing(*bound.args, **bound.kwargs) is None
