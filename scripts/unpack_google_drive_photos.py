"""Распаковка архивов фотографий, выгруженных из Google Drive, в
media/photos/<zone_id>/ — с числовым префиксом порядка.

    python -m scripts.unpack_google_drive_photos                # план, ничего не пишет
    python -m scripts.unpack_google_drive_photos --apply         # реально копирует файлы

Архивы ищутся в ~/Downloads по маске `*-2026*Z-*-*.zip` (формат имени,
который Google Drive даёт при экспорте папки) — не по конкретным именам,
они у каждого экспорта свои.

Три ловушки, которые здесь закрыты:

1. Кириллица в именах файлов внутри zip. Экспорт Google Drive обычно
   выставляет UTF-8 flag bit (ZIP General Purpose Flag, бит 11) — тогда
   `zipfile` в Python уже декодирует правильно, ничего чинить не нужно.
   Но полагаться на это нельзя: если бит не выставлен, Python по умолчанию
   декодирует как cp437 и превращает кириллицу в мусор. Здесь в этом случае
   raw-байты восстанавливаются через `.encode('cp437')` и пробуются cp866,
   затем cp1251 — побеждает первый вариант, где получилась читаемая
   кириллица без символов подстановки.
2. Многотомность (Google Drive режет большую папку на -1-001, -1-002, ...).
   Группировка идёт не по имени архива, а по имени папки ВНУТРИ архива —
   так все тома одной зоны сами собираются вместе, без отдельной логики
   разбора суффиксов.
3. Сопоставление по имени папки внутри архива, а не по имени архива
   (см. п.2 — тот же механизм это закрывает автоматически).

Порядок файлов внутри зоны: сперва пробуем понять его по имени (числовая
часть вида IMG_7941 — сортировка по номеру, это порядок съёмки камерой).
Если у файлов зоны нет общего числового шаблона (голые UUID, разные типы
имён вперемешку) — сортируем по убыванию разрешения (более крупный кадр
чаще оказывается общим планом) и явно помечаем зону как "низкая
уверенность" в выводе. В любом случае финальный порядок — это ПРЕДЛОЖЕНИЕ:
скрипт его не применяет молча, а печатает на подтверждение; --apply нужно
вызвать осознанно вторым запуском (или сразу, если план и так очевиден).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.media.photo_import import load_folder_map, normalize_name, resolve_folder, UnknownFolderError  # noqa: E402

DOWNLOADS = Path.home() / "Downloads"
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media" / "photos"
ARCHIVE_MASK = re.compile(r".*-2026\d{4}T\d{6}Z-\d+-\d+\.zip$")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic"}
# Всё остальное — видео (.mp4), системные файлы и т.п. — не фото зоны.

# Только «камерные» имена вида IMG_7940, IMG_7961(1) — специально НЕ матчит
# UUID/хэш-имена (7EE177CD-163E-...): в них тоже полно цифр, но это не номер
# кадра по порядку съёмки, а мусор, который раньше давал ложную «высокую
# уверенность» в порядке для зон вроде Юрты, где часть фото названы UUID.
_CAMERA_NAME = re.compile(r"^[A-Za-z_]*(\d{3,6})(?:\(\d+\))?$")


@dataclass
class ExtractedFile:
    archive: str
    folder_raw: str          # исходное имя папки внутри архива, с пробелами
    filename: str
    data: bytes


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """Правильное имя файла независимо от того, выставлен ли UTF-8 flag bit."""
    if info.flag_bits & 0x800:
        return info.filename   # zipfile уже декодировал как UTF-8 — верим

    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename   # ASCII-имя, декодировать не от чего

    for enc in ("cp866", "cp1251"):
        try:
            decoded = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "�" not in decoded:
            return decoded
    return info.filename


def find_archives(downloads: Path = DOWNLOADS) -> list[Path]:
    return sorted(p for p in downloads.iterdir() if p.is_file() and ARCHIVE_MASK.match(p.name))


def extract_all(archives: list[Path]) -> list[ExtractedFile]:
    files: list[ExtractedFile] = []
    for archive in archives:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = decode_zip_name(info)
                parts = name.split("/", 1)
                if len(parts) < 2:
                    continue   # файл не внутри подпапки — не наш случай
                folder_raw, filename = parts[0], parts[1]
                if filename.endswith("/") or not filename:
                    continue
                ext = Path(filename).suffix.lower()
                if ext not in IMAGE_EXTENSIONS:
                    continue
                data = zf.read(info)
                files.append(ExtractedFile(
                    archive=archive.name, folder_raw=folder_raw.strip(),
                    filename=filename, data=data,
                ))
    return files


@dataclass
class ZonePlan:
    zone_key: str            # zone_id или site-категория
    kind: str                 # "zone" | "site"
    folder_raw: str
    order_confidence: str     # "high" (числовой шаблон) | "low" (по разрешению)
    ordered: list[tuple[ExtractedFile, tuple[int, int]]] = field(default_factory=list)


def _resolution(data: bytes) -> tuple[int, int]:
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(data)) as img:
        return img.size


def _order_key_by_number(f: ExtractedFile) -> Optional[int]:
    match = _CAMERA_NAME.match(Path(f.filename).stem)
    return int(match.group(1)) if match else None


# Для grill_house и yurt имя файла не давало порядка вообще (UUID-подобные
# имена вперемешку с IMG_NNNN — см. docstring _CAMERA_NAME), поэтому вместо
# сортировки по разрешению «вслепую» эти два набора реально просмотрены
# глазами и упорядочены по смыслу: общий план участка → переходный/detail
# план → интерьер. Дубликат IMG_7961(1).PNG (побайтово идентичен IMG_7961.PNG)
# исключён — дедуп по содержимому в app/media/photo_import.py всё равно бы
# его схлопнул, но нет смысла нести его через весь пайплайн.
# Если в будущем в эти папки добавятся новые фото — ключи ниже не покрывают
# их: build_plan() возвращает неперечисленные файлы в конец в порядке имени,
# отдельно помеченными как непросмотренные (см. _apply_manual_order).
MANUAL_ORDER: dict[str, list[str]] = {
    "гриль домик": [
        "IMG_7961.PNG", "IMG_7962.PNG",
        "611EAF1D-9181-4078-9A76-53ED268E3C5D.jpeg",
        "3664765D-ECE0-477E-84C7-103DE810A73E.jpeg",
        "IMG_7959.PNG", "IMG_7960.PNG", "IMG_8162.PNG",
    ],
    "юрта": [
        "IMG_5521.PNG", "7EE177CD-163E-4B15-A77E-7AEE3A1AEA79.webp",
        "815a8f96-4833-40ce-a174-b8e5277d1f49.jpg", "IMG_5522.PNG",
        "FE6D3AA8-4138-42A1-BC28-DE60ED490625.webp",
        "IMG_5523.PNG", "4485e385-a235-4206-ac7f-893adadb202b.jpg",
        "e59684a5-c8f8-4ff8-a503-3b4c60a39ee9.jpg",
        "c1a39f94-b15e-43e5-b80e-a4c12d861608.jpg",
        "IMG_5524.PNG", "IMG_5525.PNG", "IMG_5526.PNG",
    ],
}
DROP_FILES = {"IMG_7961(1).PNG"}   # побайтовый дубликат IMG_7961.PNG


def _apply_manual_order(
    folder_raw: str, group_files: list["ExtractedFile"]
) -> Optional[list["ExtractedFile"]]:
    manual = MANUAL_ORDER.get(normalize_name(folder_raw))
    if manual is None:
        return None
    by_name = {f.filename: f for f in group_files if f.filename not in DROP_FILES}
    ordered = [by_name.pop(name) for name in manual if name in by_name]
    leftover = sorted(by_name.values(), key=lambda f: f.filename)
    if leftover:
        print(
            f"  ⚠️ новые файлы в «{folder_raw}», не просмотренные вручную — "
            f"добавлены в конец по имени: {[f.filename for f in leftover]}",
            file=sys.stderr,
        )
    return ordered + leftover


def build_plan(files: list[ExtractedFile]) -> tuple[list[ZonePlan], list[str]]:
    folder_map = load_folder_map()
    groups: dict[str, list[ExtractedFile]] = {}
    for f in files:
        groups.setdefault(f.folder_raw, []).append(f)

    plans: list[ZonePlan] = []
    unmapped: list[str] = []

    for folder_raw, group_files in sorted(groups.items()):
        try:
            resolved = resolve_folder(folder_raw, folder_map)
        except UnknownFolderError as exc:
            unmapped.append(str(exc))
            continue

        manual = _apply_manual_order(folder_raw, group_files)
        if manual is not None:
            ordered_files = manual
            confidence = "manual"
        else:
            numbers = [_order_key_by_number(f) for f in group_files]
            if all(n is not None for n in numbers) and len(set(numbers)) == len(numbers):
                ordered_files = sorted(group_files, key=_order_key_by_number)
                confidence = "high"
            else:
                ordered_files = sorted(
                    group_files, key=lambda f: _resolution(f.data), reverse=True
                )
                confidence = "low"

        plan = ZonePlan(
            zone_key=resolved.key, kind=resolved.kind, folder_raw=folder_raw,
            order_confidence=confidence,
        )
        for f in ordered_files:
            plan.ordered.append((f, _resolution(f.data)))
        plans.append(plan)

    return plans, unmapped


def print_plan(plans: list[ZonePlan], unmapped: list[str]) -> None:
    print(f"Найдено зон/категорий: {len(plans)}\n")
    for plan in plans:
        conf = {
            "high": "числовой порядок в имени",
            "manual": "просмотрено и упорядочено вручную (общий план → детали)",
            "low": "⚠️ порядок по разрешению (низкая уверенность — проверьте глазами)",
        }[plan.order_confidence]
        print(f"[{plan.kind}:{plan.zone_key}] ← папка «{plan.folder_raw}» ({conf})")
        for i, (f, (w, h)) in enumerate(plan.ordered, start=1):
            print(f"    {i:02d}_{f.filename}   {w}x{h}   ({f.archive})")
        print()

    if unmapped:
        print("НЕ СОПОСТАВЛЕНО (папка не найдена в photo_folder_map.yaml):")
        for msg in unmapped:
            print(f"  - {msg}")
        print()

    known_zone_ids = {p.zone_key for p in plans if p.kind == "zone"}
    if "house_relax" not in known_zone_ids:
        print(
            "⚠️  ВАЖНО: фотографий зоны house_relax (Домик для отдыха) НЕТ ни в одном "
            "архиве. Это единственная зона без фото, и самая дорогая (анкор 15 000 ₽, "
            "ступень уступки до 5 500 ₽). Нужен отдельный вопрос заказчику."
        )
    known_site = {p.zone_key for p in plans if p.kind == "site"}
    missing_site = {"playground", "restroom", "overview"} - known_site
    if missing_site:
        print(
            f"⚠️  ВАЖНО: нет общих фото по категориям: {', '.join(sorted(missing_site))}. "
            "В реальных переписках про санузел и детскую площадку спрашивали регулярно — "
            "нужен отдельный вопрос заказчику."
        )


def apply_plan(plans: list[ZonePlan], media_root: Path = MEDIA_ROOT) -> None:
    for plan in plans:
        if plan.kind != "zone":
            continue   # site-категории размечены в catalog.yaml иначе — см. app/media/photo_import.py
        out_dir = media_root / plan.zone_key
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, (f, _res) in enumerate(plan.ordered, start=1):
            ext = Path(f.filename).suffix.lower()
            out_path = out_dir / f"{i:02d}{ext}"
            out_path.write_bytes(f.data)
        print(f"[{plan.zone_key}] {len(plan.ordered)} файлов → {out_dir}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=DOWNLOADS)
    parser.add_argument("--apply", action="store_true", help="реально скопировать файлы")
    args = parser.parse_args()

    archives = find_archives(args.downloads)
    if not archives:
        print(f"Архивы не найдены в {args.downloads} по маске *-2026*Z-*-*.zip", file=sys.stderr)
        return 1
    print(f"Архивов найдено: {len(archives)}")
    for a in archives:
        print(f"  - {a.name}")
    print()

    files = extract_all(archives)
    plans, unmapped = build_plan(files)
    print_plan(plans, unmapped)

    if args.apply:
        apply_plan(plans)
    else:
        print("Это только план — файлы не записаны. Повторите с --apply, когда порядок подтверждён.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
