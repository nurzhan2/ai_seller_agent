"""Тесты импорта фотографий.

Реальная сеть и реальный catalog.yaml не трогаются: catalog.yaml копируется
во временный файл, AvitoClient подменяется фейком.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.kb.loader import KB_DIR, load_catalog
from app.media.photo_import import (
    FolderMap,
    ResolvedFolder,
    UnknownFolderError,
    ensure_within_limits,
    import_root,
    load_folder_map,
    normalize_name,
    patch_site_photos,
    patch_zone_photos,
    resolve_folder,
    sha256_of,
    validate_image,
)


# --------------------------------------------------------------------------
# Нормализация и сопоставление папок
# --------------------------------------------------------------------------

def test_normalize_ignores_case_dashes_and_yo():
    assert normalize_name("Баня-Русский_Стиль") == normalize_name("баня русский стиль")
    assert normalize_name("Ёлка") == normalize_name("елка")


def test_real_folder_map_loads_and_covers_every_zone():
    """Настоящий конфиг покрывает все 10 зон каталога — иначе первая же
    реальная загрузка фото упрётся в непонятную ошибку."""
    folder_map = load_folder_map()
    kb = load_catalog()
    mapped_zones = set(folder_map.zones.values())
    for zone in kb.catalog.zones:
        assert zone.id in mapped_zones, f"{zone.id} не сопоставлен ни с одной папкой"


def test_resolve_folder_matches_synonym_with_noise():
    folder_map = FolderMap(zones={"баня русский стиль": "bath_russian"}, site={})
    resolved = resolve_folder("Баня_Русский-Стиль", folder_map)
    assert resolved == ResolvedFolder("zone", "bath_russian")


def test_resolve_folder_matches_site_category():
    folder_map = FolderMap(zones={}, site={"детская площадка": "playground"})
    resolved = resolve_folder("Детская площадка", folder_map)
    assert resolved == ResolvedFolder("site", "playground")


def test_unknown_folder_raises_with_helpful_message():
    folder_map = FolderMap(zones={"баня": "bath_russian"}, site={"санузел": "restroom"})
    with pytest.raises(UnknownFolderError) as exc_info:
        resolve_folder("Бассейн", folder_map)
    message = str(exc_info.value)
    assert "Бассейн" in message
    assert "bath_russian" in message
    assert "restroom" in message
    assert "photo_folder_map.yaml" in message


# --------------------------------------------------------------------------
# Валидация файлов
# --------------------------------------------------------------------------

def _make_image(path: Path, size=(800, 600), fmt="JPEG") -> Path:
    Image.new("RGB", size, color=(120, 140, 160)).save(path, fmt)
    return path


def test_validate_rejects_bad_extension(tmp_path):
    bad = tmp_path / "photo.gif"
    bad.write_bytes(b"not really a gif")
    problems = validate_image(bad)
    assert problems and "формат" in problems[0]


def test_validate_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "photo.jpg"
    bad.write_bytes(b"this is not an image")
    problems = validate_image(bad)
    assert problems


def test_validate_rejects_too_small_image(tmp_path):
    tiny = _make_image(tmp_path / "tiny.jpg", size=(50, 50))
    problems = validate_image(tiny)
    assert any("маленькое" in p for p in problems)


def test_validate_accepts_good_image(tmp_path):
    good = _make_image(tmp_path / "good.jpg")
    assert validate_image(good) == []


# --------------------------------------------------------------------------
# Сжатие
# --------------------------------------------------------------------------

def test_small_image_is_not_touched(tmp_path):
    small = _make_image(tmp_path / "small.jpg", size=(400, 300))
    tmp_dir = tmp_path / "compressed"
    result = ensure_within_limits(small, tmp_dir)
    assert result == small


def test_oversized_dimensions_are_resized(tmp_path):
    huge = _make_image(tmp_path / "huge.jpg", size=(9000, 6000))
    tmp_dir = tmp_path / "compressed"
    result = ensure_within_limits(huge, tmp_dir)
    assert result != huge
    with Image.open(result) as img:
        assert max(img.size) <= 4000


def test_compression_keeps_original_file_untouched(tmp_path):
    huge = _make_image(tmp_path / "huge.jpg", size=(9000, 6000))
    original_bytes = huge.read_bytes()
    ensure_within_limits(huge, tmp_path / "compressed")
    assert huge.read_bytes() == original_bytes


# --------------------------------------------------------------------------
# Хеширование
# --------------------------------------------------------------------------

def test_same_content_same_hash_different_name(tmp_path):
    a = _make_image(tmp_path / "a.jpg", size=(500, 400))
    b_path = tmp_path / "b.jpg"
    shutil.copy(a, b_path)
    assert sha256_of(a) == sha256_of(b_path)


def test_different_content_different_hash(tmp_path):
    a = _make_image(tmp_path / "a.jpg", size=(500, 400))
    b = _make_image(tmp_path / "b.jpg", size=(500, 401))
    assert sha256_of(a) != sha256_of(b)


# --------------------------------------------------------------------------
# Точечная правка catalog.yaml — сохраняет комментарии
# --------------------------------------------------------------------------

@pytest.fixture
def real_catalog_text() -> str:
    return (KB_DIR / "catalog.yaml").read_text(encoding="utf-8")


def test_patch_zone_photos_updates_only_target_zone(real_catalog_text):
    patched = patch_zone_photos(real_catalog_text, "bath_russian", ["img1", "img2"])
    assert "photos: [img1, img2]" in patched
    # Соседние зоны не тронуты.
    assert 'name: "Баня «Гараж»"' in patched
    import re
    garage_block = re.search(r'- id: bath_garage.*?(?=- id: )', patched, re.DOTALL).group()
    assert "photos: []" in garage_block


def test_patch_zone_photos_preserves_comments(real_catalog_text):
    patched = patch_zone_photos(real_catalog_text, "bath_russian", ["img1"])
    # Число строк-комментариев не должно уменьшиться.
    original_comments = real_catalog_text.count("\n  #") + real_catalog_text.count("\n    #")
    patched_comments = patched.count("\n  #") + patched.count("\n    #")
    assert patched_comments == original_comments


def test_patch_zone_photos_is_idempotent(real_catalog_text):
    once = patch_zone_photos(real_catalog_text, "bath_russian", ["img1", "img2"])
    twice = patch_zone_photos(once, "bath_russian", ["img1", "img2"])
    assert once == twice


def test_patch_zone_photos_unknown_zone_raises(real_catalog_text):
    with pytest.raises(ValueError, match="not_a_real_zone"):
        patch_zone_photos(real_catalog_text, "not_a_real_zone", ["x"])


def test_patch_site_photos_creates_section_if_missing(real_catalog_text):
    assert "site_photos:" not in real_catalog_text
    patched = patch_site_photos(real_catalog_text, "playground", ["p1"])
    assert "site_photos:" in patched
    assert "playground: [p1]" in patched


def test_patch_site_photos_updates_existing_category():
    text = "site_photos:\n  playground: [p1]\n  restroom: [r1]\n"
    patched = patch_site_photos(text, "playground", ["p1", "p2"])
    assert "playground: [p1, p2]" in patched
    assert "restroom: [r1]" in patched      # другая категория не тронута


def test_patched_catalog_still_loads(real_catalog_text, tmp_path, monkeypatch):
    """Патченый файл обязан оставаться валидным YAML/каталогом."""
    patched = patch_zone_photos(real_catalog_text, "bath_russian", ["img1", "img2"])
    patched = patch_site_photos(patched, "playground", ["p1"])

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "catalog.yaml").write_text(patched, encoding="utf-8")
    for name in ("promos.yaml", "concessions.yaml", "payment.yaml"):
        shutil.copy(KB_DIR / name, kb_dir / name)

    from app.kb.loader import load_catalog as _load

    kb = _load(kb_dir=kb_dir)
    bath = next(z for z in kb.catalog.zones if z.id == "bath_russian")
    assert bath.photos == ["img1", "img2"]
    assert kb.catalog.site_photos["playground"] == ["p1"]


# --------------------------------------------------------------------------
# Пайплайн: DRY_RUN
# --------------------------------------------------------------------------

@pytest.fixture
def sample_kb_dir(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    for name in ("catalog.yaml", "promos.yaml", "concessions.yaml", "payment.yaml"):
        shutil.copy(KB_DIR / name, kb_dir / name)
    return kb_dir


class FakeAvitoClient:
    def __init__(self):
        self.uploaded: list[tuple[bytes, str]] = []
        self._next_id = 1

    async def upload_image(self, data: bytes, filename: str = "photo.jpg") -> dict:
        self.uploaded.append((data, filename))
        image_id = f"fake-{self._next_id}"
        self._next_id += 1
        return {image_id: {"main": "https://example/x.jpg"}}


@pytest.fixture
def photo_root(tmp_path):
    root = tmp_path / "photos"
    (root / "Баня Русский стиль").mkdir(parents=True)
    # Разный размер/цвет — иначе два кадра дают одинаковый sha256 и
    # корректно схлопнутся дедупликацией, что ломает счётчики в этих тестах.
    _make_image(root / "Баня Русский стиль" / "01_общий.jpg", size=(800, 600))
    _make_image(root / "Баня Русский стиль" / "02_парилка.jpg", size=(640, 480))
    (root / "Детская площадка").mkdir()
    _make_image(root / "Детская площадка" / "01.jpg", size=(700, 500))
    return root


async def test_dry_run_does_not_upload_or_touch_catalog(photo_root, sample_kb_dir):
    original = (sample_kb_dir / "catalog.yaml").read_text(encoding="utf-8")
    client = FakeAvitoClient()

    run = await import_root(
        photo_root, dry_run=True, avito_client=client,
        catalog_path=sample_kb_dir / "catalog.yaml",
        manifest_path=sample_kb_dir / ".manifest.json",
    )

    assert client.uploaded == []
    assert (sample_kb_dir / "catalog.yaml").read_text(encoding="utf-8") == original
    assert not (sample_kb_dir / ".manifest.json").exists()
    assert run.unmapped_folders == []
    assert {r.key for r in run.results} == {"bath_russian", "playground"}


async def test_dry_run_reports_skipped_bad_files(tmp_path, sample_kb_dir):
    root = tmp_path / "photos"
    zone_dir = root / "Баня Русский стиль"
    zone_dir.mkdir(parents=True)
    _make_image(zone_dir / "01_общий.jpg")
    (zone_dir / "02_bad.txt").write_text("not an image")

    run = await import_root(
        root, dry_run=True, avito_client=None,
        catalog_path=sample_kb_dir / "catalog.yaml",
        manifest_path=sample_kb_dir / ".manifest.json",
    )
    result = run.results[0]
    assert len(result.skipped) == 1
    assert result.skipped[0][0] == "02_bad.txt"


async def test_unmapped_folder_reported_but_does_not_stop_others(photo_root, sample_kb_dir):
    (photo_root / "Совершенно неизвестная папка").mkdir()
    _make_image(photo_root / "Совершенно неизвестная папка" / "1.jpg")

    run = await import_root(
        photo_root, dry_run=True, avito_client=None,
        catalog_path=sample_kb_dir / "catalog.yaml",
        manifest_path=sample_kb_dir / ".manifest.json",
    )
    assert len(run.unmapped_folders) == 1
    assert "Совершенно неизвестная папка" in run.unmapped_folders[0]
    # Известные папки всё равно обработаны.
    assert {r.key for r in run.results} == {"bath_russian", "playground"}


async def test_dry_run_requires_no_avito_client(photo_root, sample_kb_dir):
    run = await import_root(
        photo_root, dry_run=True,
        catalog_path=sample_kb_dir / "catalog.yaml",
        manifest_path=sample_kb_dir / ".manifest.json",
    )
    assert run.results


async def test_live_run_without_client_raises(photo_root, sample_kb_dir):
    with pytest.raises(ValueError, match="avito_client"):
        await import_root(
            photo_root, dry_run=False,
            catalog_path=sample_kb_dir / "catalog.yaml",
            manifest_path=sample_kb_dir / ".manifest.json",
        )


# --------------------------------------------------------------------------
# Пайплайн: реальная загрузка (с фейковым клиентом) и идемпотентность
# --------------------------------------------------------------------------

async def test_live_run_uploads_and_updates_catalog(photo_root, sample_kb_dir):
    client = FakeAvitoClient()
    catalog_path = sample_kb_dir / "catalog.yaml"

    run = await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path,
        manifest_path=sample_kb_dir / ".manifest.json",
    )

    assert len(client.uploaded) == 3       # 2 в бане + 1 на площадке
    bath_result = next(r for r in run.results if r.key == "bath_russian")
    assert bath_result.uploaded == 2
    assert len(bath_result.image_ids) == 2

    text = catalog_path.read_text(encoding="utf-8")
    assert f"photos: [{bath_result.image_ids[0]}, {bath_result.image_ids[1]}]" in text
    assert "site_photos:" in text


async def test_rerun_is_idempotent_no_duplicate_uploads(photo_root, sample_kb_dir):
    client = FakeAvitoClient()
    catalog_path = sample_kb_dir / "catalog.yaml"
    manifest_path = sample_kb_dir / ".manifest.json"

    first = await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )
    first_ids = {r.key: r.image_ids for r in first.results}
    assert len(client.uploaded) == 3

    second = await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )

    # Второй прогон не грузит те же файлы повторно.
    assert len(client.uploaded) == 3
    second_ids = {r.key: r.image_ids for r in second.results}
    assert second_ids == first_ids
    for result in second.results:
        assert result.uploaded == 0
        assert result.reused == len(result.image_ids)


async def test_new_file_added_later_only_uploads_the_new_one(photo_root, sample_kb_dir):
    client = FakeAvitoClient()
    catalog_path = sample_kb_dir / "catalog.yaml"
    manifest_path = sample_kb_dir / ".manifest.json"

    await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )
    assert len(client.uploaded) == 3

    _make_image(photo_root / "Баня Русский стиль" / "03_новое.jpg", size=(900, 700))
    run = await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )

    assert len(client.uploaded) == 4        # ровно один новый файл
    bath_result = next(r for r in run.results if r.key == "bath_russian")
    assert bath_result.uploaded == 1
    assert bath_result.reused == 2
    assert len(bath_result.image_ids) == 3


async def test_renamed_file_is_not_reuploaded(photo_root, sample_kb_dir):
    """Идемпотентность держится на содержимом, а не на имени файла."""
    client = FakeAvitoClient()
    catalog_path = sample_kb_dir / "catalog.yaml"
    manifest_path = sample_kb_dir / ".manifest.json"

    await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )
    assert len(client.uploaded) == 3

    old = photo_root / "Баня Русский стиль" / "01_общий.jpg"
    old.rename(photo_root / "Баня Русский стиль" / "01_переименован.jpg")

    await import_root(
        photo_root, dry_run=False, avito_client=client,
        catalog_path=catalog_path, manifest_path=manifest_path,
    )
    assert len(client.uploaded) == 3        # без изменений — тот же контент
