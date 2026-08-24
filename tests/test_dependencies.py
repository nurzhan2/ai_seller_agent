"""Каждый сторонний импорт в app/, migrations/, scripts/ обязан быть в
requirements.txt.

Причина существования этого файла: `prometheus_client` использовался в
app/metrics.py, но не был в requirements.txt. Локально пакет стоял в
окружении разработчика (тянулся транзитивно другим пакетом или остался от
экспериментов), поэтому все тесты были зелёными — а в чистом контейнере на
Railway `import app.main` падал `ModuleNotFoundError` ещё на старте,
раньше первой строчки собственного лога. Обычный прогон тестов эту дыру
не ловит в принципе: тесты запускаются в ТОМ ЖЕ окружении, где пакет уже
стоит. Единственный способ поймать её без реальной чистой пересборки
окружения — сверить исходники с requirements.txt статически, что этот файл
и делает при каждом прогоне `pytest`.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ("app", "migrations", "scripts")
LOCAL_PACKAGES = {"app", "migrations", "scripts", "tests"}

# Импортное имя не всегда совпадает с именем пакета на PyPI — это тот самый
# класс расхождения, из-за которого дыра вообще возможна: сверка "в лоб"
# ничего не найдёт для настоящих совпадений (SQLAlchemy/sqlalchemy и т.п. —
# ловится нормализацией регистра ниже), но для этих пар нужен явный маппинг.
IMPORT_TO_PACKAGE = {
    "pil": "pillow",
    "yaml": "pyyaml",
    "pydantic_settings": "pydantic-settings",
}


def _stdlib_modules() -> set[str]:
    if sys.version_info < (3, 10):
        raise RuntimeError(
            "sys.stdlib_module_names требует Python 3.10+ — проект и так "
            "таргетит 3.12 (см. Dockerfile), поэтому здесь без fallback."
        )
    return set(sys.stdlib_module_names)


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def _third_party_imports(paths: list[pathlib.Path]) -> dict[str, str]:
    """{нормализованное_имя_пакета: пример_файла_где_встретилось}."""
    stdlib = _stdlib_modules()
    found: dict[str, str] = {}
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # относительный импорт — точно свой код
                modules = [node.module] if node.module else []
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top in stdlib or top in LOCAL_PACKAGES or top == "__future__":
                    continue
                package = IMPORT_TO_PACKAGE.get(top.lower(), top)
                found.setdefault(_normalize(package), str(path.relative_to(REPO_ROOT)))
    return found


def _declared_packages() -> set[str]:
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    declared = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # "uvicorn[standard]>=0.32" -> "uvicorn"; отсекаем extras и версию.
        name = line.split("[")[0]
        for op in (">=", "==", "<=", "~=", "!=", ">", "<"):
            name = name.split(op)[0]
        declared.add(_normalize(name.strip()))
    return declared


def test_every_third_party_import_is_declared_in_requirements():
    paths = [
        p
        for d in SCAN_DIRS
        for p in (REPO_ROOT / d).rglob("*.py")
    ]
    imported = _third_party_imports(paths)
    declared = _declared_packages()

    missing = {pkg: example for pkg, example in imported.items() if pkg not in declared}
    assert not missing, (
        "Импортируется в коде, но не объявлено в requirements.txt (пример файла "
        "в скобках) — именно так тесты остаются зелёными локально и падают "
        f"ModuleNotFoundError в чистом контейнере: {missing}"
    )
