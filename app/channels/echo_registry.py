"""Тексты, которые мы вот-вот отправим: гонка эха (аудит 30.10).

Авито присылает событие на каждое наше исходящее, и отличить наше эхо от
сообщения живого менеджера можно только по тексту (`was_sent_by_us` ищет
его среди сохранённых ответов агента). Но конвейер сохранял ответ ПОСЛЕ
отправки - а при фото ещё и после загрузки всех картинок, это секунды.
Эхо текста успевало прийти раньше записи в базу, не находило себя и
ставило перехват «менеджер написал»: 2-е и 3-е фото блокировал гейт, бот
замолкал. Живой случай: чат DZtf 29.10 15:57 - перехват без единого
сообщения менеджера, ровно в секунду ответа бота.

Здесь текст запоминается ДО отправки, в единственной двери наружу
(app/channels/outbound_gate.py:OutboundGate.send_message), и сверка эха
смотрит сюда раньше базы. В памяти процесса: сервис - один процесс, а эхо
приходит через секунды; TTL с запасом на медленный вебхук и поллер.
"""

from __future__ import annotations

import time
from typing import Optional

TTL_SECONDS = 600
_MAX_PER_CHAT = 20

_pending: dict[str, list[tuple[str, float]]] = {}


def _norm(text: Optional[str]) -> str:
    return (text or "").strip()


def expect(chat_id: str, text: str, now: Optional[float] = None) -> None:
    """Сейчас уйдёт этот текст - его эхо не менеджер."""
    body = _norm(text)
    if not chat_id or not body:
        return
    now = time.monotonic() if now is None else now
    fresh = [(t, ts) for t, ts in _pending.get(chat_id, []) if now - ts < TTL_SECONDS]
    fresh.append((body, now))
    _pending[chat_id] = fresh[-_MAX_PER_CHAT:]


def is_expected(chat_id: str, text: str, now: Optional[float] = None) -> bool:
    body = _norm(text)
    if not chat_id or not body:
        return False
    now = time.monotonic() if now is None else now
    return any(t == body and now - ts < TTL_SECONDS for t, ts in _pending.get(chat_id, []))


def clear() -> None:
    _pending.clear()
