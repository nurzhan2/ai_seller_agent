"""Импорт фотографий по зонам: сопоставление папок, валидация, сжатие,
загрузка в Авито, запись image_id в catalog.yaml.

ИДЕМПОТЕНТНОСТЬ держится на манифесте (app/kb/.photos_manifest.json),
проиндексированном по sha256 СОДЕРЖИМОГО файла, а не по имени. Переименование
локального файла поэтому не вызывает повторную загрузку, а подмена
содержимого под тем же именем — вызывает, что и требуется: манифест отражает
факт «этот конкретный кадр уже есть в Авито», а не «этот путь мы видели».

catalog.yaml переписывается ТОЧЕЧНОЙ правкой текста (regex по блоку зоны), а
не через yaml.safe_load + yaml.dump — полная пересборка через PyYAML стёрла
бы все комментарии в файле, а их там много и они важны (провенанс каждого
разрешённого спорного поля). Тот же приём уже применялся раньше в проекте.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("parmangal.photo_import")

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
CATALOG_PATH = KB_DIR / "catalog.yaml"
FOLDER_MAP_PATH = KB_DIR / "photo_folder_map.yaml"
MANIFEST_PATH = KB_DIR / ".photos_manifest.json"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# НЕ ПОДТВЕРЖДЕНО спецификацией Авито: лимиты аплоада не описаны в
# app/channels/avito_endpoints.py (спек молчит про размер/разрешение).
# Консервативные дефолты, которые проходят у большинства площадок; если
# Авито пришлёт другой лимит — меняется одна константа здесь.
MAX_BYTES = 10 * 1024 * 1024       # 10 МБ
MAX_DIMENSION = 4000                # пикселей по длинной стороне
MIN_DIMENSION = 200                 # меньше — почти наверняка не фото зоны


# --------------------------------------------------------------------------
# Сопоставление имени папки с zone_id / категорией
# --------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Регистр, «ё», дефисы/подчёркивания/лишние пробелы — не важны."""
    text = unicodedata.normalize("NFKC", name).lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


@dataclass
class FolderMap:
    zones: dict[str, str]   # normalized synonym -> zone_id
    site: dict[str, str]    # normalized synonym -> category key


def load_folder_map(path: Path = FOLDER_MAP_PATH) -> FolderMap:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    zones: dict[str, str] = {}
    for zone_id, synonyms in (raw.get("zones") or {}).items():
        for syn in synonyms:
            zones[normalize_name(syn)] = zone_id
    site: dict[str, str] = {}
    for category, synonyms in (raw.get("site") or {}).items():
        for syn in synonyms:
            site[normalize_name(syn)] = category
    return FolderMap(zones=zones, site=site)


class UnknownFolderError(ValueError):
    """Понятная ошибка при несовпадении имени папки — с подсказкой, что делать."""

    def __init__(self, folder_name: str, folder_map: FolderMap):
        known = sorted(set(folder_map.zones.values()) | set(folder_map.site.values()))
        super().__init__(
            f"Папка {folder_name!r} не сопоставлена ни с одной зоной. "
            f"Известные зоны/категории: {', '.join(known)}. "
            "Добавьте синоним в app/kb/photo_folder_map.yaml или переименуйте папку."
        )
        self.folder_name = folder_name


@dataclass(frozen=True)
class ResolvedFolder:
    kind: str   # "zone" | "site"
    key: str    # zone_id или категория (playground/restroom/overview)


def resolve_folder(folder_name: str, folder_map: FolderMap) -> ResolvedFolder:
    normalized = normalize_name(folder_name)
    if normalized in folder_map.zones:
        return ResolvedFolder("zone", folder_map.zones[normalized])
    if normalized in folder_map.site:
        return ResolvedFolder("site", folder_map.site[normalized])
    raise UnknownFolderError(folder_name, folder_map)


# --------------------------------------------------------------------------
# Проверка формата и размера, сжатие при необходимости
# --------------------------------------------------------------------------

