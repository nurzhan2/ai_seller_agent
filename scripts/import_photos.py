"""Импорт фотографий по зонам в каталог.

    python -m scripts.import_photos /path/to/local/folder
    python -m scripts.import_photos /path/to/local/folder --dry-run

Ожидаемая структура папки — подпапка на зону/категорию, имя сопоставляется
с zone_id через app/kb/photo_folder_map.yaml (регистр и дефисы/подчёркивания
не важны):

    folder/
      Баня Русский стиль/
        01_общий.jpg
        02_парилка.jpg
      Купол мешки/
        ...
      Детская площадка/
        ...

Порядок внутри папки = алфавитный порядок имён файлов, поэтому НАЗЫВАЙТЕ
файлы с числовым префиксом: первым должен идти общий план зоны, дальше детали.

--dry-run проверяет формат/размер файлов и сопоставление папок, ничего не
грузит и не пишет в catalog.yaml — используйте перед реальным запуском.

Идемпотентен: повторный запуск не создаёт дублей — уже загруженные файлы
(по содержимому, не по имени) пропускаются.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.media.photo_import import UnknownFolderError, import_root


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="локальная папка с подпапками по зонам")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только проверить файлы и сопоставление папок, ничего не грузить",
    )
    args = parser.parse_args()

    if not args.folder.is_dir():
        print(f"Не найдена папка: {args.folder}", file=sys.stderr)
        return 2

    settings = get_settings()
    dry_run = args.dry_run or settings.dry_run

    avito_client = None
    if not dry_run:
        from app.channels.avito import AvitoClient

        avito_client = AvitoClient(settings)

    print(f"{'DRY-RUN: ' if dry_run else ''}Импорт из {args.folder}")

    run = await import_root(args.folder, dry_run=dry_run, avito_client=avito_client)

    if avito_client is not None:
        await avito_client.aclose()

    for result in run.results:
        label = f"[{result.kind}:{result.key}]"
        print(f"{label} загружено: {result.uploaded}, уже было: {result.reused}, "
              f"пропущено: {len(result.skipped)}")
        for filename, reason in result.skipped:
            print(f"    пропущен {filename}: {reason}")

    if run.unmapped_folders:
        print("\nНе сопоставленные папки:", file=sys.stderr)
        for message in run.unmapped_folders:
            print(f"  {message}", file=sys.stderr)

    if dry_run:
        print("\nDRY-RUN: ничего не загружено и не записано, это была только проверка.")

    return 1 if run.unmapped_folders else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
