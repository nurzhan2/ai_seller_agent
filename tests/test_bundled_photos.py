"""Фото из каталога вшиты в образ и отправляются свежей загрузкой.

Жалоба 2026-09-30: старые image_id в Авито протухли (404) — у клиента
вместо фото серая заглушка.
"""

import asyncio
import json
from pathlib import Path

from app.media.bundled import bundled_photo_path

MANIFEST = Path("app/kb/.photos_manifest.json")


def test_every_catalog_photo_is_bundled():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [e["image_id"] for entries in manifest.values() for e in entries.values()]
    assert ids
    missing = [i for i in ids if bundled_photo_path(i) is None]
    assert not missing, missing


def test_unsafe_ids_are_rejected():
    assert bundled_photo_path("../../etc/passwd") is None
    assert bundled_photo_path("") is None
    assert bundled_photo_path("nope.123") is None


def test_pipeline_uploads_fresh_copy_instead_of_reusing_id():
    from app.pipeline import MessagePipeline as Pipeline

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    known = next(iter(next(iter(manifest.values())).values()))["image_id"]
    calls = []

    class Client:
        async def upload_and_send_image(self, chat_id, data, filename="photo.jpg"):
            calls.append(("upload", filename, len(data)))

        async def send_image(self, chat_id, image_id):
            calls.append(("reuse", image_id))

    fake = Pipeline.__new__(Pipeline)
    fake.avito_client = Client()
    sent = asyncio.run(Pipeline._send_photos(fake, "chat", [known, "unknown.id"]))
    assert sent == [known, "unknown.id"]
    assert calls[0][0] == "upload" and calls[0][1] == f"{known}.jpg" and calls[0][2] > 10_000
    assert calls[1] == ("reuse", "unknown.id")