def validate_image(path: Path) -> list[str]:
    """Список проблем; пустой список — файл годен к загрузке."""
    problems: list[str] = []
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        problems.append(
            f"неподдерживаемый формат {path.suffix!r} — допустимы {sorted(ALLOWED_EXTENSIONS)}"
        )
        return problems

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        problems.append("Pillow не установлен — не могу проверить изображение (pip install Pillow)")
        return problems

    try:
        with Image.open(path) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError) as exc:
        problems.append(f"не удалось открыть как изображение: {exc}")
        return problems

    if max(width, height) < MIN_DIMENSION:
        problems.append(f"изображение слишком маленькое ({width}x{height})")
    return problems


def ensure_within_limits(path: Path, tmp_dir: Path) -> Path:
    """Путь к файлу, который можно грузить: исходный, если он уже укладывается
    в лимиты, иначе — пережатая копия во временной папке (исходный не трогаем)."""
    from PIL import Image

    size = path.stat().st_size
    with Image.open(path) as img:
        width, height = img.size
    needs_resize = max(width, height) > MAX_DIMENSION
    needs_compress = size > MAX_BYTES

    if not needs_resize and not needs_compress:
        return path

    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / (path.stem + ".jpg")

    with Image.open(path) as img:
        img = img.convert("RGB")
        if needs_resize:
            scale = MAX_DIMENSION / max(img.size)
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            )
        quality = 90
        img.save(out_path, "JPEG", quality=quality, optimize=True)
        while out_path.stat().st_size > MAX_BYTES and quality > 40:
            quality -= 10
            img.save(out_path, "JPEG", quality=quality, optimize=True)

    logger.info(
        "photo compressed",
        extra={
            "file": str(path),
            "original_bytes": size,
            "final_bytes": out_path.stat().st_size,
        },
    )
    return out_path


# --------------------------------------------------------------------------
# Хеширование и манифест (идемпотентность)
# --------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(data: dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Точечная правка catalog.yaml (сохраняет комментарии)
# --------------------------------------------------------------------------

def _format_list(image_ids: list[str]) -> str:
    return "[" + ", ".join(image_ids) + "]" if image_ids else "[]"


def _find_zone_block(text: str, zone_id: str) -> tuple[int, int]:
    pattern = re.compile(rf"(?ms)^  - id: {re.escape(zone_id)}\b.*?(?=^  - id: |\Z)")
    match = pattern.search(text)
    if not match:
        raise ValueError(f"зона {zone_id!r} не найдена в catalog.yaml")
    return match.start(), match.end()


def patch_zone_photos(text: str, zone_id: str, image_ids: list[str]) -> str:
    start, end = _find_zone_block(text, zone_id)
    block = text[start:end]
    new_block, count = re.subn(
        r"(\n {4}photos: )\[[^\]]*\]", rf"\1{_format_list(image_ids)}", block, count=1
    )
    if count == 0:
        raise ValueError(f"зона {zone_id!r}: поле 'photos:' не найдено в блоке")
    return text[:start] + new_block + text[end:]


def patch_site_photos(text: str, category: str, image_ids: list[str]) -> str:
    section_re = re.compile(r"(?ms)^site_photos:\n(?:^ {2}.*\n?)*")
    match = section_re.search(text)

    if match is None:
        block = f"\nsite_photos:\n  {category}: {_format_list(image_ids)}\n"
        return text.rstrip("\n") + "\n" + block

    section = match.group(0)
    line_re = re.compile(rf"(?m)^  {re.escape(category)}: \[[^\]]*\]$")
    if line_re.search(section):
        new_section = line_re.sub(f"  {category}: {_format_list(image_ids)}", section, count=1)
    else:
        new_section = section.rstrip("\n") + f"\n  {category}: {_format_list(image_ids)}\n"
    return text[: match.start()] + new_section + text[match.end():]


# --------------------------------------------------------------------------
# Пайплайн
# --------------------------------------------------------------------------

@dataclass
class ZoneImportResult:
    kind: str
    key: str
    image_ids: list[str] = field(default_factory=list)   # итоговый порядок для catalog.yaml
    uploaded: int = 0
    reused: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (filename, reason)


