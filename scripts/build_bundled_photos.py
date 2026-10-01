"""Пересобрать app/media/bundled из media/photos по .photos_manifest.json.

Запуск из корня репозитория: python scripts/build_bundled_photos.py
"""

import json
import os

from PIL import Image, ImageOps

MANIFEST = "app/kb/.photos_manifest.json"
OUT = "app/media/bundled"


def main() -> None:
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    for key, entries in manifest.items():
        zone = key.split(":", 1)[1]
        for entry in entries.values():
            src = os.path.join("media", "photos", zone, entry["file"])
            image = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
            image.thumbnail((1280, 1280))
            image.save(os.path.join(OUT, entry["image_id"] + ".jpg"), "JPEG",
                       quality=82, optimize=True, progressive=True)


if __name__ == "__main__":
    main()