async def _upload_one(avito_client: Any, path: Path) -> str:
    from app.channels.avito import _extract_image_id

    data = path.read_bytes()
    response = await avito_client.upload_image(data, filename=path.name)
    image_id = _extract_image_id(response)
    if image_id is None:
        raise RuntimeError(f"Avito uploadImages вернул неожиданный ответ для {path.name}: {response!r}")
    return image_id


async def import_folder(
    zone_folder: Path,
    resolved: ResolvedFolder,
    *,
    dry_run: bool,
    avito_client: Any,
    manifest: dict[str, Any],
    tmp_dir: Path,
) -> ZoneImportResult:
    """Импортирует один каталог (одну зону/категорию).

    Порядок файлов = алфавитный порядок имён в папке — поэтому «первым идёт
    общий план, дальше детали» обеспечивается тем, как заказчик называет
    файлы (например, 01_общий.jpg, 02_деталь.jpg), а не логикой скрипта:
    определить смысл кадра автоматически нельзя.
    """
    manifest_key = f"{resolved.kind}:{resolved.key}"
    zone_manifest = manifest.setdefault(manifest_key, {})

    result = ZoneImportResult(kind=resolved.kind, key=resolved.key)

    files = sorted((p for p in zone_folder.iterdir() if p.is_file()), key=lambda p: p.name)

    for file_path in files:
        problems = validate_image(file_path)
        if problems:
            result.skipped.append((file_path.name, "; ".join(problems)))
            logger.warning(
                "photo skipped", extra={"file": str(file_path), "problems": problems}
            )
            continue

        file_hash = sha256_of(file_path)

        existing = zone_manifest.get(file_hash)
        if existing is not None:
            result.image_ids.append(existing["image_id"])
            result.reused += 1
            continue

        if dry_run:
            # Только проверка: ни загрузки, ни записи в манифест/catalog.yaml.
            result.image_ids.append(f"<dry-run:{file_path.name}>")
            continue

        upload_path = ensure_within_limits(file_path, tmp_dir)
        image_id = await _upload_one(avito_client, upload_path)
        zone_manifest[file_hash] = {"file": file_path.name, "image_id": image_id}
        result.image_ids.append(image_id)
        result.uploaded += 1

    return result


@dataclass
class ImportRun:
    results: list[ZoneImportResult]
    unmapped_folders: list[str]


async def import_root(
    root: Path,
    *,
    dry_run: bool,
    avito_client: Any = None,
    folder_map: Optional[FolderMap] = None,
    manifest: Optional[dict[str, Any]] = None,
    catalog_path: Path = CATALOG_PATH,
    manifest_path: Path = MANIFEST_PATH,
    tmp_dir: Optional[Path] = None,
) -> ImportRun:
    if not dry_run and avito_client is None:
        raise ValueError("avito_client обязателен вне DRY_RUN")

    folder_map = folder_map or load_folder_map()
    manifest = manifest if manifest is not None else load_manifest(manifest_path)

    results: list[ZoneImportResult] = []
    unmapped: list[str] = []

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        working_tmp = tmp_dir or Path(tmp)

        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                resolved = resolve_folder(folder.name, folder_map)
            except UnknownFolderError as exc:
                unmapped.append(str(exc))
                logger.error("unmapped photo folder", extra={"folder": folder.name})
                continue

            result = await import_folder(
                folder, resolved,
                dry_run=dry_run, avito_client=avito_client,
                manifest=manifest, tmp_dir=working_tmp,
            )
            results.append(result)

            if not dry_run:
                catalog_text = catalog_path.read_text(encoding="utf-8")
                if resolved.kind == "zone":
                    catalog_text = patch_zone_photos(catalog_text, resolved.key, result.image_ids)
                else:
                    catalog_text = patch_site_photos(catalog_text, resolved.key, result.image_ids)
                catalog_path.write_text(catalog_text, encoding="utf-8")

    if not dry_run:
        save_manifest(manifest, manifest_path)

    return ImportRun(results=results, unmapped_folders=unmapped)
